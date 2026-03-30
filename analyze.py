#!/usr/bin/env python3
"""
analyze.py — statistical analysis of suslint experiment results

Usage:
  python analyze.py results/run_2026-03-29_17-00-04

For each repo:
  - Pairs baseline[i] with treatment[i] by sorted run_index (positional)
  - Shapiro-Wilk on the paired differences to select test
  - Paired t-test (normal) or Wilcoxon signed-rank (non-normal)
  - Cohen's d (parametric) or rank-biserial r (non-parametric)
  - 95% CI on mean difference (t-based) or median difference (bootstrap)
  - Summary table printed to stdout
  - Forest plot saved to <run_dir>/forest.png

Requirements:
    pip install pandas scipy matplotlib numpy
"""

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd
from scipy import stats

plt.rcParams.update({
    "font.family": "monospace",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.dpi": 150,
})

ALPHA = 0.05
N_BOOT = 10_000
RNG = np.random.default_rng(42)


# =============================================================================
# PAIRING
# =============================================================================

def make_pairs(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """
    Pair baseline and treatment rows positionally after sorting by run_index.
    If counts differ, truncate to the shorter series.
    Returns (baseline_values, treatment_values).
    """
    b = df[df["condition"] == "baseline"].dropna(subset=["total_energy_j"]) \
          .sort_values("run_index")["total_energy_j"].to_numpy()
    t = df[df["condition"] == "treatment"].dropna(subset=["total_energy_j"]) \
          .sort_values("run_index")["total_energy_j"].to_numpy()
    n = min(len(b), len(t))
    return b[:n], t[:n]


# =============================================================================
# STATISTICS
# =============================================================================

def shapiro_normal(x: np.ndarray) -> bool:
    """Return True if Shapiro-Wilk fails to reject normality (p >= ALPHA)."""
    if len(x) < 3:
        return False
    _, p = stats.shapiro(x)
    return p >= ALPHA


def cohens_d_paired(diffs: np.ndarray) -> float:
    return float(np.mean(diffs) / np.std(diffs, ddof=1))


def rank_biserial(diffs: np.ndarray) -> float:
    """
    Rank-biserial correlation for Wilcoxon signed-rank test.
    r = (R+ - R-) / (R+ + R-)
    Positive = baseline tends to be higher (more energy) than treatment.
    """
    nonzero = diffs[diffs != 0]
    if len(nonzero) == 0:
        return 0.0
    ranks = stats.rankdata(np.abs(nonzero))
    r_plus  = float(np.sum(ranks[nonzero > 0]))
    r_minus = float(np.sum(ranks[nonzero < 0]))
    total   = r_plus + r_minus
    return (r_plus - r_minus) / total if total > 0 else 0.0


def ci_mean_paired(diffs: np.ndarray, confidence: float = 0.95) -> tuple[float, float]:
    """t-distribution CI on mean of paired differences."""
    n   = len(diffs)
    se  = stats.sem(diffs)
    h   = se * stats.t.ppf((1 + confidence) / 2, df=n - 1)
    m   = float(np.mean(diffs))
    return m - h, m + h


def ci_median_bootstrap(diffs: np.ndarray, confidence: float = 0.95) -> tuple[float, float]:
    """Bootstrap CI on median of paired differences."""
    boot = np.array([
        np.median(RNG.choice(diffs, size=len(diffs), replace=True))
        for _ in range(N_BOOT)
    ])
    lo = (1 - confidence) / 2 * 100
    hi = (1 + confidence) / 2 * 100
    return float(np.percentile(boot, lo)), float(np.percentile(boot, hi))


def analyze_repo(name: str, df: pd.DataFrame) -> dict:
    b, t = make_pairs(df)
    n    = len(b)
    diffs = b - t  # positive = baseline used more energy

    sw_stat, sw_p = stats.shapiro(diffs) if n >= 3 else (float("nan"), float("nan"))
    normal = bool(sw_p >= ALPHA) if n >= 3 else False

    result = {
        "repo":         name,
        "n_pairs":      n,
        "mean_b":       float(np.mean(b)),
        "mean_t":       float(np.mean(t)),
        "std_b":        float(np.std(b, ddof=1)),
        "std_t":        float(np.std(t, ddof=1)),
        "mean_diff":    float(np.mean(diffs)),
        "median_diff":  float(np.median(diffs)),
        "sw_stat":      sw_stat,
        "sw_p":         sw_p,
        "normal":       normal,
        "test":         None,
        "stat":         None,
        "p_value":      None,
        "effect_size":  None,
        "effect_label": None,
        "ci_low":       None,
        "ci_high":      None,
        "ci_type":      None,
        "estimate":     None,  # central estimate used in forest plot
    }

    if n < 3:
        return result

    if normal:
        stat, p = stats.ttest_rel(b, t)
        d       = cohens_d_paired(diffs)
        ci_lo, ci_hi = ci_mean_paired(diffs)
        result.update({
            "test":         "paired t-test",
            "stat":         float(stat),
            "p_value":      float(p),
            "effect_size":  float(d),
            "effect_label": "Cohen's d",
            "ci_low":       ci_lo,
            "ci_high":      ci_hi,
            "ci_type":      "95% CI (mean diff, t)",
            "estimate":     float(np.mean(diffs)),
        })
    else:
        stat, p = stats.wilcoxon(diffs, alternative="two-sided")
        r       = rank_biserial(diffs)
        ci_lo, ci_hi = ci_median_bootstrap(diffs)
        result.update({
            "test":         "Wilcoxon signed-rank",
            "stat":         float(stat),
            "p_value":      float(p),
            "effect_size":  float(r),
            "effect_label": "rank-biserial r",
            "ci_low":       ci_lo,
            "ci_high":      ci_hi,
            "ci_type":      "95% CI (median diff, bootstrap)",
            "estimate":     float(np.median(diffs)),
        })

    return result


# =============================================================================
# OUTPUT
# =============================================================================

def print_table(results: list[dict]):
    print()
    print("=" * 90)
    print("STATISTICAL SUMMARY  (difference = baseline − treatment; positive = baseline uses more)")
    print("=" * 90)
    hdr = (
        f"{'Repo':<22} {'N':>4} {'Test':<22} {'p-val':>7} "
        f"{'Effect':>9} {'(type)':<18} {'CI low':>8} {'CI high':>8}"
    )
    print(hdr)
    print("-" * 90)
    for r in results:
        if r["p_value"] is None:
            print(f"  {r['repo']:<20} {'N/A — insufficient data'}")
            continue
        sig  = "*" if r["p_value"] < ALPHA else " "
        test = r["test"] or "—"
        eff  = f"{r['effect_size']:+.3f}" if r["effect_size"] is not None else "N/A"
        elbl = r["effect_label"] or ""
        pv   = f"{r['p_value']:.4f}{sig}"
        clo  = f"{r['ci_low']:+.5f}" if r["ci_low"] is not None else "N/A"
        chi  = f"{r['ci_high']:+.5f}" if r["ci_high"] is not None else "N/A"
        print(
            f"  {r['repo']:<20} {r['n_pairs']:>4} {test:<22} {pv:>8} "
            f"{eff:>9} {elbl:<18} {clo:>8} {chi:>8}"
        )
    print("=" * 90)
    print("  * p < 0.05")
    print()

    # Normality sub-table
    print("Normality check (Shapiro-Wilk on paired differences):")
    print(f"  {'Repo':<22} {'W':>8} {'p':>8} {'Normal?':>8}")
    print("  " + "-" * 50)
    for r in results:
        if np.isnan(r["sw_p"]):
            print(f"  {r['repo']:<22} {'—':>8} {'—':>8} {'N/A':>8}")
        else:
            yn = "yes" if r["normal"] else "no"
            print(f"  {r['repo']:<22} {r['sw_stat']:>8.4f} {r['sw_p']:>8.4f} {yn:>8}")
    print()


def plot_forest(results: list[dict], out_path: Path):
    valid = [r for r in results if r["ci_low"] is not None]
    if not valid:
        print("  No valid results to plot.")
        return

    n   = len(valid)
    fig, ax = plt.subplots(figsize=(8, max(3, n * 0.9 + 1.5)))

    y_pos = list(range(n - 1, -1, -1))  # top-to-bottom repo order

    for i, (r, y) in enumerate(zip(valid, y_pos)):
        est  = r["estimate"]
        lo   = r["ci_low"]
        hi   = r["ci_high"]
        p    = r["p_value"]
        color = "#61AFEF" if p is not None and p < ALPHA else "#ABB2BF"

        # CI line
        ax.plot([lo, hi], [y, y], color=color, linewidth=2, solid_capstyle="round")
        # Point estimate
        ax.scatter([est], [y], color=color, s=60, zorder=5)
        # p-value annotation
        pv_str = f"p={p:.4f}" if p is not None else ""
        ax.text(hi, y, f"  {pv_str}", va="center", fontsize=8, color="#5C6370")

    ax.axvline(0, color="#E06C75", linewidth=1.2, linestyle="--", alpha=0.8)
    ax.set_yticks(y_pos)
    ax.set_yticklabels([r["repo"] for r in valid], fontsize=10)
    ax.set_xlabel("Energy difference  baseline − treatment  (J)\n"
                  "Positive → baseline uses more energy", fontsize=10)
    ax.set_title("Forest Plot — Paired Energy Difference per Repo", fontsize=12, fontweight="bold")

    sig_patch   = mpatches.Patch(color="#61AFEF", label=f"p < {ALPHA}")
    insig_patch = mpatches.Patch(color="#ABB2BF", label=f"p ≥ {ALPHA}")
    ax.legend(handles=[sig_patch, insig_patch], fontsize=9, loc="lower right")

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  Forest plot → {out_path}")


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Statistical analysis of suslint experiment")
    parser.add_argument("run_dir", help="Path to run folder, e.g. results/run_2026-03-29_17-00-04")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.exists():
        print(f"ERROR: {run_dir} does not exist")
        sys.exit(1)

    csv_paths = sorted(run_dir.glob("*/raw.csv"))
    if not csv_paths:
        print(f"ERROR: no raw.csv files found under {run_dir}")
        sys.exit(1)

    print(f"\nRun folder : {run_dir}")
    print(f"Repos found: {[p.parent.name for p in csv_paths]}")

    results = []
    for csv_path in csv_paths:
        repo_name = csv_path.parent.name
        df        = pd.read_csv(csv_path)
        df        = df[df["total_energy_j"].notna()].copy()
        r         = analyze_repo(repo_name, df)
        results.append(r)
        pv_str = f"{r['p_value']:.4f}" if r["p_value"] is not None else "N/A"
        print(f"  {repo_name}: {r['n_pairs']} pairs, test={r['test'] or 'N/A'}, p={pv_str}")

    print_table(results)
    plot_forest(results, run_dir / "forest.png")


if __name__ == "__main__":
    main()

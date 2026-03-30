#!/usr/bin/env python3
"""
run.py — suslint experiment runner

Two modes:
  python run.py                # run full experiment (all repos in config.yml)
  python run.py --repo congo-SE  # run for one repo only

Each execution creates a timestamped folder:
  results/
  └── run_2026-03-28_17-05-41/
      ├── experiment.json          # snapshot of config used
      └── congo-SE/
          └── raw.csv              # energy per run from EcoCI

Requirements:
    pip install requests pyyaml pandas
    gh CLI must be authenticated: gh auth status
"""

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests
import yaml

CONFIG_FILE = "config.yml"
ECOCI_API   = "https://api.green-coding.io"


# =============================================================================
# PHASE 1 — ORCHESTRATE
# Trigger interleaved baseline/treatment runs on GitHub Actions
# =============================================================================

def get_token() -> str:
    result = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def trigger_workflow(repo_full: str, workflow: str, branch: str,
                     run_index: int, token: str) -> None:
    url = f"https://api.github.com/repos/{repo_full}/actions/workflows/{workflow}/dispatches"
    payload = {"ref": branch, "inputs": {"run_index": str(run_index)}}
    r = requests.post(url, headers=gh_headers(token), json=payload)
    if r.status_code != 204:
        raise RuntimeError(
            f"Failed to trigger {workflow} on {repo_full}: {r.status_code} {r.text}"
        )


def get_latest_run_id(repo_full: str, workflow: str, token: str,
                      after: datetime) -> str | None:
    url = f"https://api.github.com/repos/{repo_full}/actions/workflows/{workflow}/runs"
    r = requests.get(url, headers=gh_headers(token), params={"per_page": 5})
    r.raise_for_status()
    for run in r.json().get("workflow_runs", []):
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if created > after:
            return str(run["id"])
    return None


def poll_run(repo_full: str, run_id: str, token: str, timeout: int) -> dict:
    url = f"https://api.github.com/repos/{repo_full}/actions/runs/{run_id}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(url, headers=gh_headers(token))
        r.raise_for_status()
        data = r.json()
        if data["status"] == "completed":
            return data
        print(f"      polling {run_id}: {data['status']}...")
        time.sleep(20)
    raise TimeoutError(f"Run {run_id} timed out after {timeout}s")


def orchestrate_repo(repo_cfg: dict, exp_cfg: dict, out_dir: Path, token: str) -> list[dict]:
    """
    Trigger interleaved runs for one repo. Returns list of run records.
    """
    owner        = repo_cfg["owner"]
    name         = repo_cfg["name"]
    repo_full    = f"{owner}/{name}"
    baseline_wf  = repo_cfg["baseline_workflow"]
    treatment_wf = repo_cfg["treatment_workflow"]
    branch       = exp_cfg["branch"]
    n            = exp_cfg["runs_per_condition"]
    delay        = exp_cfg["delay_between_runs"]
    timeout      = exp_cfg["run_timeout"]

    print(f"\n{'='*65}")
    print(f"  Repo : {repo_full}")
    print(f"  Runs : {n} baseline + {n} treatment (interleaved, +1 treatment warmup)")
    print(f"{'='*65}")

    # Warmup treatment run first (run_index=0) to seed the cache.
    # This run is excluded from analysis; all subsequent treatment runs will be cache hits.
    # Interleaved sequence: T_warmup, B1, T1, B2, T2, ...
    sequence = [("treatment_warmup", treatment_wf, 0)]
    for i in range(1, n + 1):
        sequence.append(("baseline",  baseline_wf,  i))
        sequence.append(("treatment", treatment_wf, i))

    records = []
    for step, (condition, workflow, run_idx) in enumerate(sequence, 1):
        print(f"\n  [{step}/{len(sequence)}] {condition.upper()} run {run_idx}")
        try:
            dispatch_time = datetime.now(timezone.utc)
            trigger_workflow(repo_full, workflow, branch, run_idx, token)
            print(f"    dispatched at {dispatch_time.strftime('%H:%M:%S')}")

            time.sleep(8)  # let GitHub register the run

            run_id = None
            for _ in range(10):
                run_id = get_latest_run_id(repo_full, workflow, token, dispatch_time)
                if run_id:
                    break
                time.sleep(5)

            if not run_id:
                raise RuntimeError("Could not find run ID after dispatch")

            print(f"    run ID: {run_id}")
            run_data   = poll_run(repo_full, run_id, token, timeout)
            conclusion = run_data.get("conclusion", "unknown")
            print(f"    {'✓' if conclusion == 'success' else '✗'} {conclusion}")

            records.append({
                "repo":         name,
                "repo_full":    repo_full,
                "condition":    condition,
                "run_index":    run_idx,
                "run_id":       run_id,
                "workflow":     workflow,
                "conclusion":   conclusion,
                "dispatched_at": dispatch_time.isoformat(),
                "created_at":   run_data.get("created_at", ""),
                "updated_at":   run_data.get("updated_at", ""),
            })

        except Exception as e:
            print(f"    ✗ ERROR: {e}")
            records.append({
                "repo":         name,
                "repo_full":    repo_full,
                "condition":    condition,
                "run_index":    run_idx,
                "run_id":       "ERROR",
                "workflow":     workflow,
                "conclusion":   "error",
                "error":        str(e),
                "dispatched_at": datetime.now(timezone.utc).isoformat(),
            })

        if step < len(sequence):
            time.sleep(delay)

    return records


# =============================================================================
# PHASE 2 — FETCH
# Pull total energy (J) from EcoCI API for each logged run ID
# =============================================================================

def get_workflow_ids(repo_full: str, baseline_file: str, treatment_file: str) -> tuple[str, str]:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo_full}/actions/workflows",
         "--jq", ".workflows[] | {id: .id, path: .path}"],
        capture_output=True, text=True, check=True,
    )
    baseline_id = treatment_id = None
    for line in result.stdout.strip().splitlines():
        obj  = json.loads(line)
        path = obj["path"]
        if baseline_file in path:
            baseline_id = str(obj["id"])
        elif treatment_file in path:
            treatment_id = str(obj["id"])

    if not baseline_id or not treatment_id:
        raise RuntimeError(
            f"Could not resolve workflow IDs for {repo_full}\n"
            f"  Looking for: '{baseline_file}' and '{treatment_file}'\n"
            f"  Make sure both workflow files are pushed to the experiment branch."
        )
    return baseline_id, treatment_id


def fetch_ecoci_measurements(repo_full: str, workflow_id: str, branch: str,
                              start_date: str, end_date: str) -> list:
    url    = f"{ECOCI_API}/v1/ci/measurements"
    params = {
        "repo":       repo_full,
        "branch":     branch,
        "workflow":   workflow_id,
        "start_date": start_date,
        "end_date":   end_date,
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"EcoCI API error: {data.get('err')}")
    return data.get("data", [])


def sum_energy_by_run(rows: list) -> dict[str, float]:
    """
    Sum step-level nanojoule rows by GitHub run ID → total joules per run.
    Works correctly for multi-job workflows: all steps across all jobs
    share the same run_id, so summing gives the full run total.
    Column layout: [energy_nj, run_id, timestamp, label, ...]
    """
    by_run = {}
    for row in rows:
        run_id     = str(row[1])
        energy_nj  = float(row[0])
        by_run[run_id] = by_run.get(run_id, 0.0) + energy_nj
    return {rid: nj / 1_000_000_000 for rid, nj in by_run.items()}


def fetch_repo(repo_cfg: dict, exp_cfg: dict, run_dir: Path, runs: list[dict]) -> pd.DataFrame:
    """
    Given orchestration records, query EcoCI, write raw.csv.
    Returns DataFrame with columns: repo, condition, run_index, run_id,
    dispatched_at, total_energy_j
    """
    name      = repo_cfg["name"]
    repo_full = f"{repo_cfg['owner']}/{name}"
    branch    = exp_cfg["branch"]
    repo_dir  = run_dir / name
    repo_dir.mkdir(parents=True, exist_ok=True)

    successful = [r for r in runs if r.get("conclusion") == "success"
                  and r.get("condition") != "treatment_warmup"]
    warmup_ok  = any(r.get("condition") == "treatment_warmup" and r.get("conclusion") == "success"
                     for r in runs)
    if not warmup_ok:
        print(f"  WARNING: treatment warmup run did not succeed — treatment runs may include a cache miss")
    print(f"  {len(successful)} successful runs (out of {len(runs)} total, excluding warmup)")
    if not successful:
        return pd.DataFrame()

    # Resolve workflow IDs from GitHub
    print("  Resolving workflow IDs...")
    baseline_id, treatment_id = get_workflow_ids(
        repo_full,
        repo_cfg["baseline_workflow"],
        repo_cfg["treatment_workflow"],
    )
    print(f"  baseline={baseline_id}  treatment={treatment_id}")

    # Date range from dispatched_at timestamps
    dates      = [datetime.fromisoformat(r["dispatched_at"]) for r in successful
                  if r.get("dispatched_at")]
    start_date = (min(dates) - timedelta(days=1)).strftime("%Y-%m-%d")
    end_date   = (max(dates) + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"  Date range: {start_date} → {end_date}")

    # Fetch from EcoCI and index by run ID
    energy_map = {}
    for wf_id, label in [(baseline_id, "baseline"), (treatment_id, "treatment")]:
        print(f"  Querying EcoCI: {label} (workflow {wf_id})...")
        rows       = fetch_ecoci_measurements(repo_full, wf_id, branch, start_date, end_date)
        run_totals = sum_energy_by_run(rows)
        print(f"    {len(rows)} step rows → {len(run_totals)} runs")
        energy_map.update(run_totals)

    # Match logged run IDs to EcoCI energy values
    records = []
    for run in successful:
        run_id   = str(run["run_id"])
        energy_j = energy_map.get(run_id)
        status   = f"{energy_j:.4f} J" if energy_j is not None else "NO DATA"
        print(f"    {'✓' if energy_j else '✗'} {run['condition']:10s}  {run_id}  {status}")
        records.append({
            "repo":           name,
            "condition":      run["condition"],
            "run_index":      run.get("run_index"),
            "run_id":         run_id,
            "dispatched_at":  run.get("dispatched_at", ""),
            "total_energy_j": energy_j,
        })

    df       = pd.DataFrame(records)
    csv_path = repo_dir / "raw.csv"
    df.to_csv(csv_path, index=False)
    print(f"  Saved → {csv_path}")
    return df


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="suslint experiment runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py                  # full experiment, all repos
  python run.py --repo congo-SE  # one repo only
        """,
    )
    parser.add_argument("--repo",   help="Only run for this repo name")
    parser.add_argument("--config", default=CONFIG_FILE)
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    exp_cfg = config["experiment"]
    repos   = config["repos"]
    if args.repo:
        repos = [r for r in repos if r["name"] == args.repo]
        if not repos:
            print(f"ERROR: '{args.repo}' not found in config.yml")
            return

    # Create a unique timestamped results folder for this execution
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir   = Path("results") / f"run_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Save a snapshot of the config used
    with open(run_dir / "experiment.json", "w") as f:
        json.dump({"timestamp": timestamp, "config": config}, f, indent=2)

    print(f"\nResults folder: {run_dir}")
    print(f"Repos         : {[r['name'] for r in repos]}")
    print(f"Runs/condition: {exp_cfg['runs_per_condition']}")

    token = get_token()

    for repo_cfg in repos:
        name = repo_cfg["name"]

        # ── Phase 1: Orchestrate ─────────────────────────────────────────────
        print(f"\n{'#'*65}")
        print(f"  PHASE 1 — ORCHESTRATE  [{name}]")
        print(f"{'#'*65}")
        records = orchestrate_repo(repo_cfg, exp_cfg, run_dir, token)

        # ── Phase 2: Fetch ───────────────────────────────────────────────────
        print(f"\n{'#'*65}")
        print(f"  PHASE 2 — FETCH        [{name}]")
        print(f"{'#'*65}")
        fetch_repo(repo_cfg, exp_cfg, run_dir, records)

    print(f"\n{'='*65}")
    print(f"  Done. Results in: {run_dir}")
    print(f"{'='*65}")


if __name__ == "__main__":
    main()

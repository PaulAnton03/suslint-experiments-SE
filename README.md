# suslint-experiments

This repository contains the experiment infrastructure for empirically evaluating [suslint](https://github.com/your-org/suslint), a static linter for GitHub Actions workflows that detects energy-wasteful CI/CD patterns.

For each linter rule under evaluation, we select real-world open-source repositories that exhibit the anti-pattern, fork them, and add two workflow variants: a **baseline** (original, unoptimised) and a **treatment** (with the suslint fix applied). We then run both variants repeatedly on GitHub Actions and measure energy consumption using [EcoCI](https://www.green-coding.io/products/eco-ci/), comparing the results statistically.

---

## Rule Evaluated: SUS001 — Cache Dependencies

> **Warning: Runner Efficiency**
> Installing dependencies without caching causes repeated downloads and longer CI times.
> ✔ Use `actions/cache` or enable built-in caching in setup actions (e.g., `setup-node`, `setup-python`, `setup-java`, `ruby/setup-ruby`).

### Baseline (anti-pattern)

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm install
      - run: npm test
```

### Treatment (fix)

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 18
          cache: npm
      - run: npm ci
      - run: npm test
```

Adding `cache: npm` to `setup-node` instructs the runner to restore npm's download cache (`~/.npm`) from GitHub's cache storage before running `npm install`. On a cache hit, the network-heavy package download phase is skipped entirely — npm still runs but resolves packages locally. This is the dominant source of energy consumption in dependency installation steps.

---

## Subject Repositories

Each subject repo is a fork of a real open-source project. The `-SE` suffix denotes the fork. The `experiment` branch on each fork contains the two workflow files; the rest of the repo is an unmodified mirror of upstream.

| Fork | Original Project | Language | Experiment |
|------|-----------------|----------|------------|
| [PaulAnton03/congo-SE](https://github.com/PaulAnton03/congo-SE) | [jpanther/congo](https://github.com/jpanther/congo) — Hugo theme built with Tailwind CSS | JavaScript | [EXPERIMENT.md](https://github.com/PaulAnton03/congo-SE/blob/experiment/.github/workflows/EXPERIMENT.md) |
| [PaulAnton03/rq-SE](https://github.com/PaulAnton03/rq-SE) | [rq/rq](https://github.com/rq/rq) — Simple job queues for Python | Python | [EXPERIMENT.md](https://github.com/PaulAnton03/rq-SE/blob/experiment/.github/workflows/EXPERIMENT.md) |
| [PaulAnton03/ArchiSteamFarm-SE](https://github.com/PaulAnton03/ArchiSteamFarm-SE) | [JustArchiNET/ArchiSteamFarm](https://github.com/JustArchiNET/ArchiSteamFarm) — Steam farming application | C# | [EXPERIMENT.md](https://github.com/PaulAnton03/ArchiSteamFarm-SE/blob/experiment/.github/workflows/EXPERIMENT.md) |
| [PaulAnton03/django-compressor-SE](https://github.com/PaulAnton03/django-compressor-SE) | [django-compressor/django-compressor](https://github.com/django-compressor/django-compressor) — Compresses linked and inline JS/CSS | Python | [EXPERIMENT.md](https://github.com/PaulAnton03/django-compressor-SE/blob/experiment/.github/workflows/EXPERIMENT.md) |

---

## How It Works

The experiment runs in two sequential phases, driven by `run.py`, and an optional analysis step via `analyze.py`.

### Phase 1 — Orchestrate

`run.py` uses the GitHub Actions API to trigger `workflow_dispatch` runs on each subject repo. Runs are **interleaved** (baseline → treatment → baseline → treatment → ...) rather than running all baselines first. This controls for time-of-day effects on shared GitHub runners, which would otherwise introduce systematic bias between conditions.

A **treatment warmup run** (run index 0) is dispatched first to seed the dependency cache before any measured treatment runs begin. This warmup run is excluded from analysis.

### Phase 2 — Fetch

After all runs complete, `run.py` queries the [EcoCI API](https://api.green-coding.io) (`/v1/ci/measurements`) to retrieve energy measurements for each run. EcoCI instruments the workflow runner using a CPU utilisation model to estimate energy in Joules.

The API returns one row per labelled step per run. These are summed by run ID to produce a single **total energy (J)** value per run, written to `raw.csv`.

---

## Repository Structure

```
suslint-experiments/
├── config.yml          # experiment parameters and subject repo list
├── run.py              # experiment runner (orchestrate + fetch)
├── analyze.py          # statistical analysis and forest plot
└── results/
    └── run_2026-03-28_17-05-41/     # one timestamped folder per execution
        ├── experiment.json           # snapshot of config.yml used
        ├── congo-SE/
        │   └── raw.csv              # energy per run (J)
        ├── rq-SE/
        │   └── raw.csv
        └── forest.png               # generated by analyze.py
```

Each execution creates a new timestamped folder, so repeated runs never overwrite each other.

---

## Setup

### 1. Install dependencies

```bash
pip install requests pyyaml pandas matplotlib scipy numpy
```

### 2. Authenticate the GitHub CLI

```bash
gh auth login
gh auth status  # token must include the 'workflow' scope
```

### 3. Prepare a subject repo

For each fork:

1. Create an `experiment` branch:
   ```bash
   git checkout dev
   git checkout -b experiment
   ```
2. Add `.github/workflows/experiment-baseline.yml` and `experiment-treatment.yml`. Both must have:
    - `on: workflow_dispatch` as the trigger
    - EcoCI instrumentation (`green-coding-solutions/eco-ci-energy-estimation@v5`) with labelled `get-measurement` steps
    - `continue-on-error: true` on all EcoCI steps
3. Push the branch:
   ```bash
   git push origin experiment
   ```

### 4. Configure `config.yml`

```yaml
experiment:
  runs_per_condition: 15      # runs per condition (baseline + treatment) per repo
  branch: experiment          # branch workflows are dispatched on
  delay_between_runs: 15      # seconds between triggering runs (keep ≥15 to avoid GitHub API lag)
  run_timeout: 900            # seconds before giving up on a single run

repos:
  - name: congo-SE
    owner: PaulAnton03
    baseline_workflow: experiment-baseline.yml
    treatment_workflow: experiment-treatment.yml
    description: "Congo"
```

---

## Running the Experiment

```bash
# Full experiment — all repos in config.yml
python run.py

# Single repo only
python run.py --repo congo-SE
```

A full experiment with 15 runs per condition across 4 repos takes approximately 2–3 hours, depending on workflow duration.

---

## Statistical Analysis

```bash
python analyze.py results/run_2026-03-28_17-05-41
```

Baseline and treatment runs are paired positionally by sorted run index. For each repo:

1. **Shapiro-Wilk** test on the paired differences to check normality
2. **Paired t-test** (if normal) or **Wilcoxon signed-rank** (if not)
3. **Cohen's d** (parametric) or **rank-biserial r** (non-parametric) as effect size
4. **95% CI** on the mean difference (t-based) or median difference (bootstrap)
5. A **summary table** is printed to stdout
6. A **forest plot** (`forest.png`) is saved to the run folder — one row per repo showing the point estimate and CI; blue = significant (p < 0.05), grey = not significant

---

## Notes on EcoCI

- EcoCI estimates energy by sampling CPU utilisation and applying a power model for the runner hardware. All measurements are taken on GitHub-hosted `ubuntu-latest` runners (AMD EPYC 7763).
- The EcoCI web dashboard displays values in **millijoules** but labels them as Joules. The raw API returns nanojoules; dividing by 10⁹ gives the correct Joules value used throughout this experiment.
- A "Could not send data to GMT API" warning in run logs is non-fatal — data is queued asynchronously and appears on the [EcoCI dashboard](https://metrics.green-coding.io/ci-index.html) within a few minutes.

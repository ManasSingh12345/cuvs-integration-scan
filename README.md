# cuVS Integration Scan

Find OSS libraries integrating [NVIDIA cuVS](https://github.com/rapidsai/cuvs), classified by maturity
(`proposed` → `under_integration` → `integrated`) with an evidence URL for every hit. Outputs a SQLite DB, CSVs,
and a self-contained interactive HTML dashboard.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GITHUB_TOKEN=ghp_...   # classic token, public_repo scope
```

## Run

```bash
python cuvs_scan.py --db cuvs.db     # scan → DB → enrich → CSVs → dashboard
open cuvs_dashboard.html
```

Individual stages, all off an existing DB: `--report`, `--csv [PREFIX]`, `--dashboard [PATH]`, `--enrich`,
`--no-stubs`.

## How repos are identified

| Step | What happens | Signal → result |
|------|--------------|-----------------|
| 1. Search | Query GitHub code, issues/PRs, READMEs, and deps.dev for cuVS terms (`import cuvs`, `cuvs::`, `find_package(cuvs)`, `libcuvs`, `cuvs-cu11/12`, `pylibcuvs`, `CAGRA`). | candidate repos, each with an evidence URL |
| 2. Classify maturity | Map *where* the match was found to a tier, gated by who filed it and how much work it is. | code / declared dep → `integrated`; merged PR → `integrated`; open PR by a maintainer **or touching ≥ 3 files** → `under_integration`; maintainer-opened issue → `proposed`; README → by wording. External issues, trivial or closed-unmerged PRs are dropped. |
| 3. Enrich | One GitHub API call per repo. | GitHub stars + fork flag |
| 4. Score confidence | Judge how cuVS-specific the match is. | **HIGH** (cuVS symbol) vs **LOW** (generic / RAFT predecessor); **strong** (real symbol or README status) vs **keyword** (bare `CAGRA`) |
| 5. Filter & collapse | Keep the trustworthy set. | drop forks; keep HIGH-precision; the strict cut also drops keyword-only; one row per repo at its highest tier |

## Outputs

- **`cuvs.db`** — every hit (table `hits`), one row per repo / source / evidence URL.
- **`cuvs_high_confidence.csv`** — HIGH-precision, non-fork; one row per repo at its highest tier.
- **`cuvs_high_confidence_strict.csv`** — same, minus bare-`CAGRA` keyword noise (recommended).
- **`cuvs_dashboard.html`** — standalone interactive dashboard.

A prebuilt snapshot is in [`results/`](results/).

## Notes

- Needs a GitHub token for the GitHub collectors + stars; deps.dev needs none.
- Keyword search matches loosely — confirm hits via the evidence URL. The strong/keyword flag and the strict CSV
  are there to help.

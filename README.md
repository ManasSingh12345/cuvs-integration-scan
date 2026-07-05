# cuVS Integration Scan

Discover open-source libraries integrating [NVIDIA cuVS](https://github.com/rapidsai/cuvs) and classify each by
integration maturity — **`proposed` → `under_integration` → `integrated`** — with a falsifiable evidence URL for
every hit. Results are persisted to SQLite, exported to CSV, and rendered as a self-contained interactive HTML
dashboard.

Every finding carries an evidence link and a `collected_at` date, so the whole thing is **re-runnable and
auditable** — you can click through and confirm any claim.

---

## What it does

**Collectors** (each yields evidence-bearing hits):

| Collector | Source | Signal it produces |
|---|---|---|
| `github_code` | GitHub code search | symbol on a branch → `integrated` |
| `github_issue_pr` | GitHub issue/PR search | PR → `under_integration`, merged PR → `integrated`, issue → `proposed` |
| `depsdev` | [deps.dev](https://deps.dev) reverse-dependents (no auth) | declared dependency → `integrated` |
| `readme_self` | a repo's own README | maturity inferred from wording ("supported" vs "coming soon" vs …) |
| *stubs* | Sourcegraph, Gitee, ANN-Benchmarks, NVIDIA blog/GTC | wired-off placeholders, ready to implement |

**Enrichment:** after collection, each repo is annotated with its **GitHub stars** and **fork flag** (one API
call per repo, cached in the DB).

**Signal quality**, so you can separate real integrations from keyword collisions:
- `precision` — `HIGH` (cuVS-specific, e.g. `import cuvs`, `cuvs::`, `find_package(cuvs)`) vs `LOW` (predecessor
  RAFT symbols, generic terms — confirm before trusting).
- `strong` vs `keyword` — a bare **`CAGRA`** match is treated as keyword-only (it collides with unrelated code),
  while a concrete cuVS symbol or a README status statement is strong.

**Outputs:** a SQLite DB, two CSVs (a full HIGH-precision cut and a strict de-noised cut), and an interactive
dashboard.

---

## Requirements

- Python 3.9+
- [`requests`](https://pypi.org/project/requests/)
- A **GitHub token** for the GitHub collectors and star/fork enrichment (deps.dev needs none). See
  [GitHub token](#github-token).

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GITHUB_TOKEN=ghp_your_token_here
```

---

## Quick start — end to end

```bash
python cuvs_scan.py --db cuvs.db
```

A single run does the whole pipeline:

1. **collect** from every live collector,
2. **persist** to `cuvs.db` (dedup + keep the highest maturity tier ever seen per repo),
3. **enrich** every repo with stars + fork flag,
4. **report** a tiered table to the terminal,
5. **export** `cuvs_high_confidence.csv` + `cuvs_high_confidence_strict.csv`,
6. **build** `cuvs_dashboard.html`.

Then open the dashboard (it's a standalone file — no server needed):

```bash
open cuvs_dashboard.html        # macOS
xdg-open cuvs_dashboard.html    # Linux
```

> Without `GITHUB_TOKEN` the run still completes, but the GitHub collectors and enrichment are skipped — you'll
> only get deps.dev hits and no stars.

---

## Usage — individual stages

Every stage below operates on an **existing DB**, so you can re-export without re-scanning. Enrichment is
idempotent (it only fetches repos whose stars are still unknown), so it's cheap to repeat.

```bash
python cuvs_scan.py --db cuvs.db --report            # print the tiered table and exit
python cuvs_scan.py --db cuvs.db --enrich            # backfill stars/fork only
python cuvs_scan.py --db cuvs.db --csv               # (re)write the two CSVs   -> cuvs_high_confidence*.csv
python cuvs_scan.py --db cuvs.db --csv myprefix      # custom prefix            -> myprefix.csv / myprefix_strict.csv
python cuvs_scan.py --db cuvs.db --dashboard         # (re)write the dashboard  -> cuvs_dashboard.html
python cuvs_scan.py --db cuvs.db --dashboard out.html
python cuvs_scan.py --db cuvs.db --no-stubs          # full scan, skip stub collectors
```

---

## Outputs

### SQLite — `cuvs.db`

One row per piece of evidence. Primary key `(library, source, evidence_url)`; re-runs UPSERT and keep the highest
maturity tier.

| column | meaning |
|---|---|
| `library` | `owner/repo` (GitHub) or package name (deps.dev) |
| `source` | which collector produced the hit |
| `maturity` | `proposed` \| `under_integration` \| `integrated` |
| `precision` | `HIGH` \| `LOW` |
| `first_party` | 1 if owned by `rapidsai` / `nvidia` / `nvidia-merlin` |
| `github_stars` | stargazers (filled by enrichment; NULL until then) |
| `is_fork` | 1 = GitHub fork, 0 = not, NULL = unknown |
| `evidence_url` | the exact match — click to verify |
| `note` | short description of the match |
| `collected_at` | ISO date |

Query it directly, e.g.:

```bash
sqlite3 cuvs.db "SELECT library, maturity, github_stars FROM hits
                 WHERE precision='HIGH' AND COALESCE(is_fork,0)=0
                 ORDER BY github_stars DESC LIMIT 20;"
```

### CSVs

Both collapse to **one row per repo at its highest tier**, sorted by tier then stars, with columns:
`library, github_stars, maturity, first_party, high_evidence_count, sources, example_evidence_url, note, collected_at`.

- **`cuvs_high_confidence.csv`** — HIGH-precision, non-fork.
- **`cuvs_high_confidence_strict.csv`** — the same, minus bare-`CAGRA` keyword-only collisions (the recommended
  starting point).

### Dashboard — `cuvs_dashboard.html`

Self-contained (all data embedded — works offline, no server). Live summary cards plus filters:

- **Search** by repo name
- **Tier** pills: integrated / under integration / proposed
- **Party**: all / first-party (NVIDIA·RAPIDS) / third-party
- **Min stars**: Any / 100+ / 1k+ / 10k+
- **Sort**: stars / name / tier / evidence
- **Strong signal only** (default on — hides `CAGRA`-keyword noise)
- **Hide forks** (default on)

Each row links to the repo and to its representative evidence, and is flagged **● strong** or **○ keyword**.

> The dashboard is meant to be opened directly as a file (`file://`). A local static server also works, e.g.
> `python -m http.server` from the output directory, then browse to `/cuvs_dashboard.html`.

---

## Maturity & signal model

- **Tiers** are ranked `proposed(0) < under_integration(1) < integrated(2)`; UPSERT always keeps the highest tier
  observed for a repo across all sources.
- A **merged** PR is upgraded from `under_integration` to `integrated`.
- README wording is classified by ordered cues so an explicit "coming soon" downgrades a generic "supports cuVS".
- `CAGRA` is a genuine cuVS algorithm name but a noisy search term; it's kept as a signal but flagged
  keyword-only. **Always confirm `LOW`-precision / keyword hits via the evidence URL before trusting
  "integrated".**

---

## GitHub token

Create a **classic** token (works reliably with the code-search API):

1. https://github.com/settings/tokens → *Generate new token (classic)*
2. Scope: **`public_repo`** is sufficient (the tool only reads public repos).
3. Copy it (`ghp_…`) and `export GITHUB_TOKEN=…`.

Rate limits: authenticated code search allows 30 requests/min — the tool sleeps between code-search terms to stay
under the limit, so a full scan takes a couple of minutes.

---

## Extending

- **Candidate repos for README scanning:** edit `README_SEED_REPOS`. The tool also auto-scans the README of every
  `owner/repo` surfaced by other collectors.
- **Search terms:** edit `CODE_TERMS` (each tagged `HIGH`/`LOW`).
- **deps.dev seeds:** edit `DEPSDEV_SEEDS`.
- **Stub collectors:** `collect_sourcegraph`, `collect_gitee`, `collect_annbench`, `collect_nvidia_signals` share
  the same shape — drop your logic in and they flow through the same pipeline.

---

## Caveats

- **Keyword noise:** code/PR search matches loosely; incidental hits (docs, changelogs, pip-freeze notebooks) can
  appear. The strong/keyword flag and the strict CSV exist to manage this.
- **Forks** inflate raw counts; enrichment flags them and the CSV/dashboard hide them by default.
- **README classifier** is heuristic — a repo can appear at different tiers from different sources; the
  "highest-tier-wins" collapse is usually right, but individual rows aren't authoritative. Open the evidence URL.
- **deps.dev** only surfaces declared dependents that it has indexed.

---

## License

Apache-2.0 (see [`LICENSE`](LICENSE)) — chosen to match the RAPIDS/cuVS ecosystem; change it if you prefer.

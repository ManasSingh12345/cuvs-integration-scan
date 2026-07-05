# Results — cuVS integration scan snapshot

A **point-in-time snapshot** produced by [`cuvs_scan.py`](../cuvs_scan.py) on **2026-07-05**.
These files are generated artifacts — regenerate a fresh copy anytime with `python cuvs_scan.py --db cuvs.db`
(see the [root README](../README.md)). Treat every classification as a lead to confirm via its evidence URL, not
ground truth.

**Snapshot at a glance**

- **413** evidence rows across **219** repositories
- Highest tier per repo: **171 integrated · 31 under integration · 17 proposed**
- **13** first-party (NVIDIA / RAPIDS) · **11** forks · GitHub stars resolved for all **219**
- HIGH-precision, non-fork repos: **140** (strict, de-noised cut: **114**)

---

## Files

### `cuvs.db` — SQLite database (the source of truth)

The full, un-collapsed evidence: **413 rows** in table `hits`, one row per `(library, source, evidence_url)`.
Everything else in this folder is derived from it.

| column | meaning |
|---|---|
| `library` | `owner/repo` (GitHub) or package name (deps.dev) |
| `source` | collector: `github_code`, `github_issue_pr`, `readme_self`, `depsdev` |
| `maturity` | `proposed` \| `under_integration` \| `integrated` |
| `precision` | `HIGH` (cuVS-specific) \| `LOW` (predecessor/generic — confirm) |
| `first_party` | 1 if owned by `rapidsai` / `nvidia` / `nvidia-merlin` |
| `github_stars` | stargazers at snapshot time |
| `is_fork` | 1 = GitHub fork, 0 = not, NULL = unknown |
| `evidence_url` | the exact match — click to verify |
| `note` | short description of the match |
| `collected_at` | ISO date the row was collected |

```bash
sqlite3 results/cuvs.db "SELECT library, maturity, github_stars FROM hits
                         WHERE precision='HIGH' AND COALESCE(is_fork,0)=0
                         ORDER BY github_stars DESC LIMIT 15;"
```

### `cuvs_high_confidence.csv` — HIGH-precision, non-fork (140 repos)

One row per repository, collapsed to its **highest** maturity tier, sorted by tier then stars. Filtered to
`precision = HIGH` and non-forks. Columns:
`library, github_stars, maturity, first_party, high_evidence_count, sources, example_evidence_url, note, collected_at`.

Still includes some symbol-collision noise (e.g. a repo that only mentions `CAGRA`, or vendors Faiss with the
cuVS build path) — use the strict cut below unless you want maximum recall.

### `cuvs_high_confidence_strict.csv` — de-noised cut (114 repos) ⭐ recommended starting point

Identical to the full CSV, minus repositories whose only signal is a bare **`CAGRA`** keyword match (which
collides with unrelated code). A repo is kept if it has a concrete cuVS symbol (`import cuvs`, `cuvs::`,
`find_package(cuvs)`, `cuvs-cu12`, …) or a README status statement. Same columns as the full CSV.

### `cuvs_dashboard.html` — interactive dashboard (all 219 repos)

Self-contained — all data is embedded, so it works offline with no server. Open it directly:

```bash
open results/cuvs_dashboard.html        # macOS
xdg-open results/cuvs_dashboard.html    # Linux
```

Live summary cards plus filters: **search** by name, **tier** pills, **party** (all / first-party / third-party),
**min stars**, **sort**, **Strong signal only** (default on — hides `CAGRA`-keyword noise), and **Hide forks**
(default on). The default view therefore shows the **114** strong, non-fork repos; toggle the filters off to see
all 219. Each row links to the repo and to its representative evidence, flagged **● strong** or **○ keyword**.

---

## How to read the tiers & signals

- **Tier** = highest maturity observed for that repo across all sources (`integrated` > `under_integration` >
  `proposed`; a merged PR counts as `integrated`).
- **precision / strong vs keyword** exist because keyword search matches loosely. A `● strong` hit is a real cuVS
  symbol or an explicit README status; a `○ keyword` hit (bare `CAGRA`) needs a human look — open the evidence URL.
- **Forks** are flagged and hidden by default in the CSV/dashboard; the DB retains them.

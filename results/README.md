# Results

Fresh snapshot from `cuvs_scan.py`, 2026-07-05 (author-gated issues/PRs, organization-default, vendored-copy filtering).

- **`cuvs.db`** — SQLite database, all 315 evidence rows across 183 repos (every signal, incl. forks / user-owned / vendored, each flagged).
- **`cuvs_high_confidence.csv`** — 55 organization-owned, HIGH-precision, non-fork repos; one row per repo at its highest tier.
- **`cuvs_high_confidence_strict.csv`** — the 53 of those with a real cuVS symbol (2 keyword-only dropped).
- **`cuvs_dashboard.html`** — interactive dashboard; default view = 53 org repos (strong signal, non-forks). Toggle off "Organizations only" / "Hide forks" / "Strong signal only" to see all 167.

# Results

Fresh snapshot from `cuvs_scan.py`, 2026-07-05 (author-gated issues/PRs, org-default, plus vendored-copy, substring-collision, and CAGRA-corroboration filtering).

- **`cuvs.db`** — SQLite database, all 303 evidence rows across 186 repos (every signal, incl. forks / user-owned / vendored / CAGRA, each flagged).
- **`cuvs_high_confidence.csv`** — the curated set: 50 organization-owned, HIGH-precision, non-fork repos with a real cuVS signal; one row per repo at its highest tier.
- **`cuvs_dashboard.html`** — interactive dashboard, 167 repos; default view = the 50 curated repos. Toggle off "Organizations only" / "Hide forks" / "Strong signal only" to see the rest.

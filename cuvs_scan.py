#!/usr/bin/env python3
"""
cuvs_scan.py — Discover open-source libraries integrating NVIDIA cuVS,
classified by maturity: proposed | under_integration | integrated.

Design:
  - LIVE collectors: GitHub (needs GITHUB_TOKEN), deps.dev (free, no auth).
  - STUB collectors: Sourcegraph, Gitee, ANN-Benchmarks, vendor docs, NVIDIA
    blog/GTC — return [] until you wire keys/parsers. Structure is identical
    so you drop logic in one place.
  - Every hit carries an evidence URL + collected_at date -> falsifiable, re-runnable.
  - SQLite persistence with UPSERT: re-runs dedup on (library, source, evidence_url)
    and keep the HIGHEST maturity tier ever seen per library.

Usage:
  export GITHUB_TOKEN=ghp_...
  python cuvs_scan.py --db cuvs.db
  python cuvs_scan.py --db cuvs.db --report        # print tiered table
"""

from __future__ import annotations
import argparse
import csv
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable

import requests

# --------------------------------------------------------------------------- #
# Maturity model
# --------------------------------------------------------------------------- #
TIER_RANK = {"proposed": 0, "under_integration": 1, "integrated": 2}


def higher(a: str, b: str) -> str:
    return a if TIER_RANK[a] >= TIER_RANK[b] else b


# --------------------------------------------------------------------------- #
# Query surface (broadened). Precision flag: HIGH = cuVS-specific, LOW = needs
# manual confirm before trusting the "integrated" tier.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Term:
    q: str
    precision: str  # "HIGH" | "LOW"


CODE_TERMS = [
    Term("import cuvs", "HIGH"),
    Term("from cuvs", "HIGH"),
    Term('#include "cuvs', "HIGH"),
    Term("cuvs::", "HIGH"),
    Term("find_package(cuvs", "HIGH"),
    Term("libcuvs", "HIGH"),
    Term("cuvs-cu12", "HIGH"),
    Term("cuvs-cu11", "HIGH"),
    Term("pylibcuvs", "HIGH"),
    Term("CAGRA", "HIGH"),          # cuVS-specific algorithm name
    Term("raft::neighbors", "LOW"), # predecessor / migration candidate
    Term("pylibraft", "LOW"),
    Term("IVF-PQ GPU", "LOW"),
]

MANIFEST_FILES = [
    "setup.py", "pyproject.toml", "requirements.txt", "environment.yml",
    "meta.yaml", "CMakeLists.txt", "Cargo.toml", "go.mod", "pom.xml",
    "build.gradle",
]

# deps.dev reverse-dependency seeds (free API)
DEPSDEV_SEEDS = [
    ("PYPI", "cuvs"),
    ("PYPI", "pylibcuvs"),
    ("PYPI", "cuvs-cu12"),
]

# Repos we treat as first-party (RAPIDS/NVIDIA-maintained) for confidence flag
FIRST_PARTY_ORGS = {"rapidsai", "nvidia", "nvidia-merlin"}


@dataclass
class Hit:
    library: str
    source: str
    maturity: str
    precision: str
    first_party: bool
    evidence_url: str
    note: str = ""
    collected_at: str = field(
        default_factory=lambda: dt.date.today().isoformat()
    )


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS hits (
    library       TEXT NOT NULL,
    source        TEXT NOT NULL,
    maturity      TEXT NOT NULL,
    precision     TEXT NOT NULL,
    first_party   INTEGER NOT NULL,
    github_stars  INTEGER,          -- repo stargazers; NULL if unknown/non-repo
    is_fork       INTEGER,          -- 1 = GitHub fork, 0 = not, NULL = unknown
    owner_type    TEXT,             -- 'Organization' | 'User' | NULL (unknown)
    evidence_url  TEXT NOT NULL,
    note          TEXT,
    collected_at  TEXT NOT NULL,
    PRIMARY KEY (library, source, evidence_url)
);
CREATE INDEX IF NOT EXISTS idx_lib ON hits(library);
"""

# Columns added after v1; ALTER them onto pre-existing DBs.
_REPO_META_COLS = {"github_stars": "INTEGER", "is_fork": "INTEGER", "owner_type": "TEXT"}


def db_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(hits)")}
    for col, coltype in _REPO_META_COLS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE hits ADD COLUMN {col} {coltype}")
    conn.commit()
    return conn


def upsert(conn: sqlite3.Connection, h: Hit) -> None:
    # Dedup on PK; on conflict keep the higher maturity tier.
    cur = conn.execute(
        "SELECT maturity FROM hits WHERE library=? AND source=? AND evidence_url=?",
        (h.library, h.source, h.evidence_url),
    )
    row = cur.fetchone()
    if row:
        best = higher(row[0], h.maturity)
        conn.execute(
            "UPDATE hits SET maturity=?, precision=?, first_party=?, note=?, collected_at=? "
            "WHERE library=? AND source=? AND evidence_url=?",
            (best, h.precision, int(h.first_party), h.note, h.collected_at,
             h.library, h.source, h.evidence_url),
        )
    else:
        # github_stars / is_fork are filled later by enrich_repos().
        conn.execute(
            "INSERT INTO hits "
            "(library, source, maturity, precision, first_party, "
            " evidence_url, note, collected_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (h.library, h.source, h.maturity, h.precision, int(h.first_party),
             h.evidence_url, h.note, h.collected_at),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Collectors  (each yields Hit objects)
# --------------------------------------------------------------------------- #
GITHUB_API = "https://api.github.com"


def _gh_headers() -> dict:
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        raise RuntimeError("GITHUB_TOKEN not set")
    return {"Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json"}


def _repo_full_name(item: dict) -> str:
    # code-search item -> repository.full_name; issue item -> parse repository_url
    if "repository" in item:
        return item["repository"]["full_name"]
    m = re.search(r"repos/([^/]+/[^/]+)$", item.get("repository_url", ""))
    return m.group(1) if m else "unknown/unknown"


# Request matched snippets so we can reject substring collisions post-hoc.
_TEXT_MATCH_ACCEPT = "application/vnd.github.text-match+json"


def _match_is_genuine(term: str, item: dict) -> bool:
    """Reject substring collisions (e.g. 'libcuvs' matching 'libcuvslam' in
       nvidia-isaac/cuVSLAM). The core token must not run straight into more
       letters — 'libcuvs', '/cuvs', 'cuvs-cu12', 'cuvs::' pass; 'cuvslam' fails.
       Verifies against the search API's text_matches fragments."""
    tl = term.lower()
    core = ("cuvs" if "cuvs" in tl else
            "cagra" if "cagra" in tl else
            "raft" if "raft" in tl else None)
    if core is None:
        return True  # nothing collision-prone to guard (e.g. 'IVF-PQ GPU')
    frags = [m.get("fragment", "") for m in item.get("text_matches", [])]
    if not frags:
        return True  # no fragment metadata -> keep (favor recall over precision)
    pat = re.compile(core + r"(?![A-Za-z])", re.IGNORECASE)
    return any(pat.search(f) for f in frags)


def collect_github_code(terms: Iterable[Term], sleep: float = 6.0) -> Iterable[Hit]:
    """GitHub code search -> INTEGRATED (symbol present on a branch)."""
    headers = {**_gh_headers(), "Accept": _TEXT_MATCH_ACCEPT}
    for t in terms:
        url = f"{GITHUB_API}/search/code?q={requests.utils.quote(t.q)}&per_page=30"
        try:
            r = requests.get(url, headers=headers, timeout=30)
        except Exception as e:
            print(f"[github_code] {t.q!r} error: {e}", file=sys.stderr)
            continue
        if r.status_code == 403:
            print(f"[github_code] rate-limited on {t.q!r}; backing off", file=sys.stderr)
            time.sleep(30)
            continue
        if r.status_code != 200:
            print(f"[github_code] {t.q!r} -> {r.status_code}", file=sys.stderr)
            time.sleep(sleep)
            continue
        for item in r.json().get("items", []):
            if not _match_is_genuine(t.q, item):
                continue  # substring collision, e.g. libcuvs inside libcuvslam
            full = _repo_full_name(item)
            org = full.split("/")[0].lower()
            yield Hit(
                library=full,
                source="github_code",
                maturity="integrated",
                precision=t.precision,
                first_party=org in FIRST_PARTY_ORGS,
                evidence_url=item.get("html_url", f"https://github.com/{full}"),
                note=f"code match: {t.q}",
            )
        time.sleep(sleep)  # code search: strict secondary rate limits


# Signal-quality gates for issue/PR hits (see collect_github_issues_prs).
MAINTAINER_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}  # author speaks for the project
SERIOUS_PR_FILES = 3   # open PR from a non-maintainer must touch >= this many files


def _pr_workload(repo: str, number: int) -> tuple[int, int] | None:
    """Fetch (changed_files, additions) for one PR — search results omit them.
       One extra API call; returns None if the detail can't be fetched."""
    try:
        r = requests.get(f"{GITHUB_API}/repos/{repo}/pulls/{number}",
                         headers=_gh_headers(), timeout=30)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    j = r.json()
    return int(j.get("changed_files", 0)), int(j.get("additions", 0))


def collect_github_issues_prs(terms: Iterable[Term], sleep: float = 3.0) -> Iterable[Hit]:
    """GitHub issue/PR search, gated on author authority, PR outcome, and PR size
       so a stranger's "sounds exciting" issue doesn't count as project intent:

         merged PR                                          -> integrated
         open PR by a maintainer, OR touching >= SERIOUS_PR_FILES files
                                                            -> under_integration
         maintainer-opened, still-open issue                -> proposed
         external issues / trivial external PRs /
             closed-unmerged PRs                            -> dropped
    """
    headers = {**_gh_headers(), "Accept": _TEXT_MATCH_ACCEPT}
    for t in terms:
        if t.precision != "HIGH":
            continue  # keep issue search precise
        q = f'{t.q} in:title,body'
        url = f"{GITHUB_API}/search/issues?q={requests.utils.quote(q)}&per_page=30"
        try:
            r = requests.get(url, headers=headers, timeout=30)
        except Exception as e:
            print(f"[github_issues] {t.q!r} error: {e}", file=sys.stderr)
            continue
        if r.status_code != 200:
            print(f"[github_issues] {t.q!r} -> {r.status_code}", file=sys.stderr)
            time.sleep(sleep)
            continue
        for item in r.json().get("items", []):
            if not _match_is_genuine(t.q, item):
                continue  # substring collision, e.g. libcuvs inside libcuvslam
            full = _repo_full_name(item)
            org = full.split("/")[0].lower()
            assoc = (item.get("author_association") or "NONE").upper()
            maintainer = assoc in MAINTAINER_ASSOC
            state = item.get("state", "open")
            is_pr = "pull_request" in item

            if is_pr:
                merged = bool(item.get("pull_request", {}).get("merged_at"))
                if merged:
                    maturity, work = "integrated", ""
                elif state == "closed":
                    continue  # closed without merging -> rejected/abandoned
                else:
                    fa = _pr_workload(full, item["number"]) if item.get("number") else None
                    n_files, adds = fa if fa else (0, 0)
                    if not (maintainer or n_files >= SERIOUS_PR_FILES):
                        continue  # external drive-by with little work -> noise
                    maturity = "under_integration"
                    work = f", {n_files}f/+{adds}"
                note = f"PR [{assoc}{work}]: {t.q}"
            else:
                if not maintainer or state != "open":
                    continue  # only a maintainer's open issue is real project intent
                maturity = "proposed"
                note = f"issue [{assoc}]: {t.q}"

            yield Hit(
                library=full,
                source="github_issue_pr",
                maturity=maturity,
                precision=t.precision,
                first_party=org in FIRST_PARTY_ORGS,
                evidence_url=item.get("html_url", ""),
                note=note,
            )
        time.sleep(sleep)


def collect_depsdev(seeds=DEPSDEV_SEEDS) -> Iterable[Hit]:
    """deps.dev reverse dependents (free, no auth) -> INTEGRATED (declared dep)."""
    base = "https://api.deps.dev/v3alpha"
    for system, pkg in seeds:
        # resolve latest version, then pull dependents
        vurl = f"{base}/systems/{system}/packages/{pkg}"
        try:
            vr = requests.get(vurl, timeout=30)
            if vr.status_code != 200:
                print(f"[depsdev] {pkg} meta -> {vr.status_code}", file=sys.stderr)
                continue
            versions = vr.json().get("versions", [])
            if not versions:
                continue
            latest = versions[-1]["versionKey"]["version"]
            durl = (f"{base}/systems/{system}/packages/{pkg}"
                    f"/versions/{latest}:dependents")
            dr = requests.get(durl, timeout=30)
            if dr.status_code != 200:
                continue
            for dep in dr.json().get("dependents", []):
                name = dep.get("versionKey", {}).get("name", "unknown")
                yield Hit(
                    library=name,
                    source="depsdev",
                    maturity="integrated",
                    precision="HIGH",
                    first_party=False,
                    evidence_url=f"https://deps.dev/{system.lower()}/{name}",
                    note=f"declares dependency on {pkg}",
                )
        except Exception as e:
            print(f"[depsdev] {pkg} error: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# README scan: read a CANDIDATE library's OWN README and classify how *they*
# describe their cuVS status. Catches doc-stated intent that code search misses.
# --------------------------------------------------------------------------- #
# Candidate repos to inspect. Extend freely, or feed from prior hits (see run()).
README_SEED_REPOS = [
    "milvus-io/milvus",
    "facebookresearch/faiss",
    "apache/lucene",
    "opensearch-project/OpenSearch",
    "weaviate/weaviate",
    "qdrant/qdrant",
    "pgvector/pgvector",
    "redis/redis",
    "elastic/elasticsearch",
    "kinetica/kinetica",
]

# cuVS-relevant mention detector (case-insensitive)
CUVS_MENTION = re.compile(r"\bcuvs\b|\bcagra\b|\bcu-?vs\b", re.IGNORECASE)

# Ordered maturity cues: FIRST match wins, checked high->low so explicit
# "coming soon" downgrades a generic "supports cuVS" mention.
PROPOSED_CUES = re.compile(
    r"coming soon|planned|roadmap|on the roadmap|will support|"
    r"future|proposed|we intend|tracking issue|rfc",
    re.IGNORECASE,
)
UNDER_CUES = re.compile(
    r"experimental|beta|preview|work in progress|wip|in progress|"
    r"under development|alpha|early access|opt-in|unstable",
    re.IGNORECASE,
)
INTEGRATED_CUES = re.compile(
    r"supported|is supported|now supports|integrated|available|"
    r"enabled by|powered by|built on|ga\b|generally available|default",
    re.IGNORECASE,
)


def classify_readme_maturity(context: str) -> tuple[str, str]:
    """Given text around a cuVS mention, return (maturity, matched_cue).
       Order matters: proposed/under override a bare 'supported'."""
    if PROPOSED_CUES.search(context):
        return "proposed", PROPOSED_CUES.search(context).group(0)
    if UNDER_CUES.search(context):
        return "under_integration", UNDER_CUES.search(context).group(0)
    if INTEGRATED_CUES.search(context):
        return "integrated", INTEGRATED_CUES.search(context).group(0)
    # Mention with no status verb -> conservative: proposed, needs confirm.
    return "proposed", "mention-only"


def _gh_readme_text(repo: str) -> tuple[str, str] | None:
    """Fetch decoded README via GitHub contents API (allowed domain, uses token).
       Returns (text, html_url) or None."""
    import base64
    url = f"{GITHUB_API}/repos/{repo}/readme"
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=30)
    except Exception as e:
        print(f"[readme] {repo} error: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"[readme] {repo} -> {r.status_code}", file=sys.stderr)
        return None
    j = r.json()
    try:
        text = base64.b64decode(j["content"]).decode("utf-8", errors="replace")
    except Exception:
        return None
    return text, j.get("html_url", f"https://github.com/{repo}")


def collect_readmes(repos: Iterable[str], window: int = 160,
                    sleep: float = 1.0) -> Iterable[Hit]:
    """Scan each repo's OWN README for cuVS status. Maturity from context cues."""
    for repo in repos:
        got = _gh_readme_text(repo)
        if not got:
            continue
        text, url = got
        org = repo.split("/")[0].lower()
        seen_contexts = set()
        for m in CUVS_MENTION.finditer(text):
            s = max(0, m.start() - window)
            e = min(len(text), m.end() + window)
            ctx = text[s:e]
            key = ctx.strip()[:80]
            if key in seen_contexts:
                continue  # dedup near-identical mentions in same README
            seen_contexts.add(key)
            maturity, cue = classify_readme_maturity(ctx)
            snippet = " ".join(ctx.split())[:120]
            yield Hit(
                library=repo,
                source="readme_self",
                maturity=maturity,
                # README self-description is a real signal but softer than a
                # symbol import; flag LOW when it's only a bare mention.
                precision="HIGH" if cue not in ("mention-only",) else "LOW",
                first_party=org in FIRST_PARTY_ORGS,
                evidence_url=url,
                note=f"README cue='{cue}': …{snippet}…",
            )
        time.sleep(sleep)


# ---- STUB collectors: wire logic later, structure ready --------------------- #
def collect_sourcegraph(terms) -> Iterable[Hit]:
    """STUB: Sourcegraph public search (higher volume than GH code search).
       Needs SOURCEGRAPH_TOKEN. Return integrated hits on symbol match."""
    return iter(())


def collect_gitee(terms) -> Iterable[Hit]:
    """STUB: Gitee code search — critical for CN partners (Alibaba/Volcengine)."""
    return iter(())


def collect_annbench() -> Iterable[Hit]:
    """STUB: ANN-Benchmarks repo — entries adding cuVS/CAGRA impl -> integrated."""
    return iter(())


def collect_nvidia_signals() -> Iterable[Hit]:
    """STUB: cuVS README integrations list + git history, NVIDIA blog, GTC titles
       -> proposed/under_integration leading indicators."""
    return iter(())


# --------------------------------------------------------------------------- #
# Repo enrichment: stars + fork flag are repo-level, so one GitHub call per
# distinct owner/repo updates all of that library's hit rows. Idempotent — only
# fetches libraries whose github_stars is still NULL, so re-runs are cheap.
# --------------------------------------------------------------------------- #
def enrich_repos(conn: sqlite3.Connection, sleep: float = 0.12) -> int:
    """Populate github_stars + is_fork + owner_type for owner/repo libraries.
       Needs a token. Idempotent: only fetches repos missing any of these."""
    if not os.environ.get("GITHUB_TOKEN"):
        print("[enrich] GITHUB_TOKEN not set; skipping stars/fork", file=sys.stderr)
        return 0
    libs = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT library FROM hits "
            "WHERE github_stars IS NULL OR owner_type IS NULL"
        )
        if r[0] and re.fullmatch(r"[\w.-]+/[\w.-]+", r[0])
    ]
    n = 0
    for lib in libs:
        try:
            r = requests.get(f"{GITHUB_API}/repos/{lib}",
                             headers=_gh_headers(), timeout=30)
        except Exception as e:
            print(f"[enrich] {lib} error: {e}", file=sys.stderr)
            continue
        if r.status_code == 403:
            print("[enrich] rate-limited; backing off 30s", file=sys.stderr)
            time.sleep(30)
            continue
        if r.status_code != 200:
            continue  # 404 renamed/deleted -> leave NULL
        j = r.json()
        conn.execute(
            "UPDATE hits SET github_stars=?, is_fork=?, owner_type=? WHERE library=?",
            (j.get("stargazers_count"), int(bool(j.get("fork"))),
             (j.get("owner") or {}).get("type"), lib),
        )
        conn.commit()
        n += 1
        time.sleep(sleep)
    print(f"[enrich] updated {n} repos")
    return n


LIVE_COLLECTORS = [
    ("github_code", lambda: collect_github_code(CODE_TERMS)),
    ("github_issue_pr", lambda: collect_github_issues_prs(CODE_TERMS)),
    ("depsdev", lambda: collect_depsdev()),
    ("readme_self", lambda: collect_readmes(README_SEED_REPOS)),
]
STUB_COLLECTORS = [
    ("sourcegraph", lambda: collect_sourcegraph(CODE_TERMS)),
    ("gitee", lambda: collect_gitee(CODE_TERMS)),
    ("annbench", lambda: collect_annbench()),
    ("nvidia_signals", lambda: collect_nvidia_signals()),
]


# --------------------------------------------------------------------------- #
# Orchestration + report
# --------------------------------------------------------------------------- #
def run(conn: sqlite3.Connection, include_stubs: bool = True,
        readme_followup: bool = True) -> int:
    n = 0
    collectors = LIVE_COLLECTORS + (STUB_COLLECTORS if include_stubs else [])
    for name, factory in collectors:
        print(f"[run] collector={name}")
        try:
            for hit in factory():
                upsert(conn, hit)
                n += 1
        except Exception as e:
            print(f"[run] {name} failed: {e}", file=sys.stderr)

    # Second pass: scan the OWN README of every owner/repo-shaped library that
    # other collectors surfaced but that we didn't already README-scan.
    if readme_followup and os.environ.get("GITHUB_TOKEN"):
        already = {r[0] for r in conn.execute(
            "SELECT DISTINCT library FROM hits WHERE source='readme_self'"
        )}
        discovered = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT library FROM hits "
                "WHERE library LIKE '%/%' AND source!='readme_self'"
            )
            if re.fullmatch(r"[\w.-]+/[\w.-]+", r[0]) and r[0] not in already
        ]
        if discovered:
            print(f"[run] readme_followup over {len(discovered)} discovered repos")
            try:
                for hit in collect_readmes(discovered):
                    upsert(conn, hit)
                    n += 1
            except Exception as e:
                print(f"[run] readme_followup failed: {e}", file=sys.stderr)

    # Repo-level enrichment (stars + fork) once all hits are in.
    print("[run] enriching repos with stars + fork flag")
    enrich_repos(conn)
    return n


# --------------------------------------------------------------------------- #
# CSV export — priority logic
#   1. HIGH precision only          (drop LOW predecessor/mention noise)
#   2. non-fork                     (is_fork != 1)
#   3. one row per library at its HIGHEST maturity tier
#   4. sort by tier, then stars desc, then name
#   5. <prefix>.csv keeps every qualifying library; <prefix>_strict.csv keeps
#      only libraries with a STRONG signal (real cuVS symbol / README status /
#      non-CAGRA PR), dropping bare-"CAGRA"-keyword collisions.
# --------------------------------------------------------------------------- #
# Symbol cues that mean genuine cuVS usage rather than a keyword collision.
STRONG_NOTE_CUES = (
    "find_package(cuvs", "libcuvs", "import cuvs", "from cuvs", "cuvs::",
    '#include "cuvs', "pylibcuvs", "cuvs-cu12", "cuvs-cu11",
)


def _hit_is_strong(note: str, source: str) -> bool:
    """A README self-status is strong (LOW mention-only is already filtered out);
       otherwise require a concrete cuVS symbol in the note (excludes bare CAGRA)."""
    if source == "readme_self":
        return True
    return any(c in (note or "") for c in STRONG_NOTE_CUES)


# A cuVS match inside a *bundled copy* of another library (e.g. a repo that
# vendors faiss) is not evidence that THIS repo integrates cuVS. Drop such hits
# from curated views, but keep the genuine upstreams themselves.
_VENDORED_DIRS = ("/faiss/", "/third_party/", "/third-party/", "/thirdparty/",
                  "/vendor/", "/vendored/", "/external/", "/extern/", "/submodules/")
_UPSTREAM_WHITELIST = {"facebookresearch/faiss", "rapidsai/cuvs", "rapidsai/raft",
                       "nvidia/cuvs", "zilliztech/knowhere"}


def _is_vendored(evidence_url: str, library: str) -> bool:
    """True if the match sits inside a bundled copy of another library."""
    if (library or "").lower() in _UPSTREAM_WHITELIST:
        return False
    u = (evidence_url or "").lower()
    return any(d in u for d in _VENDORED_DIRS)


CSV_COLUMNS = ["library", "github_stars", "maturity", "first_party",
               "high_evidence_count", "sources", "example_evidence_url",
               "note", "collected_at"]


def export_csvs(conn: sqlite3.Connection, prefix: str = "cuvs_high_confidence") -> None:
    libs: dict[str, dict] = {}
    for lib, mat, fp, src, stars, url, note, cat in conn.execute(
        "SELECT library, maturity, first_party, source, github_stars, "
        "evidence_url, note, collected_at FROM hits "
        "WHERE precision='HIGH' AND COALESCE(is_fork, 0) = 0 "
        "AND owner_type = 'Organization' "
        "AND library NOT IN ('', 'unknown/unknown')"
    ):
        if _is_vendored(url, lib):
            continue  # match lives in a bundled copy of another lib, not this repo
        d = libs.setdefault(lib, {"library": lib, "github_stars": stars,
                                  "first_party": fp, "sources": set(),
                                  "evidence_n": 0, "rank": -1, "strong": False})
        d["sources"].add(src)
        d["evidence_n"] += 1
        d["strong"] = d["strong"] or _hit_is_strong(note, src)
        if stars is not None:
            d["github_stars"] = stars
        # keep the highest tier; prefer a code hit as the representative URL
        if TIER_RANK[mat] > d["rank"] or (TIER_RANK[mat] == d["rank"]
                                          and src == "github_code"):
            d.update(maturity=mat, rank=TIER_RANK[mat], first_party=fp,
                     example_evidence_url=url, note=note, collected_at=cat)

    def sort_key(d: dict):
        stars = d["github_stars"] if isinstance(d["github_stars"], int) else -1
        return (-d["rank"], -stars, d["library"])

    # CAGRA corroborates but can't stand alone: keep only repos with a real
    # cuVS signal (for HIGH hits, that is exactly d["strong"]).
    ranked = sorted((d for d in libs.values() if d["strong"]), key=sort_key)

    def write(path: str, rows: list[dict]) -> None:
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_COLUMNS)
            for d in rows:
                w.writerow([
                    d["library"],
                    d["github_stars"] if d["github_stars"] is not None else "",
                    d["maturity"],
                    "yes" if d["first_party"] else "no",
                    d["evidence_n"],
                    "|".join(sorted(d["sources"])),
                    d["example_evidence_url"],
                    d["note"], d["collected_at"],
                ])

    write(f"{prefix}.csv", ranked)
    print(f"[csv] {prefix}.csv: {len(ranked)} repos "
          f"(org-owned, HIGH-precision, non-fork, real cuVS signal; CAGRA-only excluded)")


# --------------------------------------------------------------------------- #
# Interactive HTML dashboard
# --------------------------------------------------------------------------- #
# Which source gives the "representative" evidence link when a repo has several.
_SRC_PRIORITY = {"github_code": 3, "readme_self": 2, "github_issue_pr": 1, "depsdev": 0}


def _aggregate_libraries(conn: sqlite3.Connection) -> list[dict]:
    """Collapse hits to one record per repo: highest tier, max stars, fork/1P
       flags, evidence count, source set, signal quality, best evidence link."""
    libs: dict[str, dict] = {}
    for lib, mat, prec, fp, fork, stars, otype, src, url, note in conn.execute(
        "SELECT library, maturity, precision, first_party, is_fork, "
        "github_stars, owner_type, source, evidence_url, note FROM hits"
    ):
        if _is_vendored(url, lib):
            continue  # match lives in a bundled copy of another lib, not this repo
        d = libs.get(lib)
        if d is None:
            d = libs[lib] = {"lib": lib, "tier": mat, "stars": None, "fp": False,
                             "fork": False, "org": False, "quality": "weak", "ev": 0,
                             "sources": set(), "url": url, "note": note or "",
                             "_score": (-1, -1, -1), "_hascore": False}
        d["ev"] += 1
        d["sources"].add(src)
        if stars is not None:
            d["stars"] = stars
        if fp:
            d["fp"] = True
        if fork == 1:
            d["fork"] = True
        if otype:
            d["org"] = (otype == "Organization")
        strong = prec == "HIGH" and _hit_is_strong(note, src)
        if strong:
            d["quality"] = "strong"
        # CAGRA corroborates but can't stand alone: repo needs >=1 non-CAGRA signal
        if strong or "cagra" not in (note or "").lower():
            d["_hascore"] = True
        score = (TIER_RANK[mat], 1 if strong else 0, _SRC_PRIORITY.get(src, 0))
        if score > d["_score"]:
            d.update(_score=score, tier=mat, url=url, note=(note or ""))
    out = []
    for d in libs.values():
        d.pop("_score", None)
        if not d.pop("_hascore", False):
            continue  # sole signal was a bare CAGRA keyword -> drop the repo
        d["sources"] = sorted(d["sources"])
        out.append(d)
    return out


def export_dashboard(conn: sqlite3.Connection, path: str = "cuvs_dashboard.html") -> None:
    libs = _aggregate_libraries(conn)
    libs.sort(key=lambda d: (-(d["stars"] or -1), d["lib"]))
    data = json.dumps(libs, ensure_ascii=False)
    # keep the JSON safe to embed inside a <script> element
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = (_DASHBOARD_TEMPLATE
            .replace("__DATA__", data)
            .replace("__GENERATED__", dt.date.today().isoformat())
            .replace("__COUNT__", str(len(libs))))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[dashboard] {path}: {len(libs)} repositories")


_DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cuVS Integration Tracker</title>
<style>
:root{--green:#76b900;--green-d:#5a8f00;--amber:#e0952b;--slate:#6b7684;
--bg:#f6f7f9;--card:#fff;--ink:#1a1f24;--muted:#6b7684;--line:#e6e9ed}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg)}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 20px;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .n{font-size:26px;font-weight:700;line-height:1}
.card .l{color:var(--muted);font-size:12px;margin-top:6px;text-transform:uppercase;letter-spacing:.04em}
.card.integrated .n{color:var(--green-d)}.card.under .n{color:var(--amber)}.card.proposed .n{color:var(--slate)}
.controls{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;display:flex;flex-wrap:wrap;gap:12px 18px;align-items:flex-end;margin-bottom:16px}
.controls label{font-size:12px;color:var(--muted);display:flex;flex-direction:column;gap:4px}
.controls input[type=text],.controls select{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:#fff;color:var(--ink);min-width:130px}
.controls input[type=text]{min-width:210px}
.pills{display:flex;gap:6px}
.pill{cursor:pointer;user-select:none;border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-size:12px;background:#fff;color:var(--muted)}
.pill.on.integrated{background:var(--green);border-color:var(--green);color:#fff}
.pill.on.under{background:var(--amber);border-color:var(--amber);color:#fff}
.pill.on.proposed{background:var(--slate);border-color:var(--slate);color:#fff}
.chk{flex-direction:row!important;align-items:center;gap:6px;color:var(--ink);font-size:13px}
.count{color:var(--muted);font-size:13px;margin:0 0 10px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top}
th{background:#fafbfc;color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em;white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafcff}
a{color:#1a6fd0;text-decoration:none}a:hover{text-decoration:underline}
.lib{font-weight:600}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;white-space:nowrap}
.b-integrated{background:#eaf5d8;color:#4a7a00}.b-under{background:#fbeccf;color:#9c6410}.b-proposed{background:#eef0f3;color:#5b6672}
.tag{display:inline-block;background:#eef0f3;color:#5b6672;border-radius:5px;padding:1px 6px;font-size:11px;margin:1px 3px 1px 0}
.fp{background:var(--green);color:#fff}
.stars{font-variant-numeric:tabular-nums;white-space:nowrap}
.sig{font-size:11px;color:var(--muted)}.sig.strong{color:var(--green-d);font-weight:600}
.dim{color:var(--muted)}
.empty{padding:40px;text-align:center;color:var(--muted)}
.reset{cursor:pointer;border:1px solid var(--line);background:#fff;border-radius:7px;padding:7px 12px;font:inherit;color:var(--muted)}
@media(max-width:820px){.cards{grid-template-columns:repeat(2,1fr)}.sources-col,.note-col{display:none}}
</style>
</head>
<body>
<div class="wrap">
  <h1>cuVS Integration Tracker</h1>
  <p class="sub">__COUNT__ repositories discovered &middot; generated __GENERATED__ &middot; tier = highest observed maturity per repo</p>
  <div class="cards" id="cards"></div>
  <div class="controls">
    <label>Search<input type="text" id="q" placeholder="repo name..."></label>
    <div><div style="font-size:12px;color:var(--muted);margin-bottom:4px">Tier</div>
      <div class="pills" id="tiers">
        <span class="pill integrated on" data-t="integrated">integrated</span>
        <span class="pill under on" data-t="under_integration">under integration</span>
        <span class="pill proposed on" data-t="proposed">proposed</span>
      </div></div>
    <label>Party<select id="party"><option value="all">All</option><option value="fp">First-party (NVIDIA/RAPIDS)</option><option value="tp">Third-party</option></select></label>
    <label>Min stars<select id="stars"><option value="0">Any</option><option value="100">100+</option><option value="1000">1,000+</option><option value="10000">10,000+</option></select></label>
    <label>Sort<select id="sort"><option value="stars">Stars &darr;</option><option value="name">Name A&ndash;Z</option><option value="tier">Tier</option><option value="ev">Evidence &darr;</option></select></label>
    <label class="chk"><input type="checkbox" id="strong" checked> Strong signal only</label>
    <label class="chk"><input type="checkbox" id="nofork" checked> Hide forks</label>
    <label class="chk"><input type="checkbox" id="orgonly" checked> Organizations only</label>
    <button class="reset" id="reset">Reset</button>
  </div>
  <p class="count" id="count"></p>
  <div id="tableWrap"></div>
</div>
<script>
const DATA = __DATA__;
const TIERLABEL={integrated:"integrated",under_integration:"under integration",proposed:"proposed"};
const TIERRANK={integrated:2,under_integration:1,proposed:0};
const BCLASS={integrated:"b-integrated",under_integration:"b-under",proposed:"b-proposed"};
const DEF=()=>({q:"",tiers:new Set(["integrated","under_integration","proposed"]),party:"all",stars:0,sort:"stars",strong:true,nofork:true,orgonly:true});
let st=DEF();
function fmt(n){return n==null?"—":n.toLocaleString()}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function apply(){
  let rows=DATA.filter(d=>{
    if(!st.tiers.has(d.tier))return false;
    if(st.strong&&d.quality!=="strong")return false;
    if(st.nofork&&d.fork)return false;
    if(st.orgonly&&!d.org)return false;
    if(st.party==="fp"&&!d.fp)return false;
    if(st.party==="tp"&&d.fp)return false;
    if((d.stars||0)<st.stars)return false;
    if(st.q&&!d.lib.toLowerCase().includes(st.q))return false;
    return true;
  });
  rows.sort((a,b)=>{
    if(st.sort==="name")return a.lib.localeCompare(b.lib);
    if(st.sort==="tier")return TIERRANK[b.tier]-TIERRANK[a.tier]||(b.stars||0)-(a.stars||0);
    if(st.sort==="ev")return b.ev-a.ev||(b.stars||0)-(a.stars||0);
    return (b.stars||0)-(a.stars||0)||a.lib.localeCompare(b.lib);
  });
  return rows;
}
function render(){
  const rows=apply();
  const c={integrated:0,under_integration:0,proposed:0,fp:0};
  rows.forEach(d=>{c[d.tier]++;if(d.fp)c.fp++;});
  document.getElementById("cards").innerHTML=
    '<div class="card"><div class="n">'+rows.length+'</div><div class="l">Repos shown</div></div>'+
    '<div class="card integrated"><div class="n">'+c.integrated+'</div><div class="l">Integrated</div></div>'+
    '<div class="card under"><div class="n">'+c.under_integration+'</div><div class="l">Under integration</div></div>'+
    '<div class="card proposed"><div class="n">'+c.proposed+'</div><div class="l">Proposed</div></div>'+
    '<div class="card"><div class="n">'+c.fp+'</div><div class="l">First-party</div></div>';
  document.getElementById("count").textContent=rows.length+" of "+DATA.length+" repositories";
  const w=document.getElementById("tableWrap");
  if(!rows.length){w.innerHTML='<div class="empty">No repositories match these filters.</div>';return;}
  let h='<table><thead><tr><th>Repository</th><th>Tier</th><th>Stars</th><th>Evidence</th><th class="sources-col">Sources</th><th class="note-col">Signal / example</th></tr></thead><tbody>';
  for(const d of rows){
    const fp=d.fp?'<span class="badge fp">1P</span> ':'';
    const src=d.sources.map(s=>'<span class="tag">'+esc(s)+'</span>').join("");
    const sig=d.quality==="strong"?'<span class="sig strong">&#9679; strong</span>':'<span class="sig" title="keyword-only match — confirm before trusting">&#9675; keyword</span>';
    h+='<tr><td class="lib">'+fp+'<a href="https://github.com/'+esc(d.lib)+'" target="_blank" rel="noopener">'+esc(d.lib)+'</a></td>'+
      '<td><span class="badge '+BCLASS[d.tier]+'">'+TIERLABEL[d.tier]+'</span></td>'+
      '<td class="stars">'+(d.stars==null?'<span class="dim">—</span>':'★ '+fmt(d.stars))+'</td>'+
      '<td>'+d.ev+'</td>'+
      '<td class="sources-col">'+src+'</td>'+
      '<td class="note-col">'+sig+' &middot; <a href="'+esc(d.url)+'" target="_blank" rel="noopener">evidence &#8599;</a></td></tr>';
  }
  w.innerHTML=h+'</tbody></table>';
}
document.getElementById("q").addEventListener("input",e=>{st.q=e.target.value.trim().toLowerCase();render();});
document.getElementById("party").addEventListener("change",e=>{st.party=e.target.value;render();});
document.getElementById("stars").addEventListener("change",e=>{st.stars=+e.target.value;render();});
document.getElementById("sort").addEventListener("change",e=>{st.sort=e.target.value;render();});
document.getElementById("strong").addEventListener("change",e=>{st.strong=e.target.checked;render();});
document.getElementById("nofork").addEventListener("change",e=>{st.nofork=e.target.checked;render();});
document.getElementById("orgonly").addEventListener("change",e=>{st.orgonly=e.target.checked;render();});
document.querySelectorAll("#tiers .pill").forEach(p=>p.addEventListener("click",()=>{
  const t=p.dataset.t;
  if(st.tiers.has(t)){st.tiers.delete(t);p.classList.remove("on");}else{st.tiers.add(t);p.classList.add("on");}
  render();
}));
document.getElementById("reset").addEventListener("click",()=>{
  st=DEF();
  document.getElementById("q").value="";document.getElementById("party").value="all";
  document.getElementById("stars").value="0";document.getElementById("sort").value="stars";
  document.getElementById("strong").checked=true;document.getElementById("nofork").checked=true;
  document.getElementById("orgonly").checked=true;
  document.querySelectorAll("#tiers .pill").forEach(p=>p.classList.add("on"));
  render();
});
render();
</script>
</body>
</html>
"""


def report(conn: sqlite3.Connection) -> None:
    # pick highest tier per library across its source rows
    best: dict[str, tuple] = {}
    for lib, mat, prec, fp, stars, n, url in conn.execute(
        "SELECT library, maturity, precision, first_party, github_stars, "
        "COUNT(*) OVER (PARTITION BY library), evidence_url FROM hits"
    ):
        cur = best.get(lib)
        if cur is None or TIER_RANK[mat] > TIER_RANK[cur[0]]:
            best[lib] = (mat, prec, fp, stars, n, url)

    order = {"integrated": 0, "under_integration": 1, "proposed": 2}
    ranked = sorted(best.items(), key=lambda kv: (order[kv[1][0]], kv[0]))

    print(f"\n{'LIBRARY':40} {'TIER':18} {'PREC':5} {'1P':3} {'STARS':>7} EVIDENCE")
    print("-" * 100)
    for lib, (mat, prec, fp, stars, n, url) in ranked:
        s = str(stars) if stars is not None else "-"
        print(f"{lib[:40]:40} {mat:18} {prec:5} {'Y' if fp else 'N':3} {s:>7} {url}")
    print(f"\n{len(ranked)} libraries. "
          f"Confirm LOW-precision hits before trusting 'integrated'.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="cuvs.db")
    ap.add_argument("--report", action="store_true", help="print table and exit")
    ap.add_argument("--csv", nargs="?", const="cuvs_high_confidence", default=None,
                    metavar="PREFIX",
                    help="write priority CSVs (<PREFIX>.csv + <PREFIX>_strict.csv) "
                         "from the existing DB and exit")
    ap.add_argument("--enrich", action="store_true",
                    help="fetch stars/fork for the existing DB and exit")
    ap.add_argument("--dashboard", nargs="?", const="cuvs_dashboard.html",
                    default=None, metavar="PATH",
                    help="write an interactive HTML dashboard from the DB and exit")
    ap.add_argument("--no-stubs", action="store_true")
    args = ap.parse_args()

    conn = db_connect(args.db)
    if args.report:
        report(conn)
        return
    if args.enrich:
        enrich_repos(conn)
        return
    if args.csv is not None:
        enrich_repos(conn)          # fill any missing stars/fork first (idempotent)
        export_csvs(conn, args.csv)
        return
    if args.dashboard is not None:
        enrich_repos(conn)
        export_dashboard(conn, args.dashboard)
        return
    total = run(conn, include_stubs=not args.no_stubs)
    print(f"[done] {total} hits written to {args.db}")
    report(conn)
    export_csvs(conn)
    export_dashboard(conn)


if __name__ == "__main__":
    main()

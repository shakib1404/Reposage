"""
search.py — GitHub repo discovery
Serper + GitHub Search API + Jina Search for candidates · Groq for query
normalization and semantic ranking. No hardcoded topic/domain word lists —
query understanding and ranking are entirely LLM-driven so any phrasing of
any task is handled the same way.

Key design decisions:
  • _normalize_query() runs FIRST: the LLM collapses the raw task into one
    canonical technical phrase, so differently-worded requests for the same
    underlying task retrieve the same candidate pool instead of diverging
    based on phrasing luck. Direction ("json TO pdf" vs "pdf to json") is
    explicitly preserved by the normalization prompt.
  • Four search sources run in parallel on the normalized query: two Serper
    queries (exact phrase + broad), GitHub's own Search API, and Jina's
    Search API (semantic, good recall on obscure repos).
  • The LLM ranking prompt scores both direction and topic/domain match,
    penalising repos that do the REVERSE of what the user asked or that
    solve a different underlying problem, even if keywords overlap.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import math
import os
import re

import httpx

from llm import chat as llm_chat

log = logging.getLogger(__name__)

SERPER_KEY = os.getenv("SERPER_API_KEY", "")
JINA_KEY   = os.getenv("JINA_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
USE_MOCK   = os.getenv("USE_MOCK", "false").lower() == "true"

# How many merged candidates get enriched + ranked. GitHub's search alone
# can return 19 raw items (4 sub-queries) and is listed first in merge
# priority, so a cap of 15 was silently dropping every Jina/Serper-only
# candidate that GitHub didn't also find — including highly-starred, clearly
# relevant repos. Raised from 30 → 80 now that the UI shows 9 results instead
# of 3: picking the best 9 needs a materially deeper pool than picking 3, and
# the expensive per-candidate work (the runnability probe) no longer runs over
# the whole pool — only over the PROBE_TOP_N survivors of the cheap prerank.
MAX_CANDIDATES = 80

# How many results the UI shows.
RESULT_LIMIT = 9

# Only this many top-preranked candidates get the extra GitHub Contents API
# call that powers the runnability signal. Classic cascade: cheap scoring over
# everything, expensive scoring over the shortlist only. Set comfortably above
# RESULT_LIMIT so runnability can still reorder the final page meaningfully.
PROBE_TOP_N = 24

# Final blend. Relevance still dominates — a perfectly maintained repo that
# does the wrong thing is useless — but popularity alone no longer decides
# the tail, and runnability is now a first-class signal because RepoSage's
# whole point is EXECUTING the selected repo, not just reading it.
W_SEMANTIC    = 0.40
W_POPULARITY  = 0.32
W_RUNNABILITY = 0.28

# Cross-encoder scores are min-max normalized across the candidate set, so the
# best candidate always scores 10 no matter how bad it is in absolute terms.
# That's fine for ordering, but it means a hugely popular yet only-vaguely
# related repo can ride its star weight into the top 9. Anything scoring below
# this on relevance is treated as noise and dropped regardless of popularity.
MIN_SEMANTIC = 4.0

# Files whose presence at the repo root means "someone wrote down how to
# install this", which is by far the strongest cheap predictor of whether an
# automated clone-and-run will succeed.
DEP_FILES = {
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "environment.yml", "environment.yaml", "pipfile", "poetry.lock",
    "requirements-dev.txt", "conda.yaml",
}
# A plausible "just run this" entry point.
ENTRY_FILES = {
    "main.py", "app.py", "run.py", "cli.py", "manage.py",
    "server.py", "demo.py", "__main__.py",
}

# Authenticated GitHub API requests get 5000 req/hr instead of 60 — without
# this, a single search (multiple GitHub Search API queries + up to
# MAX_CANDIDATES star enrichment calls) can silently exhaust the
# unauthenticated quota and start returning degraded (0-star) or empty
# results.
def _github_headers() -> dict:
    headers = {
        "Accept":     "application/vnd.github.v3+json",
        "User-Agent": "RepoSage/1.0",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers


# ─────────────────────────────────────────────────────────────────────────────
#  Public entry point
# ─────────────────────────────────────────────────────────────────────────────

async def search_repos(task: str, limit: int = RESULT_LIMIT) -> dict:
    """
    Returns {"repos": [...], "suggestions": [...], "query": "<canonical>"}.

    (Historically this returned a bare list of 9 repos. It now returns the
    richer envelope so the UI can render the "refine your search" chips and
    show what the raw task was actually normalized to.)
    """
    if USE_MOCK:
        return {"repos": _mock_repos(task), "suggestions": [], "query": task}

    # Normalize the raw task into a canonical technical phrase FIRST.
    # Two differently-worded requests for the same underlying task
    # ("audio mood classification tool" vs "help me sort my music files by
    # mood automatically") must retrieve the same candidate pool — otherwise
    # search results depend on phrasing luck instead of intent. All four
    # search sources below query on this canonical phrase; the raw `task`
    # is still used later for LLM ranking so direction/nuance judged against
    # the user's literal wording.
    query = await _normalize_query(task)

    # Run four searches in parallel:
    #   1. Serper exact phrase — preserves direction/meaning
    #   2. Serper keyword     — broader candidate pool
    #   3. GitHub Search API  — finds exact repos by name/description
    #   4. Jina Search        — semantic web search; does its own query
    #                           understanding on top of the already-canonical
    #                           phrase, for extra recall on obscure repos.
    results = await asyncio.gather(
        _serper_search_query(f'"{query}" python github'),
        _serper_search_query(_keyword_query(query)),
        _github_search(query),
        _jina_search(query),
        return_exceptions=True,
    )
    raw1 = results[0] if not isinstance(results[0], Exception) else []
    raw2 = results[1] if not isinstance(results[1], Exception) else []
    raw3 = results[2] if not isinstance(results[2], Exception) else []
    raw4 = results[3] if not isinstance(results[3], Exception) else []
    for i, label in enumerate(("serper-exact", "serper-kw", "github", "jina")):
        if isinstance(results[i], Exception):
            log.warning("Search %s failed: %s", label, results[i])

    # Merge, deduplicate, keep insertion order.
    # GitHub API (exact name/description match) first, then Jina's semantic
    # read of the raw task, then the two Serper queries.
    # Dedup is case-INSENSITIVE: GitHub treats owner/repo case-insensitively
    # and the sources disagree on casing (Serper echoes whatever the page
    # linked, the API returns canonical casing), so "cjhutto/vaderSentiment"
    # and "cjhutto/vadersentiment" were surviving as two separate candidates
    # and could both render as cards in the results.
    seen: set[str] = set()
    raw_repos: list[dict] = []
    for r in raw3 + raw4 + raw1 + raw2:
        key = r["full_name"].lower()
        if key not in seen:
            seen.add(key)
            raw_repos.append(r)

    # The "refine your search" chips are independent of retrieval, so kick the
    # LLM call off now and collect it at the end — it overlaps with the
    # enrichment/probe round trips instead of adding to total latency.
    suggest_task = asyncio.create_task(_refine_suggestions(task, query))

    if not raw_repos:
        # All 3 live search APIs came back empty/failed — last resort is to
        # ask the LLM to recall known repos from training knowledge, still
        # keyed off the actual task (not a hardcoded topic-bucket guess).
        return {
            "repos":       await _llm_recall_repos(task),
            "suggestions": await suggest_task,
            "query":       query,
        }

    # Fetch real stars/forks/topics/avatars/push-dates for all candidates
    raw_repos = await _enrich_metadata(raw_repos)

    # Drop anything that cannot plausibly be the answer before spending
    # cross-encoder time or Contents API calls on it.
    raw_repos = [r for r in raw_repos if not _is_disqualified(r)]

    # Rank against the NORMALIZED query, not the raw task — retrieval already
    # used `query`, so ranking on the same text guarantees two differently
    # -worded requests for the same intent get identical results end-to-end,
    # instead of embedding similarity re-introducing phrasing sensitivity.
    ranked = await _rank_repos(raw_repos, query)

    return {
        "repos":       ranked[:limit],
        "suggestions": await suggest_task,
        "query":       query,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Query normalization — makes retrieval phrasing-independent
# ─────────────────────────────────────────────────────────────────────────────

_NORMALIZE_SYSTEM = (
    "You normalize vague or casually-phrased software requests into the "
    "standard, canonical technical term for the underlying task — the exact "
    "phrase a developer would use to name this field/technique, as it would "
    "appear in a GitHub repo description or paper title. Always lowercase. "
    "2-6 words. No filler, no adjectives like tool/automatically/app. "
    "Two different phrasings of the same underlying task MUST map to the "
    "same canonical term. If the task converts/transforms one format or "
    "thing INTO another, preserve the exact A-to-B direction — never swap it. "
    "ALWAYS keep named platforms, products, or technologies mentioned by name "
    "(Discord, Telegram, Spotify, AWS, React, etc.) — never drop or generalize "
    "them away, they are not filler.\n\n"
    "Examples:\n"
    '"help me sort my music files by mood automatically" -> "audio mood classification"\n'
    '"audio mood classification tool" -> "audio mood classification"\n'
    '"turn my old photos into color" -> "image colorization"\n'
    '"colorize black and white photos" -> "image colorization"\n'
    '"convert json to pdf" -> "json to pdf conversion"\n'
    '"i want to turn my json data into a pdf document" -> "json to pdf conversion"\n'
    '"a discord bot that plays music" -> "discord music bot"\n'
    '"i need something to post my tweets to slack" -> "twitter to slack integration"\n'
    "Return ONLY the canonical phrase."
)


async def _normalize_query(task: str) -> str:
    """Collapse any phrasing of the same intent to one canonical search phrase."""
    try:
        # max_tokens must stay generous even though the ANSWER is 2-6 words:
        # the configured Groq model (openai/gpt-oss-120b) is a reasoning model
        # that spends tokens on internal reasoning before emitting content, so
        # a tight budget returns an EMPTY string rather than a short answer.
        # At the old value of 20 this silently returned "" on every call and
        # fell through to `or task` — normalization looked wired up but had
        # been dead since the model migration, making retrieval phrasing-
        # sensitive again. Measured: 20 -> "", 60+ -> "sentiment analysis".
        result = await llm_chat(
            system=_NORMALIZE_SYSTEM, user=task,
            max_tokens=300, temperature=0.0,
        )
        cleaned = result.strip().strip('"').strip(".").lower()
        # A reasoning model that overruns its budget can also emit a partial
        # sentence; anything long is not a canonical phrase, so ignore it.
        if not cleaned or len(cleaned.split()) > 8:
            return task
        return cleaned
    except Exception as e:
        log.warning("Query normalization failed, using raw task: %s", e)
        return task


# ─────────────────────────────────────────────────────────────────────────────
#  Serper search
# ─────────────────────────────────────────────────────────────────────────────

GITHUB_RESERVED = {
    "topics", "collections", "marketplace", "search", "explore",
    "trending", "sponsors", "features", "about", "pricing", "login",
    "signup", "orgs", "pulls", "issues", "notifications", "settings",
    "new", "import", "organizations", "apps", "users",
}

async def _serper_search_query(query: str) -> list[dict]:
    """Run one Serper query and return only real GitHub repo dicts."""
    headers = {"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"}
    payload = {"q": query + " site:github.com", "num": 10}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://google.serper.dev/search",
                headers=headers, json=payload,
            )
            data = resp.json()
    except Exception:
        return []

    repos: list[dict] = []
    seen: set[str] = set()
    for item in data.get("organic", []):
        link = item.get("link", "")
        m = re.match(r"https://github\.com/([^/]+)/([^/?\s#]+)", link)
        if not m:
            continue
        owner = m.group(1).strip("/").lower()
        name  = m.group(2).strip("/")
        # Skip GitHub reserved namespace pages
        if owner in GITHUB_RESERVED:
            continue
        # Skip anything that looks like a sub-page (has extra path segments)
        path_after = link[m.end():]
        if path_after and not path_after.startswith("?") and path_after != "/":
            continue
        full_name = f"{m.group(1)}/{name}"
        if full_name in seen:
            continue
        seen.add(full_name)
        repos.append({
            "full_name": full_name,
            "name":      name,
            "owner":     m.group(1),
            "snippet":   item.get("snippet", ""),
            "title":     item.get("title", ""),
        })
    return repos


async def _jina_search(task: str) -> list[dict]:
    """
    Run the RAW task through Jina's Search API (s.jina.ai).
    Unlike Serper/GitHub, this does its own semantic query understanding —
    it's the source that keeps working when the user's phrasing is
    colloquial, vague, or doesn't reduce cleanly to keywords.
    """
    headers = {
        "Accept": "application/json",
        "X-Respond-With": "no-content",   # we only need title/url/description
    }
    if JINA_KEY:
        headers["Authorization"] = f"Bearer {JINA_KEY}"
    params = {"q": f"{task} github repository python"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get("https://s.jina.ai/", headers=headers, params=params)
            data = resp.json()
    except Exception as e:
        log.warning("Jina search failed: %s", e)
        return []

    repos: list[dict] = []
    seen: set[str] = set()
    for item in data.get("data") or []:
        link = item.get("url") or item.get("link") or ""
        m = re.match(r"https://github\.com/([^/]+)/([^/?\s#]+)", link)
        if not m:
            continue
        owner = m.group(1).strip("/").lower()
        name  = m.group(2).strip("/")
        if owner in GITHUB_RESERVED:
            continue
        path_after = link[m.end():]
        if path_after and not path_after.startswith("?") and path_after != "/":
            continue
        full_name = f"{m.group(1)}/{name}"
        if full_name in seen:
            continue
        seen.add(full_name)
        repos.append({
            "full_name": full_name,
            "name":      name,
            "owner":     m.group(1),
            "snippet":   item.get("description", ""),
            "title":     item.get("title", ""),
        })
    return repos


async def _github_search(query: str) -> list[dict]:
    """
    Search GitHub's own repository search API.
    `query` is the LLM-normalized canonical phrase — already short and
    keyword-dense, so no hardcoded verb/stopword stripping is needed here;
    we just vary how it's matched (phrase, tokens, hyphenated repo name).
    """
    tokens = re.findall(r"[a-zA-Z0-9]+", query.lower())
    hyphen_name = "-".join(tokens)   # e.g. "json-to-pdf-conversion"

    # Each entry is (query, params). Two distinct jobs are being done here:
    #  • the `sort=stars` variants pull the well-known heavyweights that a
    #    user would be annoyed NOT to see (TextBlob, vaderSentiment, ...) —
    #    these are what "the best ones" usually means in practice;
    #  • the relevance-sorted `in:name`/`in:topics` variants pull small,
    #    exact-match repos that stars would otherwise bury forever.
    # Filling 9 slots well needs both, so both are queried explicitly rather
    # than hoping one ordering happens to surface the other's winners.
    plain = " ".join(tokens)
    queries: list[tuple[str, dict]] = [
        (f'{query} language:python',
         {"sort": "stars", "order": "desc", "per_page": 20}),
        (f'"{query}" language:python',
         {"sort": "stars", "order": "desc", "per_page": 15}),
        (f'{plain} in:name,description language:python',
         {"per_page": 20}),
        (f'{hyphen_name} in:name',
         {"per_page": 15}),
        (f'{plain} in:readme language:python stars:>50',
         {"sort": "stars", "order": "desc", "per_page": 10}),
        (f'topic:{hyphen_name}',
         {"sort": "stars", "order": "desc", "per_page": 10}),
    ]

    repos: list[dict] = []
    seen:  set[str]   = set()
    headers = _github_headers()

    async def run_one(q: str, params: dict) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.github.com/search/repositories",
                    params={"q": q, **params},
                    headers=headers,
                )
            if resp.status_code != 200:
                return []
            return resp.json().get("items", [])
        except Exception:
            return []

    # Fire all query variants concurrently — they were previously sequential,
    # which made GitHub the slowest of the four sources by a wide margin.
    batches = await asyncio.gather(*[run_one(q, p) for q, p in queries])

    for items in batches:
        for item in items:
            fn = item.get("full_name", "")
            if not fn or fn in seen:
                continue
            seen.add(fn)
            repos.append(_from_github_item(item))
    return repos


def _from_github_item(item: dict) -> dict:
    """Normalize a GitHub API repo object into our internal candidate dict."""
    owner = item.get("owner") or {}
    return {
        "full_name":  item.get("full_name", ""),
        "name":       item.get("name", ""),
        "owner":      owner.get("login", ""),
        "avatar":     owner.get("avatar_url", ""),
        "snippet":    item.get("description") or "",
        "title":      item.get("name", ""),
        "stars_int":  item.get("stargazers_count", 0),
        "forks_int":  item.get("forks_count", 0),
        "language":   item.get("language") or "",
        "topics":     item.get("topics") or [],
        "pushed_at":  item.get("pushed_at") or "",
        "archived":   bool(item.get("archived")),
        "is_fork":    bool(item.get("fork")),
        "size_kb":    item.get("size", 0),
        "has_issues": bool(item.get("has_issues")),
    }


async def _enrich_metadata(repos: list[dict]) -> list[dict]:
    """
    Fill in full repo metadata (stars, forks, avatar, push date, archived flag,
    language, topics) for candidates that came from Serper/Jina — those arrive
    as nothing but a URL and a search snippet. Candidates that came from the
    GitHub Search API already carry everything and are passed through untouched.

    Runs concurrently, capped at MAX_CANDIDATES.
    """
    headers = _github_headers()

    async def fetch_one(repo: dict) -> dict:
        if repo.get("stars_int") is not None and repo.get("pushed_at"):
            return repo          # already fully populated by GitHub search
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo['full_name']}",
                    headers=headers,
                )
            if resp.status_code == 200:
                merged = _from_github_item(resp.json())
                # Keep the search snippet if the repo has no description of
                # its own — it's the only text we'd otherwise have to rank on.
                if not merged["snippet"]:
                    merged["snippet"] = repo.get("snippet", "")
                return merged
            # 404/451 (deleted, renamed, DMCA'd) — mark it so it gets dropped.
            repo["_dead"] = True
        except Exception:
            pass
        return repo

    enriched = await asyncio.gather(*[fetch_one(r) for r in repos[:MAX_CANDIDATES]])

    # Dedup AGAIN after enrichment: the API canonicalizes names and silently
    # follows renames, so two candidates that looked distinct pre-enrichment
    # can resolve to the same repo.
    seen: set[str] = set()
    out: list[dict] = []
    for r in enriched:
        if r.get("_dead"):
            continue
        key = r["full_name"].lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _is_disqualified(repo: dict) -> bool:
    """
    Hard filters — things that are never the right answer for "clone this and
    run it", no matter how well they match the query text.
    """
    lang   = (repo.get("language") or "").lower()
    topics = {t.lower() for t in (repo.get("topics") or [])}
    name   = (repo.get("name") or "").lower()

    if repo.get("archived"):
        return True
    # Non-Python repos are excluded outright: RepoSage's executor is a Python
    # runner, so a JS/Go/Rust repo cannot be executed downstream even when it
    # is the single most relevant result. Empty language is allowed through —
    # GitHub reports null for small/new repos that are often still Python, and
    # the runnability probe will confirm or deny it from the actual file list.
    if lang and lang not in ("python", "jupyter notebook"):
        return True
    # Awesome-lists / curated link collections match sentiment-analysis-style
    # queries extremely well on text and are completely unrunnable.
    if name.startswith("awesome") or "awesome-list" in topics:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  Runnability — "can RepoSage actually clone this and execute it?"
# ─────────────────────────────────────────────────────────────────────────────
#
# Relevance and stars say nothing about whether a repo will survive an
# automated clone-install-run. The single cheapest high-signal proxy is the
# root file listing: one Contents API call tells us whether dependencies are
# declared, whether there's an obvious entry point, and whether the repo is
# really just a pile of notebooks. This runs only over the PROBE_TOP_N
# shortlist, so the extra API cost stays bounded regardless of pool size.

async def _probe_runnability(repos: list[dict]) -> None:
    """Annotate each repo in-place with `_run_score` (0-10) and `_run_tags`."""
    headers = _github_headers()

    async def probe(repo: dict) -> None:
        names: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo['full_name']}/contents/",
                    headers=headers,
                )
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, list):
                    names = [(e.get("name") or "").lower() for e in payload]
        except Exception:
            pass
        repo["_run_score"], repo["_run_tags"] = _score_runnability(repo, names)

    await asyncio.gather(*[probe(r) for r in repos])


def _score_runnability(repo: dict, root_files: list[str]) -> tuple[float, list[str]]:
    """
    0-10 runnability estimate from the root file listing plus repo metadata.
    Returns (score, human-readable tags shown in the UI).
    """
    score = 4.0                       # neutral prior when we learn nothing
    tags: list[str] = []
    files = set(root_files)

    # ── Dependencies declared ────────────────────────────────────────────
    dep_hit = files & DEP_FILES
    if dep_hit:
        score += 2.5
        tags.append("deps")
    elif root_files:
        # We successfully listed the root and there is genuinely no manifest —
        # an automated install has nothing to go on.
        score -= 1.5

    # ── An obvious way to start it ───────────────────────────────────────
    if files & ENTRY_FILES:
        score += 1.5
        tags.append("entrypoint")
    if "dockerfile" in files:
        score += 0.5
        tags.append("docker")

    # ── Notebook-only repos ──────────────────────────────────────────────
    # These match ML/NLP queries beautifully and are miserable to run
    # headlessly: no entry point, cells assume a human, paths assume Colab.
    if (repo.get("language") or "").lower() == "jupyter notebook" and not dep_hit:
        score -= 2.0
        tags.append("notebook-only")

    # ── Documented ───────────────────────────────────────────────────────
    if any(f.startswith("readme") for f in files):
        score += 0.5
    else:
        score -= 0.5

    # ── Maintenance recency ──────────────────────────────────────────────
    days = _days_since_push(repo.get("pushed_at", ""))
    if days is not None:
        if   days <= 365:  score += 1.5; tags.append("active")
        elif days <= 730:  score += 0.5
        elif days >= 1825: score -= 1.5; tags.append("stale")   # 5y+ untouched
        elif days >= 1095: score -= 0.75

    # ── Forks of someone else's work ─────────────────────────────────────
    # Usually a student copy: same content, none of the maintenance.
    if repo.get("is_fork"):
        score -= 1.0

    # ── Empty / placeholder repos ────────────────────────────────────────
    if repo.get("size_kb", 0) < 20:
        score -= 1.5

    return max(0.0, min(10.0, score)), tags


def _days_since_push(pushed_at: str) -> float | None:
    if not pushed_at:
        return None
    try:
        dt = _dt.datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        return (_dt.datetime.now(_dt.timezone.utc) - dt).days
    except Exception:
        return None


def _fmt_stars(n: int) -> str:
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def _star_weight(stars: int) -> float:
    """
    Convert a raw star count to a 0-10 weight on a log scale.
      0 stars  → 0.0
      10 stars → 2.0
      100      → 4.0
      1 000    → 6.0
      10 000   → 8.0
      100 000+ → 10.0
    """
    import math
    if stars <= 0:
        return 0.0
    return min(10.0, math.log10(stars + 1) / math.log10(100_001) * 10)


def _keyword_query(query: str) -> str:
    """
    `query` is already the LLM-normalized canonical phrase (short,
    keyword-dense, domain already resolved) — no hardcoded stopword/domain
    lists needed here anymore, just append the language filter.
    """
    return f"{query} python"


# ─────────────────────────────────────────────────────────────────────────────
#  Embedding ranking — local sentence-transformers, no LLM call
# ─────────────────────────────────────────────────────────────────────────────
#
# Ranking is the heaviest LLM call in the pipeline (800 max_tokens vs. 20 for
# normalization), and it operates over a fixed, already-fetched candidate
# list — a task that local models handle well, without any external API
# call, cost, or rate limit. Only `_normalize_query()` still calls the LLM
# (needed so Serper/GitHub's literal keyword search retrieves the same
# candidates regardless of phrasing — something local ranking alone can't
# fix since it only ranks AFTER retrieval).
#
# Two-stage retrieve-then-rerank, the standard IR pattern — but with only
# MAX_CANDIDATES candidates total (not millions of documents), the "narrow the field
# with a cheap bi-encoder first" step isn't earning its keep: it was cutting
# the shortlist to the top 8 by cosine similarity, which pruned candidates
# like `sloria/TextBlob` (9.5k★) before the cross-encoder ever saw them —
# TextBlob's description covers several capabilities (POS tagging,
# translation, ...) alongside sentiment analysis, which dilutes its bi-
# encoder cosine similarity against narrower single-purpose repos, even
# though it's clearly the better answer once judged properly. Cross-encoding
# is cheap enough (~0.02s for 4 pairs) to just run over every candidate.
#   Stage 1 (recall):    bi-encoder cosine similarity — used only as the
#                         fallback score if the cross-encoder fails to load.
#   Stage 2 (precision):  cross-encoder reranks ALL candidates — encodes
#                         (query, repo) jointly, catching things cosine
#                         similarity blurs together (e.g. platform mismatch:
#                         Discord vs Telegram bots score nearly identical on
#                         cosine similarity alone, since the surrounding
#                         text is otherwise similar).

_EMBED_MODEL  = "all-MiniLM-L6-v2"                        # same model rag.py uses
_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

_embedder  = None
_reranker  = None

def _get_embedder():
    global _embedder
    if _embedder is None:
        import warnings
        warnings.filterwarnings("ignore")
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(_EMBED_MODEL)
    return _embedder


def _get_reranker():
    global _reranker
    if _reranker is None:
        import warnings
        warnings.filterwarnings("ignore")
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder(_RERANK_MODEL)
    return _reranker


def _repo_text(r: dict) -> str:
    """
    Text the cross-encoder judges the query against.

    Description leads, name trails. Leading with the name made the reranker
    reward literal name matches above everything else — and "Sentiment-Analysis"
    is exactly what every tutorial/student project is called, while the actually
    canonical libraries have distinctive brand names (TextBlob, vaderSentiment,
    spaCy) that match the query text poorly. Description-first measured
    substantially better: it moved TextBlob and vaderSentiment into the top
    results and pushed the 0-star name-twins out.
    """
    desc   = (r.get("snippet") or "").strip()
    topics = ", ".join(r.get("topics") or [])
    name   = r.get("name", "")
    if not desc:
        return f"{name} {topics}".strip()
    return f"{desc} {topics} ({name})".strip()


def _to_result(r: dict, sem_score: float) -> dict:
    stars_int = r.get("stars_int", 0)
    star_w    = _star_weight(stars_int)
    run_score = r.get("_run_score", 4.0)
    combined  = round(
        sem_score * W_SEMANTIC
        + star_w * W_POPULARITY
        + run_score * W_RUNNABILITY,
        2,
    )
    return {
        "full_name":    r["full_name"],
        "name":         r["name"],
        "owner":        r["owner"],
        "avatar":       r.get("avatar") or
                        f"https://github.com/{r.get('owner','')}.png?size=80",
        "stars":        _fmt_stars(stars_int),
        "stars_int":    stars_int,
        "forks":        _fmt_stars(r.get("forks_int", 0)),
        "forks_int":    r.get("forks_int", 0),
        "language":     r.get("language") or "Python",
        "topics":       (r.get("topics") or [])[:4],
        "description":  (r.get("snippet") or "")[:180],
        "icon":         "📦",
        "score":        combined,
        "runnable":     round(run_score, 1),
        "run_tags":     r.get("_run_tags", []),
        "pushed_at":    r.get("pushed_at", ""),
        "_sem_score":   round(sem_score, 2),
        "_star_weight": round(star_w, 2),
    }


def _embed_rank(repos: list[dict], task: str) -> list[tuple[dict, float]]:
    """
    Score every candidate for relevance to `task`, returning (repo, 0-10) pairs.

    The cross-encoder runs FIRST and the bi-encoder is only touched if it
    fails. Previously the bi-encoder pass always ran and was then thrown away
    whenever reranking succeeded (i.e. essentially always) — pure waste, and
    expensive waste: loading the bi-encoder costs ~11s of cold start and ~0.9s
    of encoding per search, for a score that never reached the output.
    """
    texts = [_repo_text(r) for r in repos]

    # ── Preferred: cross-encoder scores each (query, repo) pair jointly ──
    # Raw logits from ms-marco-MiniLM run well past +5/-5 for anything
    # clearly relevant/irrelevant, so a sigmoid squash would saturate most
    # of them to ~10 and destroy differentiation. Min-max normalize across
    # the candidate set instead — we only need correct RELATIVE order, not
    # a globally calibrated scale.
    try:
        reranker = _get_reranker()
        logits   = [float(x) for x in reranker.predict([(task, t) for t in texts])]
        lo, hi   = min(logits), max(logits)
        spread   = hi - lo
        return [
            (r, ((logit - lo) / spread * 10) if spread > 1e-6 else 5.0)
            for r, logit in zip(repos, logits)
        ]
    except Exception as e:
        log.warning("Cross-encoder rerank failed, using bi-encoder scores: %s", e)

    # ── Fallback: bi-encoder cosine similarity ───────────────────────────
    model     = _get_embedder()
    task_emb  = model.encode(task, normalize_embeddings=True)
    repo_embs = model.encode(texts, normalize_embeddings=True)
    return [
        (r, max(0.0, min(10.0, float(task_emb @ emb) * 10)))
        for r, emb in zip(repos, repo_embs)
    ]


def warm_models() -> None:
    """
    Preload the reranker so the first user search doesn't eat ~9s of model
    load. Called from the FastAPI startup hook; safe to fail (the model just
    loads lazily on first use instead).
    """
    try:
        _get_reranker().predict([("warmup", "warmup text")])
        log.info("Search reranker warmed up")
    except Exception as e:
        log.warning("Reranker warmup failed (will load lazily): %s", e)


async def _rank_repos(repos: list[dict], task: str) -> list[dict]:
    candidates = repos[:MAX_CANDIDATES]
    try:
        scored = await asyncio.to_thread(_embed_rank, candidates, task)

        # Cheap prerank (relevance + popularity only) decides who is worth
        # spending a Contents API call on. Runnability can then reorder the
        # shortlist, but never has to be guessed for the long tail.
        scored.sort(
            key=lambda p: -(p[1] * 0.7 + _star_weight(p[0].get("stars_int", 0)) * 0.3)
        )
        await _probe_runnability([r for r, _ in scored[:PROBE_TOP_N]])

        # Relevance floor — see MIN_SEMANTIC. Applied after the prerank so a
        # popular-but-unrelated repo can't buy its way in on stars alone. Kept
        # only if it would leave us something to show.
        relevant = [p for p in scored if p[1] >= MIN_SEMANTIC]
        if len(relevant) >= RESULT_LIMIT:
            scored = relevant

        result = [_to_result(r, s) for r, s in scored]
        result.sort(key=lambda x: -x["score"])
        return result
    except Exception as e:
        log.warning("Embedding ranking failed, falling back to star sort: %s", e)
        sorted_repos = sorted(candidates, key=lambda r: -r.get("stars_int", 0))
        # Same output shape as _to_result so the UI never has to special-case
        # the degraded path — only the score is synthetic.
        out = []
        for i, r in enumerate(sorted_repos[:RESULT_LIMIT]):
            item = _to_result(r, 7.0)
            item["score"] = round(7.0 - i * 0.3, 1)
            out.append(item)
        return out


_REFINE_SYSTEM = (
    "You suggest 3 alternative search queries that narrow or sharpen a "
    "developer's repository search. Each must be a DIFFERENT, more specific "
    "sub-area or sibling technique of the original — never a restatement of "
    "it, never broader than it. Title Case, 2-4 words each, no punctuation. "
    "Return ONLY a JSON array of 3 strings.\n\n"
    "Example — for \"sentiment analysis\": "
    '["Emotion Analysis", "Intent Classification", "Topic Modeling"]\n'
    "Example — for \"discord music bot\": "
    '["Voice Channel Bot", "Spotify Integration", "Audio Queue Manager"]'
)


async def _refine_suggestions(task: str, query: str) -> list[str]:
    """3 chips shown under the search bar; failure is non-fatal (returns [])."""
    try:
        raw = await llm_chat(
            system=_REFINE_SYSTEM,
            user=f"Original request: {task}\nCanonical topic: {query}",
            # Generous budget for the same reasoning-model reason as
            # _normalize_query — see the note there.
            max_tokens=400, temperature=0.4,
        )
        parsed = _parse_json(raw)
        if isinstance(parsed, dict):          # tolerate {"suggestions": [...]}
            parsed = next(
                (v for v in parsed.values() if isinstance(v, list)), []
            )
        out, seen = [], {query.lower(), task.lower()}
        for s in parsed if isinstance(parsed, list) else []:
            s = str(s).strip().strip('"')
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
        return out[:3]
    except Exception as e:
        log.warning("Refine-suggestion generation failed: %s", e)
        return []


async def _llm_recall_repos(task: str) -> list[dict]:
    """
    Last-resort fallback when Serper, GitHub Search, and Jina all return
    zero candidates (e.g. simultaneous outage/rate-limit). Asks the LLM to
    recall real, well-known GitHub repos for the task from training
    knowledge — keyed off the actual query, not a hardcoded topic table.
    Star counts here are the LLM's recollection, not live data, so they're
    approximate; the repo still gets re-verified (README fetch, clone) once
    selected downstream.
    """
    system = (
        "Name up to 3 REAL, well-known GitHub repositories (owner/repo, "
        "must actually exist) that best accomplish the given task. "
        "Return ONLY valid JSON: "
        '{"repos": [{"full_name":"owner/repo","name":"repo","owner":"owner",'
        '"stars":"12k","language":"Python","description":"...","icon":"🔧","score":8.5}]} '
        "Never invent a repo you are not confident exists."
    )
    try:
        raw = await llm_chat(system=system, user=task, max_tokens=500, temperature=0.2)
        parsed = _parse_json(raw)
        repos = parsed.get("repos", []) if isinstance(parsed, dict) else []
        return [r for r in repos if r.get("full_name")][:3]
    except Exception as e:
        log.warning("LLM repo recall failed: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  README fetch
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_readme(full_name: str) -> str:
    """
    Fetch the README via Jina Reader (r.jina.ai).
    Uses JINA_API_KEY when available for higher rate limits and longer content.
    Falls back to unauthenticated on error.
    """
    jina_key = os.getenv("JINA_API_KEY", "")
    url      = f"https://r.jina.ai/https://github.com/{full_name}"
    headers  = {"Accept": "text/plain"}
    if jina_key:
        headers["Authorization"] = f"Bearer {jina_key}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text[:8000]
            # If auth fails, retry without key
            if resp.status_code in (401, 403) and jina_key:
                resp2 = await client.get(
                    url, headers={"Accept": "text/plain"})
                return resp2.text[:8000]
            return resp.text[:8000]
    except Exception:
        return f"Repository: {full_name}"


# ─────────────────────────────────────────────────────────────────────────────
#  JSON parse helper
# ─────────────────────────────────────────────────────────────────────────────

def _parse_json(text: str):
    import json
    text = re.sub(r"```(?:json)?|```", "", text).strip()
    ib, ibr = text.find("{"), text.find("[")
    if ib != -1 and (ibr == -1 or ib < ibr):
        end = text.rfind("}")
        if end != -1:
            return json.loads(text[ib:end + 1])
    elif ibr != -1:
        end = text.rfind("]")
        if end != -1:
            return json.loads(text[ibr:end + 1])
    return json.loads(text)


# ─────────────────────────────────────────────────────────────────────────────
#  Mock fallback
# ─────────────────────────────────────────────────────────────────────────────

def _mock_repos(task: str) -> list[dict]:
    tl = task.lower()
    if any(w in tl for w in ["scratch", "photo", "restor", "old photo"]):
        return [
            {"full_name": "microsoft/Bringing-Old-Photos-Back-to-Life", "name": "Bringing-Old-Photos-Back-to-Life", "owner": "microsoft", "stars": "14.2k", "language": "Python", "description": "Restores old photos using deep learning — removes scratches and enhances faces.", "icon": "🖼️", "score": 9.4},
            {"full_name": "jantic/DeOldify",                            "name": "DeOldify",                        "owner": "jantic",    "stars": "19.1k", "language": "Python", "description": "Colorize and restore old images using NoGAN.",                                  "icon": "🎨", "score": 8.7},
        ]
    if any(w in tl for w in ["sentiment", "review", "nlp", "movie"]):
        return [
            {"full_name": "cardiffnlp/twitter-roberta-base-sentiment", "name": "twitter-roberta-sentiment", "owner": "cardiffnlp", "stars": "8.4k",  "language": "Python", "description": "RoBERTa sentiment analysis trained on 58M tweets.", "icon": "💬", "score": 9.2},
            {"full_name": "flairNLP/flair",                            "name": "flair",                     "owner": "flairNLP",  "stars": "13.7k", "language": "Python", "description": "NLP framework for sentiment, NER, classification.",   "icon": "🏷️", "score": 8.8},
        ]
    return [
        {"full_name": "pytorch/pytorch",             "name": "pytorch",      "owner": "pytorch",      "stars": "81k",  "language": "Python", "description": "Tensors and neural networks with GPU acceleration.", "icon": "🔥", "score": 9.0},
        {"full_name": "huggingface/transformers",    "name": "transformers", "owner": "huggingface",  "stars": "129k", "language": "Python", "description": "State-of-the-art ML models.",                        "icon": "🤗", "score": 8.7},
    ]

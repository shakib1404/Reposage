"""
search.py — GitHub repo discovery
Serper for search  ·  Groq for semantic ranking.

Key design decisions:
  • The RAW task phrase is used as the primary Serper query so directional
    meaning ("json TO pdf", not "pdf to json") is preserved exactly.
  • A secondary keyword query runs in parallel to widen the result set.
  • The LLM ranking prompt explicitly penalises repos that do the REVERSE
    of what the user asked, and rewards exact semantic match.
"""
from __future__ import annotations

import asyncio
import os
import re

import httpx

from llm import chat as llm_chat

SERPER_KEY = os.getenv("SERPER_API_KEY", "")
USE_MOCK   = os.getenv("USE_MOCK", "false").lower() == "true"


# ─────────────────────────────────────────────────────────────────────────────
#  Public entry point
# ─────────────────────────────────────────────────────────────────────────────

async def search_repos(task: str) -> list[dict]:
    if USE_MOCK:
        return _mock_repos(task)

    # Run three searches in parallel:
    #   1. Serper exact phrase — preserves direction/meaning
    #   2. Serper keyword     — broader candidate pool
    #   3. GitHub Search API  — finds exact repos by name/description
    results = await asyncio.gather(
        _serper_search_query(f'"{task}" python github'),
        _serper_search_query(_keyword_query(task)),
        _github_search(task),
        return_exceptions=True,
    )
    raw1 = results[0] if not isinstance(results[0], Exception) else []
    raw2 = results[1] if not isinstance(results[1], Exception) else []
    raw3 = results[2] if not isinstance(results[2], Exception) else []
    for i, label in enumerate(("serper-exact", "serper-kw", "github")):
        if isinstance(results[i], Exception):
            log.warning("Search %s failed: %s", label, results[i])

    # Merge, deduplicate, keep insertion order
    seen: set[str] = set()
    raw_repos: list[dict] = []
    for r in raw1 + raw3 + raw2:       # GitHub API results take priority
        if r["full_name"] not in seen:
            seen.add(r["full_name"])
            raw_repos.append(r)

    if not raw_repos:
        return _mock_repos(task)

    # Fetch real star counts for all candidates
    raw_repos = await _enrich_with_stars(raw_repos)

    ranked = await _rank_repos(raw_repos, task)
    return ranked[:3]


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


async def _github_search(task: str) -> list[dict]:
    """
    Search GitHub's own repository search API.
    Finds repos by name / description — much better for specific phrases.
    No auth needed; rate limit is 10 req/min unauthenticated.
    """
    # Three GitHub queries:
    # 1. Exact phrase in description/readme
    # 2. Keywords
    # 3. NOUNS ONLY in repo name — strips verbs/prepositions so
    #    "convert json to pdf" → "json pdf" which matches "json-to-pdf"
    kw = _keyword_query(task).replace(" python", "").replace(" machine-learning", "").strip()
    SKIP_VERBS = {"convert", "converting", "create", "make", "build", "use",
                  "using", "generate", "parse", "read", "write", "get", "to",
                  "from", "into", "a", "an", "the", "and", "or", "for", "with"}
    nouns = [t for t in re.findall(r"[a-zA-Z0-9]+", task.lower())
             if t not in SKIP_VERBS and len(t) > 1]
    hyphen_name = "-".join(nouns)   # e.g. "json-to-pdf", "pdf-to-json"
    queries = [
        f'"{task}" language:python',
        f'{kw} language:python',
        f'{" ".join(nouns)} in:name,description language:python',
        f'{hyphen_name} in:name',   # catches exact hyphenated repo names
    ]
    repos: list[dict] = []
    seen:  set[str]   = set()
    headers = {
        "Accept":     "application/vnd.github.v3+json",
        "User-Agent": "RepoSage/1.0",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        for q in queries:
            try:
                # Name searches: sort by relevance (not stars) to surface
                # small exact-match repos; other queries sort by stars
                if "in:name" in q:
                    params = {"q": q, "per_page": 15}
                else:
                    params = {"q": q, "sort": "stars", "order": "desc", "per_page": 5}
                resp = await client.get(
                    "https://api.github.com/search/repositories",
                    params=params,
                    headers=headers,
                )
                if resp.status_code != 200:
                    continue
                for item in resp.json().get("items", []):
                    fn = item.get("full_name", "")
                    if fn and fn not in seen:
                        seen.add(fn)
                        repos.append({
                            "full_name":   fn,
                            "name":        item.get("name", ""),
                            "owner":       item.get("owner", {}).get("login", ""),
                            "snippet":     item.get("description") or "",
                            "title":       item.get("name", ""),
                            "stars_int":   item.get("stargazers_count", 0),
                            "language":    item.get("language") or "Python",
                        })
            except Exception:
                continue
    return repos


async def _enrich_with_stars(repos: list[dict]) -> list[dict]:
    """
    Fetch real star counts from GitHub API for repos that don't already have one.
    Runs all requests concurrently (max 10 repos).
    """
    headers = {
        "Accept":     "application/vnd.github.v3+json",
        "User-Agent": "RepoSage/1.0",
    }
    async def fetch_one(repo: dict) -> dict:
        if repo.get("stars_int") is not None:
            return repo          # already have stars from GitHub search
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo['full_name']}",
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    repo["stars_int"]  = data.get("stargazers_count", 0)
                    repo["language"]   = data.get("language") or repo.get("language", "Python")
                    if not repo.get("snippet"):
                        repo["snippet"] = data.get("description") or ""
        except Exception:
            pass
        return repo

    enriched = await asyncio.gather(*[fetch_one(r) for r in repos[:10]])
    return list(enriched)


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


def _keyword_query(task: str) -> str:
    """
    Build a secondary search query from meaningful words only.
    Preserves conversion direction words (to, from, into, convert).
    Does NOT append 'deep learning' for non-ML tasks.
    """
    # Words that convey direction/action — keep them
    KEEP = {"to", "from", "into", "convert", "parse", "generate",
            "extract", "transform", "export", "import", "read", "write"}
    # Generic noise to drop
    DROP = {"a", "an", "the", "and", "or", "for", "with", "use",
            "using", "build", "create", "make", "app", "project",
            "i", "me", "my", "we", "please", "help"}

    tokens = re.findall(r"[a-zA-Z0-9_\-]+", task.lower())
    keywords = [t for t in tokens if t not in DROP or t in KEEP]

    # Detect ML/vision tasks to add domain hint
    ml_words = {"image", "photo", "detection", "classify", "nlp",
                "sentiment", "speech", "ocr", "train", "model"}
    is_ml = any(w in ml_words for w in keywords)

    suffix = "python machine-learning" if is_ml else "python"
    return " ".join(keywords[:6]) + " " + suffix


# ─────────────────────────────────────────────────────────────────────────────
#  LLM ranking — semantic match with direction enforcement
# ─────────────────────────────────────────────────────────────────────────────

async def _rank_repos(repos: list[dict], task: str) -> list[dict]:
    # Build a lookup so we can inject real stars after LLM ranking
    stars_map = {r["full_name"]: r.get("stars_int", 0) for r in repos}
    lang_map  = {r["full_name"]: r.get("language", "Python") for r in repos}

    listing = "\n".join(
        f"{i}. {r['full_name']} ★{_fmt_stars(r.get('stars_int', 0))}"
        f"\n   {r.get('snippet', '')[:150]}"
        for i, r in enumerate(repos[:10])
    )

    system = (
        "You are a precise GitHub repo ranker. "
        "Return ONLY valid JSON — no markdown, no explanation.\n"
        'Shape: {"repos": [{"full_name":"...","name":"...","owner":"...",'
        '"stars":"12k","language":"Python","description":"...","icon":"🔧","score":9.2}]}\n\n'
        "SCORING RULES (score = semantic relevance only, 0-10):\n"
        "1. Direction matters: 'json to pdf' means JSON→PDF. "
        "   A repo doing the reverse scores 0.\n"
        "2. Score 9-10: does exactly what the task asks.\n"
        "3. Score 5-8: related but not a perfect match.\n"
        "4. Score 0-2: does the opposite or is unrelated.\n"
        "5. Among repos with the same semantic score, prefer higher-starred ones.\n"
        "6. Never invent repos not in the list. Return all repos sorted best-first."
    )

    user = (
        f"TASK (direction matters): {task}\n\n"
        f"REPOS (with real star counts):\n{listing}\n\n"
        f"Score each repo 0-10 for semantic match. "
        f"Reverse-direction repos get 0. Higher stars break ties."
    )

    try:
        raw    = await llm_chat(system=system, user=user,
                                max_tokens=800, temperature=0.1)
        ranked = _parse_json(raw)
        if not (isinstance(ranked, dict) and isinstance(ranked.get("repos"), list)):
            raise ValueError("bad shape")

        result = []
        for r in ranked["repos"]:
            fn          = r.get("full_name", "")
            stars_int   = stars_map.get(fn, 0)
            sem_score   = float(r.get("score", 5))
            # Combined score: 60% semantic + 40% star weight
            combined    = round(sem_score * 0.6 + _star_weight(stars_int) * 0.4, 2)
            result.append({
                **r,
                "stars":        _fmt_stars(stars_int),
                "language":     r.get("language") or lang_map.get(fn, "Python"),
                "score":        combined,
                "_sem_score":   sem_score,
                "_star_weight": round(_star_weight(stars_int), 2),
            })

        # Re-sort by combined score descending
        result.sort(key=lambda x: -x["score"])
        return result

    except Exception:
        # Fallback: sort by stars, no LLM
        sorted_repos = sorted(repos[:10], key=lambda r: -r.get("stars_int", 0))
        return [
            {
                "full_name":   r["full_name"],
                "name":        r["name"],
                "owner":       r["owner"],
                "stars":       _fmt_stars(r.get("stars_int", 0)),
                "language":    r.get("language", "Python"),
                "description": r.get("snippet", "")[:150],
                "icon":        "📦",
                "score":       round(7.0 - i * 0.3, 1),
            }
            for i, r in enumerate(sorted_repos[:3])
        ]


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

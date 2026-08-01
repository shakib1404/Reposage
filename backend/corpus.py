"""corpus.py — corpus-based repo similarity search

Builds a single, growing FAISS index of function embeddings across many
repos ("the corpus"). Given a new repo, finds which corpus repo(s) it's
most similar to, function by function — the 3rd Copy Detector mode,
alongside cross-repo comparison and single-repo self-scan.

Reuses dupdetect.py's AST-based function extraction and the same embedding
model (all-MiniLM-L6-v2), so a function's representation is identical
whether it's being compared within one repo, or against the whole corpus.

Storage (mirrors rag.py's ~/.repomaster/rag/ layout):
  ~/.repomaster/corpus/index.faiss   — FAISS IndexFlatIP (cosine, vectors pre-normalized)
  ~/.repomaster/corpus/metadata.json — parallel list; metadata[i] describes the function at FAISS id i
  ~/.repomaster/corpus/repos.json    — set of repo_full_names already indexed (dedupe / status)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import AsyncGenerator, Optional

from dupdetect import SKIP_DIRS, _extract_functions, _get_embedder

log = logging.getLogger(__name__)

CORPUS_ROOT = Path.home() / ".repomaster" / "corpus"
INDEX_PATH  = CORPUS_ROOT / "index.faiss"
META_PATH   = CORPUS_ROOT / "metadata.json"
REPOS_PATH  = CORPUS_ROOT / "repos.json"

EMBED_DIM = 384   # all-MiniLM-L6-v2 output size
DEFAULT_MATCH_THRESHOLD = 0.75
DEFAULT_TOP_K = 5   # nearest corpus neighbors to consider per query function


# ─────────────────────────────────────────────────────────────────────────────
#  Repo identity
# ─────────────────────────────────────────────────────────────────────────────

def _repo_full_name(repo_ref: str) -> str:
    m = re.search(r"github\.com[:/]([^/]+/[^/.\s]+?)(?:\.git)?/?$", repo_ref.strip())
    return m.group(1) if m else repo_ref.strip()


# ─────────────────────────────────────────────────────────────────────────────
#  Index persistence
# ─────────────────────────────────────────────────────────────────────────────

def _load_index():
    import faiss
    CORPUS_ROOT.mkdir(parents=True, exist_ok=True)
    if INDEX_PATH.exists() and META_PATH.exists():
        index = faiss.read_index(str(INDEX_PATH))
        metadata = json.loads(META_PATH.read_text(encoding="utf-8"))
    else:
        index = faiss.IndexFlatIP(EMBED_DIM)
        metadata = []
    repos = set(json.loads(REPOS_PATH.read_text(encoding="utf-8"))) if REPOS_PATH.exists() else set()
    return index, metadata, repos


def _save_index(index, metadata: list, repos: set) -> None:
    import faiss
    CORPUS_ROOT.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(INDEX_PATH))
    META_PATH.write_text(json.dumps(metadata), encoding="utf-8")
    REPOS_PATH.write_text(json.dumps(sorted(repos)), encoding="utf-8")


def corpus_status() -> dict:
    _, metadata, repos = _load_index()
    return {"repo_count": len(repos), "function_count": len(metadata), "repos": sorted(repos)}


# ─────────────────────────────────────────────────────────────────────────────
#  Shared: clone + extract functions from a repo ref
# ─────────────────────────────────────────────────────────────────────────────

async def _clone_and_extract(repo_ref: str):
    """Yields status events, then returns (workspace_or_None, all_funcs) via a final tuple."""
    tmp_dir: Optional[str] = None
    if repo_ref.startswith(("http://", "https://", "git@")):
        tmp_dir = tempfile.mkdtemp(prefix="corpus_")
        yield {"type": "status", "message": f"Cloning {repo_ref}…"}
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", repo_ref, tmp_dir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            yield {"type": "error", "message": "git clone timed out"}
            yield ("__RESULT__", None, None)
            return
        if proc.returncode != 0:
            yield {"type": "error", "message": f"git clone failed: {err.decode(errors='ignore')[:300]}"}
            yield ("__RESULT__", None, None)
            return
        workspace = tmp_dir
    else:
        workspace = repo_ref
        if not os.path.isdir(workspace):
            yield {"type": "error", "message": f"Path not found: {workspace}"}
            yield ("__RESULT__", None, None)
            return

    yield {"type": "status", "message": "Scanning Python files…"}
    py_files = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.join(root, f))

    all_funcs = []
    for fp in py_files:
        try:
            src = Path(fp).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = os.path.relpath(fp, workspace)
        all_funcs += _extract_functions(rel, src)

    yield ("__RESULT__", tmp_dir, all_funcs)


# ─────────────────────────────────────────────────────────────────────────────
#  Add a repo to the corpus
# ─────────────────────────────────────────────────────────────────────────────

async def add_to_corpus(repo_ref: str) -> AsyncGenerator[dict, None]:
    """SSE-friendly async generator. Clones, extracts, embeds, and appends to the corpus index."""
    repo_name = _repo_full_name(repo_ref)
    index, metadata, repos = _load_index()

    if repo_name in repos:
        yield {"type": "done", "repo": repo_name, "functions_added": 0,
               "message": f"{repo_name} is already in the corpus"}
        return

    tmp_dir = None
    try:
        all_funcs = None
        async for ev in _clone_and_extract(repo_ref):
            if isinstance(ev, tuple) and ev[0] == "__RESULT__":
                tmp_dir, all_funcs = ev[1], ev[2]
                break
            yield ev

        if all_funcs is None:
            return   # error already yielded by _clone_and_extract
        if not all_funcs:
            yield {"type": "done", "repo": repo_name, "functions_added": 0,
                   "message": "No functions found to index"}
            return

        yield {"type": "status", "message": f"Embedding {len(all_funcs)} functions…"}
        model = _get_embedder()
        texts = [f.code for f in all_funcs]
        embs = await asyncio.to_thread(model.encode, texts, normalize_embeddings=True)

        import numpy as np
        index.add(np.asarray(embs, dtype="float32"))
        for f in all_funcs:
            metadata.append({
                "repo": repo_name, "file": f.file, "class": f.cls, "name": f.name,
                "code": f.code, "start_line": f.start_line, "end_line": f.end_line,
            })
        repos.add(repo_name)
        await asyncio.to_thread(_save_index, index, metadata, repos)

        yield {"type": "done", "repo": repo_name, "functions_added": len(all_funcs)}

    except Exception as e:
        log.exception("add_to_corpus failed")
        yield {"type": "error", "message": str(e)}
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Search the corpus
# ─────────────────────────────────────────────────────────────────────────────

async def search_corpus(
    repo_ref: str,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    top_k: int = DEFAULT_TOP_K,
) -> AsyncGenerator[dict, None]:
    """
    SSE-friendly async generator. Clones `repo_ref`, embeds its functions, and
    searches the existing corpus index for near-duplicate functions in OTHER
    repos. Does NOT add repo_ref to the corpus — call add_to_corpus separately.
    """
    query_repo = _repo_full_name(repo_ref)
    index, metadata, repos = _load_index()

    if index.ntotal == 0:
        yield {"type": "error", "message": "Corpus is empty — add repos to it first"}
        return

    tmp_dir = None
    try:
        all_funcs = None
        async for ev in _clone_and_extract(repo_ref):
            if isinstance(ev, tuple) and ev[0] == "__RESULT__":
                tmp_dir, all_funcs = ev[1], ev[2]
                break
            yield ev

        if all_funcs is None:
            return
        if not all_funcs:
            yield {"type": "done", "total_matches": 0, "repo_matches": []}
            return

        yield {"type": "info", "message": f"Found {len(all_funcs)} functions — searching corpus of "
                                           f"{len(repos)} repos / {index.ntotal} functions…",
               "total_functions": len(all_funcs), "corpus_repos": len(repos), "corpus_functions": index.ntotal}

        model = _get_embedder()
        texts = [f.code for f in all_funcs]
        embs = await asyncio.to_thread(model.encode, texts, normalize_embeddings=True)

        import numpy as np
        query_vecs = np.asarray(embs, dtype="float32")
        k = min(top_k, index.ntotal)
        sims, ids = await asyncio.to_thread(index.search, query_vecs, k)

        per_repo = defaultdict(list)   # repo_name -> list of similarity scores
        match_count = 0
        for qi, qf in enumerate(all_funcs):
            for sim, idx in zip(sims[qi], ids[qi]):
                if idx < 0 or sim < threshold:
                    continue
                m = metadata[idx]
                if m["repo"] == query_repo:
                    continue   # skip self-matches if this repo is itself already in the corpus
                match_count += 1
                per_repo[m["repo"]].append(float(sim))
                yield {
                    "type": "match",
                    "query_function": qf.name, "query_file": qf.file, "query_class": qf.cls,
                    "query_code": qf.code, "query_lines": [qf.start_line, qf.end_line],
                    "matched_repo": m["repo"], "matched_function": m["name"],
                    "matched_file": m["file"], "matched_class": m["class"],
                    "matched_code": m["code"], "matched_lines": [m["start_line"], m["end_line"]],
                    "similarity": round(float(sim), 4),
                }

        summary = sorted(
            (
                {
                    "repo": repo, "match_count": len(scores),
                    "avg_similarity": round(sum(scores) / len(scores), 4),
                    "max_similarity": round(max(scores), 4),
                }
                for repo, scores in per_repo.items()
            ),
            key=lambda r: (-r["match_count"], -r["max_similarity"]),
        )
        yield {"type": "summary", "repos": summary}
        yield {"type": "done", "total_matches": match_count, "repo_matches": summary}

    except Exception as e:
        log.exception("search_corpus failed")
        yield {"type": "error", "message": str(e)}
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

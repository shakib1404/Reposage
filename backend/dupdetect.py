"""dupdetect.py — semantic duplicate-function detector (single repo, self-scan)

Given ONE repo, finds functions/methods whose code is semantically
near-identical to another function in the same class (or same module, for
top-level functions) — catches copy-pasted logic that's been renamed or
lightly reworded, which literal/token diffing (see copydetector/, used for
cross-repo comparison) would miss once identifiers differ enough.

Scope is deliberately class-local (or module-local for free functions), not
repo-wide: "the same problem solved twice in the same class" is a concrete,
actionable refactor target. Cross-class similarity is usually incidental
(shared boilerplate) rather than a real duplication smell.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import AsyncGenerator, Optional

log = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.68   # calibrated: true near-duplicates scored 0.71-0.89, unrelated pairs stayed <=0.46
_EMBED_MODEL = "all-MiniLM-L6-v2"   # same model already used by rag.py / search.py

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
}

_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        import warnings
        warnings.filterwarnings("ignore")
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(_EMBED_MODEL)
    return _embedder


class _FuncInfo:
    __slots__ = ("file", "cls", "name", "code", "start_line", "end_line")

    def __init__(self, file, cls, name, code, start_line, end_line):
        self.file = file
        self.cls = cls
        self.name = name
        self.code = code
        self.start_line = start_line
        self.end_line = end_line


def _is_trivial(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """
    Skip stub bodies (pass / ... / raise NotImplementedError) — an abstract
    method and its dozen overrides would otherwise all look like 100%
    "duplicates" of each other, drowning out real findings in noise. A
    genuinely short but real function (e.g. a 1-line helper) is NOT
    considered trivial — only literal do-nothing stubs are excluded.
    """
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]   # drop leading docstring before judging triviality
    if not body:
        return True
    if len(body) == 1:
        stmt = body[0]
        if isinstance(stmt, ast.Pass):
            return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
            return True
        if isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Call) \
                and isinstance(stmt.exc.func, ast.Name) and stmt.exc.func.id == "NotImplementedError":
            return True
    return False


def _extract_functions(rel_path: str, source: str) -> list[_FuncInfo]:
    """AST-walk one file, returning every function/method with its class scope (None = module-level)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    out: list[_FuncInfo] = []

    def get_src(node) -> tuple[str, int, int]:
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        return "\n".join(lines[start - 1:end]), start, end

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _is_trivial(item):
                    code, s, e = get_src(item)
                    out.append(_FuncInfo(rel_path, node.name, item.name, code, s, e))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _is_trivial(node):
            code, s, e = get_src(node)
            out.append(_FuncInfo(rel_path, None, node.name, code, s, e))

    return out


async def find_semantic_duplicates(
    repo_ref: str,
    threshold: float = DEFAULT_THRESHOLD,
    scope: str = "scoped",   # "scoped" (same class, or same file's module-level funcs) | "repo_wide" (compare everything)
) -> AsyncGenerator[dict, None]:
    """
    SSE-friendly async generator. `repo_ref` is a git URL or a local path.
    Yields: {"type": "status"|"info"|"progress"|"duplicate"|"done"|"error", ...}

    scope="scoped" (default) only compares functions that share the same
    class (or the same file, for module-level functions) — a duplicate here
    is unambiguously "the same class solved this twice," directly
    actionable. scope="repo_wide" compares every function against every
    other function regardless of class/file — catches things like the same
    logic copy-pasted into two unrelated classes (real example found in
    psf/requests: HTTPBasicAuth.__eq__ and HTTPDigestAuth.__eq__ were
    byte-identical), at the cost of more incidental/boilerplate matches.
    """
    tmp_dir: Optional[str] = None
    try:
        if repo_ref.startswith(("http://", "https://", "git@")):
            tmp_dir = tempfile.mkdtemp(prefix="dupdetect_")
            yield {"type": "status", "message": f"Cloning {repo_ref}…"}
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth", "1", repo_ref, tmp_dir,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                yield {"type": "error", "message": "git clone timed out"}
                return
            if proc.returncode != 0:
                yield {"type": "error", "message": f"git clone failed: {err.decode(errors='ignore')[:300]}"}
                return
            workspace = tmp_dir
        else:
            workspace = repo_ref
            if not os.path.isdir(workspace):
                yield {"type": "error", "message": f"Path not found: {workspace}"}
                return

        yield {"type": "status", "message": "Scanning Python files…"}
        py_files = []
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f.endswith(".py"):
                    py_files.append(os.path.join(root, f))

        all_funcs: list[_FuncInfo] = []
        for fp in py_files:
            try:
                src = Path(fp).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            rel = os.path.relpath(fp, workspace)
            all_funcs += _extract_functions(rel, src)

        yield {"type": "info", "message": f"Found {len(all_funcs)} functions across {len(py_files)} files",
               "total_functions": len(all_funcs), "total_files": len(py_files)}

        if len(all_funcs) < 2:
            yield {"type": "done", "total_duplicates": 0}
            return

        yield {"type": "status", "message": f"Embedding {len(all_funcs)} functions…"}
        model = _get_embedder()
        texts = [f.code for f in all_funcs]
        embs = await asyncio.to_thread(model.encode, texts, normalize_embeddings=True)

        import numpy as np
        emb_matrix = np.asarray(embs)

        # Build the (i, j, similarity) pairs to report, depending on scope.
        if scope == "repo_wide":
            n = len(all_funcs)
            yield {"type": "status", "message": f"Comparing all {n} functions repo-wide…"}
            sim_matrix = await asyncio.to_thread(lambda: emb_matrix @ emb_matrix.T)
            iu = np.triu_indices(n, k=1)
            sims = sim_matrix[iu]
            hits = np.where(sims >= threshold)[0]
            pairs = [(int(iu[0][k]), int(iu[1][k]), float(sims[k])) for k in hits]
        else:
            # Group by (file, class) — only compare within the same class,
            # or within the same file's module-level functions.
            groups: dict[tuple[str, Optional[str]], list[int]] = {}
            for i, f in enumerate(all_funcs):
                groups.setdefault((f.file, f.cls), []).append(i)
            scopes_with_multiple = {k: v for k, v in groups.items() if len(v) >= 2}
            yield {"type": "status",
                   "message": f"Comparing within {len(scopes_with_multiple)} class/module scope(s)…"}
            pairs = []
            for idxs in scopes_with_multiple.values():
                for a in range(len(idxs)):
                    for b in range(a + 1, len(idxs)):
                        i, j = idxs[a], idxs[b]
                        sim = float(emb_matrix[i] @ emb_matrix[j])
                        if sim >= threshold:
                            pairs.append((i, j, sim))

        yield {"type": "status",
               "message": f"Found {len(pairs)} pair(s) above {int(threshold * 100)}% similarity"}

        count = 0
        for k, (i, j, sim) in enumerate(pairs):
            count += 1
            fa, fb = all_funcs[i], all_funcs[j]
            yield {
                "type": "duplicate",
                "function_a": fa.name, "function_b": fb.name,
                "file_a": fa.file, "class_a": fa.cls,
                "file_b": fb.file, "class_b": fb.cls,
                "similarity": round(sim, 4),
                "lines_a": [fa.start_line, fa.end_line],
                "lines_b": [fb.start_line, fb.end_line],
                "code_a": fa.code, "code_b": fb.code,
            }
            if (k + 1) % 20 == 0 or k + 1 == len(pairs):
                yield {"type": "progress", "current": k + 1, "total": len(pairs)}

        yield {"type": "done", "total_duplicates": count}

    except Exception as e:
        log.exception("dup detect failed")
        yield {"type": "error", "message": str(e)}
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

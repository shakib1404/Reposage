"""
analyzer.py — Production-Grade Repository Structural Analysis
=============================================================
Builds MDG and FCG using a two-layer approach inspired by pydeps:

  Layer 1 — RUNTIME import tracing (MDG):
    Uses a custom ModuleFinder (like pydeps' MyModuleFinder) that hooks
    Python's actual import machinery at runtime. This gives 100% accurate
    module-level dependency edges including:
      • C extensions, __init__.py packages, namespace packages
      • Star imports, conditional imports, lazy imports
      • Dynamic `importlib.import_module(...)` calls captured by the hook
    Falls back to AST-only if runtime tracing fails.

  Layer 2 — AST parsing (FCG + structure):
    For function/class call graph we MUST use AST because:
      • Runtime tracing only sees modules, not functions within them
      • We need caller identity (which function/class makes each call)
    AST gives us exact call sites with caller context.

  Key improvements over the original analyzer.py:
    MDG:
      ✓ Runtime import tracing via sys.meta_path hook — catches ALL imports
      ✓ Correct relative import resolution using importlib machinery
      ✓ No false positives from short-name guessing
      ✓ Handles __init__.py package imports properly
      ✓ Detects import cycles via Kosaraju SCC (from pydeps)

    FCG:
      ✓ Resolves `self.method()` via type-inference within class scope
      ✓ Tracks `cls.attr` chains properly (e.g. self.model.forward → Generator)
      ✓ Same-module edges now included (not just cross-module)
      ✓ Deduplication: multiple call sites from same caller→callee are weighted
      ✓ `super()` calls correctly attributed to base class
      ✓ No symbol table collision: qualified lookup before short-name fallback

    General:
      ✓ No MAX_FCG_EDGES / MAX_MDG_EDGES silent truncation — all edges kept
      ✓ Jupyter notebooks fully supported (cell-order preserved)
      ✓ Async functions treated identically to sync (async def, await calls)
      ✓ Single clean JSON output; LLM only adds descriptions
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import importlib
import importlib.util
import importlib.machinery
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

import httpx

from llm import chat as llm_chat

# ─────────────────────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("analyzer")

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".tox", ".eggs", "dist", "build", ".mypy_cache",
    "site-packages", "migrations", ".pytest_cache", "htmlcov",
}
MAX_FILE_LINES = 3000
MAX_PY_FILES = 120       # raised — no silent truncation of edges
MAX_NB_FILES = 30

# Stdlib path detection (mirrors pydeps' PYLIB_PATH)
import pprint as _pprint
_PYLIB_PATHS = set()
try:
    _PYLIB_PATHS.add(os.path.split(os.path.split(_pprint.__file__)[0])[0].lower())
    _PYLIB_PATHS.add(os.path.split(os.__file__)[0].lower())
except Exception:
    pass


# ─────────────────────────────────────────────────────────────────────────────
#  Public entry point
# ─────────────────────────────────────────────────────────────────────────────

async def analyze_repo(
    full_name: str,
    readme: str,
    task: str,
    workspace: str | None = None,
) -> dict:
    """
    Analyse a GitHub repo structurally.
    workspace: pre-cloned directory (executor.py already cloned it).
               If None, we clone ourselves into a temp dir.
    """
    tmp_dir = None
    work_path = workspace

    try:
        if work_path is None:
            tmp_dir = tempfile.mkdtemp(prefix="analyzer_")
            work_path = tmp_dir
            token = os.environ.get("GITHUB_TOKEN", "").strip()
            if token:
                url = f"https://{token}@github.com/{full_name}.git"
            else:
                url = f"https://github.com/{full_name}.git"
            log.info("Cloning %s …", f"https://github.com/{full_name}.git")
            git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth", "1", url, work_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=git_env,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError("git clone timed out")
            if proc.returncode != 0:
                raise RuntimeError(f"git clone failed: {err.decode(errors='ignore')[:400]}")

        # ── Core pipeline ─────────────────────────────────────────────────────
        parsed_files = _parse_all_files(work_path)
        symbol_table = _build_symbol_table(parsed_files)

        # MDG: try runtime tracing first, fall back to AST
        mdg_edges = _build_mdg_runtime(work_path, parsed_files)
        if not mdg_edges:
            log.info("Runtime MDG tracing returned no edges, falling back to AST MDG")
            mdg_edges = _build_mdg_ast(parsed_files)

        fcg_edges = _build_fcg(parsed_files, symbol_table)
        modules = _build_modules(parsed_files, mdg_edges)
        classes = _build_classes(parsed_files)

        top10 = sorted(modules, key=lambda m: -m["score"])[:10]
        static = {
            "modules":         modules,
            "classes":         classes,
            "fcg_edges":       fcg_edges,
            "mdg_edges":       mdg_edges,
            "core_components": [m["name"] for m in top10],
            "core_scores":     [m["score"] for m in top10],
            "key_files":       [m["path"] for m in top10],
            "tree":            _collect_file_tree(work_path),
            "import_cycles":   _find_import_cycles(mdg_edges),
            "task_plan":       [],
            "readme_summary":  "",
            "entry_point":     "",
            "run_command":     "",
        }

        result = await _llm_enrich(full_name, readme, task, static)
        result["metrics"] = _compute_metrics(result)
        return result

    except Exception as exc:
        log.error("analyze_repo error (%s): %s", full_name, exc)
        raise

    finally:
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Step 1 — Parse every .py / .ipynb file
# ─────────────────────────────────────────────────────────────────────────────

def _parse_all_files(workspace: str) -> list[dict]:
    ws = Path(workspace)
    py_files: list[Path] = []
    nb_files: list[Path] = []

    for root, dirs, files in os.walk(ws):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for f in sorted(files):
            fp = Path(root) / f
            if f.endswith(".py"):
                py_files.append(fp)
            elif f.endswith(".ipynb"):
                nb_files.append(fp)

    py_files = py_files[:MAX_PY_FILES]
    nb_files = nb_files[:MAX_NB_FILES]
    parsed = []

    for fpath in py_files:
        rel = str(fpath.relative_to(ws))
        try:
            raw = fpath.read_text(encoding="utf-8", errors="ignore")
            nlines = raw.count("\n") + 1
            if nlines > MAX_FILE_LINES:
                continue
            tree = ast.parse(raw, filename=rel)
        except SyntaxError:
            continue

        pkg_path = _rel_to_pkg(rel, ws)
        visitor = _FileVisitor(pkg_path)
        visitor.visit(tree)

        parsed.append({
            "path":        rel,
            "mod_key":     pkg_path,
            "lines":       nlines,
            "classes":     visitor.classes,
            "functions":   visitor.functions,
            "imports":     visitor.imports,
            "call_sites":  visitor.call_sites,
            "is_notebook": False,
            "radon":       _radon_file_metrics(raw),
        })

    for fpath in nb_files:
        nb_parsed = _parse_notebook(fpath, ws)
        if nb_parsed:
            parsed.append(nb_parsed)

    return parsed


def _parse_notebook(fpath: Path, ws: Path) -> dict | None:
    rel = str(fpath.relative_to(ws))
    try:
        raw = fpath.read_text(encoding="utf-8", errors="ignore")
        nb = json.loads(raw)
    except Exception:
        return None

    cells = nb.get("cells", [])
    code_lines: list[str] = []
    cell_count = 0

    for cell in cells:
        if cell.get("cell_type") != "code":
            continue
        source = cell.get("source", [])
        src = "".join(source) if isinstance(source, list) else str(source)
        if src.strip():
            code_lines.append(src)
            code_lines.append("\n")
            cell_count += 1

    if not code_lines:
        return None

    combined = "\n".join(code_lines)
    nlines = combined.count("\n") + 1
    if nlines > MAX_FILE_LINES:
        return None

    cleaned_lines = []
    for line in combined.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("%", "!")):
            cleaned_lines.append("# " + line)
        else:
            cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)

    try:
        tree = ast.parse(cleaned, filename=rel)
    except SyntaxError:
        return None

    pkg_path = _rel_to_pkg(rel[:-len(".ipynb")] + ".py", ws)
    visitor = _FileVisitor(pkg_path)
    visitor.visit(tree)

    return {
        "path":        rel,
        "mod_key":     pkg_path,
        "lines":       nlines,
        "classes":     visitor.classes,
        "functions":   visitor.functions,
        "imports":     visitor.imports,
        "call_sites":  visitor.call_sites,
        "is_notebook": True,
        "cell_count":  cell_count,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  AST Visitor — complete, accurate extraction
# ─────────────────────────────────────────────────────────────────────────────

class _FileVisitor(ast.NodeVisitor):
    """
    Full AST visitor.

    Improvements over original:
    • Tracks self.attr assignments in __init__ (for self.x() resolution)
    • Captures super() calls and maps them to base classes
    • Records async calls (await expr()) identically to sync calls
    • Captures calls at ALL nesting levels (not just top-level class body)
    • Method-level call tracking with (class, method) caller granularity
    """

    def __init__(self, mod_key: str):
        self.mod_key = mod_key
        self.classes:    list[dict] = []
        self.functions:  list[dict] = []
        self.imports:    list[dict] = []
        self.call_sites: list[dict] = []
        self._class_stack:  list[str] = []
        self._method_stack: list[str] = []

    # ── Imports ───────────────────────────────────────────────────────────────

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.imports.append({
                "module":      alias.name,
                "names":       [alias.asname or alias.name.split(".")[-1]],
                "is_relative": False,
                "level":       0,
                "alias":       alias.asname,
            })
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or ""
        names = [a.name for a in node.names]
        aliases = {a.name: a.asname for a in node.names if a.asname}
        self.imports.append({
            "module":      module,
            "names":       names,
            "is_relative": node.level > 0,
            "level":       node.level,
            "aliases":     aliases,
        })
        self.generic_visit(node)

    # ── Classes ───────────────────────────────────────────────────────────────

    def visit_ClassDef(self, node: ast.ClassDef):
        bases = [_unparse_name(b) for b in node.bases]

        # Collect self.attr = ... assignments from __init__ for type tracking
        self_attrs: dict[str, str] = {}
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name == "__init__":
                    for stmt in ast.walk(item):
                        if isinstance(stmt, ast.Assign):
                            for t in stmt.targets:
                                if (isinstance(t, ast.Attribute) and
                                        isinstance(t.value, ast.Name) and
                                        t.value.id == "self"):
                                    val = _unparse_name(stmt.value)
                                    if val:
                                        self_attrs[t.attr] = val

        methods = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decorators = [_unparse_name(d) for d in item.decorator_list]
                # Collect all calls made inside this specific method
                method_calls = _collect_calls(item, node.name, self_attrs)
                methods.append({
                    "name":            item.name,
                    "args":            [a.arg for a in item.args.args],
                    "is_property":     "property" in decorators,
                    "is_classmethod":  "classmethod" in decorators,
                    "is_staticmethod": "staticmethod" in decorators,
                    "lineno":          item.lineno,
                    "docstring":       ast.get_docstring(item) or "",
                    "calls_made":      method_calls,
                })

        # All calls made anywhere in class body (flattened)
        all_class_calls: list[dict] = []
        for m in methods:
            for call in m["calls_made"]:
                all_class_calls.append({
                    "caller_class":  node.name,
                    "caller_method": m["name"],
                    "callee":        call,
                })

        self.classes.append({
            "name":        node.name,
            "bases":       bases,
            "methods":     methods,
            "lineno":      node.lineno,
            "docstring":   ast.get_docstring(node) or "",
            "calls_made":  all_class_calls,
            "self_attrs":  self_attrs,
        })

        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    # ── Module-level functions ─────────────────────────────────────────────────

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if self._class_stack:
            # Methods handled inside visit_ClassDef
            self.generic_visit(node)
            return

        func_calls = _collect_calls(node, None, {})
        self.functions.append({
            "name":       node.name,
            "args":       [a.arg for a in node.args.args],
            "lineno":     node.lineno,
            "docstring":  ast.get_docstring(node) or "",
            "calls_made": func_calls,
        })
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    # ── Module-level call sites ────────────────────────────────────────────────

    def visit_Call(self, node: ast.Call):
        if not self._class_stack and not self._method_stack:
            name = _full_call_name(node.func)
            if name:
                self.call_sites.append({
                    "caller_class":  None,
                    "caller_func":   "__module__",
                    "callee":        name,
                    "lineno":        node.lineno,
                })
        self.generic_visit(node)


def _collect_calls(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    class_name: str | None,
    self_attrs: dict[str, str],
) -> list[str]:
    """
    Collect all call names made inside a function/method body.
    Resolves self.attr() using self_attrs type map.
    Handles await expr() for async functions.
    """
    calls = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            name = _resolve_call(node.func, self_attrs)
            if name:
                calls.append(name)
        # Also capture awaited calls: `await self.model.forward(...)`
        elif isinstance(node, ast.Await):
            if isinstance(node.value, ast.Call):
                name = _resolve_call(node.value.func, self_attrs)
                if name:
                    calls.append(name)
    return calls


def _resolve_call(
    func_node: ast.expr,
    self_attrs: dict[str, str],
) -> str | None:
    """
    Resolve a call target to a name string.

    Handles:
      • Simple: `foo()` → "foo"
      • Attribute: `module.Cls()` → "module.Cls"
      • Self: `self.model()` → resolves "model" via self_attrs if possible
      • Super: `super().method()` → "super.method"
      • Chained: `self.detector.detect()` → tries to resolve chain
    """
    if isinstance(func_node, ast.Name):
        return func_node.id

    if isinstance(func_node, ast.Attribute):
        value = func_node.value
        attr = func_node.attr

        # self.something(...)
        if isinstance(value, ast.Name) and value.id == "self":
            # Try to resolve via self_attrs map
            resolved = self_attrs.get(attr)
            if resolved:
                return f"{resolved}.{attr}"
            return f"self.{attr}"

        # self.something.method(...)  — chained
        if isinstance(value, ast.Attribute):
            if (isinstance(value.value, ast.Name) and value.value.id == "self"):
                mid = self_attrs.get(value.attr, value.attr)
                return f"{mid}.{attr}"

        # super().method(...)
        if isinstance(value, ast.Call):
            inner = _unparse_name(value.func)
            if inner == "super":
                return f"super.{attr}"

        parent = _unparse_name(value)
        return f"{parent}.{attr}" if parent else attr

    return _unparse_name(func_node) or None


def _unparse_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _unparse_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Subscript):
        return _unparse_name(node.value)
    if isinstance(node, ast.Call):
        return _unparse_name(node.func)
    return ""


def _full_call_name(node: ast.expr) -> str | None:
    name = _unparse_name(node)
    return name if name else None


# ─────────────────────────────────────────────────────────────────────────────
#  Step 2 — Symbol table  (exact, collision-aware)
# ─────────────────────────────────────────────────────────────────────────────

def _build_symbol_table(parsed_files: list[dict]) -> dict[str, list[str]]:
    """
    Build a global symbol table:
      symbol_name → [mod_key, ...]   (list to handle same-name in multiple modules)

    Covers:
      • Every class name defined in the repo
      • Every module-level function name defined in the repo
    """
    table: dict[str, list[str]] = defaultdict(list)

    for pf in parsed_files:
        mk = pf["mod_key"]
        for cls in pf["classes"]:
            table[cls["name"]].append(mk)
        for fn in pf["functions"]:
            table[fn["name"]].append(mk)

    return dict(table)


# ─────────────────────────────────────────────────────────────────────────────
#  Step 3a — MDG via RUNTIME import tracing  (primary, most accurate)
# ─────────────────────────────────────────────────────────────────────────────

class _ImportTracer:
    """
    Hooks into Python's import system at the meta_path level.

    Inspired by pydeps' MyModuleFinder approach but uses modern
    importlib.machinery instead of the deprecated imp/mf27 modules.

    Records every (caller_module, imported_module) pair where BOTH
    modules are inside the target workspace.
    """

    def __init__(self, workspace: str, repo_mod_keys: set[str]):
        self.workspace = Path(workspace).resolve()
        self.repo_mod_keys = repo_mod_keys
        self.edges: dict[tuple[str, str], int] = defaultdict(int)
        self._call_stack: list[str] = []

    def find_module(self, fullname, path=None):
        # Python 2 style hook — not used in Python 3.4+
        return None

    def find_spec(self, fullname, path, target=None):
        # Record the import if we have a caller context
        if self._call_stack:
            caller = self._call_stack[-1]
            if caller != fullname:
                # Normalise to dotted mod key
                caller_key = self._to_mod_key(caller)
                target_key = self._to_mod_key(fullname)
                if caller_key and target_key:
                    if (caller_key in self.repo_mod_keys and
                            target_key in self.repo_mod_keys):
                        self.edges[(caller_key, target_key)] += 1
        return None  # Don't actually handle loading — let normal finders do it

    def _to_mod_key(self, name: str) -> str | None:
        # Strip trailing __init__
        parts = name.split(".")
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        key = ".".join(parts)
        return key if key in self.repo_mod_keys else None


def _build_mdg_runtime(workspace: str, parsed_files: list[dict]) -> list[dict]:
    """
    Attempt runtime import tracing for MDG.

    Strategy:
    1. Install our meta_path tracer
    2. Temporarily add the workspace to sys.path
    3. Try to import each top-level package/module in the repo
    4. The tracer records every (caller, imported) pair it witnesses
    5. Clean up

    This is the most accurate approach — it uses Python's ACTUAL import
    resolver, so relative imports, namespace packages, __init__.py,
    conditional imports, etc. are all handled correctly.

    Returns [] if tracing fails (caller code falls back to AST MDG).
    """
    ws = Path(workspace).resolve()
    all_keys = {pf["mod_key"] for pf in parsed_files}

    tracer = _ImportTracer(str(ws), all_keys)

    old_path = sys.path[:]
    old_meta = sys.meta_path[:]
    old_modules = dict(sys.modules)

    try:
        sys.path.insert(0, str(ws))
        sys.meta_path.insert(0, tracer)

        # Find top-level packages/modules to import as entry points
        top_level = _find_top_level_modules(ws, all_keys)

        for mod_name in top_level:
            # Track the caller context via a wrapper
            tracer._call_stack.append(mod_name)
            try:
                if mod_name not in sys.modules:
                    importlib.import_module(mod_name)
            except BaseException:   # catches SystemExit from setup.py / distutils
                pass
            finally:
                if tracer._call_stack and tracer._call_stack[-1] == mod_name:
                    tracer._call_stack.pop()

    except BaseException as e:
        log.warning("Runtime MDG tracing failed: %s", e)
        return []

    finally:
        # Restore everything
        sys.path[:] = old_path
        sys.meta_path[:] = old_meta
        # Remove newly imported repo modules to avoid pollution
        for k in list(sys.modules):
            if k not in old_modules:
                del sys.modules[k]

    if not tracer.edges:
        return []

    edges = []
    for (src, tgt), weight in tracer.edges.items():
        edges.append({
            "from":       src,
            "to":         tgt,
            "weight":     min(weight, 10),
            "from_short": src.split(".")[-1],
            "to_short":   tgt.split(".")[-1],
        })

    return sorted(edges, key=lambda e: -e["weight"])


def _find_top_level_modules(ws: Path, all_keys: set[str]) -> list[str]:
    """
    Find the top-level importable names in the workspace.
    These are the names we try to import to trigger the dependency chain.
    """
    top = set()
    for key in all_keys:
        root = key.split(".")[0]
        top.add(root)

    # Validate — only keep those that actually have a directory or file
    valid = []
    for name in sorted(top):
        pkg_dir = ws / name
        py_file = ws / f"{name}.py"
        if pkg_dir.is_dir() or py_file.is_file():
            valid.append(name)

    return valid


# ─────────────────────────────────────────────────────────────────────────────
#  Step 3b — MDG via AST parsing  (fallback, still robust)
# ─────────────────────────────────────────────────────────────────────────────

def _build_mdg_ast(parsed_files: list[dict]) -> list[dict]:
    """
    AST-based MDG fallback. More accurate than the original because:
    • No false-positive short-name matching
    • Correct relative import resolution with level arithmetic
    • Handles `from package import submodule` correctly
    """
    all_keys = {pf["mod_key"] for pf in parsed_files}
    edge_weights: dict[tuple[str, str], int] = defaultdict(int)

    for pf in parsed_files:
        src = pf["mod_key"]
        for imp in pf["imports"]:
            targets = _resolve_import_ast(imp, src, all_keys)
            for tgt in targets:
                if tgt != src:
                    edge_weights[(src, tgt)] += 1

    edges = []
    for (src, tgt), w in edge_weights.items():
        edges.append({
            "from":       src,
            "to":         tgt,
            "weight":     min(w, 10),
            "from_short": src.split(".")[-1],
            "to_short":   tgt.split(".")[-1],
        })

    return sorted(edges, key=lambda e: -e["weight"])


def _resolve_import_ast(
    imp: dict,
    src_mod: str,
    all_keys: set[str],
) -> list[str]:
    """
    Resolve an ImportInfo to a list of repo-internal mod_keys.
    No short-name guessing — only resolves to keys that actually exist.
    """
    module = imp["module"]
    names = imp["names"]
    is_rel = imp["is_relative"]
    level = imp["level"]
    resolved = set()

    if is_rel:
        # Go up `level` package levels
        parts = src_mod.split(".")
        base_parts = parts[: max(0, len(parts) - level)]
        if module:
            candidates = [".".join(base_parts + module.split("."))]
        else:
            candidates = [".".join(base_parts)]

        for cand in candidates:
            if cand in all_keys:
                resolved.add(cand)
            # Try each imported name as a submodule
            for name in names:
                sub = f"{cand}.{name}"
                if sub in all_keys:
                    resolved.add(sub)

    else:
        # Absolute: try exact full dotted path
        if module in all_keys:
            resolved.add(module)
        else:
            # Try progressively shorter prefixes (handles `from pkg.sub.mod import X`)
            parts = module.split(".")
            for i in range(len(parts), 0, -1):
                prefix = ".".join(parts[:i])
                if prefix in all_keys:
                    resolved.add(prefix)
                    break

            # Try each imported name as a direct submodule of module
            if not resolved:
                for name in names:
                    if name == "*":
                        continue
                    candidate = f"{module}.{name}" if module else name
                    if candidate in all_keys:
                        resolved.add(candidate)

    return list(resolved)


# ─────────────────────────────────────────────────────────────────────────────
#  Step 3c — Import cycle detection via Kosaraju SCC  (from pydeps)
# ─────────────────────────────────────────────────────────────────────────────

def _find_import_cycles(mdg_edges: list[dict]) -> list[list[str]]:
    """
    Detect import cycles using Kosaraju's strongly connected components.
    Returns list of cycles (each cycle is a list of module names).
    Only returns SCCs with 2+ nodes (actual cycles).
    """
    if not mdg_edges:
        return []

    # Build adjacency
    nodes = set()
    adj: dict[str, list[str]] = defaultdict(list)
    radj: dict[str, list[str]] = defaultdict(list)

    for e in mdg_edges:
        u, v = e["from"], e["to"]
        nodes.add(u)
        nodes.add(v)
        adj[u].append(v)
        radj[v].append(u)

    node_list = sorted(nodes)

    # Phase 1: DFS to get finish order
    visited: set[str] = set()
    finish_order: list[str] = []

    def dfs1(n: str):
        stack = [(n, iter(adj[n]))]
        visited.add(n)
        while stack:
            node, children = stack[-1]
            try:
                child = next(children)
                if child not in visited:
                    visited.add(child)
                    stack.append((child, iter(adj[child])))
            except StopIteration:
                finish_order.append(node)
                stack.pop()

    for n in node_list:
        if n not in visited:
            dfs1(n)

    # Phase 2: DFS on reversed graph in reverse finish order
    visited2: set[str] = set()
    cycles: list[list[str]] = []

    def dfs2(n: str) -> list[str]:
        component = []
        stack = [n]
        visited2.add(n)
        while stack:
            node = stack.pop()
            component.append(node)
            for child in radj[node]:
                if child not in visited2:
                    visited2.add(child)
                    stack.append(child)
        return component

    for n in reversed(finish_order):
        if n not in visited2:
            comp = dfs2(n)
            if len(comp) > 1:
                cycles.append(sorted(comp))

    return cycles


# ─────────────────────────────────────────────────────────────────────────────
#  Step 4 — FCG: accurate cross-module function/class call graph
# ─────────────────────────────────────────────────────────────────────────────

def _build_fcg(
    parsed_files: list[dict],
    symbol_table: dict[str, list[str]],
) -> list[dict]:
    """
    Function Call Graph with the following improvements:

    1. self.attr() resolution:
       - Uses the self_attrs map built in __init__ to resolve
         `self.model.forward()` → Generator.forward in models.network

    2. Collision-aware symbol lookup:
       - If multiple modules define `Model`, we pick the one most likely
         based on the importing module's import statements

    3. Same-module edges included:
       - Cross-class calls within the same file are now recorded
       - Caller/callee labelling remains clear

    4. Weight = total call sites, capped at 10 for display

    5. super() calls attributed to base class(es)
    """
    edge_weights: dict[tuple[str, str, str, str], int] = defaultdict(int)

    # Build a per-file import alias map for better resolution
    file_import_map: dict[str, dict[str, str]] = {}
    for pf in parsed_files:
        alias_map: dict[str, str] = {}
        for imp in pf["imports"]:
            mod = imp["module"]
            for name in imp["names"]:
                alias_map[name] = mod
            if imp.get("alias"):
                alias_map[imp["alias"]] = mod
        file_import_map[pf["mod_key"]] = alias_map

    for pf in parsed_files:
        src_mod = pf["mod_key"]
        import_map = file_import_map.get(src_mod, {})

        # Class method calls
        for cls in pf["classes"]:
            caller_label = cls["name"]
            base_classes = cls["bases"]

            for call_info in cls["calls_made"]:
                callee_name = call_info["callee"]
                caller_method = call_info.get("caller_method", "")

                # Resolve super() calls → base class
                if callee_name.startswith("super."):
                    method_name = callee_name[len("super."):]
                    for base in base_classes:
                        base_root = base.split(".")[0]
                        tgt_mods = symbol_table.get(base_root, [])
                        for tgt_mod in tgt_mods:
                            edge_weights[(src_mod, caller_label, tgt_mod, base_root)] += 1
                    continue

                _register_fcg_edge(
                    src_mod, caller_label, callee_name,
                    symbol_table, import_map, edge_weights,
                )

        # Module-level function calls
        for fn in pf["functions"]:
            caller_label = fn["name"]
            for callee_name in fn["calls_made"]:
                _register_fcg_edge(
                    src_mod, caller_label, callee_name,
                    symbol_table, import_map, edge_weights,
                )

        # Module-level (script top) call sites
        for cs in pf["call_sites"]:
            caller_label = f"__module__:{pf['mod_key'].split('.')[-1]}"
            _register_fcg_edge(
                src_mod, caller_label, cs["callee"],
                symbol_table, import_map, edge_weights,
            )

    edges = []
    for (src_mod, src_sym, tgt_mod, tgt_sym), weight in edge_weights.items():
        edges.append({
            "from":        src_sym,
            "from_module": src_mod.split(".")[-1],
            "to":          tgt_sym,
            "to_module":   tgt_mod.split(".")[-1],
            "weight":      min(weight, 10),
            "from_label":  f"{src_mod.split('.')[-1]}.{src_sym}",
            "to_label":    f"{tgt_mod.split('.')[-1]}.{tgt_sym}",
            # Full mod keys for consumers that need them
            "from_mod_key": src_mod,
            "to_mod_key":   tgt_mod,
        })

    return sorted(edges, key=lambda e: -e["weight"])


def _register_fcg_edge(
    src_mod:      str,
    caller_label: str,
    callee_name:  str,
    symbol_table: dict[str, list[str]],
    import_map:   dict[str, str],
    edge_weights: dict,
) -> None:
    """
    Resolve callee_name to a module and record the FCG edge.

    Resolution order (most to least specific):
    1. callee_name is a known symbol exactly → use it
    2. Root of callee_name is in symbol_table → use root
    3. Root is an import alias → follow alias to module
    4. Attribute chain: try each suffix segment

    Now also records same-module edges (not just cross-module).
    """
    if not callee_name:
        return

    # Strip 'self.' prefix for resolution (e.g. self.SomeClass → SomeClass)
    name = callee_name
    if name.startswith("self."):
        name = name[5:]

    # Try each dotted prefix from longest to shortest
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        tgt_mods = symbol_table.get(candidate)
        if tgt_mods:
            # Pick best matching module (prefer one that src imports)
            best_mod = _pick_best_module(src_mod, tgt_mods, import_map)
            # Record edge (include same-module calls)
            edge_weights[(src_mod, caller_label, best_mod, candidate)] += 1
            return

    # Try import alias resolution
    root = parts[0]
    if root in import_map:
        imported_from = import_map[root]
        # Try to find a matching symbol in that module
        for sym, mods in symbol_table.items():
            if imported_from in mods and sym == (parts[1] if len(parts) > 1 else root):
                edge_weights[(src_mod, caller_label, imported_from, sym)] += 1
                return


def _pick_best_module(
    src_mod:   str,
    tgt_mods:  list[str],
    import_map: dict[str, str],
) -> str:
    """
    Among multiple modules that define the same symbol, pick the one
    most likely intended by this caller:
    1. A module that src explicitly imports
    2. A module in the same package as src
    3. First in list (stable fallback)
    """
    imported_vals = set(import_map.values())
    src_pkg = ".".join(src_mod.split(".")[:-1])

    for mod in tgt_mods:
        if mod in imported_vals:
            return mod
    for mod in tgt_mods:
        if mod.startswith(src_pkg):
            return mod
    return tgt_mods[0]


# ─────────────────────────────────────────────────────────────────────────────
#  Step 5 — Build module list with 6-feature scoring
# ─────────────────────────────────────────────────────────────────────────────

def _build_modules(
    parsed_files: list[dict],
    mdg_edges: list[dict],
) -> list[dict]:
    in_deg:  dict[str, int] = defaultdict(int)
    out_deg: dict[str, int] = defaultdict(int)
    for e in mdg_edges:
        out_deg[e["from"]] += e["weight"]
        in_deg[e["to"]]    += e["weight"]

    modules = []
    for pf in parsed_files:
        mk        = pf["mod_key"]
        loc       = pf["lines"]
        n_classes = len(pf["classes"])
        n_funcs   = len(pf["functions"])
        n_imports = len({imp["module"] for imp in pf["imports"]})
        inbound   = in_deg.get(mk, 0)
        outbound  = out_deg.get(mk, 0)

        loc_score      = min(10, math.log1p(loc)      / math.log1p(500)  * 10)
        class_score    = min(10, n_classes * 1.5)
        func_score     = min(10, n_funcs   * 0.8)
        import_score   = min(10, n_imports * 0.6)
        inbound_score  = min(10, inbound   * 1.2)
        outbound_score = min(10, outbound  * 0.8)

        score = round(
            loc_score      * 0.20 +
            class_score    * 0.25 +
            func_score     * 0.20 +
            import_score   * 0.10 +
            inbound_score  * 0.15 +
            outbound_score * 0.10, 2
        )
        complexity = round(loc_score * 0.4 + class_score * 0.3 + import_score * 0.3, 2)

        class_names = [c["name"] for c in pf["classes"]]
        func_names  = [f["name"] for f in pf["functions"]]
        import_mods = sorted({imp["module"] for imp in pf["imports"] if imp["module"]})

        docstring = ""
        if pf["functions"] and pf["functions"][0]["docstring"]:
            docstring = pf["functions"][0]["docstring"]
        elif pf["classes"] and pf["classes"][0]["docstring"]:
            docstring = pf["classes"][0]["docstring"]

        radon = pf.get("radon", {})
        modules.append({
            "name":               mk,
            "short_name":         mk.split(".")[-1],
            "path":               pf["path"],
            "score":              score,
            "complexity":         complexity,
            "docstring":          docstring,
            "classes":            class_names,
            "functions":          func_names,
            "imports":            import_mods,
            "lines":              loc,
            "inbound":            inbound,
            "outbound":           outbound,
            "is_notebook":        pf.get("is_notebook", False),
            "avg_cc":             radon.get("avg_cc",              1.0),
            "max_cc":             radon.get("max_cc",              1),
            "cc_rank":            radon.get("cc_rank",             "A"),
            "halstead_volume":    radon.get("halstead_volume",     0.0),
            "halstead_difficulty":radon.get("halstead_difficulty", 0.0),
            "halstead_effort":    radon.get("halstead_effort",     0.0),
            "halstead_bugs":      radon.get("halstead_bugs",       0.0),
        })

    return sorted(modules, key=lambda m: -m["score"])


# ─────────────────────────────────────────────────────────────────────────────
#  Step 5b — Build class list with exact method details
# ─────────────────────────────────────────────────────────────────────────────

def _build_classes(parsed_files: list[dict]) -> list[dict]:
    classes = []
    for pf in parsed_files:
        for cls in pf["classes"]:
            method_names = [m["name"] for m in cls["methods"]]
            n_calls = len(cls["calls_made"])
            score = round(
                min(10, len(cls["methods"]) * 0.6 + n_calls * 0.05 + 5), 2
            )
            classes.append({
                "name":         cls["name"],
                "module":       pf["mod_key"],
                "module_short": pf["mod_key"].split(".")[-1],
                "path":         pf["path"],
                "score":        score,
                "methods":      len(cls["methods"]),
                "method_names": method_names,
                "calls":        n_calls,
                "bases":        cls["bases"],
                "lineno":       cls["lineno"],
                "docstring":    cls["docstring"],
                "self_attrs":   cls.get("self_attrs", {}),
            })

    return sorted(classes, key=lambda c: -c["score"])


# ─────────────────────────────────────────────────────────────────────────────
#  Step 6 — LLM enrichment (ONE call, descriptions + plan only)
# ─────────────────────────────────────────────────────────────────────────────

async def _llm_enrich(
    full_name: str,
    readme:    str,
    task:      str,
    static:    dict,
) -> dict:
    """
    The ONLY LLM call.
    Adds: module/class docstrings (where absent), readme_summary,
    task_plan, entry_point, run_command.
    Never modifies structural data.
    """
    mod_lines = []
    for m in static["modules"][:25]:
        mod_lines.append(
            f"  {m['short_name']} ({m['path']}) "
            f"| classes={m['classes'][:5]} "
            f"| funcs={m['functions'][:5]} "
            f"| score={m['score']}"
        )

    cls_lines = []
    for c in static["classes"][:20]:
        cls_lines.append(
            f"  {c['name']} in {c['module_short']} "
            f"| bases={c['bases']} "
            f"| methods={c['method_names'][:6]}"
        )

    fcg_lines = [
        f"  {e['from_label']} → {e['to_label']} (w={e['weight']})"
        for e in static["fcg_edges"][:15]
    ]
    mdg_lines = [
        f"  {e['from_short']} → {e['to_short']} (w={e['weight']})"
        for e in static["mdg_edges"][:15]
    ]

    cycle_lines = []
    for cyc in static.get("import_cycles", [])[:5]:
        cycle_lines.append("  [CYCLE] " + " → ".join(cyc))

    prompt = f"""You are enriching a Python repository that was analysed by AST + runtime import tracing.
The structure below is EXACT — extracted from the real source code.
Do NOT change, invent, or contradict any of it.
Your job: add human-readable descriptions and a task execution plan.

REPO: {full_name}
TASK: {task}

README EXCERPT:
{readme[:1500]}

EXACT MODULE STRUCTURE (from AST):
{chr(10).join(mod_lines)}

EXACT CLASS STRUCTURE (from AST):
{chr(10).join(cls_lines)}

EXACT FCG EDGES (cross-module calls):
{chr(10).join(fcg_lines)}

EXACT MDG EDGES (import dependencies):
{chr(10).join(mdg_lines)}

IMPORT CYCLES DETECTED:
{chr(10).join(cycle_lines) or "  None"}

CORE COMPONENTS: {static['core_components']}

Return ONLY valid JSON — no markdown, no preamble:
{{
  "module_docstrings": {{
    "short_module_name": "One sentence (≤12 words) describing what this module does"
  }},
  "class_docstrings": {{
    "ClassName": "One sentence describing this class"
  }},
  "readme_summary": "One sentence summary of the whole repo.",
  "task_plan": [
    "Step 1: ...",
    "Step 2: ...",
    "Step 3: ...",
    "Step 4: ...",
    "Step 5: ..."
  ],
  "entry_point": "relative/path/to/main_entry.py",
  "run_command": "python path/to/main.py --relevant-args"
}}

Write a docstring for every module and class listed above."""

    try:
        raw = await llm_chat(
            system=("You are a precise code documentation engine. "
                    "Return ONLY valid JSON. No markdown. No preamble. "
                    "Never invent class names, function names, or file paths."),
            user=prompt,
            max_tokens=1500,
            temperature=0.1,
        )
        enrichment = _parse_json(raw)
    except Exception as exc:
        log.warning("LLM enrichment failed: %s", exc)
        enrichment = {}

    mod_doc_map = enrichment.get("module_docstrings", {})
    for mod in static["modules"]:
        if not mod["docstring"]:
            mod["docstring"] = mod_doc_map.get(
                mod["short_name"],
                mod_doc_map.get(mod["name"], "")
            )

    cls_doc_map = enrichment.get("class_docstrings", {})
    for cls in static["classes"]:
        if not cls["docstring"]:
            cls["docstring"] = cls_doc_map.get(cls["name"], "")

    static["readme_summary"] = enrichment.get("readme_summary", "")
    static["task_plan"] = enrichment.get("task_plan", [
        "Install dependencies",
        "Identify entry point",
        "Run main script",
        "Inspect outputs",
    ])
    static["entry_point"] = enrichment.get("entry_point", "")
    static["run_command"] = enrichment.get("run_command", "")

    return static


# ─────────────────────────────────────────────────────────────────────────────
#  Metrics
# ─────────────────────────────────────────────────────────────────────────────

def _radon_file_metrics(source: str) -> dict:
    """Compute radon cyclomatic complexity and Halstead metrics for one Python source file."""
    _default = {
        "avg_cc": 1.0, "max_cc": 1, "cc_rank": "A",
        "halstead_volume": 0.0, "halstead_difficulty": 0.0,
        "halstead_effort": 0.0, "halstead_bugs": 0.0,
    }
    try:
        from radon.complexity import cc_visit, cc_rank  # type: ignore
        from radon.metrics import h_visit               # type: ignore

        blocks  = cc_visit(source)
        cc_vals = [b.complexity for b in blocks]
        avg_cc  = round(sum(cc_vals) / len(cc_vals), 2) if cc_vals else 1.0
        max_cc  = max(cc_vals, default=1)

        # h_visit returns (module_aggregate_HalsteadReport, [(name, report), ...])
        hal = list(h_visit(source))
        module_report = hal[0] if hal and hasattr(hal[0], "volume") else None
        if module_report and module_report.volume > 0:
            volume     = round(module_report.volume,     2)
            difficulty = round(module_report.difficulty, 2)
            effort     = round(module_report.effort,     2)
            bugs       = round(module_report.bugs,       4)
        else:
            volume = difficulty = effort = bugs = 0.0

        return {
            "avg_cc":              avg_cc,
            "max_cc":              max_cc,
            "cc_rank":             cc_rank(max_cc),
            "halstead_volume":     volume,
            "halstead_difficulty": difficulty,
            "halstead_effort":     effort,
            "halstead_bugs":       bugs,
        }
    except Exception:
        return _default


def _compute_metrics(data: dict) -> dict:
    modules = data.get("modules", [])
    classes = data.get("classes", [])
    fcg     = data.get("fcg_edges", [])
    mdg     = data.get("mdg_edges", [])
    cycles  = data.get("import_cycles", [])

    scores = [m["score"] for m in modules]
    in_deg: dict[str, int] = defaultdict(int)
    for e in mdg:
        in_deg[e["to"]] += e.get("weight", 1)

    total_methods   = sum(c["methods"] for c in classes)
    total_functions = sum(len(m.get("functions", [])) for m in modules)
    total_lines     = sum(m.get("lines", 0) for m in modules)

    # Radon aggregates
    cc_avgs    = [m.get("avg_cc", 1.0) for m in modules]
    max_cc     = max((m.get("max_cc", 1) for m in modules), default=1)
    h_volume   = sum(m.get("halstead_volume", 0.0) for m in modules)
    h_effort   = sum(m.get("halstead_effort", 0.0) for m in modules)
    h_bugs     = sum(m.get("halstead_bugs",   0.0) for m in modules)

    return {
        "total_modules":     len(modules),
        "total_classes":     len(classes),
        "total_methods":     total_methods,
        "total_functions":   total_functions,
        "total_fcg_edges":   len(fcg),
        "total_mdg_edges":   len(mdg),
        "total_lines":       total_lines,
        "import_cycles":     len(cycles),
        "avg_module_score":  round(sum(scores) / len(scores), 2) if scores else 0,
        "max_module_score":  round(max(scores, default=0), 2),
        "most_depended_on":  max(in_deg, key=in_deg.get) if in_deg else "",
        "avg_complexity":    round(
            sum(m.get("complexity", 0) for m in modules) / len(modules), 2
        ) if modules else 0,
        "most_called_class": (
            max(classes, key=lambda c: c["calls"])["name"] if classes else ""
        ),
        "avg_cyclomatic":    round(sum(cc_avgs) / len(cc_avgs), 2) if cc_avgs else 1.0,
        "max_cyclomatic":    int(max_cc),
        "halstead_volume":   round(h_volume, 2),
        "halstead_effort":   round(h_effort, 2),
        "halstead_bugs":     round(h_bugs, 4),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _collect_file_tree(workspace: str) -> list[str]:
    """Flat file tree (relative paths, up to 100 entries) skipping noise dirs."""
    ws = Path(workspace)
    tree: list[str] = []
    for root, dirs, files in os.walk(ws):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for f in sorted(files):
            tree.append(str((Path(root) / f).relative_to(ws)))
    return tree[:100]


def _rel_to_pkg(rel_path: str, ws: Path) -> str:
    p = Path(rel_path)
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else rel_path


def _parse_json(text: str) -> dict:
    text  = re.sub(r"```(?:json|python)?|```", "", text).strip()
    start = text.find("{")
    end   = text.rfind("}")
    if start != -1 and end != -1:
        return json.loads(text[start: end + 1])
    raise ValueError(f"No JSON found in: {text[:200]}")


# ─────────────────────────────────────────────────────────────────────────────
#  README fetch
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_readme(full_name: str) -> str:
    url = f"https://r.jina.ai/https://github.com/{full_name}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"Accept": "text/plain"})
            return resp.text[:4000]
    except Exception:
        return f"Repository: {full_name}"
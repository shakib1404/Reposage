"""
Module Dependency Graph (MDG)

Parses import statements across all source files to build a directed graph
of inter-module dependencies, then computes PageRank to identify high-risk
central modules.

Nodes  →  relative file path (normalised)
Edges  →  importer → importee
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Optional

import networkx as nx


class ModuleDependencyGraph:
    def __init__(self):
        self.graph: nx.DiGraph = nx.DiGraph()
        self._pagerank: dict[str, float] = {}
        # maps importable module name → file path
        self._module_index: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, files: dict[str, str]) -> "ModuleDependencyGraph":
        """files: {relative_path: source_code}"""
        # index all known module paths
        for path in files:
            mod_name = self._path_to_module(path)
            if mod_name:
                self._module_index[mod_name] = path
            self.graph.add_node(path)

        # parse imports
        for path, source in files.items():
            ext = Path(path).suffix
            if ext == ".py":
                deps = self._parse_python_imports(path, source)
            else:
                deps = self._parse_generic_imports(path, source)
            for dep in deps:
                if dep and dep != path:
                    self.graph.add_node(dep)
                    self.graph.add_edge(path, dep)

        # compute PageRank (higher = more central / risky to change)
        if self.graph.number_of_nodes() > 0:
            try:
                self._pagerank = nx.pagerank(self.graph, alpha=0.85)
            except nx.PowerIterationFailedConvergence:
                self._pagerank = {n: 1.0 / self.graph.number_of_nodes()
                                  for n in self.graph.nodes}
        return self

    # ------------------------------------------------------------------
    # Python import parser
    # ------------------------------------------------------------------

    def _parse_python_imports(self, path: str, source: str) -> list[str]:
        deps: list[str] = []
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return self._parse_generic_imports(path, source)

        base_dir = str(Path(path).parent)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    resolved = self._resolve(alias.name, base_dir)
                    if resolved:
                        deps.append(resolved)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    resolved = self._resolve(node.module, base_dir, level=node.level or 0)
                    if resolved:
                        deps.append(resolved)
        return deps

    def _resolve(self, module_name: str, base_dir: str, level: int = 0) -> Optional[str]:
        if level > 0:
            # relative import — reconstruct path
            parts = base_dir.split("/") if base_dir else []
            for _ in range(level - 1):
                if parts:
                    parts.pop()
            mod_parts = module_name.split(".")
            candidate = "/".join(parts + mod_parts) + ".py"
            if candidate in self.graph:
                return candidate
            candidate_init = "/".join(parts + mod_parts + ["__init__.py"])
            if candidate_init in self.graph:
                return candidate_init
            return None

        # absolute import — look in index
        for suffix in ["", ".py", "/__init__.py"]:
            key = module_name.replace(".", "/") + suffix
            if key in self._module_index:
                return self._module_index[key]
        return self._module_index.get(module_name.split(".")[0])

    # ------------------------------------------------------------------
    # Generic (JS / TS / Go / etc.) import parser
    # ------------------------------------------------------------------

    def _parse_generic_imports(self, path: str, source: str) -> list[str]:
        deps: list[str] = []
        base_dir = str(Path(path).parent)

        # JS/TS: import ... from '...' / require('...')
        js_patterns = [
            re.compile(r"""(?:import|from)\s+['"]([^'"]+)['"]"""),
            re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),
        ]
        # Go: import "..."
        go_patterns = [
            re.compile(r'''import\s+["']([^"']+)["']'''),
        ]

        for pat in js_patterns + go_patterns:
            for m in pat.finditer(source):
                spec = m.group(1)
                if spec.startswith("."):
                    # relative: resolve to a path
                    resolved = self._resolve_relative(spec, base_dir)
                    if resolved:
                        deps.append(resolved)
                else:
                    # absolute/third-party: only track if it maps to our files
                    resolved = self._module_index.get(spec.replace("/", "."))
                    if resolved:
                        deps.append(resolved)
        return deps

    def _resolve_relative(self, spec: str, base_dir: str) -> Optional[str]:
        base = Path(base_dir) / spec
        for ext in ["", ".py", ".js", ".ts", ".jsx", ".tsx"]:
            candidate = str(base) + ext
            if candidate in self.graph:
                return candidate
        return None

    @staticmethod
    def _path_to_module(path: str) -> Optional[str]:
        p = Path(path)
        if p.suffix not in {".py", ".js", ".ts", ".jsx", ".tsx"}:
            return None
        parts = list(p.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_pagerank(self, path: str) -> float:
        return self._pagerank.get(path, 0.0)

    def get_dependencies(self, path: str) -> list[str]:
        """What does `path` import?"""
        if path not in self.graph:
            return []
        return list(self.graph.successors(path))

    def get_dependents(self, path: str) -> list[str]:
        """What files import `path`?"""
        if path not in self.graph:
            return []
        return list(self.graph.predecessors(path))

    def get_risk_level(self, path: str) -> str:
        pr = self.get_pagerank(path)
        if pr >= 0.05:
            return "HIGH"
        if pr >= 0.01:
            return "MEDIUM"
        return "LOW"

    def get_high_risk_modules(self, threshold: float = 0.05) -> list[str]:
        return [p for p, pr in self._pagerank.items() if pr >= threshold]

    def get_safe_modules(self, threshold: float = 0.01) -> list[str]:
        return [p for p, pr in self._pagerank.items() if pr < threshold]

    def summary(self) -> dict:
        return {
            "nodes": self.graph.number_of_nodes(),
            "edges": self.graph.number_of_edges(),
            "high_risk": len(self.get_high_risk_modules()),
            "top_5_by_pagerank": sorted(
                self._pagerank.items(), key=lambda x: x[1], reverse=True
            )[:5],
        }

"""
Function Call Graph (FCG)

Uses Python's built-in `ast` module to parse .py files and build a directed
graph of function definitions and calls. Falls back to regex-based extraction
for non-Python files (.js, .ts, .go, etc.).

Graph structure:
  Nodes  →  "file::function"  (or just "file" for module-level code)
  Edges  →  caller → callee
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

import networkx as nx


class FunctionCallGraph:
    def __init__(self):
        self.graph: nx.DiGraph = nx.DiGraph()
        # maps function name → set of fully-qualified node ids
        self._name_index: dict[str, set[str]] = defaultdict(set)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, files: dict[str, str]) -> "FunctionCallGraph":
        """
        files: {relative_path: source_code}
        """
        for path, source in files.items():
            ext = Path(path).suffix
            if ext == ".py":
                self._parse_python(path, source)
            else:
                self._parse_generic(path, source)
        return self

    # ---- Python AST parser -------------------------------------------

    def _parse_python(self, path: str, source: str):
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError:
            self._parse_generic(path, source)
            return

        visitor = _PythonVisitor(path)
        visitor.visit(tree)

        for func_node_id, calls in visitor.calls.items():
            self.graph.add_node(func_node_id, file=path, kind="function")
            self._name_index[func_node_id.split("::")[-1]].add(func_node_id)
            for call_name in calls:
                # add a placeholder callee; resolved later
                self.graph.add_edge(func_node_id, f"?::{call_name}", call=call_name)

        # add test nodes
        for node_id in visitor.test_nodes:
            self.graph.nodes[node_id]["is_test"] = True

    # ---- Generic regex parser ----------------------------------------

    def _parse_generic(self, path: str, source: str):
        fn_pattern = re.compile(
            r"(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(?|"
            r"def\s+(\w+)|func\s+(\w+)|fun\s+(\w+))"
        )
        call_pattern = re.compile(r"(\w+)\s*\(")

        lines = source.splitlines()
        current_fn: Optional[str] = None

        for line in lines:
            m = fn_pattern.search(line)
            if m:
                name = next(g for g in m.groups() if g)
                current_fn = f"{path}::{name}"
                self.graph.add_node(current_fn, file=path, kind="function")
                self._name_index[name].add(current_fn)

            scope = current_fn or f"{path}::__module__"
            if not self.graph.has_node(scope):
                self.graph.add_node(scope, file=path, kind="module")

            for call_m in call_pattern.finditer(line):
                callee_name = call_m.group(1)
                if callee_name in {"if", "for", "while", "return", "print", "import"}:
                    continue
                self.graph.add_edge(scope, f"?::{callee_name}", call=callee_name)

    # ------------------------------------------------------------------
    # Resolve placeholder nodes
    # ------------------------------------------------------------------

    def resolve(self) -> "FunctionCallGraph":
        """Replace '?::name' placeholders with real node ids where known."""
        to_add: list[tuple[str, str, dict]] = []
        to_remove: list[tuple[str, str]] = []

        for u, v, data in list(self.graph.edges(data=True)):
            if v.startswith("?::"):
                call_name = data.get("call", v[3:])
                candidates = self._name_index.get(call_name, set())
                to_remove.append((u, v))
                for real_v in candidates:
                    to_add.append((u, real_v, data))

        for u, v in to_remove:
            self.graph.remove_edge(u, v)
        for u, v, d in to_add:
            self.graph.add_edge(u, v, **d)

        # remove orphan placeholders
        to_delete = [n for n in list(self.graph.nodes) if str(n).startswith("?::")]
        self.graph.remove_nodes_from(to_delete)
        return self

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_callers(self, node_id: str) -> list[str]:
        """Return all nodes that call node_id (predecessors)."""
        if node_id not in self.graph:
            return []
        return list(self.graph.predecessors(node_id))

    def get_callees(self, node_id: str) -> list[str]:
        """Return all nodes that node_id calls (successors)."""
        if node_id not in self.graph:
            return []
        return list(self.graph.successors(node_id))

    def get_impact_radius(self, node_ids: list[str], depth: int = 2) -> set[str]:
        """BFS outward from node_ids to find everything that could break."""
        visited: set[str] = set()
        frontier = set(node_ids)
        for _ in range(depth):
            next_frontier: set[str] = set()
            for nid in frontier:
                preds = set(self.graph.predecessors(nid))
                succs = set(self.graph.successors(nid))
                next_frontier |= (preds | succs) - visited
            visited |= frontier
            frontier = next_frontier
        return visited

    def get_test_nodes_for(self, node_ids: list[str]) -> list[str]:
        """Return test functions that directly or indirectly call given nodes."""
        all_tests = [
            n for n, d in self.graph.nodes(data=True)
            if d.get("is_test") or "test" in str(n).lower()
        ]
        relevant = set(node_ids)
        result = []
        for test in all_tests:
            reachable = nx.descendants(self.graph, test)
            if reachable & relevant:
                result.append(test)
        return result

    def get_nodes_for_file(self, file_path: str) -> list[str]:
        return [
            n for n, d in self.graph.nodes(data=True)
            if d.get("file") == file_path
        ]

    def summary(self) -> dict:
        return {
            "nodes": self.graph.number_of_nodes(),
            "edges": self.graph.number_of_edges(),
            "files": len({d.get("file") for _, d in self.graph.nodes(data=True)}),
        }


# ---------------------------------------------------------------------------
# Internal AST visitor
# ---------------------------------------------------------------------------

class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.calls: dict[str, set[str]] = {}
        self.test_nodes: set[str] = set()
        self._stack: list[str] = []

    def _current_scope(self) -> str:
        return self._stack[-1] if self._stack else f"{self.path}::__module__"

    def visit_FunctionDef(self, node: ast.FunctionDef):
        nid = f"{self.path}::{node.name}"
        self.calls.setdefault(nid, set())
        if node.name.startswith("test_") or node.name.startswith("Test"):
            self.test_nodes.add(nid)
        self._stack.append(nid)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call):
        scope = self._current_scope()
        self.calls.setdefault(scope, set())
        name = self._call_name(node.func)
        if name:
            self.calls[scope].add(name)
        self.generic_visit(node)

    @staticmethod
    def _call_name(node: ast.expr) -> Optional[str]:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

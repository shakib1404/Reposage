"""
Hierarchical Code Tree (HCT)

Builds a scored directory tree of the repository. Each node carries a
relevance score against the current task so the agent only sends the
most important files to Claude.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SUPPORTED_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".go", ".rb", ".rs", ".cpp",
    ".c", ".h", ".cs", ".php", ".swift",
}

IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "env", ".env", "dist", "build", ".next", ".nuxt",
    "coverage", ".pytest_cache", ".mypy_cache",
}


@dataclass
class HCTNode:
    path: str          # relative path from repo root
    name: str
    is_dir: bool
    score: float = 0.0
    content: Optional[str] = None
    children: list["HCTNode"] = field(default_factory=list)
    parent: Optional["HCTNode"] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "is_dir": self.is_dir,
            "score": round(self.score, 4),
            "children": [c.to_dict() for c in self.children],
        }


class HierarchicalCodeTree:
    """
    Scans a repo root into a tree of HCTNodes, then scores each node
    by keyword overlap with the task description.
    """

    def __init__(self, repo_root: str):
        self.repo_root = Path(repo_root)
        self.root: Optional[HCTNode] = None
        self._all_file_nodes: list[HCTNode] = []

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self) -> "HierarchicalCodeTree":
        self.root = self._walk(self.repo_root, parent=None)
        return self

    def _walk(self, path: Path, parent: Optional[HCTNode]) -> HCTNode:
        rel = str(path.relative_to(self.repo_root)) if path != self.repo_root else "."
        node = HCTNode(path=rel, name=path.name or ".", is_dir=path.is_dir(), parent=parent)

        if path.is_dir():
            try:
                entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
            except PermissionError:
                return node
            for entry in entries:
                if entry.name in IGNORE_DIRS:
                    continue
                if entry.is_file() and entry.suffix not in SUPPORTED_EXTENSIONS:
                    continue
                child = self._walk(entry, parent=node)
                node.children.append(child)
        else:
            try:
                node.content = path.read_text(errors="ignore")
            except OSError:
                node.content = ""
            self._all_file_nodes.append(node)

        return node

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def score(self, task: str) -> "HierarchicalCodeTree":
        """Score every file node against the task; propagate up to dirs."""
        keywords = self._extract_keywords(task)
        for node in self._all_file_nodes:
            node.score = self._score_node(node, keywords)
        # propagate max score upward so dirs reflect their best child
        if self.root:
            self._propagate(self.root)
        return self

    def _extract_keywords(self, task: str) -> list[str]:
        stop = {"the", "a", "an", "is", "in", "of", "to", "and", "or", "for",
                "with", "on", "at", "by", "from", "this", "that", "fix", "add",
                "make", "create", "update", "change", "please", "need", "want"}
        tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", task.lower())
        return [t for t in tokens if t not in stop and len(t) > 2]

    def _score_node(self, node: HCTNode, keywords: list[str]) -> float:
        if not keywords:
            return 0.5
        score = 0.0
        name_lower = node.name.lower()
        content_lower = (node.content or "").lower()

        for kw in keywords:
            if kw in name_lower:
                score += 2.0       # keyword in filename → high relevance
            count = content_lower.count(kw)
            if count:
                score += min(count * 0.1, 1.0)   # cap per-keyword content bonus

        # normalise to [0, 1]
        return min(score / (len(keywords) * 3.0), 1.0)

    def _propagate(self, node: HCTNode) -> float:
        if not node.is_dir:
            return node.score
        child_scores = [self._propagate(c) for c in node.children]
        node.score = max(child_scores) if child_scores else 0.0
        return node.score

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_top_files(self, top_k: int = 15) -> list[HCTNode]:
        sorted_nodes = sorted(self._all_file_nodes, key=lambda n: n.score, reverse=True)
        return sorted_nodes[:top_k]

    def get_file_map(self) -> dict[str, str]:
        """Return {relative_path: content} for all files."""
        return {n.path: (n.content or "") for n in self._all_file_nodes}

    def get_structure_summary(self, max_depth: int = 4) -> str:
        lines: list[str] = []
        self._render(self.root, lines, depth=0, max_depth=max_depth)
        return "\n".join(lines)

    def _render(self, node: HCTNode, lines: list[str], depth: int, max_depth: int):
        if depth > max_depth:
            return
        indent = "  " * depth
        marker = "/" if node.is_dir else ""
        score_tag = f" [{node.score:.2f}]" if not node.is_dir else ""
        lines.append(f"{indent}{node.name}{marker}{score_tag}")
        for child in node.children:
            self._render(child, lines, depth + 1, max_depth)

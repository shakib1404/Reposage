"""
Graph Context Builder

Combines HCT + FCG + MDG outputs into a single rich context package that
gets sent to Claude. This is the key intelligence layer: instead of
dumping the full repo, we hand Claude a surgical subset.

For large files we use FCG-guided section extraction: instead of a blunt
line-count cap, we extract only the function bodies that are relevant to
the task (plus a small surrounding context window). This keeps tokens low
without creating blind spots.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from .hct import HierarchicalCodeTree, HCTNode
from .fcg import FunctionCallGraph
from .mdg import ModuleDependencyGraph

# Lines of context to show above/below each extracted function
_CONTEXT_LINES = 5
# If a file has fewer lines than this, always send it whole — no extraction needed
_SMALL_FILE_LINES = 150
# Hard cap per file when we cannot extract sections (non-Python or parse failure)
_FALLBACK_CAP_LINES = 300
# Total character budget for everything sent to Claude (~60k tokens)
_TOKEN_BUDGET_CHARS = 200_000


@dataclass
class SmartContext:
    task: str
    relevant_files: dict[str, str]       # path → content (possibly section-extracted)
    impact_files: dict[str, str]         # callers/callees to be aware of
    module_risks: dict[str, dict]        # path → {risk, pagerank, deps, dependents}
    test_targets: list[str]              # test node ids to run
    repo_structure: str                  # pretty-printed tree
    hct_top_nodes: list[dict]           # top scored HCT nodes
    total_token_estimate: int = 0

    def format_for_claude(self) -> str:
        parts: list[str] = []

        parts.append("=== REPO STRUCTURE ===")
        parts.append(self.repo_structure)

        parts.append("\n=== RELEVANT FILES (edit these) ===")
        for path, content in self.relevant_files.items():
            risk = self.module_risks.get(path, {}).get("risk", "LOW")
            parts.append(f"\n--- FILE: {path}  [RISK: {risk}] ---")
            parts.append(content)

        if self.impact_files:
            parts.append("\n=== IMPACT RADIUS (callers/callees — update if signatures change) ===")
            for path, content in self.impact_files.items():
                parts.append(f"\n--- FILE: {path} ---")
                lines = content.splitlines()
                shown = lines[:_FALLBACK_CAP_LINES]
                parts.append("\n".join(shown))
                if len(lines) > _FALLBACK_CAP_LINES:
                    parts.append(f"... ({len(lines) - _FALLBACK_CAP_LINES} more lines not shown)")

        if self.module_risks:
            parts.append("\n=== MODULE RISK SCORES ===")
            for path, info in self.module_risks.items():
                parts.append(
                    f"  {path}: {info['risk']} "
                    f"(pagerank={info['pagerank']:.4f}, "
                    f"depended_by={len(info['dependents'])} modules)"
                )

        if self.test_targets:
            parts.append("\n=== TEST TARGETS (run these to validate) ===")
            for t in self.test_targets:
                parts.append(f"  {t}")

        return "\n".join(parts)


class GraphContextBuilder:
    def __init__(
        self,
        hct: HierarchicalCodeTree,
        fcg: FunctionCallGraph,
        mdg: ModuleDependencyGraph,
        top_k: int = 15,
        impact_depth: int = 2,
    ):
        self.hct = hct
        self.fcg = fcg
        self.mdg = mdg
        self.top_k = top_k
        self.impact_depth = impact_depth

    def build(self, task: str) -> SmartContext:
        # 1. Score HCT against the task
        self.hct.score(task)
        top_nodes: list[HCTNode] = self.hct.get_top_files(self.top_k)

        # 2. Collect relevant FCG function names per file before we trim content
        #    so we know which sections to extract from large files
        all_files = self.hct.get_file_map()
        relevant_fn_names: dict[str, set[str]] = {}
        for node in top_nodes:
            fcg_nodes = self.fcg.get_nodes_for_file(node.path)
            relevant_fn_names[node.path] = {
                nid.split("::")[-1] for nid in fcg_nodes if "::" in nid
            }

        # 3. Build relevant_files with smart section extraction, respecting budget
        relevant_files: dict[str, str] = {}
        chars_used = 0
        for node in top_nodes:
            if node.content is None:
                continue
            content = self._smart_extract(
                path=node.path,
                source=node.content,
                fn_names=relevant_fn_names.get(node.path, set()),
            )
            if chars_used + len(content) > _TOKEN_BUDGET_CHARS:
                break
            relevant_files[node.path] = content
            chars_used += len(content)

        # 4. Impact radius via FCG
        relevant_fcg_nodes: list[str] = []
        for path in relevant_files:
            relevant_fcg_nodes.extend(self.fcg.get_nodes_for_file(path))

        impact_node_ids = self.fcg.get_impact_radius(relevant_fcg_nodes, depth=self.impact_depth)
        impact_files: dict[str, str] = {}
        for nid in impact_node_ids:
            file_path = nid.split("::")[0] if "::" in nid else nid
            if file_path not in relevant_files and file_path in all_files:
                impact_files[file_path] = all_files[file_path]

        impact_files = dict(list(impact_files.items())[:10])

        # 5. Module risk scores
        module_risks: dict[str, dict] = {}
        for path in list(relevant_files) + list(impact_files):
            module_risks[path] = {
                "risk": self.mdg.get_risk_level(path),
                "pagerank": self.mdg.get_pagerank(path),
                "deps": self.mdg.get_dependencies(path),
                "dependents": self.mdg.get_dependents(path),
            }

        # 6. Test targets + repo structure
        test_targets = self.fcg.get_test_nodes_for(relevant_fcg_nodes)
        repo_structure = self.hct.get_structure_summary(max_depth=3)

        ctx = SmartContext(
            task=task,
            relevant_files=relevant_files,
            impact_files=impact_files,
            module_risks=module_risks,
            test_targets=test_targets,
            repo_structure=repo_structure,
            hct_top_nodes=[n.to_dict() for n in top_nodes],
        )
        ctx.total_token_estimate = len(ctx.format_for_claude()) // 4
        return ctx

    # ------------------------------------------------------------------
    # Smart section extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _smart_extract(path: str, source: str, fn_names: set[str]) -> str:
        """
        For small files: return the whole file (no extraction needed).
        For large Python files: return only the relevant function bodies
        (identified by FCG) plus _CONTEXT_LINES lines of surrounding context,
        plus imports/module-level code from the top.
        For large non-Python files: fall back to a hard line cap.

        Claude always receives the FULL file content for small files, so it
        can return a complete corrected version. For large files it receives
        the relevant sections but is instructed to return the full file.
        """
        lines = source.splitlines()

        if len(lines) <= _SMALL_FILE_LINES:
            return source

        if not path.endswith(".py") or not fn_names:
            # Non-Python or no FCG info: use hard cap
            cap = lines[:_FALLBACK_CAP_LINES]
            result = "\n".join(cap)
            if len(lines) > _FALLBACK_CAP_LINES:
                result += f"\n# ... {len(lines) - _FALLBACK_CAP_LINES} more lines (not shown)"
            return result

        # Python: AST-based function range extraction
        ranges = _get_function_ranges(source)  # {fn_name: (start_line, end_line)} 1-indexed

        # Always include imports and module-level code (first non-blank, non-comment section)
        preamble_end = _preamble_end(lines)
        selected: set[int] = set(range(0, preamble_end))  # 0-indexed

        found_any = False
        for fn_name in fn_names:
            if fn_name not in ranges:
                continue
            start, end = ranges[fn_name]  # 1-indexed
            # Add with context window
            lo = max(0, start - 1 - _CONTEXT_LINES)
            hi = min(len(lines), end + _CONTEXT_LINES)
            selected.update(range(lo, hi))
            found_any = True

        if not found_any:
            # FCG had names but AST didn't match — fall back to hard cap
            return "\n".join(lines[:_FALLBACK_CAP_LINES]) + (
                f"\n# ... {len(lines) - _FALLBACK_CAP_LINES} more lines (not shown)"
                if len(lines) > _FALLBACK_CAP_LINES else ""
            )

        # Render selected lines, inserting "..." for skipped regions
        out_lines: list[str] = []
        prev = -1
        for i in sorted(selected):
            if i > prev + 1:
                out_lines.append(f"# ... (lines {prev + 2}–{i} not shown) ...")
            out_lines.append(lines[i])
            prev = i

        total_shown = len(selected)
        total_skipped = len(lines) - total_shown
        if total_skipped > 0:
            out_lines.append(
                f"\n# NOTE: {total_skipped} lines skipped (not relevant to this task).\n"
                "# When returning the fixed file, return the COMPLETE file including all lines."
            )
        return "\n".join(out_lines)

    @staticmethod
    def _estimate_tokens(ctx: SmartContext) -> int:
        return len(ctx.format_for_claude()) // 4


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def _get_function_ranges(source: str) -> dict[str, tuple[int, int]]:
    """Return {function_name: (first_line, last_line)} using 1-based line numbers."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    ranges: dict[str, tuple[int, int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            ranges[node.name] = (node.lineno, end)
    return ranges


def _preamble_end(lines: list[str]) -> int:
    """Return the index (exclusive) of the last import / module-level line."""
    last_import = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("import ", "from ", "#!", "# -*-", '"""', "'''")):
            last_import = i + 1
        elif stripped == "" or stripped.startswith("#"):
            continue
        elif last_import > 0:
            break
    return min(last_import + 2, len(lines))

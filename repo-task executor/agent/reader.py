"""
Code Reader

Reads the cloned repository, builds all three graphs (HCT, FCG, MDG),
and returns a SmartContext ready to be sent to Claude.
"""

from __future__ import annotations

from pathlib import Path

from graphs import (
    HierarchicalCodeTree,
    FunctionCallGraph,
    ModuleDependencyGraph,
    GraphContextBuilder,
)
from graphs.context_builder import SmartContext
from utils.logger import log


class CodeReader:
    def __init__(
        self,
        repo_root: str,
        top_k: int = 15,
        impact_depth: int = 2,
    ):
        self.repo_root = repo_root
        self.top_k = top_k
        self.impact_depth = impact_depth

        self.hct: HierarchicalCodeTree | None = None
        self.fcg: FunctionCallGraph | None = None
        self.mdg: ModuleDependencyGraph | None = None

    def read(self, task: str) -> SmartContext:
        """
        Full pipeline:
          1. Build HCT (directory tree with relevance scoring)
          2. Build FCG (function call graph)
          3. Build MDG (module dependency graph + PageRank)
          4. Build SmartContext (graph-guided file selection)
        """
        log.info("Building Hierarchical Code Tree (HCT)...")
        self.hct = HierarchicalCodeTree(self.repo_root).build()
        log.info(f"  HCT: {len(self.hct._all_file_nodes)} source files indexed")

        all_files = self.hct.get_file_map()

        log.info("Building Function Call Graph (FCG)...")
        self.fcg = FunctionCallGraph().build(all_files).resolve()
        log.info(f"  FCG: {self.fcg.summary()}")

        log.info("Building Module Dependency Graph (MDG)...")
        self.mdg = ModuleDependencyGraph().build(all_files)
        log.info(f"  MDG: {self.mdg.summary()}")

        log.info("Building smart context from graphs...")
        builder = GraphContextBuilder(
            hct=self.hct,
            fcg=self.fcg,
            mdg=self.mdg,
            top_k=self.top_k,
            impact_depth=self.impact_depth,
        )
        ctx = builder.build(task)
        log.info(
            f"  Smart context: {len(ctx.relevant_files)} relevant files, "
            f"{len(ctx.impact_files)} impact files, "
            f"~{ctx.total_token_estimate:,} tokens"
        )
        return ctx

    def get_all_files(self) -> dict[str, str]:
        if self.hct is None:
            raise RuntimeError("Call read() first.")
        return self.hct.get_file_map()

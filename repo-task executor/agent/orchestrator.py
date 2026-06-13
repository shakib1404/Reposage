"""
Agent Orchestrator

The main pipeline controller. Runs the full agent loop:
  Clone → Read → (Build Graphs) → Generate Fix → Apply → Test → [Retry] → Push PR

Implements a self-healing retry loop: when tests fail, Claude receives the
error output and generates a corrected patch, up to MAX_RETRIES times.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent.cloner import RepoCloner
from agent.reader import CodeReader
from agent.writer import CodeWriter
from agent.tester import TestRunner, TestResult
from agent.github_pr import GitHubPRAgent
from agent.claude_agent import ClaudeCodeAgent, CodeChanges
from graphs.context_builder import SmartContext
from utils.logger import log


@dataclass
class AgentResult:
    success: bool
    task: str
    github_url: str
    pr_url: str = ""
    branch_name: str = ""
    explanation: str = ""
    attempts: int = 0
    test_result: TestResult | None = None
    error: str = ""
    changed_files: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def summary(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        lines = [
            f"Status   : {status}",
            f"Task     : {self.task}",
            f"Attempts : {self.attempts}",
            f"Duration : {self.duration_seconds:.1f}s",
        ]
        if self.pr_url:
            lines.append(f"PR URL   : {self.pr_url}")
        if self.branch_name:
            lines.append(f"Branch   : {self.branch_name}")
        if self.changed_files:
            lines.append(f"Changed  : {', '.join(self.changed_files)}")
        if self.error:
            lines.append(f"Error    : {self.error}")
        return "\n".join(lines)


class AgentOrchestrator:
    def __init__(
        self,
        workspace_dir: str | None = None,
        max_retries: int | None = None,
        top_k: int = 15,
        open_pr: bool = True,
        run_tests: bool = True,
    ):
        self.workspace_dir = workspace_dir or os.getenv("WORKSPACE_DIR", "./workspace")
        self.max_retries = max_retries or int(os.getenv("MAX_RETRIES", "3"))
        self.top_k = top_k
        self.open_pr = open_pr
        self.run_tests = run_tests

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, github_url: str, task: str) -> AgentResult:
        start = time.monotonic()
        result = AgentResult(success=False, task=task, github_url=github_url)

        log.section(f"AGENT START — {task}")

        try:
            # ── Step 1: Clone ─────────────────────────────────────────
            log.step(1, "Cloning repository")
            cloner = RepoCloner(self.workspace_dir)
            default_branch = cloner.get_default_branch(github_url)
            cloner.clone(github_url)

            # ── Step 2: Read & Build Graphs ──────────────────────────
            log.step(2, "Reading codebase & building graphs")
            reader = CodeReader(self.workspace_dir, top_k=self.top_k)
            ctx: SmartContext = reader.read(task)

            # ── Step 3: Self-healing generate → test loop ─────────────
            log.step(3, "Generating code fix (with retry)")
            changes, test_result = self._generate_and_test_loop(task, ctx, result)

            result.test_result = test_result
            result.changed_files = changes.all_changed_paths()
            result.explanation = changes.explanation

            if not test_result.passed and self.run_tests:
                result.error = "Max retries exhausted; tests still failing."
                log.error(result.error)
                result.duration_seconds = time.monotonic() - start
                return result

            # ── Step 4: Commit & Push PR ──────────────────────────────
            log.step(4, "Pushing to GitHub")
            pr_agent = GitHubPRAgent(self.workspace_dir, github_url)
            branch = pr_agent.commit_and_push(
                task=task,
                changed_paths=changes.all_changed_paths(),
                explanation=changes.explanation,
            )
            result.branch_name = branch

            if self.open_pr:
                pr_url = pr_agent.create_pr(
                    branch_name=branch,
                    task=task,
                    explanation=changes.explanation,
                    test_summary=test_result.short_summary(),
                    base_branch=default_branch,
                )
                result.pr_url = pr_url

            result.success = True
            log.section(f"DONE — {result.pr_url or result.branch_name}")

        except Exception as exc:
            result.error = str(exc)
            log.error(f"Agent failed: {exc}")
            import traceback
            log.debug(traceback.format_exc())

        result.duration_seconds = time.monotonic() - start
        return result

    # ------------------------------------------------------------------
    # Self-healing loop
    # ------------------------------------------------------------------

    def _generate_and_test_loop(
        self,
        task: str,
        ctx: SmartContext,
        result: AgentResult,
    ) -> tuple[CodeChanges, TestResult]:
        claude = ClaudeCodeAgent()
        writer = CodeWriter(self.workspace_dir)
        tester = TestRunner(self.workspace_dir)

        previous_error: str | None = None
        last_changes: CodeChanges | None = None
        last_test: TestResult | None = None

        for attempt in range(1, self.max_retries + 1):
            log.info(f"Attempt {attempt}/{self.max_retries}")
            result.attempts = attempt

            # Generate fix
            log.info("Calling Claude...")
            changes = claude.generate_fix(task, ctx, previous_error=previous_error)
            last_changes = changes
            log.info(f"Claude explanation: {changes.explanation}")
            log.info(f"Confidence: {changes.confidence:.0%}")
            if changes.risk_notes:
                log.warning(f"Risk notes: {changes.risk_notes}")

            # Apply changes
            log.info("Applying file changes...")
            writer.apply(changes)

            # Run tests
            if not self.run_tests:
                dummy = TestResult(
                    passed=True, output="Tests skipped.", errors="",
                    return_code=0, runner="none"
                )
                return changes, dummy

            if not tester.has_tests():
                log.info("No test suite found — skipping test step")
                return changes, tester.run_no_tests()

            log.info("Running tests...")
            test_result = tester.run(custom_commands=changes.test_commands or None)
            last_test = test_result

            if test_result.passed:
                log.success(f"Tests passed on attempt {attempt}")
                return changes, test_result

            # Tests failed — prepare error context for retry
            log.warning(f"Tests failed (attempt {attempt})")
            log.debug(test_result.short_summary())
            previous_error = test_result.short_summary()

            # Re-read ctx to reflect current on-disk state
            reader = CodeReader(self.workspace_dir, top_k=self.top_k)
            ctx = reader.read(task)

        # All retries exhausted
        return last_changes, last_test  # type: ignore[return-value]

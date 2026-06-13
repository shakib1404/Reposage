"""
Test Runner

Runs the project's test suite inside a Docker sandbox (safe, isolated).
Auto-detects the language and test framework from the repo.
Falls back to direct subprocess execution if Docker is not available.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from utils.logger import log


@dataclass
class TestResult:
    passed: bool
    output: str
    errors: str
    return_code: int
    runner: str  # "docker" | "local"

    def short_summary(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        lines = (self.output + self.errors).strip().splitlines()
        tail = "\n".join(lines[-30:]) if len(lines) > 30 else "\n".join(lines)
        return f"[{status}]\n{tail}"


DOCKER_IMAGES = {
    "python": "python:3.11-slim",
    "node":   "node:20-alpine",
    "go":     "golang:1.22-alpine",
    "java":   "maven:3.9-eclipse-temurin-21",
    "ruby":   "ruby:3.3-alpine",
    "rust":   "rust:1.78-slim",
}

TEST_COMMANDS = {
    "python": (
        "pip install -r requirements.txt -q 2>/dev/null || true; "
        "pip install pytest -q 2>/dev/null || true; "
        "pytest --tb=short -q"
    ),
    "node": "npm install -q && (npm test 2>&1 || npx jest 2>&1)",
    "go":   "go test ./... -v",
    "java": "mvn test -q",
    "ruby": "bundle install -q && rspec",
    "rust": "cargo test",
}


class TestRunner:
    def __init__(self, workspace_dir: str, timeout: int = 180):
        self.workspace_dir = Path(workspace_dir)
        self.timeout = timeout
        self._docker_available = shutil.which("docker") is not None

    def run(self, custom_commands: list[str] | None = None) -> TestResult:
        """
        Run tests. If custom_commands is provided (from Claude's output),
        use those; otherwise auto-detect.
        """
        lang = self._detect_language()
        log.info(f"Detected language: {lang}")

        if custom_commands:
            cmd = " && ".join(custom_commands)
        else:
            cmd = TEST_COMMANDS.get(lang, TEST_COMMANDS["python"])

        if self._docker_available:
            return self._run_docker(lang, cmd)
        else:
            log.warning("Docker not found — running tests locally (less safe)")
            return self._run_local(cmd)

    def run_no_tests(self) -> TestResult:
        """Return a passing result when there are no tests in the repo."""
        return TestResult(
            passed=True,
            output="No test suite detected — skipping test step.",
            errors="",
            return_code=0,
            runner="none",
        )

    # ------------------------------------------------------------------
    # Runners
    # ------------------------------------------------------------------

    def _docker_daemon_ok(self) -> bool:
        """Quick check that Docker daemon is reachable."""
        try:
            r = subprocess.run(
                ["docker", "info"],
                capture_output=True, timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _run_docker(self, lang: str, cmd: str) -> TestResult:
        if not self._docker_daemon_ok():
            log.warning("Docker daemon not reachable — falling back to local execution")
            return self._run_local(cmd)

        image = DOCKER_IMAGES.get(lang, DOCKER_IMAGES["python"])
        abs_workspace = str(self.workspace_dir.resolve())

        docker_cmd = [
            "docker", "run", "--rm",
            "--memory=1g",
            "--cpus=2",
            "-v", f"{abs_workspace}:/app",
            "-w", "/app",
            image,
            "bash", "-c", cmd,
        ]

        log.info(f"Running tests in Docker ({image})...")
        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return TestResult(
                passed=False,
                output="",
                errors=f"Tests timed out after {self.timeout}s",
                return_code=-1,
                runner="docker",
            )

        # If Docker itself errored (daemon issue, image pull fail), fall back locally
        docker_errors = ("permission denied", "Cannot connect", "No such file")
        if result.returncode != 0 and any(e in result.stderr for e in docker_errors):
            log.warning(f"Docker error: {result.stderr[:120].strip()} — falling back to local")
            return self._run_local(cmd)

        tr = TestResult(
            passed=result.returncode == 0,
            output=result.stdout,
            errors=result.stderr,
            return_code=result.returncode,
            runner="docker",
        )
        # Always show test output so failures are visible
        log.info(f"Test output:\n{tr.short_summary()}")
        return tr

    def _run_local(self, cmd: str) -> TestResult:
        log.info(f"Running locally: {cmd}")
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=str(self.workspace_dir),
            )
        except subprocess.TimeoutExpired:
            return TestResult(
                passed=False,
                output="",
                errors=f"Tests timed out after {self.timeout}s",
                return_code=-1,
                runner="local",
            )

        tr = TestResult(
            passed=result.returncode == 0,
            output=result.stdout,
            errors=result.stderr,
            return_code=result.returncode,
            runner="local",
        )
        log.info(f"Test output:\n{tr.short_summary()}")
        return tr

    # ------------------------------------------------------------------
    # Language detection
    # ------------------------------------------------------------------

    def _detect_language(self) -> str:
        files = {f.name for f in self.workspace_dir.iterdir() if f.is_file()}
        if "requirements.txt" in files or "setup.py" in files or "pyproject.toml" in files:
            return "python"
        if "package.json" in files:
            return "node"
        if "go.mod" in files:
            return "go"
        if "pom.xml" in files or "build.gradle" in files:
            return "java"
        if "Gemfile" in files:
            return "ruby"
        if "Cargo.toml" in files:
            return "rust"
        return "python"

    def has_tests(self) -> bool:
        for root, _, files in os.walk(self.workspace_dir):
            for f in files:
                if f.startswith("test_") or f.endswith("_test.py") or f == "spec.js":
                    return True
        return False

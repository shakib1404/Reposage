"""
Doc Agent

Two modes:
  1. docstrings  — finds every function/class missing a docstring and patches them in
  2. readme      — generates a project-level README.md from the codebase
  3. module      — generates a brand-new .py module that fits the repo's conventions

Uses the same SmartContext + patch-hunk format as ClaudeCodeAgent so the
existing CodeWriter can apply results without changes.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import anthropic

from agent.claude_agent import CodeChanges, Hunk, _make_changes
from graphs.context_builder import SmartContext
from graphs.hct import HierarchicalCodeTree
from utils.logger import log

import json, re


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_DOCSTRING_PROMPT = """
You are a technical documentation expert. You will receive Python source files.

Your job: add or improve docstrings for every function and class that is missing one
or has a very short/unhelpful one (single word, placeholder, etc.).

Rules:
1. Follow Google-style docstrings.
2. Include Args, Returns, Raises sections where relevant.
3. Keep it concise — 2-5 lines for simple functions, more for complex ones.
4. Do NOT change any logic — only add/improve docstrings.
5. Preserve all existing code exactly, including indentation and formatting.

Respond with ONLY valid JSON (no markdown fences):
{
  "explanation": "summary of what was documented",
  "confidence": 0.95,
  "risk_notes": "",
  "files": {
    "path/to/file.py": [
      {
        "old": "    def function_name(self, x):\\n        pass",
        "new": "    def function_name(self, x):\\n        \\\"\\\"\\\"Brief description.\\\\n\\\\n        Args:\\\\n            x: description\\\\n        \\\"\\\"\\\"\\n        pass"
      }
    ]
  },
  "new_files": {},
  "deleted_files": [],
  "test_commands": []
}
"""

_README_PROMPT = """
You are a technical writer. You will receive a repository's file structure and key source files.

Generate a professional README.md for this project.

Include:
- Project title and one-line description
- Features list (derived from the code)
- Installation instructions (infer from requirements.txt / setup.py)
- Usage examples (derive from the code's public API / main entry points)
- Project structure overview
- Contributing section (brief)
- License section (MIT if unknown)

Respond with ONLY valid JSON (no markdown fences):
{
  "explanation": "Generated README.md",
  "confidence": 0.9,
  "risk_notes": "",
  "files": {},
  "new_files": {
    "README.md": "# full readme content here"
  },
  "deleted_files": [],
  "test_commands": []
}
"""

_MODULE_PROMPT = """
You are a senior Python engineer. You will receive an existing codebase's structure and key files.

Your job: generate a brand-new Python module that:
1. Fits the coding style and conventions of the existing codebase
2. Implements exactly what the user's task describes
3. Includes proper docstrings, type hints, and error handling
4. Follows the same import patterns and module structure as existing files

Respond with ONLY valid JSON (no markdown fences):
{
  "explanation": "What module was created and why",
  "confidence": 0.95,
  "risk_notes": "",
  "files": {},
  "new_files": {
    "path/to/new_module.py": "full module content"
  },
  "deleted_files": [],
  "test_commands": ["pytest tests/test_new_module.py -v"]
}
"""


class DocAgent:
    def __init__(self, model: str | None = None):
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")

    # ------------------------------------------------------------------
    # Docstring generation
    # ------------------------------------------------------------------

    def generate_docstrings(
        self, ctx: SmartContext, target_file: str | None = None
    ) -> CodeChanges:
        """Add/improve docstrings for all undocumented functions in relevant files."""
        files = (
            {target_file: ctx.relevant_files[target_file]}
            if target_file and target_file in ctx.relevant_files
            else ctx.relevant_files
        )

        # Filter to only files that actually need docstrings
        needy_files = {
            path: content
            for path, content in files.items()
            if path.endswith(".py") and self._has_undocumented(content)
        }

        if not needy_files:
            log.info("All functions already have docstrings — nothing to do")
            return CodeChanges(explanation="All functions already documented.",
                               confidence=1.0, risk_notes="")

        log.info(f"Generating docstrings for {len(needy_files)} files...")

        file_block = ""
        for path, content in needy_files.items():
            file_block += f"\n--- FILE: {path} ---\n{content}\n"

        user_msg = (
            f"Add/improve docstrings for all undocumented functions in these files.\n\n"
            f"{file_block}"
        )
        return self._call(user_msg, _DOCSTRING_PROMPT)

    # ------------------------------------------------------------------
    # README generation
    # ------------------------------------------------------------------

    def generate_readme(self, ctx: SmartContext, repo_root: str) -> CodeChanges:
        """Generate a README.md from the repo structure + key files."""
        log.info("Generating README.md...")

        # grab requirements / setup files if present
        extra = ""
        for fname in ("requirements.txt", "setup.py", "pyproject.toml", "setup.cfg"):
            p = Path(repo_root) / fname
            if p.exists():
                extra += f"\n--- {fname} ---\n{p.read_text(errors='ignore')[:500]}\n"

        # top-5 most-central files for style context
        sample_files = "\n".join(
            f"\n--- FILE: {path} ---\n{content[:800]}"
            for path, content in list(ctx.relevant_files.items())[:5]
        )

        user_msg = (
            f"Project repo structure:\n{ctx.repo_structure}\n\n"
            f"Key source files (excerpts):\n{sample_files}\n\n"
            f"Config/dependency files:\n{extra}"
        )
        return self._call(user_msg, _README_PROMPT)

    # ------------------------------------------------------------------
    # New module generation
    # ------------------------------------------------------------------

    def generate_module(self, task: str, ctx: SmartContext) -> CodeChanges:
        """Generate a new Python module based on the task and codebase conventions."""
        log.info(f"Generating new module: {task}")

        # Send top-3 files as style examples
        style_examples = "\n".join(
            f"\n--- EXAMPLE FILE: {path} ---\n{content[:600]}"
            for path, content in list(ctx.relevant_files.items())[:3]
        )

        user_msg = (
            f"TASK: {task}\n\n"
            f"Repo structure:\n{ctx.repo_structure}\n\n"
            f"Existing files (for style reference):\n{style_examples}"
        )
        return self._call(user_msg, _MODULE_PROMPT)

    # ------------------------------------------------------------------
    # Shared Claude call + parse
    # ------------------------------------------------------------------

    def _call(self, user_msg: str, system_prompt: str) -> CodeChanges:
        log.info(f"Calling Claude ({self.model}) — ~{len(user_msg)//4:,} tokens")
        response = self.client.messages.create(
            model=self.model,
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text
        return self._parse(raw)

    @staticmethod
    def _parse(raw: str) -> CodeChanges:
        if not raw or not raw.strip():
            raise ValueError("Claude returned an empty response")

        fence = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", raw.strip(), re.DOTALL)
        cleaned = fence.group(1).strip() if fence else raw.strip()

        try:
            data = json.loads(cleaned)
            return _make_changes(data)
        except json.JSONDecodeError:
            pass

        # brace-count fallback
        start = cleaned.find("{")
        if start != -1:
            depth = 0
            for i, ch in enumerate(cleaned[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return _make_changes(json.loads(cleaned[start: i + 1]))
                        except json.JSONDecodeError:
                            break
        raise ValueError(f"Could not parse response.\nRaw (first 400 chars):\n{raw[:400]}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _has_undocumented(source: str) -> bool:
        """Return True if any function/class in the file lacks a docstring."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    return True
        return False

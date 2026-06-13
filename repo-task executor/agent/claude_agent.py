"""
Claude Code Agent

Sends the smart context + task to Claude and parses its structured JSON response.

Output format uses PATCH HUNKS instead of full file rewrites. This keeps
Claude's output under ~500 tokens regardless of file size, avoiding the
8192-token output limit that truncates large files.

Patch format:
  "files": {
    "path/to/file.py": [
      {"old": "exact lines to replace", "new": "replacement lines"}
    ]
  }
For brand-new files, "new_files" still uses full content (files are small).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

import anthropic

from graphs.context_builder import SmartContext
from utils.logger import log


SYSTEM_PROMPT = """
You are an expert autonomous coding agent with deep graph intelligence about the repository.

You will receive:
- TASK: what needs to be done
- REPO STRUCTURE: the directory tree
- RELEVANT FILES: the files most likely to need editing (from HCT scoring)
- IMPACT RADIUS: files that call or are called by relevant code (from FCG)
- MODULE RISK SCORES: PageRank-based risk (HIGH = be careful, LOW = safe)

Your rules:
1. Analyze the task carefully using all provided context.
2. Only edit files shown in RELEVANT FILES unless absolutely necessary.
3. Check IMPACT RADIUS — if you change a function signature, update its callers too.
4. Never freely edit HIGH-risk modules without explicitly noting the risk.
5. Write clean, idiomatic code without unnecessary comments.
6. If the task requires a new file, add it to new_files with full content.
7. Preserve existing code style and formatting conventions.

You MUST respond with ONLY valid JSON — no markdown fences, no text outside the JSON.

Use this EXACT format:
{
  "explanation": "Concise description of what you changed and why",
  "confidence": 0.95,
  "risk_notes": "Any concerns about high-risk modules or breaking changes",
  "files": {
    "path/to/file.py": [
      {
        "old": "exact original lines to replace (copy verbatim from the file shown)",
        "new": "replacement lines"
      }
    ]
  },
  "new_files": {
    "path/to/new/file.py": "full content of a brand-new file"
  },
  "deleted_files": [],
  "test_commands": ["pytest path/to/test_file.py -v"]
}

IMPORTANT for "files":
- Each entry is a LIST of patch hunks, not the full file content.
- "old" must be an EXACT copy of the lines you are replacing (including indentation).
- "new" is what replaces those lines.
- If no changes needed for a file, omit it from "files".
- Keep hunks small and focused — only the lines that change.
"""

RETRY_SYSTEM_PROMPT = """
You are an expert autonomous coding agent. A previous fix attempt failed its tests.

Diagnose the test failure and produce corrected patch hunks.

Respond with ONLY valid JSON — no markdown, no text outside JSON:
{
  "explanation": "what you changed and why the previous attempt failed",
  "confidence": 0.9,
  "risk_notes": "",
  "files": {
    "path/to/file.py": [
      {"old": "exact lines to replace", "new": "corrected replacement"}
    ]
  },
  "new_files": {},
  "deleted_files": [],
  "test_commands": []
}
"""


@dataclass
class Hunk:
    old: str
    new: str


@dataclass
class CodeChanges:
    explanation: str
    confidence: float
    risk_notes: str
    # path → list of patch hunks  OR  path → full string (legacy small files)
    files: dict[str, list[Hunk] | str] = field(default_factory=dict)
    new_files: dict[str, str] = field(default_factory=dict)
    deleted_files: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)

    def all_changed_paths(self) -> list[str]:
        return list(self.files.keys()) + list(self.new_files.keys())


class ClaudeCodeAgent:
    def __init__(self, model: str | None = None, max_tokens: int = 8192):
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")
        self.max_tokens = max_tokens

    def generate_fix(
        self,
        task: str,
        context: SmartContext,
        previous_error: str | None = None,
    ) -> CodeChanges:
        system = RETRY_SYSTEM_PROMPT if previous_error else SYSTEM_PROMPT

        context_text = context.format_for_claude()
        if previous_error:
            context_text += f"\n\n=== PREVIOUS TEST FAILURE ===\n{previous_error}\n"

        user_message = f"TASK: {task}\n\n{context_text}"

        log.info(f"Calling Claude ({self.model}) — ~{len(user_message)//4:,} tokens")

        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text
        log.debug(f"Raw response: {len(raw)} chars")
        return self._parse_response(raw)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(raw: str) -> CodeChanges:
        if not raw or not raw.strip():
            raise ValueError("Claude returned an empty response")

        # Strip a single wrapping code fence if present
        fence_match = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", raw.strip(), re.DOTALL)
        cleaned = fence_match.group(1).strip() if fence_match else raw.strip()

        # Direct parse
        try:
            data = json.loads(cleaned)
            return _make_changes(data)
        except json.JSONDecodeError:
            pass

        # Brace-counting fallback for slightly malformed JSON
        log.warning("Direct JSON parse failed — scanning for JSON object...")
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
                            data = json.loads(cleaned[start: i + 1])
                            return _make_changes(data)
                        except json.JSONDecodeError:
                            break

        raise ValueError(
            f"Could not parse Claude response as JSON.\n"
            f"Raw (first 500 chars):\n{raw[:500]}"
        )


def _make_changes(data: dict) -> CodeChanges:
    raw_files = data.get("files", {})
    parsed_files: dict[str, list[Hunk] | str] = {}

    for path, value in raw_files.items():
        if isinstance(value, list):
            # Patch hunk format (expected)
            parsed_files[path] = [
                Hunk(old=h.get("old", ""), new=h.get("new", ""))
                for h in value
                if isinstance(h, dict)
            ]
        elif isinstance(value, str):
            # Legacy full-file format (small files / fallback)
            parsed_files[path] = value
        # else: ignore unexpected types

    return CodeChanges(
        explanation=data.get("explanation", ""),
        confidence=float(data.get("confidence", 0.5)),
        risk_notes=data.get("risk_notes", ""),
        files=parsed_files,
        new_files=data.get("new_files", {}),
        deleted_files=data.get("deleted_files", []),
        test_commands=data.get("test_commands", []),
    )

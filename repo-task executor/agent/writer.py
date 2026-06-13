"""
Code Writer

Applies Claude's changes to the workspace directory.

Supports two modes per file:
  - Patch hunks  (list of {old, new}) — replaces exact strings in-place
  - Full content (str)                — overwrites the entire file (new/small files)
"""

from __future__ import annotations

from pathlib import Path

from agent.claude_agent import CodeChanges, Hunk
from utils.logger import log


class CodeWriter:
    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)
        self.changed_paths: list[str] = []

    def apply(self, changes: CodeChanges) -> list[str]:
        self.changed_paths = []

        # Edit existing files (patch or full rewrite)
        for rel_path, value in changes.files.items():
            full_path = self.workspace_dir / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)

            if isinstance(value, list):
                self._apply_patches(full_path, rel_path, value)
            else:
                full_path.write_text(value, encoding="utf-8")
                log.success(f"Rewrote: {rel_path}")

            self.changed_paths.append(rel_path)

        # Create new files
        for rel_path, content in changes.new_files.items():
            full_path = self.workspace_dir / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            log.success(f"Created: {rel_path}")
            self.changed_paths.append(rel_path)

        # Delete files
        for rel_path in changes.deleted_files:
            full_path = self.workspace_dir / rel_path
            if full_path.exists():
                full_path.unlink()
                log.info(f"Deleted: {rel_path}")
                self.changed_paths.append(rel_path)
            else:
                log.warning(f"Tried to delete non-existent file: {rel_path}")

        return self.changed_paths

    def _apply_patches(self, full_path: Path, rel_path: str, hunks: list[Hunk]):
        if not full_path.exists():
            log.warning(f"Patch target not found: {rel_path} — skipping")
            return

        content = full_path.read_text(encoding="utf-8", errors="ignore")
        applied = 0

        for hunk in hunks:
            if not hunk.old:
                continue
            if hunk.old in content:
                content = content.replace(hunk.old, hunk.new, 1)
                applied += 1
            else:
                # Try normalising line endings
                old_norm = hunk.old.replace("\r\n", "\n").replace("\r", "\n")
                content_norm = content.replace("\r\n", "\n").replace("\r", "\n")
                if old_norm in content_norm:
                    content = content_norm.replace(old_norm, hunk.new, 1)
                    applied += 1
                else:
                    log.warning(
                        f"Patch hunk not found in {rel_path}:\n"
                        f"  Expected: {hunk.old[:80]!r}..."
                    )

        if applied:
            full_path.write_text(content, encoding="utf-8")
            log.success(f"Patched {rel_path} ({applied}/{len(hunks)} hunks applied)")
        else:
            log.warning(f"No hunks applied to {rel_path}")

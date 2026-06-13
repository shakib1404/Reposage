"""
Rich-based coloured logger for the agent pipeline.
Falls back to plain print if rich is not installed.
"""

from __future__ import annotations

import sys

try:
    from rich.console import Console
    from rich.text import Text
    _console = Console(stderr=False)
    _err_console = Console(stderr=True)
    _RICH = True
except ImportError:
    _RICH = False


class _Logger:
    def _print(self, msg: str, style: str = "", err: bool = False):
        if _RICH:
            c = _err_console if err else _console
            c.print(msg, style=style)
        else:
            print(msg, file=sys.stderr if err else sys.stdout)

    def info(self, msg: str):
        self._print(f"  {msg}", style="dim")

    def success(self, msg: str):
        self._print(f"✓ {msg}", style="green")

    def warning(self, msg: str):
        self._print(f"⚠ {msg}", style="yellow")

    def error(self, msg: str):
        self._print(f"✗ {msg}", style="bold red", err=True)

    def debug(self, msg: str):
        # only shown if DEBUG=1
        import os
        if os.getenv("DEBUG"):
            self._print(f"  [debug] {msg}", style="dim blue")

    def step(self, n: int, label: str):
        self._print(f"\n[Step {n}] {label}", style="bold cyan")

    def section(self, label: str):
        sep = "─" * 60
        self._print(f"\n{sep}\n{label}\n{sep}", style="bold white")


log = _Logger()

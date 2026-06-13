from __future__ import annotations

from pathlib import Path


def read_file_safe(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def detect_language(repo_root: str) -> str:
    root = Path(repo_root)
    names = {f.name for f in root.iterdir() if f.is_file()}
    if "requirements.txt" in names or "setup.py" in names or "pyproject.toml" in names:
        return "python"
    if "package.json" in names:
        return "node"
    if "go.mod" in names:
        return "go"
    if "pom.xml" in names or "build.gradle" in names:
        return "java"
    if "Gemfile" in names:
        return "ruby"
    if "Cargo.toml" in names:
        return "rust"
    return "python"

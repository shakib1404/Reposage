"""
Repo Cloner

Clones a GitHub repository into a local workspace directory.
Supports both HTTPS and SSH URLs, with token injection for private repos.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

import git

from utils.logger import log


class RepoCloner:
    def __init__(self, workspace_dir: str = "./workspace"):
        self.workspace_dir = Path(workspace_dir)

    def clone(self, github_url: str, branch: str | None = None) -> git.Repo:
        """
        Clone (or refresh) repo at github_url into workspace_dir.
        Returns the local git.Repo object.
        """
        # Inject token for private repos
        clone_url = self._inject_token(github_url)

        dest = self.workspace_dir
        if dest.exists():
            log.info(f"Removing existing workspace: {dest}")
            shutil.rmtree(dest)

        dest.mkdir(parents=True, exist_ok=True)

        log.info(f"Cloning {self._safe_url(github_url)} → {dest}")
        kwargs: dict = {"to_path": str(dest)}
        if branch:
            kwargs["branch"] = branch

        repo = git.Repo.clone_from(clone_url, **kwargs)
        log.success(f"Cloned successfully (branch: {repo.active_branch.name})")
        return repo

    def get_default_branch(self, github_url: str) -> str:
        """Detect the default branch (main / master) without cloning."""
        token = os.getenv("GITHUB_TOKEN", "")
        repo_name = self._repo_name(github_url)
        import httpx
        url = f"https://api.github.com/repos/{repo_name}"

        # Try authenticated first (needed for private repos), then anonymously.
        # A dead or rate-limited token makes the authenticated call 401 even on
        # a PUBLIC repo, and the old code then silently assumed "main" — which
        # for a master-default repo means the PR is opened against a base that
        # does not exist. The unauthenticated retry resolves that case.
        attempts = []
        if token:
            attempts.append({"Authorization": f"Bearer {token}"})
        attempts.append({})

        for headers in attempts:
            try:
                r = httpx.get(url, headers=headers, timeout=10)
            except Exception:
                continue
            if r.status_code == 200:
                return r.json().get("default_branch", "main")

        # Last resort: whatever the clone actually checked out beats a guess.
        try:
            import git
            return git.Repo(self.workspace_dir).active_branch.name
        except Exception:
            log.warning("Could not detect the default branch — assuming 'main'")
            return "main"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _inject_token(url: str) -> str:
        token = os.getenv("GITHUB_TOKEN", "")
        if not token:
            return url
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.hostname == "github.com":
            return url.replace("https://", f"https://{token}@")
        return url

    @staticmethod
    def _safe_url(url: str) -> str:
        """Strip token from URL for logging."""
        token = os.getenv("GITHUB_TOKEN", "")
        return url.replace(token, "***") if token else url

    @staticmethod
    def _repo_name(url: str) -> str:
        """Extract 'owner/repo' from a GitHub URL."""
        url = url.rstrip("/").removesuffix(".git")
        return "/".join(url.split("/")[-2:])

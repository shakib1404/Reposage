"""
GitHub PR Agent

Commits the changes to a new branch and opens a Pull Request via the
GitHub API. Also supports commit-only mode (no PR) for simple tasks.
"""

from __future__ import annotations

import os
import re
from datetime import datetime

import git
from github import Github, GithubException

from utils.logger import log


class GitHubPRAgent:
    def __init__(self, workspace_dir: str, github_url: str):
        self.workspace_dir = workspace_dir
        self.github_url = github_url.rstrip("/").removesuffix(".git")
        self.repo_name = self._extract_repo_name(github_url)
        self.token = os.getenv("GITHUB_TOKEN", "")

    def commit_and_push(
        self,
        task: str,
        changed_paths: list[str],
        explanation: str,
        branch_prefix: str = "agent-fix",
    ) -> str:
        """Commit changes and push to a new branch. Returns branch name."""
        local_repo = git.Repo(self.workspace_dir)
        branch_name = self._branch_name(task, branch_prefix)

        log.info(f"Creating branch: {branch_name}")
        local_repo.git.checkout("-b", branch_name)

        # Stage changed files
        for path in changed_paths:
            local_repo.git.add(path)

        commit_msg = f"fix: {task[:72]}\n\nAutomated by Coding Agent\n\n{explanation[:500]}"
        local_repo.git.commit("-m", commit_msg)
        log.success("Committed changes")

        # Push with token
        remote_url = f"https://{self.token}@github.com/{self.repo_name}.git"
        local_repo.git.push(remote_url, branch_name)
        log.success(f"Pushed branch: {branch_name}")

        return branch_name

    def create_pr(
        self,
        branch_name: str,
        task: str,
        explanation: str,
        test_summary: str,
        base_branch: str = "main",
    ) -> str:
        """Open a Pull Request. Returns the PR URL."""
        if not self.token:
            raise ValueError("GITHUB_TOKEN is required to create PRs")

        g = Github(self.token)
        try:
            gh_repo = g.get_repo(self.repo_name)
        except GithubException as e:
            raise RuntimeError(f"Cannot access repo {self.repo_name}: {e}")

        pr_body = self._pr_body(task, explanation, test_summary)
        pr_title = f"fix: {task[:70]}"

        try:
            pr = gh_repo.create_pull(
                title=pr_title,
                body=pr_body,
                head=branch_name,
                base=base_branch,
            )
            log.success(f"PR created: {pr.html_url}")
            return pr.html_url
        except GithubException as e:
            # PR may already exist
            log.warning(f"PR creation failed ({e}), returning branch URL instead")
            return f"https://github.com/{self.repo_name}/tree/{branch_name}"

    def get_default_branch(self) -> str:
        if not self.token:
            return "main"
        g = Github(self.token)
        try:
            gh_repo = g.get_repo(self.repo_name)
            return gh_repo.default_branch
        except GithubException:
            return "main"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _branch_name(task: str, prefix: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:40].strip("-")
        ts = datetime.now().strftime("%m%d%H%M")
        return f"{prefix}/{slug}-{ts}"

    @staticmethod
    def _extract_repo_name(url: str) -> str:
        url = url.rstrip("/").removesuffix(".git")
        return "/".join(url.split("/")[-2:])

    @staticmethod
    def _pr_body(task: str, explanation: str, test_summary: str) -> str:
        return f"""## Task
{task}

## What Changed
{explanation}

## Test Results
```
{test_summary}
```

---
*Automated by Coding Agent powered by Claude*
"""

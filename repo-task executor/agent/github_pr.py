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


def scrub_secrets(text: str) -> str:
    """Redact anything that looks like a credential in git/GitHub output.

    git prints the full remote URL on failure, which embeds the token. This
    ran once with a real ghp_ token rendered verbatim in the web UI.
    """
    text = re.sub(r"(gh[pousr]_)[A-Za-z0-9]{16,}", r"\1<redacted>", text)
    text = re.sub(r"(github_pat_)[A-Za-z0-9_]{20,}", r"\1<redacted>", text)
    # https://user:secret@host  and  https://secret@host
    text = re.sub(r"(https?://)[^/\s:@]+(?::[^/\s@]+)?@", r"\1<redacted>@", text)
    tok = os.getenv("GITHUB_TOKEN", "")
    if tok and len(tok) > 8:
        text = text.replace(tok, "<redacted>")
    return text


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

        # Push with token.
        #
        # The URL needs BOTH halves of basic auth. "https://<token>@github.com"
        # gives git a username and no password, so it falls back to prompting —
        # and with no TTY that dies as
        #     fatal: could not read Password for 'https://ghp_...@github.com'
        # with the raw token in the message. GitHub's documented form is a
        # fixed username plus the token as the password.
        remote_url = f"https://x-access-token:{self.token}@github.com/{self.repo_name}.git"
        try:
            local_repo.git.push(remote_url, branch_name)
        except Exception as exc:
            # git echoes the remote URL in its errors, so anything raised here
            # can carry the token into logs and into the browser. Never let the
            # raw value out.
            raise RuntimeError(f"git push failed: {scrub_secrets(str(exc))}") from None
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
            # A PR can legitimately fail to open — one already exists for this
            # branch, or the token lacks pull-request scope. The pushed branch
            # is still useful, so fall back to it rather than losing the work,
            # but say plainly that no PR was opened: the caller prints this as
            # "PR URL", and a /tree/ link silently standing in for a PR reads
            # as success when the requested thing did not happen.
            log.warning(f"PR NOT created ({scrub_secrets(str(e))}) — "
                        f"the fix is pushed to branch '{branch_name}', "
                        f"open the PR manually")
            return (f"(no PR — branch only) "
                    f"https://github.com/{self.repo_name}/tree/{branch_name}")

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

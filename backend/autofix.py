"""
autofix.py — Semgrep autofix → GitHub pull request
====================================================
Clones a repo, runs `semgrep --config auto --autofix`, and — if anything
actually changed — pushes the fix to a new branch and opens a pull request.
Never commits directly to the repo's default branch, regardless of whether
GITHUB_TOKEN has write access: with write access the branch is pushed
straight to the repo and a same-repo PR is opened; without it, the repo is
forked first and the PR is opened from the fork (the standard GitHub
contribution flow). Streams structured SSE events (same shape as
tester.py / executor.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from typing import AsyncGenerator

import httpx

log = logging.getLogger("autofix")

GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_API        = "https://api.github.com"
CLONE_TIMEOUT     = 120
SEMGREP_TIMEOUT   = 240
FORK_POLL_TIMEOUT = 30   # seconds to wait for a freshly-created fork to become clonable

BOT_NAME  = "RepoSage Autofix"
BOT_EMAIL = "reposage-bot@users.noreply.github.com"


def _github_headers() -> dict:
    return {
        "Accept":        "application/vnd.github+json",
        "User-Agent":    "RepoSage/1.0",
        "Authorization": f"token {GITHUB_TOKEN}",
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Public entry point
# ─────────────────────────────────────────────────────────────────────────────

async def run_autofix_loop(
    repo_full_name: str,
    job_id: str = "",
) -> AsyncGenerator[dict, None]:
    if not GITHUB_TOKEN:
        yield _ev("error", "Autofix unavailable",
                  "No GITHUB_TOKEN configured on the server — a token with "
                  "repo access is required to push a branch and open a pull request.")
        return

    if "/" not in repo_full_name:
        yield _ev("error", "Invalid repo", f"Expected 'owner/repo', got {repo_full_name!r}")
        return
    owner, repo = repo_full_name.split("/", 1)
    workspace: str | None = None

    try:
        # ── 1. Look up the repo + check write access ────────────────────────────
        yield _ev("status", "Checking repository", f"github.com/{repo_full_name}")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=_github_headers())
        if resp.status_code != 200:
            yield _ev("error", "Repository lookup failed",
                      f"GitHub API returned {resp.status_code} for {repo_full_name}")
            return
        repo_meta       = resp.json()
        default_branch  = repo_meta.get("default_branch", "main")
        can_push        = bool((repo_meta.get("permissions") or {}).get("push"))

        push_owner, push_repo = owner, repo
        if not can_push:
            yield _ev("status", "No write access — forking",
                      f"Forking {repo_full_name} so the fix branch can be pushed")
            push_owner, push_repo = await _fork_repo(owner, repo)
            yield _ev("status", "Fork ready", f"{push_owner}/{push_repo}")
        else:
            yield _ev("status", "Write access confirmed",
                      "Pushing directly to a new branch — never to the default branch")

        # ── 2. Clone ─────────────────────────────────────────────────────────────
        yield _ev("status", "Cloning repository", f"git clone github.com/{repo_full_name}")
        workspace = await _clone(repo_full_name)
        yield _ev("status", "Repository cloned", f"✓ {repo_full_name}")

        # ── 3. Isolated venv + semgrep ───────────────────────────────────────────
        yield _ev("status", "Setting up environment", "Installing semgrep…")
        venv_path = os.path.join(workspace, ".autofix_venv")
        await _create_venv(venv_path)
        pip = os.path.join(venv_path, "bin", "pip")
        await _run([pip, "install", "semgrep", "--quiet"], workspace, timeout=120)

        # ── 4. Run semgrep --autofix ─────────────────────────────────────────────
        yield _ev("status", "Running semgrep --autofix",
                  "Applying every automatic fix the \"auto\" ruleset has a patch for…")
        semgrep = os.path.join(venv_path, "bin", "semgrep")
        ok, out = await _run(
            [semgrep, "--config", "auto", "--autofix", "--json", "--quiet",
             "--disable-version-check", "."],
            workspace, timeout=SEMGREP_TIMEOUT)

        fixed_count = 0
        try:
            data = json.loads(out)
            fixed_count = sum(1 for r in data.get("results", [])
                              if (r.get("extra") or {}).get("fix") or r.get("fix"))
        except Exception:
            pass

        # ── 5. Anything actually change? ─────────────────────────────────────────
        _, status_out = await _run(["git", "status", "--porcelain"], workspace, timeout=30)
        changed_files = [line[3:] for line in status_out.splitlines() if line.strip()]
        if not changed_files:
            yield _ev("done", "No autofixable findings",
                      "Semgrep didn't find anything it could fix automatically — nothing to open a PR for.",
                      pr_url=None)
            return
        yield _ev("status", "Fixes applied",
                  f"{len(changed_files)} file(s) changed ({fixed_count} semgrep autofix(es))")

        # ── 6. Commit ────────────────────────────────────────────────────────────
        branch = f"reposage-autofix-{(job_id or str(int(time.time())))[:12]}"
        await _run(["git", "checkout", "-b", branch], workspace, timeout=30)
        await _run(["git", "config", "user.name", BOT_NAME], workspace, timeout=10)
        await _run(["git", "config", "user.email", BOT_EMAIL], workspace, timeout=10)
        await _run(["git", "add", "-A"], workspace, timeout=30)
        commit_msg = f"fix: Semgrep autofix ({fixed_count} fix(es) across {len(changed_files)} file(s))"
        commit_ok, commit_out = await _run(["git", "commit", "-m", commit_msg], workspace, timeout=30)
        if not commit_ok:
            yield _ev("error", "Commit failed", commit_out[:300])
            return
        yield _ev("status", "Committed", branch)

        # ── 7. Push ──────────────────────────────────────────────────────────────
        yield _ev("status", "Pushing branch", f"{push_owner}/{push_repo}:{branch}")
        push_url = f"https://{GITHUB_TOKEN}@github.com/{push_owner}/{push_repo}.git"
        git_env  = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
        push_ok, push_out = await _run(
            ["git", "push", push_url, f"HEAD:{branch}"], workspace, timeout=60, env=git_env)
        if not push_ok:
            yield _ev("error", "Push failed", push_out[:300])
            return
        yield _ev("status", "Branch pushed", "✓")

        # ── 8. Open the pull request ─────────────────────────────────────────────
        yield _ev("status", "Opening pull request", "…")
        head = branch if push_owner == owner else f"{push_owner}:{branch}"
        pr_body = (
            f"Automated fixes from Semgrep's `auto` ruleset — {fixed_count} fix(es) "
            f"across {len(changed_files)} file(s).\n\n"
            f"Opened automatically by RepoSage. Please review the diff before merging."
        )
        async with httpx.AsyncClient(timeout=30) as client:
            pr_resp = await client.post(
                f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
                headers=_github_headers(),
                json={
                    "title": f"Semgrep autofix: {fixed_count} automated fix(es)",
                    "head":  head,
                    "base":  default_branch,
                    "body":  pr_body,
                },
            )
        if pr_resp.status_code not in (200, 201):
            yield _ev("error", "Pull request creation failed",
                      f"{pr_resp.status_code}: {pr_resp.text[:300]}")
            return

        pr = pr_resp.json()
        yield _ev("done", "Pull request opened",
                  f"{fixed_count} fix(es) across {len(changed_files)} file(s)",
                  pr_url=pr.get("html_url"), branch=branch,
                  files_changed=len(changed_files), fixes=fixed_count)

    except Exception as exc:
        log.exception("Autofix pipeline error")
        yield _ev("error", "Autofix failed", _redact(str(exc)))

    finally:
        if workspace and os.path.isdir(workspace):
            shutil.rmtree(workspace, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
#  GitHub helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _fork_repo(owner: str, repo: str) -> tuple[str, str]:
    """
    Forks {owner}/{repo} into the token owner's account and waits for it to
    become clonable — GitHub creates forks asynchronously, so an immediate
    clone right after the fork call can 404.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{GITHUB_API}/repos/{owner}/{repo}/forks", headers=_github_headers())
        if resp.status_code not in (200, 202):
            raise RuntimeError(f"Fork request failed: {resp.status_code} {resp.text[:200]}")
        fork = resp.json()
        fork_owner, fork_repo = fork["owner"]["login"], fork["name"]

        deadline = time.monotonic() + FORK_POLL_TIMEOUT
        while time.monotonic() < deadline:
            check = await client.get(f"{GITHUB_API}/repos/{fork_owner}/{fork_repo}",
                                     headers=_github_headers())
            if check.status_code == 200:
                return fork_owner, fork_repo
            await asyncio.sleep(1.5)

    return fork_owner, fork_repo   # best-effort — proceed even if the poll timed out


# ─────────────────────────────────────────────────────────────────────────────
#  Shell / git helpers
# ─────────────────────────────────────────────────────────────────────────────

def _redact(text: str) -> str:
    """Strips GITHUB_TOKEN out of anything headed for an SSE event — git
    error messages sometimes echo back the remote URL they tried, and that
    URL has the token embedded in it."""
    if GITHUB_TOKEN and GITHUB_TOKEN in text:
        return text.replace(GITHUB_TOKEN, "***")
    return text


async def _clone(repo_full_name: str) -> str:
    ws  = tempfile.mkdtemp(prefix="autofix_")
    url = f"https://{GITHUB_TOKEN}@github.com/{repo_full_name}.git" if GITHUB_TOKEN \
          else f"https://github.com/{repo_full_name}.git"
    git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    proc = await asyncio.create_subprocess_exec(
        # A little history (not --depth 1) so the working branch has room
        # to commit on top of without shallow-clone edge cases.
        "git", "clone", "--depth", "50", url, ws,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=git_env,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=CLONE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        shutil.rmtree(ws, ignore_errors=True)
        raise RuntimeError("git clone timed out")
    if proc.returncode != 0:
        shutil.rmtree(ws, ignore_errors=True)
        raise RuntimeError(f"git clone failed: {_redact(err.decode(errors='ignore')[:400])}")
    return ws


async def _create_venv(venv_path: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "venv", venv_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=60)


async def _run(cmd: list[str], cwd: str, timeout: int = 120,
               env: dict | None = None) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        combined = _redact(stdout.decode(errors="ignore") + stderr.decode(errors="ignore"))
        return proc.returncode == 0, combined
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return False, "timed out"
    except Exception as exc:
        return False, _redact(str(exc))


def _ev(type_: str, title: str, body: str, **extra) -> dict:
    return {"type": type_, "title": title, "body": body, **extra}

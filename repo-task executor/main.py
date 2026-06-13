"""
Autonomous Coding Agent — CLI & Server launcher

Usage:
  # Run the agent on a GitHub repo
  python main.py run https://github.com/owner/repo "Fix the divide by zero bug"

  # Analyze a repo (show what files the agent would touch, no edits)
  python main.py analyze https://github.com/owner/repo "Add rate limiting"

  # Start the REST API server
  python main.py serve

  # Run with options
  python main.py run <url> <task> --no-pr --no-tests --retries 5
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env before anything that reads env vars
load_dotenv(Path(__file__).parent / ".env", override=True)

import typer
import uvicorn

app = typer.Typer(
    name="coding-agent",
    help="Autonomous GitHub coding agent powered by Claude + HCT/FCG/MDG graphs.",
    add_completion=False,
)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

@app.command()
def run(
    github_url: str = typer.Argument(..., help="GitHub repo URL"),
    task: str = typer.Argument(..., help="Task to perform (e.g. 'Fix the login bug')"),
    no_pr: bool = typer.Option(False, "--no-pr", help="Commit only, don't open a PR"),
    no_tests: bool = typer.Option(False, "--no-tests", help="Skip test step"),
    retries: int = typer.Option(3, "--retries", "-r", help="Max self-healing retries"),
    top_k: int = typer.Option(15, "--top-k", "-k", help="Top-K files to send Claude"),
    workspace: str = typer.Option("./workspace", "--workspace", "-w"),
):
    """Run the full agent pipeline on a GitHub repo."""
    _check_env()

    from agent.orchestrator import AgentOrchestrator

    orchestrator = AgentOrchestrator(
        workspace_dir=workspace,
        max_retries=retries,
        top_k=top_k,
        open_pr=not no_pr,
        run_tests=not no_tests,
    )
    result = orchestrator.run(github_url, task)
    print("\n" + result.summary())
    sys.exit(0 if result.success else 1)


@app.command()
def analyze(
    github_url: str = typer.Argument(..., help="GitHub repo URL"),
    task: str = typer.Argument(..., help="Task description"),
    top_k: int = typer.Option(15, "--top-k", "-k"),
    workspace: str = typer.Option("./workspace", "--workspace", "-w"),
):
    """Clone the repo, build graphs, show what the agent would touch — no edits."""
    _check_env(require_github=False)

    from agent.cloner import RepoCloner
    from agent.reader import CodeReader
    from utils.logger import log

    log.section(f"ANALYZE: {task}")
    cloner = RepoCloner(workspace)
    cloner.clone(github_url)

    reader = CodeReader(workspace, top_k=top_k)
    ctx = reader.read(task)

    print("\n=== RELEVANT FILES ===")
    for path in ctx.relevant_files:
        risk = ctx.module_risks.get(path, {}).get("risk", "LOW")
        print(f"  [{risk:6}] {path}")

    print("\n=== IMPACT RADIUS ===")
    for path in ctx.impact_files:
        print(f"  {path}")

    print("\n=== REPO STRUCTURE ===")
    print(ctx.repo_structure)

    print(f"\n~{ctx.total_token_estimate:,} tokens would be sent to Claude")


@app.command()
def doc(
    github_url: str = typer.Argument(..., help="GitHub repo URL"),
    target_file: str = typer.Option(None, "--file", "-f", help="Specific file to document (relative path)"),
    readme: bool = typer.Option(False, "--readme", help="Generate README.md instead of docstrings"),
    no_pr: bool = typer.Option(False, "--no-pr"),
    top_k: int = typer.Option(15, "--top-k", "-k"),
    workspace: str = typer.Option("./workspace", "--workspace", "-w"),
):
    """Generate docstrings for all undocumented functions, or create a README.md."""
    _check_env()

    from agent.cloner import RepoCloner
    from agent.reader import CodeReader
    from agent.doc_agent import DocAgent
    from agent.writer import CodeWriter
    from agent.github_pr import GitHubPRAgent
    from utils.logger import log

    mode = "README" if readme else f"docstrings{' for ' + target_file if target_file else ''}"
    log.section(f"DOC — {mode} — {github_url}")

    cloner = RepoCloner(workspace)
    default_branch = cloner.get_default_branch(github_url)
    cloner.clone(github_url)

    reader = CodeReader(workspace, top_k=top_k)
    ctx = reader.read("documentation docstrings functions classes")

    agent = DocAgent()
    if readme:
        changes = agent.generate_readme(ctx, workspace)
    else:
        changes = agent.generate_docstrings(ctx, target_file=target_file)

    writer = CodeWriter(workspace)
    changed = writer.apply(changes)
    print(f"\nExplanation: {changes.explanation}")
    print(f"Changed: {changed}")

    if not no_pr and changed:
        pr_agent = GitHubPRAgent(workspace, github_url)
        branch = pr_agent.commit_and_push(
            task=f"docs: {mode}",
            changed_paths=changed,
            explanation=changes.explanation,
            branch_prefix="agent-docs",
        )
        pr_url = pr_agent.create_pr(
            branch_name=branch,
            task=f"docs: {mode}",
            explanation=changes.explanation,
            test_summary="Documentation only — no logic changed.",
            base_branch=default_branch,
        )
        print(f"\nPR: {pr_url}")


@app.command()
def generate(
    github_url: str = typer.Argument(..., help="GitHub repo URL"),
    task: str = typer.Argument(..., help="Describe the module to create, e.g. 'Create a rate limiter module'"),
    no_pr: bool = typer.Option(False, "--no-pr"),
    top_k: int = typer.Option(10, "--top-k", "-k"),
    workspace: str = typer.Option("./workspace", "--workspace", "-w"),
):
    """Generate a brand-new Python module that fits the repo's style and conventions."""
    _check_env()

    from agent.cloner import RepoCloner
    from agent.reader import CodeReader
    from agent.doc_agent import DocAgent
    from agent.writer import CodeWriter
    from agent.github_pr import GitHubPRAgent
    from utils.logger import log

    log.section(f"GENERATE MODULE — {task}")

    cloner = RepoCloner(workspace)
    default_branch = cloner.get_default_branch(github_url)
    cloner.clone(github_url)

    reader = CodeReader(workspace, top_k=top_k)
    ctx = reader.read(task)

    agent = DocAgent()
    changes = agent.generate_module(task, ctx)

    print(f"\nExplanation: {changes.explanation}")
    print(f"New files: {list(changes.new_files.keys())}")

    writer = CodeWriter(workspace)
    changed = writer.apply(changes)

    if not no_pr and changed:
        pr_agent = GitHubPRAgent(workspace, github_url)
        branch = pr_agent.commit_and_push(
            task=f"feat: {task[:60]}",
            changed_paths=changed,
            explanation=changes.explanation,
            branch_prefix="agent-generate",
        )
        pr_url = pr_agent.create_pr(
            branch_name=branch,
            task=task,
            explanation=changes.explanation,
            test_summary="New module — review before merging.",
            base_branch=default_branch,
        )
        print(f"\nPR: {pr_url}")


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
):
    """Start the FastAPI REST server."""
    uvicorn.run(
        "api.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_env(require_github: bool = True):
    missing = []
    if not os.getenv("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if require_github and not os.getenv("GITHUB_TOKEN"):
        typer.echo("Warning: GITHUB_TOKEN not set — cannot push PRs", err=True)
    if missing:
        typer.echo(f"Error: missing env vars: {', '.join(missing)}", err=True)
        typer.echo("Copy .env.example → .env and fill in your keys.", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

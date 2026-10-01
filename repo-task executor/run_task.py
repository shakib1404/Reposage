"""
Helper entry-point used by the RepoSage backend to run the agent pipeline
and stream all output to stdout line-by-line.

Usage:
    python run_task.py <github_url> <task> [workspace_dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repo-task executor package is on sys.path
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from dotenv import load_dotenv
load_dotenv(_HERE / ".env", override=True)

if len(sys.argv) < 3:
    print("Usage: run_task.py <github_url> <task> [workspace_dir]", file=sys.stderr)
    sys.exit(2)

github_url    = sys.argv[1]
task          = sys.argv[2]
workspace_dir = sys.argv[3] if len(sys.argv) > 3 else str(_HERE / "workspace")

from agent.orchestrator import AgentOrchestrator

# Open a pull request rather than only pushing the branch. create_pr() has
# always existed; this entry point just had it switched off, so a finished run
# left the fix sitting on a branch nobody was told about.
# Set REPO_TASK_OPEN_PR=0 to go back to push-only.
import os
open_pr = os.getenv("REPO_TASK_OPEN_PR", "1").strip().lower() not in ("0", "false", "no")

orchestrator = AgentOrchestrator(
    workspace_dir=workspace_dir,
    max_retries=3,
    top_k=15,
    open_pr=open_pr,
    run_tests=True,
)

result = orchestrator.run(github_url, task)

print("")
print(result.summary())

sys.exit(0 if result.success else 1)

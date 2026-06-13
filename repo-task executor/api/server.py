"""
FastAPI REST Server

Endpoints:
  POST /run           — start a full agent job (clone → fix → test → PR)
  POST /analyze       — build graphs and return context only (no edits)
  GET  /health        — health check
  GET  /job/{job_id}  — get status of a background job (in-memory)

Jobs run in a background thread so the HTTP response returns immediately.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl, field_validator

from agent.orchestrator import AgentOrchestrator, AgentResult
from agent.cloner import RepoCloner
from agent.reader import CodeReader


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Autonomous Coding Agent",
    description="Give it a GitHub repo + task — it writes the fix and opens a PR.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store (replace with Redis/DB for production)
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    github_url: str
    task: str
    open_pr: bool = True
    run_tests: bool = True
    max_retries: int = 3
    top_k: int = 15

    @field_validator("github_url")
    @classmethod
    def validate_github(cls, v: str) -> str:
        if "github.com" not in v:
            raise ValueError("Only GitHub URLs are supported")
        return v.strip()


class AnalyzeRequest(BaseModel):
    github_url: str
    task: str
    top_k: int = 15


class JobStatus(BaseModel):
    job_id: str
    status: str          # pending | running | done | failed
    created_at: float
    finished_at: float | None = None
    result: dict | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY"))}


@app.post("/run", response_model=JobStatus)
def run_agent(req: RunRequest, background_tasks: BackgroundTasks):
    """
    Start an agent job in the background. Returns a job_id to poll.
    """
    job_id = str(uuid.uuid4())[:8]
    now = time.time()

    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "created_at": now,
            "finished_at": None,
            "result": None,
        }

    background_tasks.add_task(_run_job, job_id, req)

    return JobStatus(job_id=job_id, status="pending", created_at=now)


@app.get("/job/{job_id}", response_model=JobStatus)
def get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return JobStatus(**job)


@app.post("/analyze")
def analyze_repo(req: AnalyzeRequest):
    """
    Clone the repo and return the graph-based context without making edits.
    Useful for previewing what files the agent would touch.
    """
    workspace = f"./workspace_{req.github_url.split('/')[-1]}"
    try:
        cloner = RepoCloner(workspace)
        cloner.clone(req.github_url)

        reader = CodeReader(workspace, top_k=req.top_k)
        ctx = reader.read(req.task)

        return {
            "task": req.task,
            "relevant_files": list(ctx.relevant_files.keys()),
            "impact_files": list(ctx.impact_files.keys()),
            "module_risks": {
                k: {"risk": v["risk"], "pagerank": round(v["pagerank"], 6)}
                for k, v in ctx.module_risks.items()
            },
            "test_targets": ctx.test_targets,
            "token_estimate": ctx.total_token_estimate,
            "repo_structure": ctx.repo_structure,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/jobs")
def list_jobs():
    with _jobs_lock:
        return list(_jobs.values())


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

def _run_job(job_id: str, req: RunRequest):
    with _jobs_lock:
        _jobs[job_id]["status"] = "running"

    workspace = f"./workspace_{job_id}"
    try:
        orchestrator = AgentOrchestrator(
            workspace_dir=workspace,
            max_retries=req.max_retries,
            top_k=req.top_k,
            open_pr=req.open_pr,
            run_tests=req.run_tests,
        )
        result: AgentResult = orchestrator.run(req.github_url, req.task)

        with _jobs_lock:
            _jobs[job_id].update({
                "status": "done" if result.success else "failed",
                "finished_at": time.time(),
                "result": {
                    "success": result.success,
                    "pr_url": result.pr_url,
                    "branch_name": result.branch_name,
                    "explanation": result.explanation,
                    "attempts": result.attempts,
                    "changed_files": result.changed_files,
                    "duration_seconds": result.duration_seconds,
                    "error": result.error,
                    "test_passed": result.test_result.passed if result.test_result else None,
                    "test_output": (
                        result.test_result.short_summary() if result.test_result else ""
                    ),
                },
            })
    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id].update({
                "status": "failed",
                "finished_at": time.time(),
                "result": {"error": str(exc)},
            })

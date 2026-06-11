"""RepoSage backend — FastAPI"""
import asyncio
import json
import mimetypes
import os
import shutil
import uuid
from pathlib import Path
from typing import AsyncGenerator, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, EmailStr

load_dotenv()

from search import search_repos, fetch_readme
from analyzer import analyze_repo
from executor import run_execution_loop, OUTPUT_ROOT, CREDENTIAL_STORE, CREDENTIAL_EVENTS
from tester import run_test_loop
from auth import (
    create_user, authenticate_user,
    generate_reset_token, reset_password,
    create_access_token, get_current_user, get_optional_user,
    send_reset_email,
)
from history_db import (
    history_create, history_update,
    history_list, history_get, history_delete,
)

app = FastAPI(title="RepoSage API")


@app.on_event("startup")
async def warm_db():
    """Ping MongoDB on startup so the first user request is fast."""
    try:
        from auth import get_db
        db = get_db()
        await asyncio.wait_for(db.command("ping"), timeout=10)
    except Exception:
        pass  # non-fatal — app still starts


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Auth models & routes
# ═══════════════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    username: str
    email:    str
    password: str

class LoginRequest(BaseModel):
    email:    str
    password: str

class ForgotRequest(BaseModel):
    email: str

class ResetRequest(BaseModel):
    token:        str
    new_password: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if len(req.username.strip()) < 2:
        raise HTTPException(400, "Username must be at least 2 characters")
    try:
        user  = await create_user(req.username.strip(), req.email.strip(), req.password)
        token = create_access_token(user["_id"])
        return {
            "token": token,
            "user":  {"id": user["_id"], "username": user["username"], "email": user["email"]},
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Registration failed: {e}")


@app.post("/api/auth/login")
async def login(req: LoginRequest):
    try:
        user  = await authenticate_user(req.email.strip(), req.password)
        token = create_access_token(user["_id"])
        return {
            "token": token,
            "user":  {"id": user["_id"], "username": user["username"], "email": user["email"]},
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Login failed: {e}")


@app.post("/api/auth/forgot-password")
async def forgot_password(req: ForgotRequest, bg: BackgroundTasks):
    token = await generate_reset_token(req.email)
    if token:
        # Send email in background so response is instant
        bg.add_task(send_reset_email, req.email, token)
    # Always return 200 — don't reveal whether email exists
    return {"message": "If that email is registered, a reset link has been sent."}


@app.post("/api/auth/reset-password")
async def do_reset_password(req: ResetRequest):
    await reset_password(req.token, req.new_password)
    return {"message": "Password updated successfully"}


@app.get("/api/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return {"id": user["_id"], "username": user["username"], "email": user["email"]}


# ═══════════════════════════════════════════════════════════════════════════════
#  History routes
# ═══════════════════════════════════════════════════════════════════════════════

class HistoryCreateRequest(BaseModel):
    task:  str
    repos: list = []

class HistoryUpdateRequest(BaseModel):
    selected_repo: Optional[dict] = None
    analysis:      Optional[dict] = None
    execution:     Optional[dict] = None
    audit:         Optional[dict] = None
    job_id:        Optional[str]  = None
    status:        Optional[str]  = None


@app.get("/api/history")
async def list_history(user: dict = Depends(get_current_user)):
    entries = await history_list(user["_id"])
    return {"history": entries}


@app.get("/api/history/{history_id}")
async def get_history_entry(history_id: str, user: dict = Depends(get_current_user)):
    entry = await history_get(history_id, user["_id"])
    if not entry:
        raise HTTPException(404, "History entry not found")
    return entry


@app.post("/api/history")
async def create_history_entry(req: HistoryCreateRequest,
                                user: dict = Depends(get_current_user)):
    hid = await history_create(user["_id"], req.task, req.repos)
    return {"history_id": hid}


@app.patch("/api/history/{history_id}")
async def update_history_entry(history_id: str, req: HistoryUpdateRequest,
                                user: dict = Depends(get_current_user)):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    await history_update(history_id, user["_id"], **fields)
    return {"status": "updated"}


@app.delete("/api/history/{history_id}")
async def delete_history_entry(history_id: str, user: dict = Depends(get_current_user)):
    ok = await history_delete(history_id, user["_id"])
    if not ok:
        raise HTTPException(404, "History entry not found")
    return {"status": "deleted"}


# ═══════════════════════════════════════════════════════════════════════════════
#  Existing routes  (auth optional — works with or without JWT)
# ═══════════════════════════════════════════════════════════════════════════════

class SearchRequest(BaseModel):
    task: str

class SelectRequest(BaseModel):
    task:             str
    repo_full_name:   str
    repo_description: str

class ExecuteRequest(BaseModel):
    task:          str
    repo_full_name: str
    analysis:      dict
    job_id:        str  = ""
    input_files:   list[str] = []

class TestRequest(BaseModel):
    repo_full_name: str
    job_id:         str = ""

class CredentialSubmit(BaseModel):
    credentials: dict[str, str]

# ── File upload ──────────────────────────────────────────────────────────────
@app.post("/api/upload")
async def upload_files(
    task:  str          = Form(""),
    files: list[UploadFile] = File(default=[]),
    user:  Optional[dict]   = Depends(get_optional_user),
):
    job_id     = uuid.uuid4().hex
    upload_dir = Path(OUTPUT_ROOT) / job_id / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    uploaded: list[str] = []
    for f in files:
        if not f.filename:
            continue
        safe_name = Path(f.filename).name.replace("..", "_")
        dest      = upload_dir / safe_name
        content   = await f.read()
        if len(content) > 500 * 1024 * 1024:
            continue
        dest.write_bytes(content)
        uploaded.append(safe_name)

    return {"job_id": job_id, "uploaded_files": uploaded}


# ── Search ───────────────────────────────────────────────────────────────────
@app.post("/api/search")
async def search(req: SearchRequest,
                 user: Optional[dict] = Depends(get_optional_user)):
    try:
        repos = await search_repos(req.task)
        return {"repos": repos}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Analyze ──────────────────────────────────────────────────────────────────
@app.post("/api/analyze")
async def analyze(req: SelectRequest,
                  user: Optional[dict] = Depends(get_optional_user)):
    try:
        readme = await fetch_readme(req.repo_full_name)
        result = await analyze_repo(req.repo_full_name, readme, req.task)
        return result
    except HTTPException:
        raise
    except BaseException as e:          # catches SystemExit from repo setup.py
        raise HTTPException(500, str(e) or type(e).__name__)


# ── Execute (SSE) ────────────────────────────────────────────────────────────
@app.post("/api/execute")
async def execute(req: ExecuteRequest,
                  user: Optional[dict] = Depends(get_optional_user)):
    job_id = req.job_id or uuid.uuid4().hex

    async def event_stream() -> AsyncGenerator[str, None]:
        async for event in run_execution_loop(
            req.task, req.repo_full_name, req.analysis,
            job_id=job_id, input_files=req.input_files or None,
        ):
            yield f"data: {json.dumps(event)}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Output files ─────────────────────────────────────────────────────────────
@app.get("/api/outputs/{job_id}")
async def list_outputs(job_id: str):
    job_dir = Path(OUTPUT_ROOT) / job_id
    if not job_dir.is_dir():
        raise HTTPException(404, "Job not found")
    files = []
    for f in sorted(job_dir.iterdir()):
        if f.is_file() and f.name != ".gitkeep":
            files.append({"name": f.name, "size_bytes": f.stat().st_size,
                          "url": f"/api/outputs/{job_id}/{f.name}"})
    return {"job_id": job_id, "files": files}


@app.get("/api/outputs/{job_id}/{filename:path}")
async def download_output(job_id: str, filename: str):
    safe_name = Path(filename).name
    file_path = Path(OUTPUT_ROOT) / job_id / safe_name
    if not file_path.is_file():
        raise HTTPException(404, "File not found")
    mime, _ = mimetypes.guess_type(str(file_path))
    if safe_name.endswith((".csv", ".txt", ".log", ".md")):
        mime = "text/plain; charset=utf-8"
    return FileResponse(str(file_path), filename=safe_name,
                        media_type=mime or "application/octet-stream")


@app.delete("/api/outputs/{job_id}")
async def delete_outputs(job_id: str):
    job_dir = Path(OUTPUT_ROOT) / job_id
    if not job_dir.is_dir():
        raise HTTPException(404, "Job not found")
    shutil.rmtree(str(job_dir), ignore_errors=True)
    return {"status": "deleted", "job_id": job_id}


# ── Credentials ──────────────────────────────────────────────────────────────
@app.post("/api/credentials/{job_id}")
async def submit_credentials(job_id: str, req: CredentialSubmit):
    CREDENTIAL_STORE[job_id] = req.credentials
    ev = CREDENTIAL_EVENTS.get(job_id)
    if ev:
        ev.set()
    return {"status": "ok", "job_id": job_id}


# ── Test / Audit (SSE) ───────────────────────────────────────────────────────
@app.post("/api/test")
async def test_repo(req: TestRequest,
                    user: Optional[dict] = Depends(get_optional_user)):
    job_id = req.job_id or ""

    async def event_stream() -> AsyncGenerator[str, None]:
        async for event in run_test_loop(req.repo_full_name, job_id=job_id):
            yield f"data: {json.dumps(event)}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

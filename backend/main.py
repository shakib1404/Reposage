"""RepoSage backend — FastAPI"""
import asyncio
import json
import mimetypes
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import AsyncGenerator, Optional

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, EmailStr

# Load project-root .env first (has MONGO_URL, JWT_SECRET, GROQ keys …)
# then backend/.env for any backend-specific overrides.
_HERE = Path(__file__).parent
load_dotenv(_HERE.parent / ".env")
load_dotenv(_HERE / ".env", override=True)

from search import search_repos, fetch_readme
from analyzer import analyze_repo
from architect import generate_architecture
from chat import answer_question
from rag import build_index, index_info
from executor import run_execution_loop, OUTPUT_ROOT, CREDENTIAL_STORE, CREDENTIAL_EVENTS
from tester import run_test_loop
from autofix import run_autofix_loop
from copydetector.detector import VenDetector, Detection, Source, Status
from copydetector.repo import Repository, File as RepoFile
from copydetector.errors import VendetectError, VendetectRuntimeError
from dupdetect import find_semantic_duplicates, DEFAULT_THRESHOLD as DUP_DEFAULT_THRESHOLD
from corpus import (
    add_to_corpus, search_corpus, corpus_status,
    DEFAULT_MATCH_THRESHOLD as CORPUS_DEFAULT_THRESHOLD,
)
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
    """Ping MongoDB and pre-load bcrypt on startup so first login is instant."""
    import asyncio as _asyncio

    # 1. Warm bcrypt — pre-jit the C extension in the thread pool
    async def _warm_bcrypt():
        try:
            from auth import hash_password
            await hash_password("warmup")
        except Exception:
            pass

    # 2. Warm MongoDB connection
    async def _warm_mongo():
        try:
            from auth import get_db
            db = get_db()
            await _asyncio.wait_for(db.command("ping"), timeout=8)
        except Exception:
            pass

    # 3. Warm the search reranker — loading the cross-encoder takes ~9s, and
    #    paying that inside the first user's search made it feel broken.
    #    Runs in a thread so it never blocks startup or the event loop.
    async def _warm_reranker():
        try:
            from search import warm_models
            await _asyncio.to_thread(warm_models)
        except Exception:
            pass

    await _asyncio.gather(_warm_bcrypt(), _warm_mongo(), _warm_reranker())


# In the Docker deployment Caddy serves the SPA and proxies /api to this app,
# so browser requests are same-origin and CORS never comes into play. These
# defaults are the local Vite dev server; CORS_ALLOW_ORIGINS (comma-separated)
# overrides them for any deployment that does split the two across origins,
# without needing a code change.
_CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ALLOW_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
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
    task:  str
    limit: int = 9

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

class AutofixRequest(BaseModel):
    repo_full_name: str
    job_id:         str = ""

class TaskExecRequest(BaseModel):
    task:           str
    repo_full_name: str
    job_id:         str = ""
    input_file:     str = ""  # filename only; resolved to full path on server

class ArchitectRequest(BaseModel):
    repo_full_name: str

class RagBuildRequest(BaseModel):
    repo_full_name: str

class ChatMessage(BaseModel):
    role:    str   # "user" | "assistant"
    content: str

class ChatRequest(BaseModel):
    repo_full_name: str
    messages:       list[ChatMessage]
    analysis:       Optional[dict] = None

class CredentialSubmit(BaseModel):
    credentials: dict[str, str]

class CopyDetectRequest(BaseModel):
    mode:           str = "cross_repo"   # "cross_repo" | "self_scan"
    test_repo:      str
    source_repo:    str = ""             # unused in self_scan mode
    min_similarity: float = 0.5          # cross_repo: token-overlap threshold
    dup_threshold:  float = DUP_DEFAULT_THRESHOLD   # self_scan: embedding cosine-similarity threshold
    dup_scope:      str = "scoped"       # self_scan: "scoped" (same class/module) | "repo_wide" (everything)
    file_types:     list[str] = []

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
        # search_repos returns {"repos": [...], "suggestions": [...], "query": ...}
        return await search_repos(req.task, limit=req.limit)
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


# ── Codebase chatbot ─────────────────────────────────────────────────────────
@app.post("/api/chat")
async def chat(req: ChatRequest,
               user: Optional[dict] = Depends(get_optional_user)):
    try:
        msgs = [{"role": m.role, "content": m.content} for m in req.messages]
        answer = await answer_question(req.repo_full_name, msgs, req.analysis)
        return {"answer": answer}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── RAG: build index (SSE) ───────────────────────────────────────────────────
@app.post("/api/rag/build")
async def rag_build(req: RagBuildRequest,
                    user: Optional[dict] = Depends(get_optional_user)):
    async def event_stream() -> AsyncGenerator[str, None]:
        async for event in build_index(req.repo_full_name):
            yield f"data: {json.dumps(event)}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/rag/status/{repo_owner}/{repo_name}")
async def rag_status(repo_owner: str, repo_name: str,
                     user: Optional[dict] = Depends(get_optional_user)):
    repo = f"{repo_owner}/{repo_name}"
    return index_info(repo)


# ── Architecture diagram (SSE) ───────────────────────────────────────────────
@app.post("/api/architect")
async def architect(req: ArchitectRequest,
                    user: Optional[dict] = Depends(get_optional_user)):
    async def event_stream() -> AsyncGenerator[str, None]:
        async for event in generate_architecture(req.repo_full_name):
            yield f"data: {json.dumps(event)}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


# ── RepoTask Executor (SSE) — uses repo-task executor agent pipeline ──────────

# Where the repo-task executor package lives.
#
# On a dev checkout it is a sibling of backend/ (reposage/repo-task executor).
# In the container backend/ IS the root (/app), so "parent.parent" resolved to
# "/" and every run died with
#     can't open file '/repo-task executor/run_task.py'
# — the package is copied to /opt/repo-task-executor there instead. Resolve by
# looking, and let REPO_TASK_DIR override for anything else.
def _find_repo_task_dir() -> Path:
    env = os.getenv("REPO_TASK_DIR", "").strip()
    candidates = [Path(env)] if env else []
    candidates += [
        Path(__file__).parent.parent / "repo-task executor",   # dev checkout
        Path("/opt/repo-task-executor"),                       # container image
    ]
    for c in candidates:
        if (c / "run_task.py").is_file():
            return c
    # Nothing found — return the first candidate so the error message names a
    # path the operator can actually act on.
    return candidates[0]


_REPO_TASK_DIR   = _find_repo_task_dir()
_RUN_TASK_SCRIPT = _REPO_TASK_DIR / "run_task.py"

# Workspaces are cloned repos plus their edits — they must go somewhere
# writable, which the image's /opt tree is not meant to be.
_REPO_TASK_WORKSPACES = Path(
    os.getenv("REPO_TASK_WORKSPACE_ROOT")
    or (os.getenv("EXECUTOR_OUTPUT_ROOT", "") and
        os.path.join(os.environ["EXECUTOR_OUTPUT_ROOT"], "taskexec"))
    or str(_REPO_TASK_DIR / "workspace")
)


# Anything the agent subprocess prints goes straight to the browser. git
# embeds the push token in its error messages, so a failed push once rendered
# a live ghp_ token in the UI. Redact on the way out, regardless of which
# subcomponent leaked it.
_SECRET_PATTERNS = [
    re.compile(r"(gh[pousr]_)[A-Za-z0-9]{16,}"),
    re.compile(r"(github_pat_)[A-Za-z0-9_]{20,}"),
    re.compile(r"(sk-ant-)[A-Za-z0-9\-_]{20,}"),
    re.compile(r"(https?://)[^/\s:@]+(?::[^/\s@]+)?@"),
]
_SECRET_ENV_VARS = ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "JWT_SECRET",
                    "SERPER_API_KEY", "JINA_API_KEY", "GMAIL_PASS")


def _scrub_secrets(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(r"\1<redacted>", text)
    for var in _SECRET_ENV_VARS:
        val = os.getenv(var, "")
        if val and len(val) > 8:
            text = text.replace(val, "<redacted>")
    return text


@app.post("/api/taskexec")
async def task_exec(req: TaskExecRequest,
                    user: Optional[dict] = Depends(get_optional_user)):

    async def event_stream() -> AsyncGenerator[str, None]:
        def _out(line: str) -> str:
            return f"data: {json.dumps({'type': 'output', 'line': _scrub_secrets(line)})}\n\n"

        github_url = f"https://github.com/{req.repo_full_name}"
        job_id     = req.job_id or uuid.uuid4().hex
        workspace  = str(_REPO_TASK_WORKSPACES / job_id)

        yield _out(f"🚀  RepoTask Executor — {req.repo_full_name}")
        yield _out(f"📋  Task: {req.task}")
        yield _out("")
        await asyncio.sleep(0)

        # Fail with something actionable. Without this the subprocess just
        # prints Python's raw "can't open file ..." to the log and the run
        # looks like an agent failure rather than a missing deployment piece.
        if not _RUN_TASK_SCRIPT.is_file():
            yield _out(f"✕  RepoTask Executor is not installed at {_REPO_TASK_DIR}")
            yield _out("   The agent package was not found. Set REPO_TASK_DIR to its")
            yield _out("   location, or rebuild the backend image so it is copied in.")
            yield f"data: {json.dumps({'type': 'done', 'returncode': 1})}\n\n"
            return

        if not os.getenv("ANTHROPIC_API_KEY") and not (_REPO_TASK_DIR / ".env").is_file():
            yield _out("✕  ANTHROPIC_API_KEY is not set.")
            yield _out("   RepoTask Executor drives Claude directly (not Groq like the")
            yield _out("   rest of RepoSage), so it needs its own key in the backend env.")
            yield f"data: {json.dumps({'type': 'done', 'returncode': 1})}\n\n"
            return

        try:
            os.makedirs(workspace, exist_ok=True)
            # Inherit current env + keys from repo-task executor .env
            env = os.environ.copy()
            rte_env = _REPO_TASK_DIR / ".env"
            if rte_env.is_file():
                for raw in rte_env.read_text().splitlines():
                    raw = raw.strip()
                    if raw and not raw.startswith("#") and "=" in raw:
                        k, _, v = raw.partition("=")
                        env.setdefault(k.strip(), v.strip())

            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(_RUN_TASK_SCRIPT),
                github_url, req.task, workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )

            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    yield _out(line)
                await asyncio.sleep(0)

            rc = await proc.wait()
            yield f"data: {json.dumps({'type': 'done', 'returncode': rc})}\n\n"

        except Exception as exc:
            yield _out(f"✕  Unexpected error: {exc}")
            yield f"data: {json.dumps({'type': 'done', 'returncode': 1})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Copy Detector (SSE) — powered by vendetect ───────────────────────────────

_CD_SENTINEL = object()


class _SSEStatus(Status):
    """Forward VenDetector progress callbacks to an asyncio queue."""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self._q = queue
        self._loop = loop
        self._total = 0
        self._done = 0

    def _send(self, ev: dict) -> None:
        self._loop.call_soon_threadsafe(self._q.put_nowait, ev)

    def update_num_comparisons(self, num: int) -> None:
        self._total = num
        self._send({"type": "status", "message": f"Comparing {num} file pair(s)…"})

    def update_compare_progress(self, file: RepoFile | None = None) -> None:
        self._done += 1
        ev: dict = {"type": "progress", "current": self._done, "total": self._total}
        if file is not None:
            ev["file"] = str(file.relative_path)
        self._send(ev)


def _detection_to_dict(det: Detection, min_similarity: float) -> dict | None:
    """Convert a vendetect Detection to a JSON-serialisable dict with code snippets."""
    avg = (det.comparison.similarity1 + det.comparison.similarity2) / 2
    if avg < min_similarity:
        return None

    test_src = det.test_source  # Source(det.test, det.comparison.slices1)
    slices = []

    for s1, s2 in zip(det.comparison.slices1, det.comparison.slices2):
        # Convert byte offsets → 0-indexed line numbers (vendetect convention)
        try:
            ls1 = test_src.byte_offset_slice_to_lines_slice(s1)
            t_from = int(ls1.from_index) + 1   # display as 1-indexed
            t_to   = int(ls1.to_index) + 1
        except Exception:
            t_from = t_to = 0

        try:
            src_obj = Source(det.source, (s2,))
            ls2 = src_obj.byte_offset_slice_to_lines_slice(s2)
            s_from = int(ls2.from_index) + 1
            s_to   = int(ls2.to_index) + 1
        except Exception:
            s_from = s_to = 0

        test_code = src_code = ""
        try:
            with det.test.repo:
                lines = det.test.path.read_text(errors="replace").splitlines()
                test_code = "\n".join(lines[max(0, t_from - 1):t_to])[:3000]
        except Exception:
            pass
        try:
            with det.source.repo:
                lines = det.source.path.read_text(errors="replace").splitlines()
                src_code = "\n".join(lines[max(0, s_from - 1):s_to])[:3000]
        except Exception:
            pass

        slices.append({
            "test_lines":   [int(t_from), int(t_to)],
            "source_lines": [int(s_from), int(s_to)],
            "test_code":    test_code,
            "source_code":  src_code,
        })

    return {
        "type":              "detection",
        "test_file":         str(det.test.relative_path),
        "source_file":       str(det.source.relative_path),
        "similarity":        float(round(avg, 4)),
        "similarity_test":   float(round(det.comparison.similarity1, 4)),
        "similarity_source": float(round(det.comparison.similarity2, 4)),
        "token_overlap":     int(det.comparison.token_overlap),
        "slices":            slices,
    }


@app.post("/api/copydetect")
async def copy_detect(req: CopyDetectRequest,
                      user: Optional[dict] = Depends(get_optional_user)):

    async def event_stream() -> AsyncGenerator[str, None]:
        # Self-scan: one repo, find semantically duplicated functions within
        # the same class/module — natively async, no thread/queue needed.
        if req.mode == "self_scan":
            async for ev in find_semantic_duplicates(req.test_repo, threshold=req.dup_threshold, scope=req.dup_scope):
                yield f"data: {json.dumps(ev)}\n\n"
                await asyncio.sleep(0)
            return

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()
        min_sim = req.min_similarity
        file_types = req.file_types or []

        def _run() -> None:
            try:
                with (
                    Repository.load(req.test_repo) as test_repo,
                    Repository.load(req.source_repo) as source_repo,
                ):
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {"type": "status", "message": "Repos loaded. Starting detection…"},
                    )

                    if file_types:
                        suffixes = {"." + t.lstrip(".") for t in file_types}
                        def file_filter(f: RepoFile) -> bool:
                            return f.relative_path.suffix in suffixes
                    else:
                        def file_filter(f: RepoFile) -> bool:  # type: ignore[misc]
                            return True

                    status = _SSEStatus(queue, loop)
                    vend = VenDetector(status=status)
                    count = 0

                    for det in vend.detect(test_repo, source_repo, file_filter=file_filter):
                        det_dict = _detection_to_dict(det, min_sim)
                        if det_dict:
                            count += 1
                            loop.call_soon_threadsafe(queue.put_nowait, det_dict)

                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {"type": "done", "total_detections": count},
                    )

            except VendetectRuntimeError as e:
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"type": "error", "message": str(e)}
                )
            except Exception as e:
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"type": "error", "message": str(e)}
                )
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _CD_SENTINEL)

        # Announce remote clones immediately before starting the thread
        if req.test_repo.startswith(("http://", "https://", "git@")):
            yield f"data: {json.dumps({'type': 'status', 'message': f'Cloning test repo: {req.test_repo}…'})}\n\n"
        if req.source_repo.startswith(("http://", "https://", "git@")):
            yield f"data: {json.dumps({'type': 'status', 'message': f'Cloning source repo: {req.source_repo}…'})}\n\n"

        future = loop.run_in_executor(None, _run)

        while True:
            item = await queue.get()
            if item is _CD_SENTINEL:
                break
            yield f"data: {json.dumps(item)}\n\n"
            await asyncio.sleep(0)

        await future

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Corpus similarity search (3rd Copy Detector mode) ───────────────────────────

class CorpusAddRequest(BaseModel):
    repo: str

class CorpusSearchRequest(BaseModel):
    repo:           str
    match_threshold: float = CORPUS_DEFAULT_THRESHOLD
    top_k:          int = 5


@app.get("/api/corpus/status")
async def corpus_status_route(user: Optional[dict] = Depends(get_optional_user)):
    return corpus_status()


@app.post("/api/corpus/add")
async def corpus_add_route(req: CorpusAddRequest,
                           user: Optional[dict] = Depends(get_optional_user)):
    async def event_stream() -> AsyncGenerator[str, None]:
        async for ev in add_to_corpus(req.repo):
            yield f"data: {json.dumps(ev)}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/corpus/search")
async def corpus_search_route(req: CorpusSearchRequest,
                              user: Optional[dict] = Depends(get_optional_user)):
    async def event_stream() -> AsyncGenerator[str, None]:
        async for ev in search_corpus(req.repo, threshold=req.match_threshold, top_k=req.top_k):
            yield f"data: {json.dumps(ev)}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


# ── Semgrep autofix → pull request (SSE) ─────────────────────────────────────
@app.post("/api/autofix")
async def autofix_repo(req: AutofixRequest,
                       user: Optional[dict] = Depends(get_optional_user)):
    job_id = req.job_id or ""

    async def event_stream() -> AsyncGenerator[str, None]:
        async for event in run_autofix_loop(req.repo_full_name, job_id=job_id):
            yield f"data: {json.dumps(event)}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

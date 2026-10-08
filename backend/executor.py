"""
executor.py — Autonomous Python Project Execution Engine v4
============================================================
Robust, self-healing runner for any Python GitHub repository.

PIPELINE:
  1.  Clone repo (shallow, with timeout)
  2.  Inspect workspace — tree, README, setup files, entry candidates
  3.  Deep README parse — extract exact ordered commands
  4.  Read top-10 key source files (real content, not inferred)
  5.  Detect package manager → create venv → install deps
  6.  Discover env-var hints (.env.example / .env.sample)
  7.  LLM comprehensive plan (full context: tree + file content + README)
       ↳ Decides runnable/not-runnable with DETAILED reasons
  8.  NOT-RUNNABLE gate — explains exactly why + what user must do
  9.  Credential gate — pauses for missing API keys, times out gracefully
  10. Apply env vars / extra deps / pre-run steps
  11. Build exact run command (README → LLM → heuristic, in priority order)
  12. Execution loop:
        run → classify error → autofix (no LLM) → LLM diagnose → retry
        Max 7 attempts.  Fix journal prevents duplicate fixes.
  13. Capture output files
  14. Generate complete copyable bash script (always, even on failure)
  15. Final report: success summary OR detailed manual guide with root cause
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

import httpx

from llm import chat as llm_chat

# ─────────────────────────────────────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────────────────────────────────────

CREDENTIAL_STORE:  dict[str, dict]          = {}
CREDENTIAL_EVENTS: dict[str, asyncio.Event] = {}
CREDENTIAL_TIMEOUT = 300          # seconds to wait for user credentials

MAX_RETRIES    = int(os.getenv("EXECUTOR_MAX_RETRIES",   "7"))
SCRIPT_TIMEOUT = int(os.getenv("EXECUTOR_TIMEOUT",       "180"))
TOKEN_BUDGET   = int(os.getenv("EXECUTOR_TOKEN_BUDGET",  "60000"))
# How long a detected server (FastAPI/Django/Flask/...) must stay up before
# we treat it as a confirmed success and close it ourselves — servers never
# exit 0 on their own, so without this we'd block for the full SCRIPT_TIMEOUT
# on every single attempt.
SERVER_CONFIRM_GRACE = int(os.getenv("EXECUTOR_SERVER_GRACE", "10"))
_RUN_POLL_INTERVAL   = 1.0
LOG_LEVEL      = os.getenv("EXECUTOR_LOG_LEVEL",         "INFO")
OUTPUT_ROOT    = os.getenv(
    "EXECUTOR_OUTPUT_ROOT",
    os.path.join(os.path.expanduser("~"), ".repomaster", "outputs"),
)

MAX_FILE_SIZE   = 500 * 1024 * 1024   # 500 MB
TOP_FILES_N     = 10
FILE_READ_CHARS = 2500

OUTPUT_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg",
    ".csv", ".json", ".jsonl", ".xml",
    ".txt", ".md", ".log", ".html", ".pdf",
    ".pt", ".pth", ".onnx", ".h5", ".pkl", ".joblib",
    ".mp4", ".avi", ".mov", ".webm",
    ".wav", ".mp3", ".flac", ".ogg",
    ".npy", ".npz", ".parquet",
}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("executor")

# Package managers in priority order (first match wins)
PKG_MANAGERS = [
    ("uv",         "uv.lock",           ["uv", "sync"]),
    ("poetry",     "poetry.lock",       ["poetry", "install", "--no-interaction"]),
    ("pipenv",     "Pipfile.lock",      ["pipenv", "install", "--skip-lock"]),
    ("conda",      "environment.yml",   ["conda", "env", "update", "-f", "environment.yml"]),
    ("pip-req",    "requirements.txt",  None),
    ("pip-setup",  "setup.py",          None),
    ("pip-pyproj", "pyproject.toml",    None),
]

# Patterns that indicate a web server or UI frontend launched successfully.
# Processes like Flask/Streamlit/Gradio run forever and get killed by timeout,
# so we check output to decide they actually succeeded before being terminated.
_UI_SERVER_STARTED = re.compile(
    r"Running on http://|"                     # Flask
    r"Uvicorn running on|"                     # FastAPI / uvicorn
    r"Application startup complete|"           # uvicorn / starlette
    r"You can now view your Streamlit app|"    # Streamlit
    r"Running on local URL:|"                  # Gradio
    r"Dash is running on|"                     # Dash
    r"\* Running on|"                          # Flask dev server
    r"Started server process|"                 # uvicorn log
    r"Serving Flask app|"                      # Flask
    r"Notebook server is running|"             # Jupyter
    r"To access the notebook|"                 # Jupyter
    r"http://localhost:\d+|"                   # any localhost URL
    r"http://127\.0\.0\.1:\d+",               # any 127.0.0.1 URL
    re.IGNORECASE,
)

# Desktop GUI toolkits. A program built on one of these behaves like a server
# and nothing like a script: it opens a window, spins an event loop and never
# exits on its own, so waiting for an exit code means waiting for the timeout
# and then calling a perfectly healthy run a failure. Reported by a user whose
# maze and qrcode repos both "ran" and were marked failed.
_GUI_TOOLKITS = re.compile(
    r"^\s*(?:import|from)\s+(tkinter|Tkinter|pygame|PyQt5|PyQt6|PySide2|PySide6"
    r"|kivy|wx|pyglet|arcade|customtkinter|ttkbootstrap|turtle)\b",
    re.MULTILINE,
)

# Evidence in the output that a GUI toolkit actually came up.
_GUI_STARTED = re.compile(
    r"pygame community|"                       # pygame's banner
    r"Hello from the pygame community|"
    r"initialising display|"
    r"Initializing (?:display|window)",
    re.IGNORECASE,
)

# Regex patterns for automatic error classification
AUTOFIX_PATTERNS: dict[str, re.Pattern] = {
    "missing_module": re.compile(
        r"ModuleNotFoundError[^\n]*['\"]([a-zA-Z0-9_\-]+)['\"]"),
    "api_change": re.compile(
        r"ImportError: cannot import name ['\"]([a-zA-Z0-9_\-]+)['\"]"),
    # A library dropped a keyword the repo still passes (e.g. matplotlib 3.3
    # removed savefig(frameon=)). Must be classified before missing_argument,
    # whose TypeError branch would otherwise swallow it and send the LLM
    # hunting for a CLI option that cannot exist.
    "unexpected_kwarg": re.compile(
        r"TypeError:[^\n]*(?:unexpected keyword argument|"
        r"takes no keyword arguments)[^\n]*?['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
    "interactive_prompt": re.compile(
        r"\[y/n\]|\[yes/no\]|\(y\)\s*:|\(n\)\s*:|Are you sure|"
        r"Continue\?|Overwrite\?|Proceed\?|already exists.*\[y",
        re.IGNORECASE),
    "missing_argument": re.compile(
        r"Missing option|Missing argument|missing.*required|required.*argument|"
        r"Error: Missing|the following arguments are required|"
        r"error: argument .* is required|"
        r"TypeError:.*argument|"
        r"Usage:.*\[OPTIONS\]",
        re.IGNORECASE),
    "missing_file": re.compile(r"FileNotFoundError.*['\"](.+?)['\"]"),
    "bad_encoding": re.compile(r"UnicodeDecodeError"),
    "timeout":      re.compile(r"TimeoutError|timed out", re.I),
    "cuda_error":   re.compile(r"CUDA error|RuntimeError.*CUDA|cuda.*invalid", re.I),
    "port_in_use":  re.compile(r"Address already in use|port.*in use", re.I),
    "permission":   re.compile(r"PermissionError|Permission denied", re.I),
}

# Common import alias → PyPI package name
IMPORT_TO_PYPI: dict[str, str] = {
    "cv2":      "opencv-python",
    "sklearn":  "scikit-learn",
    "PIL":      "Pillow",
    "bs4":      "beautifulsoup4",
    "yaml":     "PyYAML",
    "dotenv":   "python-dotenv",
    "Crypto":   "pycryptodome",
    "serial":   "pyserial",
    "gi":       "PyGObject",
    "wx":       "wxPython",
    "usb":      "pyusb",
    "skimage":  "scikit-image",
    "tensorflow": "tensorflow-cpu",
    "tf":       "tensorflow-cpu",
    "torch":    "torch",
    "torchvision": "torchvision",
    "flask":    "flask",
    "fastapi":  "fastapi",
    "uvicorn":  "uvicorn",
    "aiohttp":  "aiohttp",
    "requests": "requests",
    "httpx":    "httpx",
    "pydantic": "pydantic",
    "sqlalchemy": "SQLAlchemy",
    "pymongo":  "pymongo",
    "redis":    "redis",
    "celery":   "celery",
    "numpy":    "numpy",
    "pandas":   "pandas",
    "matplotlib": "matplotlib",
    "seaborn":  "seaborn",
    "plotly":   "plotly",
    "scipy":    "scipy",
    "nltk":     "nltk",
    "spacy":    "spacy",
    "transformers": "transformers",
    "datasets": "datasets",
    "tqdm":     "tqdm",
    "click":    "click",
    "typer":    "typer",
    "rich":     "rich",
    "loguru":   "loguru",
    "pexpect":  "pexpect",
    "paramiko": "paramiko",
    "boto3":    "boto3",
    "google":   "google-cloud",
    "azure":    "azure-identity",
    "openai":   "openai",
    "anthropic": "anthropic",
    "groq":     "groq",
    "langchain": "langchain",
    "streamlit": "streamlit",
    "gradio":   "gradio",
    "dash":     "dash",
}

# ─────────────────────────────────────────────────────────────────────────────
#  Runnability Categories  (used in not-runnable analysis)
# ─────────────────────────────────────────────────────────────────────────────

NOT_RUNNABLE_CATEGORIES = {
    "pure_library": (
        "This is a Python library/package meant to be imported, not executed directly. "
        "It has no CLI entry point or main script."
    ),
    "missing_data": (
        "The repo requires large datasets, model weights, or external data files "
        "that are not included and must be downloaded separately."
    ),
    "gpu_required": (
        "This project requires a CUDA GPU to run. "
        "It cannot run on CPU-only systems."
    ),
    "paid_api_required": (
        "This project requires paid API credentials (e.g. OpenAI, AWS, GCP) "
        "that cannot be stubbed or mocked."
    ),
    "incomplete_project": (
        "The repository appears incomplete — missing key files, broken imports, "
        "or placeholder code that was never finished."
    ),
    "os_specific": (
        "This project only runs on a specific OS (Windows-only .bat scripts, "
        "macOS-only frameworks, etc.) that differs from the current environment."
    ),
    "compiled_only": (
        "This project requires compiled C/C++/Rust extensions that must be "
        "built from source, which may fail without the correct build toolchain."
    ),
    "notebook_only": (
        "This is a Jupyter notebook project with no standalone Python script. "
        "It must be run interactively in a Jupyter environment."
    ),
    "docker_only": (
        "This project is designed to run exclusively inside Docker. "
        "No plain Python entry point exists."
    ),
    "gui_only": (
        "This is a GUI application (Tkinter, PyQt, wxPython, etc.) "
        "that cannot run in a headless/terminal environment."
    ),
    "missing_credentials": (
        "This project requires API keys or secrets that were not provided."
    ),
    "other": (
        "The project cannot be run automatically for reasons identified below."
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
#  Main Entry Point
# ─────────────────────────────────────────────────────────────────────────────

async def run_execution_loop(
    task:           str,
    repo_full_name: str,
    analysis:       dict,
    job_id:         str = "",
    input_files:    list[str] | None = None,
) -> AsyncGenerator[dict, None]:
    """
    Autonomous execution pipeline.  Yields SSE event dicts.

    Final 'done' event extra fields:
        job_id, returncode, output, output_files,
        run_script    — complete copy-pasteable bash script
        manual_guide  — step-by-step guide (populated when run fails)
        fix_journal, iterations, elapsed_s
        not_runnable  — bool: True if repo cannot be run
        not_runnable_reason  — detailed explanation string
    """
    metrics    = _fresh_metrics()
    # Env vars for THIS job only. Writing them to os.environ leaked them into
    # every later job in the same server process: one repo's
    # DJANGO_SETTINGS_MODULE=config.settings broke every Django repo after it.
    job_env: dict[str, str] = {}
    workspace: Optional[str] = None
    venv_path: Optional[str] = None
    fix_journal: list[dict]  = []
    t_start = time.monotonic()

    repo_name = repo_full_name.split("/")[-1]
    llm_plan: dict = {}
    pm_info:  dict = {"name": "pip-req", "file": "requirements.txt", "cmd": None}

    yield _ev("context", "Execution context assembled",
              f"Task: {task}\nRepo: {repo_full_name}\n"
              f"Max retries: {MAX_RETRIES} | Token budget: {TOKEN_BUDGET}",
              metrics=metrics)

    try:
        # ── 1. Clone ──────────────────────────────────────────────────────────
        workspace = await _clone_repo(repo_full_name, metrics)
        yield _ev("explore", "Repository cloned",
                  f"✓ {repo_full_name}", tool="git.clone", metrics=metrics)

        # ── 2. Inject user input files ────────────────────────────────────────
        if input_files:
            injected = await _inject_input_files(workspace, input_files, job_id)
            yield _ev("explore", f"Input files injected ({len(injected)})",
                      "\n".join(injected), tool="file.inject", metrics=metrics)

        # ── 3. Inspect workspace + README ─────────────────────────────────────
        repo_ctx    = _inspect_workspace(workspace)
        readme_cmds = _parse_readme_commands(repo_ctx.get("readme", ""))
        metrics["files"] = repo_ctx["file_count"]

        yield _ev("explore", "Workspace & README analysed",
                  f"{repo_ctx['summary']}\n"
                  f"README commands: {len(readme_cmds.get('all_commands', []))} found | "
                  f"Has README: {readme_cmds.get('has_readme', False)} | "
                  f"Primary cmd: {readme_cmds.get('primary_cmd', 'none')}",
                  code="\n".join(repo_ctx["tree"][:60]),
                  tool="repo.explore", metrics=metrics)

        # ── 4. Read key file contents ─────────────────────────────────────────
        file_contents = await _read_key_files(workspace, analysis, repo_ctx)
        yield _ev("explore",
                  f"Read {len(file_contents)} key source files",
                  "\n".join(
                      f"  {p} ({len(c)} chars)"
                      for p, c in list(file_contents.items())[:12]),
                  tool="files.read", metrics=metrics)

        # ── 5. Package manager + venv + deps ──────────────────────────────────
        pm_info = _detect_package_manager(workspace)
        yield _ev("exec", f"Package manager: {pm_info['name']}",
                  f"Detected: {pm_info['name']} (marker: {pm_info['file']})",
                  tool="pkg.detect", metrics=metrics)

        venv_path = os.path.join(workspace, ".venv")
        venv_ok, venv_out = await _setup_environment(
            workspace, venv_path, pm_info, metrics)

        yield _ev(
            "exec" if venv_ok else "feedback",
            "Environment ready" if venv_ok else "Environment warnings (continuing)",
            venv_out[:2000], tool="venv.setup", metrics=metrics)

        # ── 6. Env-var hints ───────────────────────────────────────────────────
        env_hints = _discover_env_hints(workspace)
        if env_hints:
            yield _ev("explore",
                      f"Env-var hints found ({len(env_hints)})",
                      "\n".join(f"  {k}={v}" for k, v in env_hints.items()),
                      tool="env.discover", metrics=metrics)

        # ── 7. LLM comprehensive plan ─────────────────────────────────────────
        yield _ev("explore", "Sending full context to LLM for analysis…",
                  f"{len(file_contents)} files + "
                  f"{len(repo_ctx.get('readme',''))} chars README + "
                  f"{len(analysis.get('tree') or repo_ctx.get('tree', []))} tree entries",
                  tool="llm.plan", metrics=metrics)

        llm_plan = await _llm_plan(
            task, repo_full_name, analysis, repo_ctx,
            file_contents, readme_cmds, env_hints, pm_info, metrics)

        yield _ev("explore", "Execution plan ready",
                  llm_plan.get("summary", ""),
                  code=json.dumps(
                      {k: v for k, v in llm_plan.items()
                       if k not in ("revised_script",)},
                      indent=2)[:3000],
                  tool="llm.plan", metrics=metrics)

        # ── 8. NOT-RUNNABLE GATE ───────────────────────────────────────────────
        if not llm_plan.get("runnable", True):
            not_runnable_detail = _build_not_runnable_report(llm_plan, repo_ctx)

            yield _ev("not_runnable",
                      "❌ Repository cannot be run automatically",
                      not_runnable_detail,
                      tool="runnable.gate", metrics=metrics)

            total_elapsed = round(time.monotonic() - t_start, 2)
            metrics["elapsed_s"] = total_elapsed

            # Still generate a helpful bash script showing what *would* be needed
            run_script = _generate_run_script(
                repo_full_name, task, llm_plan, pm_info,
                env_hints,
                llm_plan.get("run_command") or "# No runnable command found",
                [], [], 1, workspace)

            yield _ev("done", "Analysis complete — not runnable",
                      not_runnable_detail,
                      metrics=metrics,
                      extra={
                          "job_id":              job_id,
                          "returncode":          -1,
                          "not_runnable":        True,
                          "not_runnable_reason": not_runnable_detail,
                          "run_script":          run_script,
                          "manual_guide":        _not_runnable_markdown(llm_plan, repo_full_name),
                          "iterations":          0,
                          "fix_journal":         [],
                          "elapsed_s":           total_elapsed,
                          "output_files":        [],
                          "output":              "",
                      })
            return

        # ── 9. Credential gate ────────────────────────────────────────────────
        missing_creds: dict[str, str] = {}
        for k, v in llm_plan.get("env_vars", {}).items():
            if k and (not v or str(v).upper() in
                      ("YOUR_VALUE_HERE", "REPLACE_ME", "")) \
                    and not os.environ.get(k):
                missing_creds[k] = ""
        for k in llm_plan.get("required_credentials", []):
            if k and not os.environ.get(k):
                missing_creds.setdefault(k, "")

        if missing_creds:
            ev_obj = asyncio.Event()
            CREDENTIAL_EVENTS[job_id] = ev_obj
            hints_for_creds = {
                k: llm_plan.get("credential_hints", {}).get(k, "")
                for k in missing_creds
            }
            yield _ev("credential_needed",
                      f"Credentials required ({len(missing_creds)} missing)",
                      "This repo needs API keys or secrets.\n"
                      + "\n".join(
                          f"  • {k}"
                          + (f" — {hints_for_creds[k]}" if hints_for_creds.get(k) else "")
                          for k in missing_creds
                      ),
                      tool="credential.gate", metrics=metrics,
                      extra={
                          "fields": list(missing_creds.keys()),
                          "hints":  hints_for_creds,
                      })
            try:
                await asyncio.wait_for(ev_obj.wait(), timeout=CREDENTIAL_TIMEOUT)
                submitted = CREDENTIAL_STORE.pop(job_id, {})
                for k, v in submitted.items():
                    if k and v:
                        job_env[str(k)] = str(v)
                yield _ev("explore",
                          f"Credentials received ({len(submitted)})",
                          "✓ Injected as environment variables.",
                          tool="credential.gate", metrics=metrics)
            except asyncio.TimeoutError:
                yield _ev("feedback",
                          f"Credential timeout ({CREDENTIAL_TIMEOUT}s) — continuing",
                          "Missing credentials may cause execution to fail.",
                          tool="credential.gate", metrics=metrics)
            finally:
                CREDENTIAL_EVENTS.pop(job_id, None)

        # Apply env vars with real values
        env_export_lines: list[str] = []
        for k, v in llm_plan.get("env_vars", {}).items():
            if k and v and str(v).upper() not in ("YOUR_VALUE_HERE", "REPLACE_ME", ""):
                job_env.setdefault(str(k), str(v))
                env_export_lines.append(f'export {k}="{v}"')

        # ── 10. Extra deps from LLM ────────────────────────────────────────────
        extra_deps = llm_plan.get("extra_deps", [])
        if extra_deps and venv_path:
            pip = _pip_path(venv_path)
            for dep in extra_deps[:15]:
                dep = _sanitise(str(dep))
                if dep:
                    ok, out = await _run_cmd([pip, "install", dep], workspace, timeout=120, env=job_env)
                    yield _ev("exec", f"Extra dep: {dep}",
                              "✓ installed" if ok else f"⚠ failed: {out[:200]}",
                              tool="deps.extra", metrics=metrics)

        # ── 11. Pre-run setup steps ────────────────────────────────────────────
        pre_steps      = llm_plan.get("pre_run_steps", [])
        pre_steps_done: list[str] = []
        if pre_steps and venv_path:
            yield _ev("exec", f"Running {len(pre_steps)} pre-run setup steps",
                      "\n".join(pre_steps[:5]), tool="setup.pre_run", metrics=metrics)
            for step in pre_steps[:5]:
                step = step.strip()
                if not step or step.startswith("#"):
                    continue
                # Safety: block destructive commands
                if any(bad in step for bad in (
                    "rm -rf /", "sudo rm", "> /dev", "curl | bash", "wget | bash",
                )):
                    yield _ev("feedback", f"Blocked unsafe pre-run step: {step[:60]}",
                              "Skipped for safety.", tool="setup.pre_run", metrics=metrics)
                    continue
                ok, out = await _run_cmd(["bash", "-c", step], workspace, timeout=300, env=job_env)
                pre_steps_done.append(step)
                yield _ev("exec", f"Pre-run: {step[:60]}",
                          ("✓ done" if ok else "⚠ non-zero") + f"\n{out[:400]}",
                          tool="setup.pre_run", metrics=metrics)

        # ── 12. Build exact run command ────────────────────────────────────────
        run_cmd, cmd_source = _build_run_command(
            llm_plan, analysis, repo_ctx,
            input_files or [], workspace, readme_cmds)

        yield _ev("exec", f"Run command — {cmd_source}",
                  f"$ {run_cmd}",
                  code=run_cmd, tool="cmd.build", metrics=metrics)

        # Snapshot the workspace before anything runs. Whatever appears after
        # this point was produced BY the run, which is what makes it safe to
        # sweep the repo for artefacts without dragging in files the project
        # already shipped.
        pre_run_files = _snapshot_files(workspace)

        # Does this run drive a desktop GUI? Decided from the source of the
        # file the run command actually targets, so a repo that merely ships an
        # unrelated tkinter demo is not misread as a GUI app.
        is_gui = _targets_gui(run_cmd, workspace)
        if is_gui:
            yield _ev("exec", "Desktop GUI detected",
                      "This entry point drives a GUI event loop, so it will "
                      "never exit on its own. Rendering headless and treating "
                      "a stable window as success.",
                      tool="gui.detect", metrics=metrics)

        # Files the task explicitly asks for. Empty for the many tasks whose
        # deliverable is stdout, and the artifact gate stays dormant then.
        expected_artifacts = _expected_artifacts(task)
        if expected_artifacts:
            yield _ev("exec", "Expected output file(s)",
                      ", ".join(expected_artifacts) +
                      " — exit 0 alone will not be accepted as success.",
                      tool="artifact.expect", metrics=metrics)

        # ── 13. Execution + fix loop ───────────────────────────────────────────
        final_rc     = 1
        final_stdout = ""
        final_stderr = ""
        unmet        = []
        install_only = False
        iteration_log: list[dict] = []

        for attempt in range(MAX_RETRIES):
            metrics["iters"] = attempt + 1
            t_attempt = time.monotonic()

            yield _ev("exec",
                      f"Attempt {attempt + 1}/{MAX_RETRIES}",
                      f"$ {run_cmd}",
                      code=run_cmd, tool="bash.run", metrics=metrics)

            final_rc, final_stdout, final_stderr = await _run_direct(
                run_cmd, workspace, venv_path or "",
                is_gui=_targets_gui(run_cmd, workspace), extra_env=job_env)

            elapsed    = round(time.monotonic() - t_attempt, 2)
            run_output = _fmt_output(final_rc, final_stdout, final_stderr)

            iteration_log.append({
                "attempt":    attempt + 1,
                "command":    run_cmd,
                "returncode": final_rc,
                "stdout":     _clip(final_stdout, 3000, 900),
                "stderr":     _clip(final_stderr, 3000, 900),
                "elapsed_s":  elapsed,
            })

            # Treat a running web server / UI frontend as success.
            # These processes run forever and are killed by timeout (rc != 0),
            # but if the output shows the server started we count it as done.
            if final_rc != 0 and _UI_SERVER_STARTED.search(final_stdout + final_stderr):
                final_rc = 0

            # Exit 0 is necessary but not sufficient: if the task named a file
            # and that file does not exist, the task is not done. Feed the gap
            # back into the retry loop instead of reporting a hollow success.
            #
            # Staying up used to exempt a run from this check, on the grounds
            # that a GUI or server was never going to write anything. That is
            # true for "run the snake game" or "start the web server" — and
            # those tasks name no artefacts, so they are exempt anyway, because
            # expected_artifacts is empty. The exemption only ever bit when the
            # task DID name a file: asked for outputs/barcode.png, the agent
            # launched the repo's Flask app instead, the server sat there for
            # ten seconds, and that was reported as success with no image
            # anywhere. Picking a server when the task wanted a file is exactly
            # the wrong-entry-point mistake this gate exists to send back round
            # the loop, so a named artefact is now required either way.
            unmet = []
            unmet_detail: list[tuple[str, str]] = []
            if final_rc == 0 and expected_artifacts:
                unmet_detail = _missing_artifacts_detail(expected_artifacts, workspace,
                                                         pre_run_files)
                unmet = [e for e, _ in unmet_detail]
            install_only = final_rc == 0 and _is_install_only(run_cmd)

            if final_rc == 0 and not unmet and not install_only:
                yield _ev("exec",
                          f"✅ Attempt {attempt + 1} succeeded (exit 0)",
                          f"Completed in {elapsed}s.",
                          code=_clip(run_output, 4000, 1200),
                          tool="bash.run", metrics=metrics)
                break

            if install_only:
                error_class = "install_only"
                gap = ("The command only installed a dependency — it never ran "
                       "the project, so nothing was demonstrated. Give a "
                       "revised_command that actually exercises the repo.")
                run_output = gap + "\n\n" + run_output
                yield _ev("feedback",
                          f"Attempt {attempt + 1} only installed dependencies",
                          gap, code=_clip(run_output, 3000, 900),
                          tool="feedback.loop", metrics=metrics)
            elif unmet:
                error_class = "missing_artifact"
                # Say WHY each file failed the gate, not just that it did: a
                # file that exists but fails its format check needs a different
                # fix than one that was never written, and handing the model
                # the same "never created" line for both used to send it
                # inventing an unrelated cause (a missing input, a wrong
                # directory) while it rewrote the same working command under a
                # cosmetic quoting change every attempt.
                reasons = "; ".join(f"{e} — {why}" for e, why in unmet_detail)
                gap = ("The command exited 0 but the task's output file(s) did "
                       f"not satisfy the task: {reasons}. Fix the actual gap "
                       "named above, not the exit code.")
                run_output = gap + "\n\n" + run_output
                yield _ev("feedback",
                          f"Attempt {attempt + 1} exited 0 but the output file(s) did not qualify",
                          gap,
                          code=_clip(run_output, 3000, 900),
                          tool="feedback.loop", metrics=metrics)
            else:
                error_class = _classify_error(final_stderr + final_stdout)
                yield _ev("feedback",
                          f"Attempt {attempt + 1} failed — class: [{error_class}]",
                          _error_summary(final_stderr, final_stdout),
                          code=_clip(run_output, 3000, 900),
                          tool="feedback.loop", metrics=metrics)

            if attempt >= MAX_RETRIES - 1:
                break

            # ── Auto-fix (no LLM call) ─────────────────────────────────────
            auto_fix = await _try_autofix(
                error_class, final_stderr + final_stdout,
                workspace, venv_path or "", fix_journal)

            if auto_fix:
                fix_journal.append({
                    **auto_fix,
                    "attempt": attempt + 1,
                    "result":  "autofix_applied",
                })
                yield _ev("feedback",
                          f"Auto-fix applied: {auto_fix['fix_type']}",
                          auto_fix.get("explanation", ""),
                          tool="autofix", metrics=metrics)
                if auto_fix.get("fix_type") == "pipe_yes":
                    run_cmd = f"yes | bash -c {shlex.quote(run_cmd)}"
                if auto_fix.get("revised_command"):
                    run_cmd = auto_fix["revised_command"]
                continue

            if metrics["tokens"] >= TOKEN_BUDGET:
                yield _ev("feedback", "Token budget exhausted",
                          f"Used {metrics['tokens']} / {TOKEN_BUDGET} tokens",
                          tool="budget", metrics=metrics)
                break

            # ── LLM diagnose + targeted fix ────────────────────────────────
            fix = await _llm_diagnose(
                run_cmd, run_output, attempt, error_class, task,
                workspace, venv_path or "",
                repo_ctx, file_contents, fix_journal, metrics)

            already_tried = {
                e.get("detail", "") for e in fix_journal
                if e.get("fix_type") == "fix_command"
            }
            if fix.get("revised_command") and fix.get("fix_type") == "fix_command":
                fix["detail"] = fix["revised_command"].strip()

            fix_journal.append({
                **fix,
                "attempt": attempt + 1,
                "result":  "llm_fix_applied",
            })
            yield _ev("feedback",
                      f"LLM fix → {fix.get('fix_type', 'unknown')}",
                      fix.get("explanation", ""),
                      code=fix.get("detail", "")[:1500],
                      tool="feedback.fix", metrics=metrics)

            await _apply_fix(fix, workspace, venv_path or "", job_env)

            if fix.get("revised_command"):
                new_cmd = fix["revised_command"].strip()
                if new_cmd and new_cmd not in already_tried:
                    run_cmd = new_cmd

        # ── 14. Generate bash script ───────────────────────────────────────────
        run_script = _generate_run_script(
            repo_full_name, task, llm_plan, pm_info,
            env_hints, run_cmd, pre_steps_done,
            env_export_lines, final_rc, workspace)

        # ── 15. Write artifacts ────────────────────────────────────────────────
        ws = Path(workspace)
        (ws / "run_final.sh").write_text(run_script, encoding="utf-8")
        (ws / "execution_output.txt").write_text(
            _fmt_output(final_rc, final_stdout, final_stderr), encoding="utf-8")
        (ws / "execution_log.json").write_text(
            json.dumps({
                "task":        task,
                "repo":        repo_full_name,
                "success":     final_rc == 0 and not unmet and not install_only,
                "run_command": run_cmd,
                "iterations":  iteration_log,
                "fix_journal": fix_journal,
                "metrics":     metrics,
            }, indent=2), encoding="utf-8")

        # ── 16. Capture output files ───────────────────────────────────────────
        output_files = _capture_outputs(workspace, job_id, pre_run_files)
        yield _ev("exec",
                  f"Outputs captured ({len(output_files)})",
                  "\n".join(output_files),
                  tool="output.capture", metrics=metrics)

        total_elapsed = round(time.monotonic() - t_start, 2)
        metrics["elapsed_s"] = total_elapsed

        # ── 17. Final report ───────────────────────────────────────────────────
        if final_rc == 0 and not unmet and not install_only:
            summary      = await _build_success_summary(
                task, repo_full_name, iteration_log,
                fix_journal, run_cmd, final_stdout, metrics)
            manual_guide = ""
        else:
            summary, manual_guide = await _build_failure_guide(
                task, repo_full_name, iteration_log, fix_journal,
                run_cmd, final_stdout, final_stderr, llm_plan, metrics)

        yield _ev("done", "Task completed", summary,
                  metrics=metrics,
                  extra={
                      "job_id":              job_id,
                      "returncode":          final_rc,
                      "not_runnable":        False,
                      "not_runnable_reason": "",
                      "iterations":          len(iteration_log),
                      "fix_journal":         fix_journal,
                      "elapsed_s":           total_elapsed,
                      "output_files":        output_files,
                      "run_script":          run_script,
                      "manual_guide":        manual_guide,
                      "output": _clip(_fmt_output(
                          final_rc, final_stdout, final_stderr)),
                  })

    except Exception as exc:
        log.exception("Execution pipeline error")
        yield _ev("feedback", "Pipeline error", str(exc), metrics=metrics)
        fallback_script = _generate_run_script(
            repo_full_name, task, llm_plan, pm_info,
            {}, "# Could not determine run command — see error above",
            [], [], 1, None)
        yield _ev("done", "Task stopped",
                  f"Pipeline error: {exc}",
                  metrics=metrics,
                  extra={
                      "job_id":              job_id,
                      "returncode":          1,
                      "not_runnable":        False,
                      "not_runnable_reason": "",
                      "run_script":          fallback_script,
                      "manual_guide":        f"## Pipeline Error\n\n```\n{exc}\n```\n",
                      "output":              str(exc),
                  })

    finally:
        if workspace and os.path.isdir(workspace):
            shutil.rmtree(workspace, ignore_errors=True)
            log.info("Workspace cleaned — outputs at %s/%s", OUTPUT_ROOT, job_id)


# ─────────────────────────────────────────────────────────────────────────────
#  Not-Runnable Report Builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_not_runnable_report(llm_plan: dict, repo_ctx: dict) -> str:
    """
    Build a rich, human-readable explanation of WHY the repo cannot be run.
    Covers: category, specific reason, what user needs, workarounds.
    """
    category   = llm_plan.get("not_runnable_category", "other")
    why        = llm_plan.get("why_not_runnable", "")
    workaround = llm_plan.get("workaround", "")
    what_need  = llm_plan.get("what_user_needs", [])
    summary    = llm_plan.get("summary", "")

    # Get the canonical category description
    cat_desc = NOT_RUNNABLE_CATEGORIES.get(
        category, NOT_RUNNABLE_CATEGORIES["other"])

    parts = [
        f"CATEGORY: {category.replace('_', ' ').upper()}",
        "",
        f"WHY: {cat_desc}",
        "",
    ]

    if why:
        parts += [f"SPECIFIC REASON: {why}", ""]

    if summary:
        parts += [f"WHAT THIS REPO IS: {summary}", ""]

    if what_need:
        parts += ["WHAT YOU NEED TO RUN THIS:"]
        for item in what_need:
            parts.append(f"  • {item}")
        parts.append("")

    if workaround:
        parts += [f"POSSIBLE WORKAROUND: {workaround}", ""]

    # Add repo-specific clues from inspection
    setup_files = repo_ctx.get("setup_files", [])
    has_tests   = any("test" in str(f).lower() for f in repo_ctx.get("key_sources", []))

    if "setup.py" in setup_files or "pyproject.toml" in setup_files:
        if category == "pure_library":
            parts += [
                "HOW TO INSTALL AND USE:",
                "  pip install -e .",
                "  # Then import the package in your own Python script",
                "",
            ]

    if has_tests and category == "pure_library":
        parts += [
            "HOW TO RUN TESTS:",
            "  pip install -e .",
            "  pytest",
            "",
        ]

    return "\n".join(parts)


def _not_runnable_markdown(llm_plan: dict, repo_full_name: str) -> str:
    """Generate a full markdown manual guide for not-runnable repos."""
    repo_name  = repo_full_name.split("/")[-1]
    category   = llm_plan.get("not_runnable_category", "other")
    why        = llm_plan.get("why_not_runnable", "Unknown reason")
    workaround = llm_plan.get("workaround", "")
    what_need  = llm_plan.get("what_user_needs", [])
    summary    = llm_plan.get("summary", "")
    cat_desc   = NOT_RUNNABLE_CATEGORIES.get(category, NOT_RUNNABLE_CATEGORIES["other"])

    md = [
        f"## ❌ Cannot Run Automatically: `{repo_name}`",
        "",
        f"**Category:** {category.replace('_', ' ').title()}  ",
        f"**What this repo is:** {summary}",
        "",
        f"### Why It Cannot Run",
        "",
        f"{cat_desc}",
        "",
        f"**Specific reason:** {why}",
        "",
    ]

    if what_need:
        md += ["### What You Need", ""]
        for item in what_need:
            md.append(f"- {item}")
        md.append("")

    if workaround:
        md += [
            "### Possible Workaround",
            "",
            workaround,
            "",
        ]

    # Category-specific guidance
    if category == "pure_library":
        md += [
            "### How to Use This Library",
            "",
            "```bash",
            f"git clone https://github.com/{repo_full_name}.git {repo_name}",
            f"cd {repo_name}",
            "python3 -m venv .venv && source .venv/bin/activate",
            "pip install -e .",
            "python -c 'import " + repo_name + "; help(" + repo_name + ")'",
            "```",
            "",
        ]
    elif category == "missing_data":
        md += [
            "### How to Get the Required Data",
            "",
            "Check the README for data download instructions.",
            "Common locations: Hugging Face datasets, Google Drive, official project page.",
            "",
        ]
    elif category == "gpu_required":
        md += [
            "### Running Without GPU",
            "",
            "Look for `--device cpu`, `--no-cuda`, or `USE_CPU=1` flags.",
            "Some models support CPU inference but will be very slow.",
            "",
        ]
    elif category == "missing_credentials":
        required = llm_plan.get("required_credentials", [])
        if required:
            md += ["### Required Credentials", ""]
            for cred in required:
                hint = llm_plan.get("credential_hints", {}).get(cred, "")
                md.append(f"- `{cred}`" + (f" — {hint}" if hint else ""))
            md += [
                "",
                "```bash",
            ]
            for cred in required:
                md.append(f'export {cred}="your_value_here"')
            md += ["```", ""]

    return "\n".join(md)


# ─────────────────────────────────────────────────────────────────────────────
#  README Command Parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_readme_commands(readme: str) -> dict:
    """
    Extract shell commands from README with multiple strategies:
      1. Fenced code blocks (```bash / ```sh / ```console / ```)
      2. Dollar-prompt lines ($ cmd  or  > cmd)
      3. Inline backtick with prompt (`$ cmd`)
      4. Section-aware extraction (Quick Start / Usage / Install)

    Derives primary_cmd — the single best "run" command to try first,
    taken from highest-priority section (quickstart > usage > examples).
    """
    if not readme:
        return {
            "all_commands": [], "install_cmds": [], "run_cmds": [],
            "sections": {}, "primary_cmd": "", "has_readme": False,
        }

    all_cmds:     list[str] = []
    install_cmds: list[str] = []
    run_cmds:     list[str] = []
    seen: set[str] = set()

    RUN_STARTERS = re.compile(
        r"^(python3?|python3?\.\d+|jupyter|streamlit|uvicorn|gunicorn|flask|"
        r"node|npm\s+start|yarn\s+start|cargo\s+run|go\s+run|"
        r"bash\s+\S|sh\s+\S|\.\/\S)",
        re.I,
    )
    INSTALL_STARTERS = re.compile(
        r"^(pip3?\s+install|conda\s+install|poetry\s+(install|add)|"
        r"pipenv\s+install|npm\s+install|yarn\s+install)",
        re.I,
    )

    def _add(cmd: str) -> None:
        cmd = re.sub(r"^[\$\>\#]\s*", "", cmd.strip()).strip()
        if not cmd or cmd in seen or cmd.startswith("#"):
            return
        seen.add(cmd)
        all_cmds.append(cmd)
        if INSTALL_STARTERS.match(cmd):
            install_cmds.append(cmd)
        if RUN_STARTERS.match(cmd):
            run_cmds.append(cmd)

    # 1. Fenced code blocks
    for block in re.findall(
        r"```(?:bash|sh|shell|console|terminal|zsh|cmd|text)?\n(.*?)```",
        readme, re.DOTALL | re.IGNORECASE,
    ):
        for line in block.splitlines():
            _add(line)

    # 2. Dollar-prompt lines outside code blocks
    clean = re.sub(r"```.*?```", "", readme, flags=re.DOTALL)
    for line in clean.splitlines():
        if re.match(r"^\s*[\$\>]\s+\S", line):
            _add(line.strip())

    # 3. Inline `$ cmd`
    for m in re.findall(r"`\$\s+([^`]+)`", readme):
        _add(m)

    # 4. Section-aware extraction
    section_patterns = {
        "quickstart": re.compile(r"quick\s*start|getting\s*started|quick\s*setup",  re.I),
        "install":    re.compile(r"^install|setup|prerequisites|requirements",       re.I),
        "usage":      re.compile(r"usage|how\s+to\s+use|running|run|inference",      re.I),
        "examples":   re.compile(r"example|demo",                                    re.I),
    }
    sections: dict[str, list[str]] = {}
    cur_sec: Optional[str] = None

    for line in readme.splitlines():
        if re.match(r"^#{1,3}\s+", line):
            cur_sec = None
            for sec, pat in section_patterns.items():
                if pat.search(line):
                    cur_sec = sec
                    sections.setdefault(sec, [])
                    break
        elif cur_sec:
            stripped = re.sub(r"^[\$\>]\s*", "", line.strip())
            if stripped and any(kw in stripped.lower() for kw in (
                "python", "pip", "conda", "bash", "sh ",
                "run", "npm", "node", "poetry", "streamlit",
                "uvicorn", "flask", "gunicorn",
            )):
                sections[cur_sec].append(stripped)

    def _best_run_from(cmds: list[str]) -> str:
        candidates = [c for c in cmds if RUN_STARTERS.match(
            re.sub(r"^[\$\>]\s*", "", c.strip()))]
        return candidates[-1] if candidates else ""

    primary_cmd = ""
    for sec in ("quickstart", "usage", "examples"):
        candidate = _best_run_from(sections.get(sec, []))
        if candidate:
            primary_cmd = candidate
            break
    if not primary_cmd and run_cmds:
        primary_cmd = run_cmds[0]

    return {
        "all_commands": all_cmds[:30],
        "install_cmds": install_cmds[:10],
        "run_cmds":     run_cmds[:10],
        "sections":     sections,
        "primary_cmd":  primary_cmd,
        "has_readme":   True,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Key File Reader
# ─────────────────────────────────────────────────────────────────────────────

async def _read_key_files(
    workspace: str,
    analysis:  dict,
    repo_ctx:  dict,
    max_files: int = TOP_FILES_N,
    max_chars: int = FILE_READ_CHARS,
) -> dict[str, str]:
    ws   = Path(workspace)
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(path: str) -> None:
        p = path.strip()
        if p and p not in seen:
            seen.add(p)
            ordered.append(p)

    # Primary: analyzer modules sorted by score — top-N by score from analyzer
    for m in (analysis.get("modules") or [])[:max_files]:
        _add(m.get("path", ""))
    # Secondary: explicit key_files list (top-10 scored paths from analyzer)
    for p in analysis.get("key_files", []):
        _add(p)
    for fname in (
        "main.py", "app.py", "run.py", "cli.py", "demo.py",
        "__main__.py", "inference.py", "predict.py",
        "requirements.txt", "pyproject.toml", "setup.py",
        "Makefile", "Dockerfile",
    ):
        if (ws / fname).is_file():
            _add(fname)
    for p in repo_ctx.get("setup_files",      []):
        _add(p)
    for p in repo_ctx.get("entry_candidates", []):
        _add(p)

    result: dict[str, str] = {}
    count = 0
    for rel in ordered:
        if count >= max_files + 5:
            break
        full = ws / rel
        if not full.is_file():
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="ignore")
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n... [truncated at {max_chars} chars]"
            result[rel] = content
            count += 1
        except Exception as exc:
            log.warning("Could not read %s: %s", rel, exc)

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  LLM Execution Plan  (comprehensive single call)
# ─────────────────────────────────────────────────────────────────────────────

# Installing a packaged repo puts its console scripts on PATH, and for a library
# that is usually the ONLY supported way to drive it: python-qrcode's documented
# CLI is `qr`, and running its source tree directly (python -m qrcode.main) dies
# on an ImportError. The planner cannot infer those names from the file tree, so
# hand them over.
_PYPROJECT_SCRIPTS = re.compile(
    r"^\[project\.scripts\]\s*$(.*?)(?=^\[|\Z)", re.MULTILINE | re.DOTALL)
_POETRY_SCRIPTS = re.compile(
    r"^\[tool\.poetry\.scripts\]\s*$(.*?)(?=^\[|\Z)", re.MULTILINE | re.DOTALL)
_TOML_SCRIPT_LINE = re.compile(
    r"^\s*[\"']?([A-Za-z0-9_.\-]+)[\"']?\s*=", re.MULTILINE)
_SETUP_CONSOLE = re.compile(
    r"console_scripts[\"']?\s*:?\s*=?\s*\[(.*?)\]", re.DOTALL)
_CFG_CONSOLE = re.compile(
    r"console_scripts\s*=\s*(.*?)(?=^\S|\Z)", re.MULTILINE | re.DOTALL)
_EP_NAME = re.compile(r"[\"']?\s*([A-Za-z0-9_.\-]+)\s*=\s*[A-Za-z0-9_.]+[:\s]")


def _console_scripts(file_contents: dict[str, str]) -> list[str]:
    """Command names this repo installs, read from its packaging metadata."""
    names: list[str] = []

    def take(raw: str, pattern: re.Pattern) -> None:
        for m in pattern.finditer(raw or ""):
            for n in _TOML_SCRIPT_LINE.findall(m.group(1)):
                if n not in names:
                    names.append(n)

    for path, content in (file_contents or {}).items():
        base = os.path.basename(path)
        if base == "pyproject.toml":
            take(content, _PYPROJECT_SCRIPTS)
            take(content, _POETRY_SCRIPTS)
        elif base in ("setup.py", "setup.cfg"):
            for pat in (_SETUP_CONSOLE, _CFG_CONSOLE):
                for m in pat.finditer(content or ""):
                    for n in _EP_NAME.findall(m.group(1)):
                        if n not in names:
                            names.append(n)
    return names


async def _llm_plan(
    task:           str,
    repo_full_name: str,
    analysis:       dict,
    repo_ctx:       dict,
    file_contents:  dict[str, str],
    readme_cmds:    dict,
    env_hints:      dict,
    pm_info:        dict,
    metrics:        dict,
) -> dict:
    files_block = ""
    for path, content in list(file_contents.items())[:TOP_FILES_N]:
        files_block += (
            f"\n{'─'*50}\n"
            f"📄 FILE: {path}\n"
            f"{'─'*50}\n"
            f"{content}\n"
        )

    scripts = _console_scripts(file_contents)
    scripts_block = (
        "After the install step these commands are on PATH and are the "
        f"SUPPORTED way to run this project: {', '.join(scripts)}. "
        "Prefer one of them over running a source file directly."
        if scripts else
        "This repo installs no console scripts."
    )

    not_runnable_categories_list = "\n".join(
        f'  "{k}": {v[:80]}...' for k, v in NOT_RUNNABLE_CATEGORIES.items()
    )

    prompt = f"""You are a Python project execution expert.
Analyse this GitHub repository and produce a PRECISE, WORKING execution plan.

━━━━ REPOSITORY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{repo_full_name}

━━━━ TASK ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{task}

━━━━ FILE TREE ({len(analysis.get('tree') or repo_ctx.get('tree', []))} entries) ━━━━━━━━━━━━━━━━
{chr(10).join((analysis.get('tree') or repo_ctx.get('tree', []))[:80])}

━━━━ PACKAGE MANAGER ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Detected: {pm_info['name']}  (marker: {pm_info['file']})
Setup files: {repo_ctx.get('setup_files', [])}

━━━━ FULL README ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{repo_ctx.get('readme', 'No README found.')[:5000]}

━━━━ README COMMANDS EXTRACTED ━━━━━━━━━━━━━━━━━━
Run commands  : {readme_cmds.get('run_cmds', [])}
Install cmds  : {readme_cmds.get('install_cmds', [])}
All commands  : {readme_cmds.get('all_commands', [])[:20]}
Quickstart    : {readme_cmds.get('sections', {}).get('quickstart', [])}
Usage section : {readme_cmds.get('sections', {}).get('usage', [])}
Primary cmd   : {readme_cmds.get('primary_cmd', 'none')}

━━━━ KEY SOURCE FILES ({len(file_contents)} read) ━━━━━━━━━━━━━━━━━━━
{files_block[:7000]}

━━━━ INSTALLED CONSOLE SCRIPTS ━━━━━━━━━━━━━━━━━━
{scripts_block}

━━━━ STATIC ANALYSIS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Entry point : {analysis.get('entry_point', '')}
Run command : {analysis.get('run_command', '')}
Task plan   : {analysis.get('task_plan', [])}

━━━━ ENV-VAR HINTS (.env.example) ━━━━━━━━━━━━━━━
{list(env_hints.keys())}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PORT RULE: port {_SELF_PORT} is already taken by the host service in this environment.
Never start a server on it and never curl it. Django's default is 8000, so ALWAYS
pass an explicit free port, e.g. `python manage.py runserver 0.0.0.0:8765`, and
fetch pages from that port (add `-H "Host: localhost"` if ALLOWED_HOSTS rejects it).

RUNNABILITY DECISION RULES (follow strictly):
- runnable=true if there is a concrete Python entry point (main.py, app.py,
  __main__.py, a CLI entry, or a script under examples/ demos/ scripts/) that
  does something observable when executed.
- THIS ENVIRONMENT HAS A VIRTUAL DISPLAY and headless graphics drivers, so:
    • Desktop GUI apps (Tkinter, PyQt, PySide, pygame, kivy, turtle) ARE
      runnable. Launch them the normal way. They never exit on their own and
      that is expected — starting without crashing is their success condition.
    • matplotlib renders to file rather than to a window, so a script that
      calls plt.show() runs fine and plt.savefig() produces a real image.
    • Web UIs (Flask, FastAPI, Streamlit, Gradio, Django) ARE runnable for the
      same reason: serving is the success condition, not exiting.
  Do NOT mark any of these not_runnable for "needs a display", "is
  interactive", or "never terminates".
- A library with no entry point is still runnable if you can write a short
  driver that imports it and prints or saves a result. Prefer doing that over
  declaring it not runnable.
- runnable=false only if ANY of these is true:
    • Requires GPU/CUDA that is unavailable
    • Requires large external data/weights not included
    • Requires paid API that cannot be stubbed (OpenAI, AWS, etc.)
    • Project is obviously incomplete (TODO placeholders, missing core files)
    • Missing essential credentials with no demo/mock mode
- When runnable=false, pick the MOST SPECIFIC category from this list:
{not_runnable_categories_list}

INSTRUCTIONS:
1. READ the README fully — find Quick Start / Usage / Examples section.
2. READ the source files to understand entry points & required arguments.
3. required_credentials: ONLY truly essential credentials (not optional).
4. credential_hints: describe WHAT each credential is (e.g. "OpenAI API key for GPT-4 calls").
5. pre_run_steps: commands to download weights, init DB, etc.
6. run_command MUST be immediately executable — no <placeholder> values.
7. what_user_needs: concrete list of things the user must provide/do if not runnable.
8. workaround: if not runnable, suggest the best alternative approach.

Return ONLY valid JSON — no markdown fences, no prose:
{{
  "summary": "1-2 sentence description of what this repo does",
  "project_type": "cli_tool|web_app|api|ml_model|library|notebook|script|docker|other",
  "runnable": true,
  "not_runnable_category": "",
  "why_not_runnable": "",
  "what_user_needs": [],
  "workaround": "",
  "uses_docker": false,
  "docker_command": "",
  "entry_point": "relative/path/to/entry.py",
  "run_command": "EXACT shell command",
  "pre_run_steps": [],
  "install_command": "pip install -r requirements.txt",
  "env_vars": {{"KEY": "value"}},
  "required_credentials": ["ENV_VAR_NAME"],
  "credential_hints": {{"ENV_VAR_NAME": "what this key is used for"}},
  "extra_deps": ["package_missing_from_requirements"],
  "known_issues": ["description of known issue"],
  "quickstart_steps": ["step1", "step2"],
  "notes": "any important caveats from README"
}}"""

    try:
        raw = await llm_chat(
            system=(
                "You are an expert Python project execution specialist. "
                "Analyse the repository carefully — read README AND source files. "
                "Be STRICT about runnability: only mark runnable=true if you "
                "can construct a working run command right now. "
                "Return ONLY valid JSON. No markdown fences. No preamble."
            ),
            user=prompt,
            # The execution plan is the single most important LLM output in
            # the app — everything downstream (run command, pre-steps, env
            # vars, credential detection) comes from this JSON. Reasoning
            # headroom so it can never come back empty on a complex repo.
            max_tokens=6000,
            metrics=metrics,
            temperature=0.1,
        )
        result = _parse_json(raw)
        # Ensure all required keys exist
        result.setdefault("runnable", True)
        result.setdefault("not_runnable_category", "other")
        result.setdefault("why_not_runnable", "")
        result.setdefault("what_user_needs", [])
        result.setdefault("workaround", "")
        result.setdefault("credential_hints", {})
        result.setdefault("required_credentials", [])
        return result
    except Exception as exc:
        log.warning("LLM plan failed: %s", exc)
        return _fallback_plan(repo_ctx, analysis)


def _fallback_plan(repo_ctx: dict, analysis: dict) -> dict:
    """Heuristic fallback when LLM call fails."""
    entry = (
        analysis.get("entry_point")
        or next(
            (f for f in repo_ctx.get("entry_candidates", []) if f), "main.py"
        )
    )
    # If no entry point and only setup files → probably a library
    setup_only = (
        not repo_ctx.get("entry_candidates")
        and any(f in repo_ctx.get("setup_files", []) for f in
                ["setup.py", "pyproject.toml"])
    )
    return {
        "summary":              "LLM analysis unavailable — using heuristics.",
        "project_type":         "library" if setup_only else "script",
        "runnable":             not setup_only,
        "not_runnable_category": "pure_library" if setup_only else "",
        "why_not_runnable":     "No executable entry point found; appears to be a library." if setup_only else "",
        "what_user_needs":      ["Import the package in your own script."] if setup_only else [],
        "workaround":           "pip install -e . && python -c 'import <package>'" if setup_only else "",
        "uses_docker":          False,
        "docker_command":       "",
        "entry_point":          entry,
        "run_command":          f"python {entry}",
        "pre_run_steps":        [],
        "install_command":      "pip install -r requirements.txt",
        "env_vars":             {},
        "required_credentials": [],
        "credential_hints":     {},
        "extra_deps":           [],
        "known_issues":         [],
        "quickstart_steps":     [f"python {entry}"],
        "notes":                "",
    }


# ─────────────────────────────────────────────────────────────────────────────
#  LLM Diagnosis  (per failed attempt)
# ─────────────────────────────────────────────────────────────────────────────

async def _llm_diagnose(
    run_cmd:       str,
    run_output:    str,
    attempt:       int,
    error_class:   str,
    task:          str,
    workspace:     str,
    venv_path:     str,
    repo_ctx:      dict,
    file_contents: dict[str, str],
    fix_journal:   list[dict],
    metrics:       dict,
) -> dict:
    journal_summary = _summarise_journal(fix_journal)

    # Source files mentioned in traceback
    error_files = re.findall(r'File "([^"]+\.py)"', run_output)
    relevant: dict[str, str] = {}
    for ef in error_files[:3]:
        basename = os.path.basename(ef)
        for kp, content in file_contents.items():
            if os.path.basename(kp) == basename:
                relevant[kp] = content[:800]
                break

    # A missing artifact has no traceback, so the loop above finds nothing.
    # The LLM still needs to see the entry point it must add the save call to.
    if error_class == "missing_artifact" and not relevant:
        for cand in re.findall(r"([\w./\-]+\.py)", run_cmd):
            base = os.path.basename(cand)
            for kp, content in file_contents.items():
                if os.path.basename(kp) == base:
                    relevant[kp] = content[:2500]
                    break
            if relevant:
                break

    extra_context = ""

    if error_class in ("missing_argument", "missing_artifact"):
        help_output = await _fetch_help_output(run_cmd, workspace, venv_path)
        if help_output and error_class == "missing_artifact":
            extra_context = (
                f"\n━━━━ --help OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{help_output[:3000]}\n\n"
                f"INSTRUCTION: look for the option that sets the output file "
                f"and build a revised_command that uses it."
            )
        elif help_output:
            extra_context = (
                f"\n━━━━ --help OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{help_output[:3000]}\n\n"
                f"TASK (use to infer argument values): {task}\n\n"
                f"INSTRUCTION: Build a revised_command supplying ALL required "
                f"options with sensible values inferred from the task description. "
                f"For text/topic options use the task as value. "
                f"For file paths use './outputs/' as output dir. "
                f"For model/size options pick the smallest/fastest default."
            )
        elif error_class == "missing_argument":
            extra_context = (
                f"\nNOTE: Missing-argument error. Task: '{task}'. "
                f"Build a revised_command with required args from the task."
            )

    if error_class == "install_only":
        extra_context += (
            f"\nNOTE: the previous command only installed a package. Installing "
            f"is a pre-step, not a run. Task: '{task}'. Give a revised_command "
            f"that actually exercises the repo — run its entry point, its "
            f"console script, or a short driver you place in files_to_create "
            f"that imports the library and prints or saves a real result."
        )

    if error_class == "missing_artifact":
        extra_context += (
            f"\nNOTE: the command already exits 0 — do NOT chase a crash. "
            f"The problem is that the task's output file was never written. "
            f"Task: '{task}'. Either pass the option that sets the output path "
            f"(check the --help output / the project's CLI), or add the explicit "
            f"save call to the entry point and put that file's COMPLETE new "
            f"content in files_to_create. Do not replace a working script with "
            f"a guessed API: use only functions you can see in RELEVANT SOURCE "
            f"FILES."
        )

    if error_class == "unexpected_kwarg":
        m   = AUTOFIX_PATTERNS["unexpected_kwarg"].search(run_output)
        kw  = m.group(1) if m else ""
        extra_context += (
            f"\nNOTE: a library removed the keyword argument '{kw}' that this "
            f"repo still passes — the repo predates the installed version. "
            f"No command-line option can fix this. Either pin the library to a "
            f"release that still accepted '{kw}' via packages_to_install, or "
            f"delete the '{kw}' argument from the call site and put the file's "
            f"COMPLETE new content in files_to_create. Prefer editing the call "
            f"site when the keyword was a no-op or cosmetic."
        )

    if AUTOFIX_PATTERNS["api_change"].search(run_output):
        m   = AUTOFIX_PATTERNS["api_change"].search(run_output)
        sym = m.group(1) if m else ""
        extra_context += (
            f"\nNOTE: '{sym}' was removed/renamed. "
            f"Fix: pin an older version (e.g. 'package<X.Y')."
        )

    prompt = f"""A Python project run command failed. Diagnose the ROOT CAUSE and give a precise fix.

FAILED COMMAND:
{run_cmd}

ERROR OUTPUT:
{_clip(run_output, 3000, 900)}
{extra_context}

RELEVANT SOURCE FILES:
{json.dumps(relevant, indent=2)[:1500] if relevant else "(none)"}

REPO FILE TREE: {repo_ctx['tree'][:25]}
ATTEMPT: {attempt + 1}/{MAX_RETRIES}

FIX JOURNAL (do NOT repeat these):
{journal_summary}

Return ONLY valid JSON — no markdown:
{{
  "fix_type": "install_package|set_env_var|create_file|fix_command|other",
  "root_cause": "Exact root cause in one clear sentence",
  "explanation": "What this fix does and why (2-3 sentences) — description only, never code",
  "detail": "One-line human-readable summary of the fix. NEVER put file content or a diff here — it is not applied, it is only shown in logs.",
  "packages_to_install": ["pkg==version"],
  "env_vars_to_set": {{"KEY": "value"}},
  "files_to_create": [{{"path": "rel/path", "content": "COMPLETE new file content"}}],
  "revised_command": "complete corrected shell command if fix_command, else empty"
}}

CRITICAL: this JSON is applied mechanically — nothing in "explanation" or "detail" ever
touches disk. If the fix requires ANY change to a source file (existing or new), you MUST
put the file's COMPLETE new content (the whole file, not a diff/snippet) in
"files_to_create", using the file's exact existing relative path from REPO FILE TREE so it
overwrites in place. This applies even when fix_type is "other" — fix_type is just a label,
files_to_create is what actually takes effect. A fix that only describes a code change in
words and leaves files_to_create empty will be silently discarded and the exact same failure
will repeat next attempt."""

    try:
        raw = await llm_chat(
            system=(
                "You are a Python debugging expert. "
                "Identify the exact root cause and provide a working fix. "
                "For missing-argument errors, read --help carefully and build "
                "a complete revised_command with all required options. "
                "Any fix that edits a source file's contents is only real if the "
                "complete new file content is placed in files_to_create — prose "
                "in 'detail' or 'explanation' describing the code change is "
                "never applied. "
                "Return ONLY valid JSON. No markdown."
            ),
            user=prompt,
            # This call is asked for a file's COMPLETE new content, and the
            # reasoning models behind it spend tokens before emitting any. At
            # 3000 a pymaze attempt came back with finish_reason=length and no
            # content at all, burning a retry for nothing.
            max_tokens=6000,
            metrics=metrics,
            temperature=0.1,
        )
        return _parse_json(raw)
    except Exception as exc:
        log.warning("LLM diagnosis failed: %s", exc)
        return {
            "fix_type":            "unknown",
            "root_cause":          str(exc),
            "explanation":         str(exc),
            "detail":              "",
            "packages_to_install": [],
            "env_vars_to_set":     {},
            "files_to_create":     [],
            "revised_command":     "",
        }


async def _fetch_help_output(run_cmd: str, workspace: str, venv_path: str) -> str:
    """Run the failed command with --help and return its output."""
    base = re.sub(r"^mkdir\s+-p\s+\S+\s*&&\s*", "", run_cmd.strip())
    base = re.sub(r"^yes\s*\|\s*bash\s+-c\s+", "", base).strip("'\"")
    base = re.sub(r"^timeout\s+\d+\s+", "", base).strip()
    if not re.match(r"python|python3|\.py", base, re.I):
        return ""
    help_cmd = base + " --help"
    _, out   = await _run_cmd(["bash", "-c", help_cmd], workspace, timeout=15)
    return out.strip()


# ─────────────────────────────────────────────────────────────────────────────
#  Run Script Generator
# ─────────────────────────────────────────────────────────────────────────────

def _generate_run_script(
    repo_full_name:   str,
    task:             str,
    llm_plan:         dict,
    pm_info:          dict,
    env_hints:        dict,
    run_cmd:          str,
    pre_steps:        list[str],
    env_export_lines: list[str],
    final_rc:         int,
    workspace:        Optional[str],
) -> str:
    repo_name      = repo_full_name.split("/")[-1]
    status_tag     = "SUCCESS" if final_rc == 0 else "ATTEMPTED (may need adjustments)"
    required_creds = llm_plan.get("required_credentials", [])
    env_vars       = llm_plan.get("env_vars", {})
    known_issues   = llm_plan.get("known_issues", [])
    qs_steps       = llm_plan.get("quickstart_steps", [])
    notes          = llm_plan.get("notes", "")
    uses_docker    = llm_plan.get("uses_docker") or "docker" in run_cmd.lower()

    lines = [
        "#!/usr/bin/env bash",
        "# ================================================================",
        "#  RepoRunner — Auto-Generated Execution Script",
        f"#  Repository : {repo_full_name}",
        f"#  Task       : {task}",
        f"#  Status     : {status_tag}",
        "# ================================================================",
        "set -euo pipefail",
        "",
        "# ── Step 1: Clone ─────────────────────────────────────────────────",
        f'git clone --depth 1 "https://github.com/{repo_full_name}.git" "{repo_name}"',
        f'cd "{repo_name}"',
        "",
    ]

    if uses_docker:
        lines += [
            "# ── Step 2: Docker setup ─────────────────────────────────────────",
            "# Ensure Docker is running: docker info",
            "mkdir -p outputs",
            "",
        ]
    else:
        lines += [
            "# ── Step 2: Virtual environment ──────────────────────────────────",
            "python3 -m venv .venv",
            "source .venv/bin/activate",
            "pip install --upgrade pip --quiet",
            "",
            "# ── Step 3: Install dependencies ─────────────────────────────────",
        ]
        pm   = pm_info.get("name", "pip-req")
        pmf  = pm_info.get("file", "requirements.txt")
        if pm == "poetry"  and shutil.which("poetry"):
            lines.append("poetry install --no-interaction")
        elif pm == "pipenv" and shutil.which("pipenv"):
            lines.append("pipenv install --skip-lock")
        elif pm == "uv"     and shutil.which("uv"):
            lines.append("uv sync")
        elif pm == "conda":
            lines.append(f"conda env update -f {pmf}")
        elif pmf.endswith(".txt"):
            lines.append(f"pip install -r {pmf}")
        elif pmf in ("setup.py", "pyproject.toml", "setup.cfg"):
            lines.append("pip install -e .")
        else:
            lines.append(
                "pip install -r requirements.txt 2>/dev/null || "
                "pip install -e . 2>/dev/null || true"
            )
        lines.append("")

    # Credentials
    all_creds: dict[str, str] = {}
    for k in required_creds:
        all_creds[k] = env_vars.get(k, "")
    for k, v in env_vars.items():
        if k not in all_creds:
            all_creds[k] = v

    has_unfilled = any(
        not v or str(v).upper() in ("YOUR_VALUE_HERE", "REPLACE_ME", "")
        for v in all_creds.values()
    )

    if all_creds:
        lines += ["# ── Step 4: Environment variables ────────────────────────────────"]
        if has_unfilled:
            lines.append("# ⚠  Fill in REQUIRED values before running!")
        hints = llm_plan.get("credential_hints", {})
        for k, v in all_creds.items():
            hint    = hints.get(k, "")
            comment = f"  # ← {hint}" if hint else (
                "  # ← REQUIRED" if k in required_creds else "  # ← optional")
            if not v or str(v).upper() in ("YOUR_VALUE_HERE", "REPLACE_ME", ""):
                lines.append(f'export {k}=""{comment}')
            else:
                lines.append(f'export {k}="{v}"{comment}')
        for k, v in env_hints.items():
            if k not in all_creds:
                lines.append(f'# export {k}="{v}"   # hint from .env.example')
        lines.append("")
    elif env_export_lines:
        lines += (
            ["# ── Step 4: Environment variables ────────────────────────────────"]
            + env_export_lines + [""]
        )

    step_n = 5
    if pre_steps:
        lines += [f"# ── Step {step_n}: Pre-run setup ─────────────────────────────────────"]
        for step in pre_steps:
            lines.append(step)
        lines.append("")
        step_n += 1

    lines += [
        f"# ── Step {step_n}: Run ────────────────────────────────────────────────",
        "mkdir -p outputs",
        run_cmd,
        "",
    ]

    if known_issues:
        lines += ["# ── Known issues ─────────────────────────────────────────────────"]
        for issue in known_issues:
            lines.append(f"# {issue}")
        lines.append("")

    if notes:
        lines += ["# ── Notes ────────────────────────────────────────────────────────",
                  f"# {notes}", ""]

    if qs_steps and len(qs_steps) > 1:
        lines += ["# ── README Quick Start (reference) ───────────────────────────────"]
        for s in qs_steps[:8]:
            lines.append(f"# {s}")
        lines.append("")

    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
#  Success Summary
# ─────────────────────────────────────────────────────────────────────────────

async def _build_success_summary(
    task:          str,
    repo:          str,
    iteration_log: list,
    fix_journal:   list,
    run_cmd:       str,
    stdout:        str,
    metrics:       dict,
) -> str:
    autofix = sum(1 for f in fix_journal if f.get("result") == "autofix_applied")
    llm_fix = sum(1 for f in fix_journal if f.get("result") == "llm_fix_applied")
    prompt  = (
        f"Summarise this successful execution in 2-3 sentences.\n"
        f"Task: {task}\nRepo: {repo}\n"
        f"Iterations: {len(iteration_log)} | "
        f"Auto-fixes: {autofix} | LLM fixes: {llm_fix}\n"
        f"Final command: {run_cmd}\n"
        f"Output (first 600 chars):\n{stdout[:600]}"
    )
    try:
        return await llm_chat(
            system="Write a concise, helpful execution summary. No markdown.",
            # 200 was too tight for a reasoning model (gpt-oss-120b): the
            # reasoning pass alone can consume the whole budget and return an
            # empty/truncated summary. The answer is still only 2-3 sentences.
            user=prompt, max_tokens=800, metrics=metrics, temperature=0.2)
    except Exception:
        return (
            f"✅ Completed in {len(iteration_log)} attempt(s). "
            f"Auto-fixes: {autofix} | LLM fixes: {llm_fix}. "
            f"Command: {run_cmd}"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Failure Guide
# ─────────────────────────────────────────────────────────────────────────────

async def _build_failure_guide(
    task:           str,
    repo_full_name: str,
    iteration_log:  list,
    fix_journal:    list,
    run_cmd:        str,
    stdout:         str,
    stderr:         str,
    llm_plan:       dict,
    metrics:        dict,
) -> tuple[str, str]:
    error_msg      = _error_summary(stderr, stdout)
    required_creds = llm_plan.get("required_credentials", [])
    why_not        = llm_plan.get("why_not_runnable", "")
    known_issues   = llm_plan.get("known_issues", [])
    qs_steps       = llm_plan.get("quickstart_steps", [])
    repo_name      = repo_full_name.split("/")[-1]

    prompt = f"""A GitHub repository failed to run after {len(iteration_log)} automated attempts.
Write a clear failure analysis and manual run guide.

Repo        : {repo_full_name}
Task        : {task}
Final cmd   : {run_cmd}
Final error : {error_msg}
Why not run : {why_not}
Required    : {required_creds}
Known issues: {known_issues}
Fix journal : {_summarise_journal(fix_journal)}
Stdout tail : {stdout[-500:]}
Stderr tail : {stderr[-500:]}

Return ONLY valid JSON:
{{
  "failure_reason": "Clear 1-sentence root cause",
  "category": "missing_credentials|missing_data|gpu_required|build_error|incompatible|other",
  "what_you_need": ["Item 1", "Item 2"],
  "manual_steps": ["Step 1: ...", "Step 2: ..."],
  "suggested_command": "best guess at a working command",
  "one_line_summary": "Short failure summary for the UI header"
}}"""

    try:
        raw  = await llm_chat(
            system="You are a Python debugging expert. Return ONLY valid JSON.",
            # Reasoning headroom — see the note in _llm_diagnose.
            user=prompt, max_tokens=4000, metrics=metrics, temperature=0.2)
        data = _parse_json(raw)
    except Exception:
        data = {
            "failure_reason":    error_msg or "Unknown error",
            "category":          "other",
            "what_you_need":     ([f"Provide: {', '.join(required_creds)}"]
                                  if required_creds else []),
            "manual_steps":      [f"Run: {run_cmd}"],
            "suggested_command": run_cmd,
            "one_line_summary":  f"Failed after {len(iteration_log)} attempts: {error_msg}",
        }

    md: list[str] = [
        "## ❌ Automated Execution Failed",
        "",
        f"**Repository:** `{repo_full_name}`  ",
        f"**Root cause:** {data.get('failure_reason', 'Unknown')}",
        "",
    ]

    if data.get("what_you_need"):
        md += ["### What You Need", ""]
        for item in data["what_you_need"]:
            md.append(f"- {item}")
        md.append("")

    if required_creds:
        md += ["### Required Credentials", ""]
        hints = llm_plan.get("credential_hints", {})
        for cred in required_creds:
            hint = hints.get(cred, "")
            md.append(f"- `{cred}`" + (f" — {hint}" if hint else "")
                      + f"\n  `export {cred}=\"your_value\"`")
        md.append("")

    if known_issues:
        md += ["### Known Issues", ""]
        for issue in known_issues:
            md.append(f"- {issue}")
        md.append("")

    md += ["### Manual Steps", "", "```bash"]
    md.append(f"git clone --depth 1 https://github.com/{repo_full_name}.git {repo_name}")
    md.append(f"cd {repo_name}")
    md.append("python3 -m venv .venv && source .venv/bin/activate")
    md.append("pip install -r requirements.txt")
    if required_creds:
        for c in required_creds:
            md.append(f'export {c}="YOUR_{c}_HERE"')
    for step in data.get("manual_steps", [])[:8]:
        md.append(f"# {step}")
    suggested = data.get("suggested_command", run_cmd)
    if suggested:
        md.append(suggested)
    md += ["```", ""]

    if fix_journal:
        md += ["### Attempts Made (Auto-Fix Journal)", ""]
        for entry in fix_journal[:6]:
            expl = (entry.get("explanation") or entry.get("root_cause") or "")[:80]
            md.append(
                f"- Attempt {entry.get('attempt', '?')}: "
                f"`{entry.get('fix_type', '?')}` — {expl}"
            )
        md.append("")

    one_line     = data.get("one_line_summary",
                            f"❌ Failed after {len(iteration_log)} attempts: {error_msg}")
    manual_guide = "\n".join(md)
    return one_line, manual_guide


# ─────────────────────────────────────────────────────────────────────────────
#  Clone
# ─────────────────────────────────────────────────────────────────────────────

async def _clone_repo(repo_full_name: str, metrics: dict,
                      max_attempts: int = 3) -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        url = f"https://{token}@github.com/{repo_full_name}.git"
    else:
        url = f"https://github.com/{repo_full_name}.git"
    log_url  = f"https://github.com/{repo_full_name}.git"
    git_env  = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    last_err = ""

    for attempt in range(1, max_attempts + 1):
        workspace = tempfile.mkdtemp(prefix="executor_")
        log.info("Cloning %s → %s (attempt %d/%d)", log_url, workspace, attempt, max_attempts)
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", url, workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=git_env,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            shutil.rmtree(workspace, ignore_errors=True)
            last_err = "git clone timed out after 120s"
            if attempt < max_attempts:
                await asyncio.sleep(5)
            continue

        if proc.returncode == 0:
            return workspace

        last_err = stderr.decode(errors="ignore")[:600]
        shutil.rmtree(workspace, ignore_errors=True)
        log.warning("Clone attempt %d failed: %s", attempt, last_err[:120])
        if attempt < max_attempts:
            await asyncio.sleep(5 * attempt)   # 5s, 10s back-off

    raise RuntimeError(f"git clone failed after {max_attempts} attempts: {last_err}")


# ─────────────────────────────────────────────────────────────────────────────
#  Workspace Inspection
# ─────────────────────────────────────────────────────────────────────────────

def _inspect_workspace(workspace: str) -> dict:
    skip = {
        ".git", ".venv", "__pycache__", "node_modules",
        ".tox", ".eggs", "dist", "build", ".mypy_cache",
    }
    tree, setup_files, key_sources = [], [], []
    total = 0

    for root, dirs, files in os.walk(workspace):
        dirs[:] = sorted(d for d in dirs if d not in skip)
        for f in files:
            total += 1
            rel = os.path.relpath(os.path.join(root, f), workspace)
            tree.append(rel)
            fl  = f.lower()
            if fl in {
                "requirements.txt", "requirements-dev.txt",
                "pyproject.toml", "setup.py", "setup.cfg",
                "environment.yml", "pipfile", "pipfile.lock",
                "uv.lock", "poetry.lock",
                ".env.example", ".env.sample", ".env.template",
                "makefile", "dockerfile",
                "docker-compose.yml", "docker-compose.yaml",
            }:
                setup_files.append(rel)
            if fl.endswith(".py") or fl.endswith(".ipynb"):
                key_sources.append(rel)

    readme_text = ""
    for rname in ("README.md", "README.rst", "README.txt", "README"):
        rp = os.path.join(workspace, rname)
        if os.path.isfile(rp):
            try:
                readme_text = Path(rp).read_text(
                    encoding="utf-8", errors="ignore")[:5000]
            except Exception:
                pass
            break

    entry_kw = (
        "main", "app", "run", "cli", "demo",
        "inference", "__main__", "server", "start", "predict",
    )
    entry_candidates = [
        f for f in key_sources
        if any(k in os.path.basename(f).lower() for k in entry_kw)
    ]

    source_snippets: dict[str, str] = {}
    for fp in (entry_candidates or key_sources)[:6]:
        try:
            txt = Path(os.path.join(workspace, fp)).read_text(
                encoding="utf-8", errors="ignore")
            source_snippets[fp] = "\n".join(txt.splitlines()[:100])
        except Exception:
            pass

    summary = (
        f"{total} files | setup: {setup_files or 'none'} | "
        f"py sources: {len(key_sources)} | "
        f"entry candidates: "
        f"{[os.path.basename(f) for f in entry_candidates[:5]]}"
    )
    return {
        "tree":             tree[:100],
        "file_count":       total,
        "setup_files":      setup_files,
        "key_sources":      key_sources[:30],
        "entry_candidates": entry_candidates[:10],
        "source_snippets":  source_snippets,
        "readme":           readme_text,
        "summary":          summary,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Package Manager
# ─────────────────────────────────────────────────────────────────────────────

def _detect_package_manager(workspace: str) -> dict:
    for name, marker, cmd in PKG_MANAGERS:
        if os.path.isfile(os.path.join(workspace, marker)):
            return {"name": name, "file": marker, "cmd": cmd}
    return {"name": "pip-bare", "file": "none", "cmd": None}


def _pip_path(venv_path: str) -> str:
    if sys.platform == "win32":
        return os.path.join(venv_path, "Scripts", "pip.exe")
    return os.path.join(venv_path, "bin", "pip")


def _python_path(venv_path: str) -> str:
    if sys.platform == "win32":
        return os.path.join(venv_path, "Scripts", "python.exe")
    return os.path.join(venv_path, "bin", "python")


# ─────────────────────────────────────────────────────────────────────────────
#  Python version detection — venv must use the repo's own required Python,
#  not whatever version happens to run this backend server. Without this, a
#  repo pinned to an old Python (say 3.8-only syntax/deps) or one requiring a
#  brand-new version silently gets built against the wrong interpreter and
#  fails in ways that look like dependency bugs.
# ─────────────────────────────────────────────────────────────────────────────

# Candidate interpreter binaries to look for on PATH, newest first so ties
# in _find_best_python prefer the newer one without extra sorting logic.
# Python 2 hit end-of-life at 2.7 (the only minor version anyone still pins)
# — old repos still declare it via runtime.txt / setup.py python_requires.
_PYTHON_CANDIDATES = [f"python3.{m}" for m in range(14, 5, -1)] + ["python2.7"]


def _detect_python_requirement(workspace: str) -> Optional[str]:
    """
    Look for a declared Python version/constraint, most explicit source
    first. Returns a PEP 440-style specifier string (e.g. ">=3.8,<3.11") or
    None if the repo doesn't declare one.
    """
    def read(fname: str) -> Optional[str]:
        fp = os.path.join(workspace, fname)
        if os.path.isfile(fp):
            try:
                return Path(fp).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                return None
        return None

    def minor_pin(version_str: str) -> Optional[str]:
        """'3.9.18' / '3.9' -> '==3.9.*' (only minor granularity is meaningful
        here since we only ever have one interpreter per minor version)."""
        m = re.match(r"(\d+)\.(\d+)", version_str.strip())
        return f"=={m.group(1)}.{m.group(2)}.*" if m else None

    def poetry_constraint(spec: str) -> Optional[str]:
        """Convert poetry's ^/~ shorthand to a plain PEP 440 range."""
        spec = spec.strip()
        m = re.match(r"\^(\d+)\.(\d+)", spec)
        if m:
            major, minor = int(m.group(1)), int(m.group(2))
            return f">={major}.{minor},<{major + 1}"
        m = re.match(r"~(\d+)\.(\d+)", spec)
        if m:
            major, minor = int(m.group(1)), int(m.group(2))
            return f">={major}.{minor},<{major}.{minor + 1}"
        if re.match(r"^[<>=!]", spec):
            return spec   # already a plain specifier, e.g. ">=3.9,<3.13"
        return minor_pin(spec)

    # 1. .python-version (pyenv) — most explicit, wins outright
    pv = read(".python-version")
    if pv and pv.strip():
        spec = minor_pin(pv.splitlines()[0])
        if spec:
            return spec

    # 2. runtime.txt (Heroku-style: "python-3.9.18")
    rt = read("runtime.txt")
    if rt:
        m = re.search(r"python-(\d+\.\d+(?:\.\d+)?)", rt)
        if m:
            spec = minor_pin(m.group(1))
            if spec:
                return spec

    # 3. pyproject.toml — PEP 621 `requires-python` or Poetry's `python`
    pp = read("pyproject.toml")
    if pp:
        m = re.search(r'requires-python\s*=\s*["\']([^"\']+)["\']', pp)
        if m:
            return m.group(1)
        m = re.search(r'^\s*python\s*=\s*["\']([^"\']+)["\']', pp, re.MULTILINE)
        if m and m.group(1).strip() not in ("*",):
            spec = poetry_constraint(m.group(1))
            if spec:
                return spec

    # 4. setup.cfg
    sc = read("setup.cfg")
    if sc:
        m = re.search(r'python_requires\s*=\s*(.+)', sc)
        if m:
            return m.group(1).strip()

    # 5. setup.py
    sp = read("setup.py")
    if sp:
        m = re.search(r'python_requires\s*=\s*["\']([^"\']+)["\']', sp)
        if m:
            return m.group(1)

    # 6. Dockerfile — last resort, weakest signal
    df = read("Dockerfile")
    if df:
        m = re.search(r'FROM\s+python:(\d+\.\d+)', df, re.IGNORECASE)
        if m:
            return minor_pin(m.group(1))

    return None


PYENV_INSTALL_TIMEOUT = int(os.environ.get("PYENV_INSTALL_TIMEOUT", "360"))  # seconds


def _pyenv_bin() -> Optional[str]:
    """
    Locate the pyenv binary. This backend process's own PATH was captured at
    startup, before pyenv may have been installed/added to ~/.bashrc, so
    shutil.which() alone can miss it — check the standard install location
    directly first.
    """
    direct = os.path.expanduser("~/.pyenv/bin/pyenv")
    if os.path.isfile(direct):
        return direct
    return shutil.which("pyenv")


async def _pyenv_install(requirement, spec) -> tuple[Optional[str], str]:
    """
    On-demand install a Python version satisfying `spec` via pyenv, when no
    already-installed interpreter matches. Returns (python_path or None, msg).
    """
    from packaging.version import Version

    pyenv = _pyenv_bin()
    if not pyenv:
        return None, "pyenv not available"

    pyenv_root = os.path.dirname(os.path.dirname(pyenv))  # .../pyenv/bin/pyenv -> .../pyenv

    ok, out = await _run_cmd([pyenv, "install", "--list"], os.getcwd(), timeout=30)
    if not ok:
        return None, f"pyenv install --list failed: {out[:200]}"

    # Only consider plain "X.Y.Z" releases (skip alpha/rc/dev/anaconda/etc.)
    installable = []
    for line in out.splitlines():
        line = line.strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+", line):
            continue
        try:
            v = Version(line)
            if spec.contains(v):
                installable.append((v, line))
        except Exception:
            continue

    if not installable:
        return None, f"no installable pyenv version satisfies {requirement}"

    installable.sort(key=lambda x: x[0], reverse=True)
    _, target = installable[0]

    ok, out = await _run_cmd(
        [pyenv, "install", "--skip-existing", target],
        os.getcwd(), timeout=PYENV_INSTALL_TIMEOUT,
    )
    py_path = os.path.join(pyenv_root, "versions", target, "bin", "python")
    if ok and os.path.isfile(py_path):
        return py_path, f"pyenv installed Python {target}"
    return None, f"pyenv install {target} failed: {out[-300:]}"


def _pyenv_installed_interpreters() -> dict[str, str]:
    """
    Concrete (non-shim) interpreter paths for every pyenv-installed version,
    keyed by minor version ("3.11" -> .../versions/3.11.15/bin/python).
    Shims on PATH resolve lazily based on pyenv's global/local version
    config at *invocation* time, so `shutil.which("python3.11")` can return
    a shim that fails with "command not found" even right after pyenv just
    installed that exact version — the concrete versions/ path always works.
    """
    pyenv = _pyenv_bin()
    if not pyenv:
        return {}
    versions_dir = os.path.join(os.path.dirname(os.path.dirname(pyenv)), "versions")
    if not os.path.isdir(versions_dir):
        return {}

    from packaging.version import Version

    best: dict[str, tuple] = {}
    for entry in os.listdir(versions_dir):
        py = os.path.join(versions_dir, entry, "bin", "python")
        if not os.path.isfile(py):
            continue
        m = re.match(r"(\d+)\.(\d+)", entry)
        if not m:
            continue
        minor = f"{m.group(1)}.{m.group(2)}"
        try:
            v = Version(entry)
        except Exception:
            continue
        if minor not in best or v > best[minor][0]:
            best[minor] = (v, py)
    return {minor: path for minor, (_, path) in best.items()}


async def _find_best_python(requirement: Optional[str]) -> tuple[str, str]:
    """
    Pick the interpreter to build the venv with.
    Returns (python_executable, status_message).
    """
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    if not requirement:
        return sys.executable, f"No Python version declared — using host default (Python {current})"

    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
        spec = SpecifierSet(requirement)
    except Exception as e:
        return sys.executable, f"Couldn't parse Python requirement {requirement!r} ({e}) — using host default (Python {current})"

    # Concrete pyenv installs first (always reliable), then every python3.X
    # on PATH that ISN'T a pyenv shim (shims are fragile — see docstring
    # above), plus the interpreter already running us.
    found: dict[str, str] = {current: sys.executable}
    found.update(_pyenv_installed_interpreters())
    for name in _PYTHON_CANDIDATES:
        path = shutil.which(name)
        if path and "/.pyenv/shims/" not in path:
            found.setdefault(name.removeprefix("python"), path)

    candidates = []
    for ver_str, path in found.items():
        try:
            candidates.append((Version(f"{ver_str}.0"), ver_str, path))
        except Exception:
            continue

    matching = sorted((c for c in candidates if spec.contains(c[0])), key=lambda c: c[0], reverse=True)
    if matching:
        _, ver_str, path = matching[0]
        return path, f"Repo requires Python {requirement} — using Python {ver_str} ({path})"

    # Nothing installed satisfies the constraint — try compiling the exact
    # version via pyenv before giving up (bounded by PYENV_INSTALL_TIMEOUT).
    pyenv_path, pyenv_msg = await _pyenv_install(requirement, spec)
    if pyenv_path:
        return pyenv_path, f"Repo requires Python {requirement} — {pyenv_msg} ({pyenv_path})"

    # pyenv unavailable/failed — fall back to the closest already-installed
    # version rather than failing outright (best-effort > hard stop).
    closest = sorted(candidates, key=lambda c: c[0], reverse=True)
    if closest:
        _, ver_str, path = closest[0]
        return path, (f"Repo requires Python {requirement} but no matching interpreter is "
                       f"installed and {pyenv_msg} — falling back to Python {ver_str} (may not work)")

    return sys.executable, f"Repo requires Python {requirement} — no alternative interpreter found, using host default (Python {current})"


# ─────────────────────────────────────────────────────────────────────────────
#  Environment Setup
# ─────────────────────────────────────────────────────────────────────────────

async def _create_venv(python_exe: str, venv_path: str) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        python_exe, "-m", "venv", venv_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        return False, "venv creation timed out"
    return proc.returncode == 0, err.decode(errors="ignore")


def _looks_like_missing_venv_module(err: str) -> bool:
    return "No module named venv" in err or "No module named 'venv'" in err


async def _create_venv_via_virtualenv(python_exe: str, venv_path: str) -> tuple[bool, str]:
    """
    Python 2 (and some minimal Python 3 installs) has no stdlib `venv`
    module. Orchestrating `virtualenv` from OUR OWN (modern) interpreter via
    `-p <target>` doesn't work for Python-2 targets — recent virtualenv
    releases probe the target with a script that uses 3.6+ syntax
    (variable annotations), which Python 2 can't even parse, so the probe
    itself crashes. Instead we install virtualenv INTO the target
    interpreter and let it build its own env, self-hosted: pip running
    under that interpreter automatically resolves the newest release still
    compatible with it (old for py2, current for py3), so no version needs
    to be hardcoded here.
    """
    ok, out = await _run_cmd(
        [python_exe, "-m", "pip", "install", "--quiet", "virtualenv"],
        os.getcwd(), timeout=120)
    if not ok:
        return False, f"could not install virtualenv into target interpreter: {out[:300]}"

    proc = await asyncio.create_subprocess_exec(
        python_exe, "-m", "virtualenv", venv_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
    except asyncio.TimeoutError:
        return False, "virtualenv creation timed out"
    return proc.returncode == 0, out_b.decode(errors="ignore")


async def _setup_environment(
    workspace: str,
    venv_path: str,
    pm_info:   dict,
    metrics:   dict,
) -> tuple[bool, str]:
    parts: list[str] = []

    requirement = _detect_python_requirement(workspace)
    python_exe, python_msg = await _find_best_python(requirement)
    parts.append(f"ℹ {python_msg}")

    venv_ok, err = await _create_venv(python_exe, venv_path)

    if not venv_ok and _looks_like_missing_venv_module(err):
        parts.append(f"⚠ '{os.path.basename(python_exe)} -m venv' unavailable "
                      f"({err[:150]}) — falling back to virtualenv")
        venv_ok, err = await _create_venv_via_virtualenv(python_exe, venv_path)

    if not venv_ok and python_exe != sys.executable:
        # Last resort: the selected interpreter may be broken/missing
        # entirely — retry with the host default before giving up, so a
        # version-selection choice can't fully block execution.
        parts.append(f"⚠ venv creation failed with {python_exe} ({err[:200]}), retrying with host default")
        venv_ok, err = await _create_venv(sys.executable, venv_path)

    if not venv_ok:
        return False, f"venv creation failed: {err[:300]}"
    parts.append("✓ Virtual environment created")

    pip = _pip_path(venv_path)
    await _run_cmd([pip, "install", "--upgrade", "pip", "--quiet"], workspace, timeout=60)

    pm_name = pm_info["name"]
    ok = True

    if pm_name == "uv" and shutil.which("uv"):
        ok, out = await _run_cmd(["uv", "sync"], workspace, timeout=300)
        parts += [f"{'✓' if ok else '⚠'} uv sync", out[-600:]]

    elif pm_name == "poetry" and shutil.which("poetry"):
        ok, out = await _run_cmd(
            ["poetry", "install", "--no-interaction"], workspace, timeout=300)
        parts += [f"{'✓' if ok else '⚠'} poetry install", out[-600:]]

    elif pm_name == "pipenv" and shutil.which("pipenv"):
        ok, out = await _run_cmd(
            ["pipenv", "install", "--skip-lock"], workspace, timeout=300)
        parts += [f"{'✓' if ok else '⚠'} pipenv install", out[-600:]]

    else:
        installed = False
        for fname, install_cmd in [
            ("requirements.txt",     [pip, "install", "-r", "requirements.txt"]),
            ("requirements-dev.txt", [pip, "install", "-r", "requirements-dev.txt"]),
            ("pyproject.toml",       [pip, "install", "."]),
            ("setup.py",             [pip, "install", "-e", "."]),
            ("setup.cfg",            [pip, "install", "."]),
        ]:
            if os.path.isfile(os.path.join(workspace, fname)):
                r, out = await _run_cmd(install_cmd, workspace, timeout=300)
                parts += [f"{'✓' if r else '⚠'} {' '.join(install_cmd[-2:])}", out[-600:]]
                installed = True
        if not installed:
            parts.append("⚠ No dependency file found — proceeding bare")

    return ok, "\n".join(filter(None, parts))


# ─────────────────────────────────────────────────────────────────────────────
#  Env-var Discovery
# ─────────────────────────────────────────────────────────────────────────────

def _discover_env_hints(workspace: str) -> dict[str, str]:
    hints: dict[str, str] = {}
    for fname in (".env.example", ".env.sample", ".env.template", ".env.default"):
        fp = os.path.join(workspace, fname)
        if os.path.isfile(fp):
            try:
                for line in Path(fp).read_text(
                        encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        hints[k.strip()] = v.strip()
            except Exception:
                pass
    return hints


# ─────────────────────────────────────────────────────────────────────────────
#  Run Command Builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_run_command(
    llm_plan:    dict,
    analysis:    dict,
    repo_ctx:    dict,
    input_files: list[str],
    workspace:   str,
    readme_cmds: dict,
) -> tuple[str, str]:
    """
    Returns (run_command, source_description).
    Priority: Docker > README primary > LLM plan > static analysis > heuristic.
    """
    ws = Path(workspace)

    # 1. Docker
    has_compose    = (ws / "docker-compose.yml").exists() or (ws / "docker-compose.yaml").exists()
    has_dockerfile = (ws / "Dockerfile").exists()

    if llm_plan.get("uses_docker") or has_compose or has_dockerfile:
        llm_docker = llm_plan.get("docker_command", "").strip()
        if has_compose:
            if llm_docker and "docker" in llm_docker:
                return _inject_inputs(llm_docker, input_files), "Docker (LLM)"
            if shutil.which("docker-compose"):
                return _inject_inputs("docker-compose up --build", input_files), "Docker (compose)"
            if shutil.which("docker"):
                return _inject_inputs("docker compose up --build", input_files), "Docker (plugin)"
        if has_dockerfile and shutil.which("docker"):
            if llm_docker and "docker" in llm_docker:
                return _inject_inputs(llm_docker, input_files), "Docker (LLM)"
            safe_tag     = re.sub(r"[^a-z0-9._-]", "-", ws.name.lower())[:40] or "repo-app"
            input_mount  = ""
            if input_files:
                first       = input_files[0]
                first_path  = first if first.startswith("inputs/") else f"inputs/{first}"
                input_mount = f' -v "$(pwd)/{first_path}:/{first_path}"'
            cmd = (
                f'docker build -t {safe_tag} . && '
                f'docker run --rm{input_mount} '
                f'-v "$(pwd)/outputs:/outputs" {safe_tag}'
            )
            return cmd, "Docker (Dockerfile)"

        # A Dockerfile/compose file is here but no docker binary is. Every
        # branch above is gated on shutil.which("docker"), so we silently fell
        # through to the venv path and the repo's own documented way of running
        # itself was ignored without a word. Say so — "it didn't use my
        # Dockerfile" is otherwise indistinguishable from a bug.
        _docker_skipped = (
            "docker-compose.yml" if has_compose else "Dockerfile"
        )
        log.warning(
            "%s found but no docker binary is available in this container — "
            "falling back to a virtualenv run. See DEPLOY.md to enable Docker "
            "builds.", _docker_skipped
        )

    # 2. README primary command
    readme_primary = readme_cmds.get("primary_cmd", "").strip()
    if readme_primary and _is_valid_shell_cmd(readme_primary):
        return (
            f"mkdir -p outputs && {_inject_inputs(readme_primary, input_files)}",
            "README (primary command)",
        )

    # 3. Any README run command
    for rc in readme_cmds.get("run_cmds", []):
        if _is_valid_shell_cmd(rc):
            return (
                f"mkdir -p outputs && {_inject_inputs(rc, input_files)}",
                "README (run_cmds)",
            )

    # 4. LLM plan
    cmd = llm_plan.get("run_command", "").strip()
    if cmd and _is_valid_shell_cmd(cmd):
        return (
            f"mkdir -p outputs && {_inject_inputs(cmd, input_files)}",
            "LLM analysis",
        )

    # 5. Static analyzer
    raw = (analysis.get("run_command") or "").strip()
    if raw and _is_valid_shell_cmd(raw):
        return (
            f"mkdir -p outputs && {_inject_inputs(raw, input_files)}",
            "static analysis",
        )

    # 6. Entry-point heuristic
    entry = (
        llm_plan.get("entry_point")
        or analysis.get("entry_point")
        or next((f for f in repo_ctx.get("entry_candidates", []) if f), "")
    )
    if entry:
        return (
            f"mkdir -p outputs && {_inject_inputs(_cmd_for_entry(entry), input_files)}",
            f"heuristic entry point ({entry})",
        )

    # 7. Last resort
    for src in repo_ctx.get("key_sources", []):
        if src.endswith((".py", ".ipynb")):
            return (
                f"mkdir -p outputs && {_inject_inputs(_cmd_for_entry(src), input_files)}",
                f"fallback ({src})",
            )

    return "mkdir -p outputs && python main.py", "last resort fallback"


def _is_valid_shell_cmd(cmd: str) -> bool:
    if not cmd:
        return False
    first = cmd.split()[0].lower().rstrip(":")
    NOT_COMMANDS = {
        "run", "execute", "start", "launch", "open", "use", "the", "a", "an",
        "to", "in", "with", "and", "or", "for", "this", "that", "then",
        "just", "simply", "first", "next", "finally", "note", "example",
    }
    return first not in NOT_COMMANDS


def _cmd_for_entry(entry: str) -> str:
    if entry.endswith(".ipynb"):
        py = entry.replace(".ipynb", ".py")
        return f"jupyter nbconvert --to script '{entry}' && python '{py}'"
    if entry.endswith(".py"):
        return f"python '{entry}'"
    if entry.endswith(".sh"):
        return f"bash '{entry}'"
    return f"python '{entry}'"


def _inject_inputs(cmd: str, input_files: list[str]) -> str:
    if not input_files:
        return cmd
    first      = input_files[0]
    first_path = first if first.startswith("inputs/") else f"inputs/{first}"
    for ph in ("{input}", "{input_path}", "{image}", "{file}",
               "INPUT_PATH", "INPUT_FILE", "INPUT_IMAGE"):
        if ph in cmd:
            return cmd.replace(ph, first_path, 1)
    if "--input" not in cmd and "--image" not in cmd and "--img" not in cmd:
        cmd += f" --input {first_path}"
    return cmd


# ─────────────────────────────────────────────────────────────────────────────
#  Direct Command Runner
# ─────────────────────────────────────────────────────────────────────────────

_PORT_RE = re.compile(
    r"--port[= ](\d{2,5})|"
    r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})"
)
_DEFAULT_SERVER_PORTS = (8000, 5000, 8501, 7860, 8050, 8888, 3000)
# RepoSage's own API listens here. A repo server started on the same port can
# never bind it, and probing it (or curl-ing it) answers with RepoSage's own
# 404 — which used to be mistaken for the repo's homepage.
_SELF_PORT = int(os.environ.get("PORT", "8000"))


def _guess_ports(run_cmd: str, text: str) -> set[int]:
    """Ports worth probing to confirm a server is actually listening —
    from an explicit --port flag, from any host:port already seen in the
    logs, plus the well-known defaults for common Python web frameworks."""
    ports: set[int] = set()
    for m in _PORT_RE.finditer(run_cmd + "\n" + text):
        for g in m.groups():
            if g:
                try:
                    ports.add(int(g))
                except ValueError:
                    pass
    ports.discard(_SELF_PORT)
    return ports or {p for p in _DEFAULT_SERVER_PORTS if p != _SELF_PORT}


async def _probe_port(port: int, timeout: float = 0.3) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _kill_process_tree(proc: "asyncio.subprocess.Process") -> None:
    """Kill the whole process group, not just the wrapper shell — server
    frameworks (uvicorn workers, Django's autoreloader, etc.) fork children
    that survive a plain proc.kill() on the parent."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _targets_gui(run_cmd: str, workspace: str) -> bool:
    """True when the file this command runs imports a desktop GUI toolkit.

    Scoped to the targeted file rather than the whole repo on purpose: plenty
    of projects ship a tkinter example next to a perfectly ordinary CLI, and
    treating those as GUI apps would make a genuine hang look like a success.
    """
    candidates: list[str] = []
    try:
        # An LLM-authored command can carry an unbalanced quote, and shlex
        # raises on those. A GUI guess is never worth killing the run for.
        tokens = shlex.split(run_cmd.replace("&&", " ")) if run_cmd else []
    except ValueError:
        return False
    for tok in tokens:
        if tok.endswith(".py"):
            candidates.append(tok)
        elif tok.count(".") and "/" not in tok and not tok.startswith("-"):
            candidates.append(tok.replace(".", "/") + ".py")   # `python -m pkg.app`
    for rel in candidates:
        path = os.path.join(workspace, rel)
        try:
            if not os.path.realpath(path).startswith(os.path.realpath(workspace)):
                continue
            if os.path.getsize(path) > 1_000_000:
                continue
            with open(path, encoding="utf-8", errors="ignore") as fh:
                if _GUI_TOOLKITS.search(fh.read()):
                    return True
        except (OSError, ValueError):
            continue
    return False


async def _run_direct(
    run_cmd:   str,
    workspace: str,
    venv_path: str,
    is_gui:    bool = False,
    extra_env: Optional[dict] = None,
) -> tuple[int, str, str]:
    """
    Runs the command, streaming stdout/stderr while it's alive.

    Servers (FastAPI/uvicorn, Django, Flask, Streamlit, Gradio, ...) never
    exit with code 0 on their own — waiting on proc.communicate() would
    block for the full SCRIPT_TIMEOUT on every attempt just to find that
    out. Instead we watch the output (and probe likely ports) as it runs;
    once a startup banner or an open port is seen and stays up for
    SERVER_CONFIRM_GRACE seconds, we treat it as a confirmed success and
    close it ourselves rather than waiting out the timeout.

    `is_gui` extends exactly the same reasoning to desktop applications. A
    tkinter or pygame program prints no banner and opens no port, so there is
    nothing to watch for — but it is just as deliberately long-running, and
    staying alive IS its success condition. When the caller has established
    from the source that the entry point drives a GUI event loop, surviving
    SERVER_CONFIRM_GRACE seconds without dying counts as a successful run.
    """
    activate = f'source "{venv_path}/bin/activate"' if venv_path else ""

    # tkinter is the one toolkit that cannot be talked out of needing a real X
    # display, so a GUI run gets a throwaway one. Deliberately NOT added to the
    # generated run_final.sh the user copies: their machine has a display, and
    # xvfb-run probably is not installed on it.
    gui_prefix = "xvfb-run -a " if (is_gui and shutil.which("xvfb-run")) else ""

    shell_script = (
        "#!/bin/bash\n"
        "set -o pipefail\n"
        "export PYTHONIOENCODING=utf-8\n"
        "export PYTHONDONTWRITEBYTECODE=1\n"
        + (f'set +u\n{activate}\nset -u\n' if activate else "")
        + f'cd "{workspace}"\n'
        # --foreground keeps the monitored command in OUR process group instead
        # of spawning its own — otherwise os.killpg() in _kill_process_tree()
        # can't reach it and server processes (uvicorn, runserver, ...) leak.
        + f"{gui_prefix}timeout --foreground {SCRIPT_TIMEOUT} "
          f"bash -c {shlex.quote(run_cmd)}\n"
    )
    tmp = Path(workspace) / ".run_direct.sh"
    tmp.write_text(shell_script, encoding="utf-8")
    tmp.chmod(0o755)

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(tmp), cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                **(extra_env or {}),
                "PYTHONPATH":  workspace,
                "HOME":        os.environ.get("HOME", "/tmp"),
                # Headless rendering for every toolkit we might meet. There is
                # no display in the container, and without these a GUI repo
                # dies on `couldn't connect to display` or `No available video
                # device` instead of running. With them, matplotlib writes PNGs
                # instead of opening windows and pygame/Qt draw to an offscreen
                # buffer, so the program runs its real code path.
                "MPLBACKEND":        "Agg",
                "SDL_VIDEODRIVER":   "dummy",
                "SDL_AUDIODRIVER":   "dummy",
                "QT_QPA_PLATFORM":   "offscreen",
                "PYGAME_HIDE_SUPPORT_PROMPT": "1",
                # Deliberately NOT inside the workspace: matplotlib writes a
                # font cache here, and the output sweep would then offer the
                # user "fontlist-v3.11.0.json" as a result of their run.
                "MPLCONFIGDIR":      tempfile.gettempdir(),
            },
            start_new_session=True,   # own process group so we can kill server workers too
        )
    except Exception as exc:
        return 1, "", str(exc)

    stdout_buf = bytearray()
    stderr_buf = bytearray()

    async def _drain(stream, buf: bytearray) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            buf.extend(chunk)

    stdout_task = asyncio.ensure_future(_drain(proc.stdout, stdout_buf))
    stderr_task = asyncio.ensure_future(_drain(proc.stderr, stderr_buf))

    server_since: Optional[float] = None
    started_at   = time.monotonic()
    confirmed_server = False

    try:
        while True:
            if proc.returncode is not None:
                break  # exited on its own

            if time.monotonic() - started_at >= SCRIPT_TIMEOUT + 10:
                break  # hard cap — matches the `timeout` wrapper + margin

            combined = (bytes(stdout_buf) + bytes(stderr_buf)).decode(errors="ignore")
            looks_like_server = bool(_UI_SERVER_STARTED.search(combined))
            # A GUI app that is still alive and has not raised is running. The
            # traceback check is what keeps this honest: a window that fails to
            # open still prints one, and that must stay a failure.
            if not looks_like_server and is_gui:
                alive_for = time.monotonic() - started_at
                if alive_for >= SERVER_CONFIRM_GRACE and "Traceback" not in combined:
                    looks_like_server = True
            if not looks_like_server:
                for port in _guess_ports(run_cmd, combined):
                    if await _probe_port(port):
                        looks_like_server = True
                        break

            if looks_like_server and server_since is None:
                server_since = time.monotonic()
            elif not looks_like_server:
                server_since = None  # e.g. port probe was a fluke — reset and keep watching

            if server_since is not None and time.monotonic() - server_since >= SERVER_CONFIRM_GRACE:
                confirmed_server = True
                break

            try:
                await asyncio.wait_for(proc.wait(), timeout=_RUN_POLL_INTERVAL)
                break  # exited while we were polling
            except asyncio.TimeoutError:
                continue
    finally:
        if proc.returncode is None:
            _kill_process_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        for t in (stdout_task, stderr_task):
            t.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    stdout = _clip(bytes(stdout_buf).decode(errors="ignore"))
    stderr = _clip(bytes(stderr_buf).decode(errors="ignore"))

    if confirmed_server:
        kind = "GUI application" if is_gui else "server"
        note = (f"\n[RepoSage] {kind} started and stayed up for "
                f"{SERVER_CONFIRM_GRACE}s, so the run is counted as successful. "
                f"A {kind} never exits on its own, which is why there is no "
                f"exit code of its own; RepoSage stopped it.")
        return 0, (stdout + note if stdout.strip() else note.lstrip()), stderr
    if proc.returncode is not None:
        return proc.returncode, stdout, stderr
    return 1, stdout, stderr + f"\nCommand timed out after {SCRIPT_TIMEOUT}s"


# ─────────────────────────────────────────────────────────────────────────────
#  Error Classification
# ─────────────────────────────────────────────────────────────────────────────

def _classify_error(output: str) -> str:
    if AUTOFIX_PATTERNS["interactive_prompt"].search(output):
        return "interactive_prompt"
    if AUTOFIX_PATTERNS["unexpected_kwarg"].search(output):
        return "unexpected_kwarg"
    if AUTOFIX_PATTERNS["missing_argument"].search(output):
        return "missing_argument"
    if AUTOFIX_PATTERNS["api_change"].search(output):
        return "api_change"
    if AUTOFIX_PATTERNS["missing_module"].search(output):
        return "missing_module"
    if AUTOFIX_PATTERNS["cuda_error"].search(output):
        return "cuda_error"
    if AUTOFIX_PATTERNS["port_in_use"].search(output):
        return "port_in_use"
    if AUTOFIX_PATTERNS["permission"].search(output):
        return "permission_error"
    for cls, pattern in AUTOFIX_PATTERNS.items():
        if cls in ("api_change", "missing_module", "interactive_prompt",
                   "cuda_error", "missing_argument", "unexpected_kwarg",
                   "port_in_use", "permission"):
            continue
        if pattern.search(output):
            return cls
    if "SyntaxError"  in output: return "syntax_error"
    if "MemoryError"  in output: return "memory_error"
    if "ConnectionError" in output: return "network_error"
    return "runtime_error"


# ─────────────────────────────────────────────────────────────────────────────
#  Auto-fix  (no LLM call)
# ─────────────────────────────────────────────────────────────────────────────

async def _try_autofix(
    error_class: str,
    output:      str,
    workspace:   str,
    venv_path:   str,
    fix_journal: list[dict] | None = None,
) -> Optional[dict]:
    already_tried: set[str] = set()
    for entry in (fix_journal or []):
        detail   = entry.get("detail", "")
        fix_type = entry.get("fix_type", "")
        if "pip install" in str(detail):
            already_tried.add(str(detail).split("pip install")[-1].strip().lower())
        if fix_type:
            already_tried.add(fix_type)

    if error_class == "missing_module" and venv_path:
        m = AUTOFIX_PATTERNS["missing_module"].search(output)
        if m:
            import_name = m.group(1)
            pkg         = IMPORT_TO_PYPI.get(import_name, import_name)
            if pkg.lower() not in already_tried:
                pip = _pip_path(venv_path)
                ok, _ = await _run_cmd([pip, "install", pkg], workspace, timeout=120)
                if ok:
                    return {
                        "fix_type":    "install_package",
                        "explanation": f"Auto-installed missing package: '{pkg}'",
                        "detail":      f"pip install {pkg}",
                    }

    if error_class == "api_change":
        return None  # needs LLM for version pinning

    if error_class == "interactive_prompt":
        if "pipe_yes" not in already_tried:
            import glob
            for d in glob.glob(os.path.expanduser("~/.cookiecutters/*")):
                try:
                    shutil.rmtree(d)
                except Exception:
                    pass
            return {
                "fix_type":       "pipe_yes",
                "explanation":    "Cleared cached templates; auto-answering y/n prompts.",
                "detail":         "pipe_yes",
                "revised_command": None,
            }
        return None

    if error_class == "bad_encoding":
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        return {
            "fix_type":    "env_set",
            "explanation": "Set PYTHONIOENCODING=utf-8",
            "detail":      "PYTHONIOENCODING=utf-8",
        }

    if error_class == "port_in_use":
        # Try a different port
        import shlex
        new_port = "8001"
        return {
            "fix_type":       "fix_command",
            "explanation":    f"Original port in use — trying port {new_port}",
            "detail":         f"--port {new_port}",
            "revised_command": None,  # LLM will handle this
        }

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Apply Fix
# ─────────────────────────────────────────────────────────────────────────────

async def _apply_fix(fix: dict, workspace: str, venv_path: str,
                     job_env: Optional[dict] = None):
    pip = _pip_path(venv_path) if venv_path else "pip"

    for pkg in fix.get("packages_to_install", [])[:10]:
        pkg = _sanitise(str(pkg))
        if pkg and venv_path:
            await _run_cmd([pip, "install", pkg], workspace, timeout=120, env=job_env)

    env_vars = fix.get("env_vars_to_set", {})
    if isinstance(env_vars, dict):
        for k, v in env_vars.items():
            if k:
                if job_env is not None:
                    job_env[str(k)] = str(v)

    for file_spec in fix.get("files_to_create", [])[:5]:
        try:
            rel_path = _sanitise_path(file_spec.get("path", ""))
            content  = file_spec.get("content", "")
            if rel_path and content:
                full_path = Path(workspace) / rel_path
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content, encoding="utf-8")
                log.info("Created file: %s", rel_path)
        except Exception as exc:
            log.warning("Failed to create file: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
#  Input File Injection
# ─────────────────────────────────────────────────────────────────────────────

async def _inject_input_files(
    workspace:   str,
    input_files: list[str],
    job_id:      str,
) -> list[str]:
    inputs_dir = Path(workspace) / "inputs"
    inputs_dir.mkdir(exist_ok=True)
    injected: list[str] = []

    for fpath in input_files:
        try:
            if fpath.startswith(("http://", "https://")):
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.get(fpath, follow_redirects=True)
                    resp.raise_for_status()
                    if len(resp.content) > MAX_FILE_SIZE:
                        continue
                    fname = fpath.split("/")[-1].split("?")[0] or "download"
                    dest  = inputs_dir / _sanitise_path(fname)
                    dest.write_bytes(resp.content)
                    injected.append(f"inputs/{dest.name}")
            else:
                src = Path(fpath)
                if not src.is_absolute():
                    src = Path(OUTPUT_ROOT) / job_id / "uploads" / fpath
                if src.is_file() and src.stat().st_size <= MAX_FILE_SIZE:
                    dest = inputs_dir / src.name
                    shutil.copy2(str(src), str(dest))
                    injected.append(f"inputs/{dest.name}")
        except Exception as exc:
            log.warning("Failed to inject %s: %s", fpath, exc)

    return injected


# ─────────────────────────────────────────────────────────────────────────────
#  Output Capture
# ─────────────────────────────────────────────────────────────────────────────

# Directories that never hold a run artefact worth showing the user.
_CAPTURE_SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "site-packages",
    ".idea", ".vscode", "build", "dist", ".eggs",
}
_CAPTURE_SCAN_DEPTH = 3     # scan the repo root and two levels below it
_CAPTURE_MAX_SWEPT  = 25    # a run that writes 10k tiles is not a gallery


# A task that names a file is a task with a deliverable. "exit 0" alone does not
# mean that deliverable exists — pymaze exits 0 after the LLM rewrote its example
# into a script that builds a Visualizer and saves nothing. Only paths introduced
# by a save/write cue count, so "read data.csv and print stats" names an input,
# not an artifact, and is left alone.
# Unambiguous verbs only. "output" is deliberately absent: as a noun ("compare
# the output to baseline.json") it would license a bare preposition and turn an
# input reference into a phantom deliverable.
_SAVE_VERB = re.compile(
    r"\b(?:save[ds]?|saving|writ(?:e|es|ing|ten)|export(?:s|ed|ing)?|"
    r"stor(?:e|es|ing|ed)|produce[sd]?|generate[sd]?|convert(?:s|ed|ing)?|"
    r"creat(?:e|es|ing|ed)|render(?:s|ed|ing)?|dump(?:s|ed)?)\b", re.IGNORECASE)

# Only a path introduced by a cue counts, and a bare preposition ("as", "to",
# "into") counts only when the task also expresses an output intent somewhere —
# otherwise "count the words in sample.txt" would be read as a deliverable.
# "in" and "at" are excluded entirely: they almost always introduce an input.
_ARTIFACT_CUE = re.compile(
    r"\b(?P<cue>save[ds]?|saving|writ(?:e|es|ing|ten)|export(?:s|ed|ing)?|"
    r"output(?:s|ting)?|stor(?:e|es|ing|ed)|produce[sd]?|generate[sd]?|"
    r"creat(?:e|es|ing|ed)|render(?:s|ed|ing)?|dump(?:s|ed)?|as|to|into)\s+"
    r"(?:a\s+|an\s+|the\s+|it\s+as\s+|file\s+|image\s+)*"
    r"[`\'\"]?(?P<path>(?:[\w.\-]+[/\\])*[\w.\-]+\.(?:png|jpe?g|gif|svg|bmp|webp|pdf|"
    r"csv|tsv|json|jsonl|txt|md|html|xml|yaml|yml|mp3|mp4|wav|avi|zip|xlsx))[`\'\"]?",
    re.IGNORECASE)

_BARE_CUES = {"as", "to", "into"}


# Installing a dependency is a pre-step, never the run. Textualize/rich was
# reported successful off `python -m pip install rich`: pip exits 0, no table is
# ever printed, and because the task named no file the artifact gate stays
# dormant. Nothing legitimate runs a project by only installing something.
_INSTALL_ONLY = re.compile(
    r"^(?:python[\d.]*\s+-m\s+)?(?:pip[\d.]*|pipx|uv|poetry|conda|apt|apt-get|"
    r"npm|yarn|pnpm)\s+(?:install|add|-r)\b", re.IGNORECASE)


def _is_install_only(run_cmd: str) -> bool:
    """True when every step of the command is just installing something."""
    cmd = (run_cmd or "").strip()
    if not cmd:
        return False
    steps = [t.strip() for t in re.split(r"&&|;", cmd) if t.strip()]
    # Scaffolding like `mkdir -p outputs` or `cd src` says nothing either way.
    real = [t for t in steps
            if not re.match(r"^(?:mkdir|cd|export|set|source|\.)\b", t, re.IGNORECASE)]
    return bool(real) and all(_INSTALL_ONLY.match(t) for t in real)


def _expected_artifacts(task: str) -> list[str]:
    """Output paths the task names explicitly, as repo-relative strings."""
    task = task or ""
    has_intent = bool(_SAVE_VERB.search(task))
    seen: list[str] = []
    for m in _ARTIFACT_CUE.finditer(task):
        if m.group("cue").lower() in _BARE_CUES and not has_intent:
            continue
        rel = m.group("path").replace("\\", "/").lstrip("./")
        if rel and rel not in seen:
            seen.append(rel)
    return seen


# Formats whose first bytes identify them beyond argument. Only extensions that
# can be judged with certainty belong here — a wrong guess would reject correct
# work, which is far worse than the hollow success this is guarding against.
def _is_html(b: bytes) -> bool:
    return re.search(rb"<\s*(!doctype|html|body|div|p|h[1-6]|table|span|a)\b",
                     b[:4096], re.I) is not None


def _is_json(b: bytes, truncated: bool = False) -> bool:
    """Parse when the whole file is in hand; otherwise judge the opening byte.

    Parsing a prefix always fails, so reading only the head would reject every
    large-but-valid JSON — a 39MB data.json written by a perfectly good run was
    the case that caught this.

    JSON Lines (one JSON value per line, no enclosing array) is accepted too.
    It is exactly what pandas' to_json(..., lines=True) writes, and a repo
    asked to convert a CSV reached for that form; rejecting it as "not JSON"
    sent the self-healing loop in circles re-running a command that already
    worked, seven times, under a different cosmetic diagnosis each attempt,
    because json.loads() on the whole file never passed no matter what the
    quoting looked like.
    """
    head = b.lstrip()[:1]
    if head not in (b"{", b"["):
        return False
    if truncated:
        return True
    text = b.decode("utf-8", "ignore")
    try:
        json.loads(text)
        return True
    except Exception:
        pass
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    try:
        for ln in lines:
            json.loads(ln)
    except Exception:
        return False
    return True


def _is_png(b: bytes) -> bool:
    """A real PNG, and one with actual picture in it.

    A 1x1 PNG is a valid file and a useless deliverable: asked for a barcode
    image, one repo's script wrote a 67-byte single pixel, which satisfied a
    magic-byte check and showed up in the UI as a blank square. Nothing a task
    asks to "save as an image" is legitimately one pixel.
    """
    if not b.startswith(b"\x89PNG\r\n\x1a\n") or len(b) < 24:
        return False
    width  = int.from_bytes(b[16:20], "big")
    height = int.from_bytes(b[20:24], "big")
    return width > 1 and height > 1


_FORMAT_CHECKS = {
    ".png":  _is_png,
    ".jpg":  lambda b: b.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda b: b.startswith(b"\xff\xd8\xff"),
    ".gif":  lambda b: b.startswith((b"GIF87a", b"GIF89a")),
    ".pdf":  lambda b: b.lstrip()[:5] == b"%PDF-",
    ".zip":  lambda b: b.startswith(b"PK\x03\x04"),
    ".xlsx": lambda b: b.startswith(b"PK\x03\x04"),
    ".docx": lambda b: b.startswith(b"PK\x03\x04"),
    ".svg":  lambda b: b"<svg" in b[:4096].lower(),
    ".html": _is_html,
    ".htm":  _is_html,
    ".json": _is_json,
}


def _wrong_format(path: Path, name: str) -> bool:
    """True when the file exists but plainly is not the format its name claims.

    Existence alone proved too weak. Asked to render markdown to
    outputs/page.html, the agent picked `markdownify` — which converts the
    other direction — and wrote markdown into a .html file: exit 0, file
    present, gate satisfied, and not one HTML tag in it. An empty file counts
    as wrong too; a zero-byte PNG is not a PNG.
    """
    return _format_problem(path, name) is not None


def _format_problem(path: Path, name: str) -> str | None:
    """Why `_wrong_format` rejected the file, or None if it did not.

    `_wrong_format` collapses every rejection to one bit, so the retry loop
    could only ever tell the model "the file was never created" — even when it
    plainly existed and only failed its format check. That false "missing"
    report sent a diagnosis model hunting for a reason the output never
    appeared (a missing input file, a wrong working directory) instead of the
    real, fixable gap, and a run that reproduced correct data on attempt 2
    still burned every remaining retry on cosmetic rewrites of a command that
    already worked.
    """
    check = _FORMAT_CHECKS.get(Path(name).suffix.lower())
    if check is None:
        return None
    LIMIT = 4_000_000
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(LIMIT)
    except OSError:
        return None
    if not head.strip():
        return "the file is empty"
    truncated = size > LIMIT
    try:
        ok = check(head, truncated)
    except TypeError:                     # magic-byte checks take bytes only
        ok = check(head)
    if ok:
        return None
    return f"the file exists but its content is not valid {Path(name).suffix.lstrip('.').upper()}"


def _missing_artifacts(expected: list[str], workspace: str,
                       pre_run_files: set[str] | None = None) -> list[str]:
    """Which expected artifacts the run did not produce, or produced wrongly."""
    return [e for e, _ in _missing_artifacts_detail(expected, workspace, pre_run_files)]


def _missing_artifacts_detail(expected: list[str], workspace: str,
                              pre_run_files: set[str] | None = None,
                              ) -> list[tuple[str, str]]:
    """Which expected artifacts the run did not produce, or produced wrongly,
    paired with WHY — "never created" or the specific format problem.

    Matched on basename anywhere in the tree, because a script that honours the
    task but writes ./maze.png instead of ./outputs/maze.png has still done the
    work. Files that existed before the run do not count: pymaze ships a
    maze_solution.png at its root, and a repo shipping the very name the task
    asks for must not be able to satisfy the task by doing nothing.

    The reason matters downstream: a run that wrote the file but failed its
    format check used to be reported to the diagnosis model identically to one
    that wrote nothing at all ("never created"), which sent it looking for the
    wrong kind of bug entirely.
    """
    if not expected:
        return []
    ws = Path(workspace)
    pre = pre_run_files or set()
    produced: dict[str, Path] = {}
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in _CAPTURE_SKIP_DIRS]
        for f in files:
            full = Path(root, f)
            try:
                rel = str(full.relative_to(ws))
            except ValueError:
                continue
            if rel not in pre:
                produced.setdefault(f.lower(), full)

    unmet = []
    for e in expected:
        base = os.path.basename(e).lower()
        hit = produced.get(base)
        if hit is None:
            unmet.append((e, "never created"))
            continue
        problem = _format_problem(hit, base)
        if problem is not None:
            unmet.append((e, problem))
    return unmet


def _snapshot_files(workspace: str) -> set[str]:
    """Relative paths present before the run, so new ones can be told apart."""
    seen: set[str] = set()
    ws = Path(workspace)
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in _CAPTURE_SKIP_DIRS]
        for f in files:
            try:
                seen.add(str(Path(root, f).relative_to(ws)))
            except ValueError:
                pass
    return seen


def _capture_outputs(workspace: str, job_id: str,
                     pre_run_files: set[str] | None = None) -> list[str]:
    job_dir = Path(OUTPUT_ROOT) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    ws      = Path(workspace)
    captured: list[str] = []

    def _copy(src: Path, dest_name: str) -> None:
        try:
            if src.is_file() and src.stat().st_size <= MAX_FILE_SIZE:
                shutil.copy2(str(src), str(job_dir / dest_name))
                captured.append(dest_name)
        except Exception as exc:
            log.warning("Failed to capture %s: %s", src, exc)

    for fname in ("run_final.sh", "execution_output.txt", "execution_log.json"):
        _copy(ws / fname, fname)

    outputs_dir = ws / "outputs"
    if outputs_dir.is_dir():
        for fpath in sorted(outputs_dir.rglob("*")):
            if fpath.is_file():
                rel       = fpath.relative_to(outputs_dir)
                dest_name = "__".join(rel.parts)
                _copy(fpath, dest_name)

    # Artefacts written anywhere else in the repo.
    #
    # Capturing only `outputs/` meant the result depended on where the LLM
    # happened to point the run command. Measured twice on amueller/word_cloud
    # with the same task: one run wrote outputs/wordcloud.png and the image
    # appeared, the next wrote wordcloud.png at the repo root and the user was
    # told "Task completed — producing a wordcloud.png image" above a file list
    # that did not contain it. Same repo, same prompt, different outcome.
    #
    # Only files that did not exist before the run are swept, so a project's
    # own committed screenshots and fixtures are never picked up.
    if pre_run_files is not None:
        swept = 0
        for root, dirs, files in os.walk(ws):
            dirs[:] = [d for d in dirs if d not in _CAPTURE_SKIP_DIRS]
            rel_root = Path(root).relative_to(ws)
            # Pruning `dirs` stops the walk DESCENDING, but os.walk still hands
            # us this directory's own files — so too-deep files need their own
            # `continue`, not just a pruned child list.
            if len(rel_root.parts) >= _CAPTURE_SCAN_DEPTH:
                dirs[:] = []
                continue
            if rel_root.parts and rel_root.parts[0] == "outputs":
                continue                       # already taken, verbatim
            for fname in sorted(files):
                if swept >= _CAPTURE_MAX_SWEPT:
                    break
                rel = str(rel_root / fname) if rel_root.parts else fname
                if rel in pre_run_files:
                    continue                   # shipped with the repo
                if Path(fname).suffix.lower() not in OUTPUT_EXTENSIONS:
                    continue
                dest = "__".join((rel_root / fname).parts) if rel_root.parts else fname
                if dest in captured:
                    continue
                _copy(Path(root) / fname, dest)
                swept += 1

    return captured


# ─────────────────────────────────────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────────────────────────────────────

async def _run_cmd(
    cmd:     list,
    cwd:     str,
    timeout: int = 120,
    env:     Optional[dict] = None,
) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            env={**os.environ, **env} if env else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode(errors="ignore") + stderr.decode(errors="ignore")
        return proc.returncode == 0, out
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return False, "Command timed out"
    except Exception as exc:
        return False, str(exc)


def _clip(text: str, limit: int = 6000, head: int = 1800) -> str:
    """Trim a stream to `limit` chars while keeping BOTH ends.

    Plain truncation keeps the first 6000 characters, which is exactly the
    wrong half: a program that logs verbosely pushes its traceback past the
    cut, so the error that explains the failure never reaches the report or
    the LLM that is supposed to fix it. Measured on jostbr/pymaze, whose
    matplotlib font-manager debug output filled the whole buffer and left
    "EXIT CODE: 1" with no visible cause.
    """
    if len(text) <= limit:
        return text
    tail = limit - head
    cut  = len(text) - head - tail
    return (f"{text[:head]}\n"
            f"... [{cut} characters omitted] ...\n"
            f"{text[-tail:]}")


def _fmt_output(rc: int, stdout: str, stderr: str) -> str:
    parts = []
    if stdout.strip():
        parts.append(f"STDOUT:\n{stdout.strip()}")
    if stderr.strip():
        parts.append(f"STDERR:\n{stderr.strip()}")
    parts.append(f"EXIT CODE: {rc}")
    return "\n".join(parts)


def _error_summary(stderr: str, stdout: str) -> str:
    for text in (stderr, stdout):
        lines     = text.strip().splitlines()
        err_lines = [
            l for l in lines
            if any(k in l.lower() for k in (
                "error", "exception", "traceback",
                "modulenotfounderror", "no module",
                "importerror", "filenotfound",
                "syntaxerror", "typeerror", "valueerror",
            ))
        ]
        if err_lines:
            return " | ".join(err_lines[-4:])[:600]
    return "Script exited with non-zero code"


def _summarise_journal(fix_journal: list[dict]) -> str:
    if not fix_journal:
        return "No fixes attempted yet."
    return "\n".join(
        f"  Attempt {e.get('attempt','?')}: "
        f"fix_type={e.get('fix_type','?')} | "
        f"result={e.get('result','?')} | "
        f"detail={str(e.get('detail',''))[:100]}"
        for e in fix_journal
    )


def _parse_json(text: str) -> dict:
    text  = re.sub(r"```(?:json|python)?|```", "", text).strip()
    start = text.find("{")
    end   = text.rfind("}")
    if start != -1 and end != -1:
        return json.loads(text[start:end + 1])
    raise ValueError(f"No JSON object found in: {text[:200]}")


def _fresh_metrics() -> dict:
    return {
        "calls":         0,
        "tokens":        0,
        "iters":         0,
        "files":         0,
        "autofix_count": 0,
        "llm_fix_count": 0,
        "elapsed_s":     0.0,
    }


def _ev(
    type_:   str,
    title:   str,
    body:    str,
    code:    str  = "",
    tool:    str  = "",
    metrics: dict = None,
    extra:   dict = None,
) -> dict:
    return {
        "type":    type_,
        "title":   title,
        "body":    body,
        "code":    code,
        "tool":    tool,
        "metrics": metrics or {},
        **(extra or {}),
    }


def _sanitise(s: str) -> str:
    return re.sub(r"[;&|`$<>\s\"'\\]", "", s)[:80]


def _sanitise_path(s: str) -> str:
    p     = Path(s)
    parts = [
        part for part in p.parts
        if part not in ("", "/", "\\") and ".." not in part
    ]
    return str(Path(*parts)) if parts else ""
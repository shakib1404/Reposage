"""
tester.py — Python Repository Audit Engine
===========================================
Clones a GitHub repo, installs analysis tools, runs 7 scanners,
normalises findings, scores by severity, generates an audit report.
Streams structured SSE events (same shape as executor.py).

Scanners (Python-only):
  lint      — ruff (style + common bugs)
  security  — bandit (security anti-patterns)
  deps      — pip-audit (known CVEs in dependencies)
  types     — mypy (static type errors)
  secrets   — detect-secrets (leaked credentials / keys)
  deadcode  — vulture (unused functions / variables)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import AsyncGenerator

log = logging.getLogger("tester")

OUTPUT_ROOT   = os.getenv("EXECUTOR_OUTPUT_ROOT",
                           os.path.join(os.path.expanduser("~"), ".repomaster", "outputs"))
CLONE_TIMEOUT = 120
TOOL_TIMEOUT  = 120   # seconds per scanner run

# Severity → score penalty
WEIGHTS: dict[str, int] = {
    "critical": 20,
    "high":     10,
    "medium":    5,
    "low":       1,
    "info":      0,
}

SCANNER_META = [
    {"id": "lint",     "name": "Linting",           "icon": "🔍", "tool": "ruff"},
    {"id": "security", "name": "Security",           "icon": "🔒", "tool": "bandit"},
    {"id": "deps",     "name": "Dependency CVEs",   "icon": "📦", "tool": "pip-audit"},
    {"id": "types",    "name": "Type Analysis",     "icon": "🔬", "tool": "mypy"},
    {"id": "secrets",  "name": "Secret Detection",  "icon": "🔑", "tool": "detect-secrets"},
    {"id": "deadcode", "name": "Dead Code",         "icon": "💀", "tool": "vulture"},
]


# ─────────────────────────────────────────────────────────────────────────────
#  Public entry point
# ─────────────────────────────────────────────────────────────────────────────

async def run_test_loop(
    repo_full_name: str,
    job_id: str = "",
) -> AsyncGenerator[dict, None]:
    """
    Run all scanners on a GitHub repo.
    Yields SSE-compatible event dicts: {type, title, body, ...extra}
    """
    workspace: str | None = None
    venv_path: str | None = None
    all_findings: list[dict] = []
    t_start = time.monotonic()

    try:
        # ── 1. Clone ─────────────────────────────────────────────────────────
        yield _ev("status", "Cloning repository",
                  f"git clone --depth 1 github.com/{repo_full_name}")
        workspace = await _clone(repo_full_name)
        yield _ev("status", "Repository cloned", f"✓ {repo_full_name}")

        # ── 2. Create isolated venv ───────────────────────────────────────────
        yield _ev("status", "Setting up audit environment",
                  "Creating isolated virtual environment…")
        venv_path = os.path.join(workspace, ".audit_venv")
        venv_ok, venv_msg = await _create_venv(venv_path)
        if not venv_ok:
            yield _ev("warning", "Venv warning", venv_msg)

        # ── 3. Install project deps (best-effort, for accurate dep scan) ──────
        req_files = _find_req_files(workspace)
        if req_files:
            yield _ev("status", "Installing project deps (for dep scan)",
                      f"Found: {req_files}")
            pip = _pip(venv_path)
            for rf in req_files[:2]:
                await _run([pip, "install", "-r", rf, "--quiet",
                            "--no-deps"],    # --no-deps keeps it fast
                           workspace, timeout=180)

        # ── 4. Run each scanner ───────────────────────────────────────────────
        for meta in SCANNER_META:
            sid = meta["id"]
            yield _ev("scanner_start", meta["name"],
                      f"Installing & running {meta['tool']}…",
                      scanner=sid, icon=meta["icon"])

            try:
                findings = await _run_scanner(sid, workspace, venv_path)
                all_findings.extend(findings)
                yield _ev("scanner_done", meta["name"],
                          f"{len(findings)} finding(s)",
                          scanner=sid, icon=meta["icon"],
                          findings=findings[:200])
            except Exception as exc:
                log.warning("Scanner %s failed: %s", sid, exc)
                yield _ev("scanner_error", meta["name"], str(exc)[:300],
                          scanner=sid, icon=meta["icon"], findings=[])

        # ── 5. Aggregate & score ──────────────────────────────────────────────
        sev_counts = _count_severities(all_findings)
        score, grade = _compute_score(sev_counts)
        elapsed = round(time.monotonic() - t_start, 1)

        # Group by scanner for the report
        by_scanner: dict[str, list[dict]] = {}
        for f in all_findings:
            by_scanner.setdefault(f["scanner"], []).append(f)

        report = {
            "repo":       repo_full_name,
            "score":      score,
            "grade":      grade,
            "elapsed_s":  elapsed,
            "total":      len(all_findings),
            "severity":   sev_counts,
            "by_scanner": {k: len(v) for k, v in by_scanner.items()},
            "findings":   all_findings,
        }

        # ── 6. Persist report ─────────────────────────────────────────────────
        if job_id:
            job_dir = Path(OUTPUT_ROOT) / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            rp = job_dir / "audit_report.json"
            rp.write_text(json.dumps(report, indent=2), encoding="utf-8")
            yield _ev("status", "Audit report saved",
                      f"audit_report.json — {len(all_findings)} findings")

        yield _ev("done",
                  f"Audit complete — Grade {grade}  ({score}/100)",
                  f"{sev_counts['critical']} critical · "
                  f"{sev_counts['high']} high · "
                  f"{sev_counts['medium']} medium · "
                  f"{sev_counts['low']} low",
                  report=report)

    except Exception as exc:
        log.exception("Test pipeline error")
        yield _ev("error", "Audit failed", str(exc))

    finally:
        if workspace and os.path.isdir(workspace):
            shutil.rmtree(workspace, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Scanner dispatch
# ─────────────────────────────────────────────────────────────────────────────

async def _run_scanner(scanner_id: str,
                        workspace: str, venv_path: str) -> list[dict]:
    dispatch = {
        "lint":     _scan_lint,
        "security": _scan_security,
        "deps":     _scan_deps,
        "types":    _scan_types,
        "secrets":  _scan_secrets,
        "deadcode": _scan_deadcode,
    }
    fn = dispatch.get(scanner_id)
    return await fn(workspace, venv_path) if fn else []


# ── lint ─────────────────────────────────────────────────────────────────────

async def _scan_lint(workspace: str, venv_path: str) -> list[dict]:
    pip = _pip(venv_path)
    await _run([pip, "install", "ruff", "--quiet"], workspace, timeout=60)
    ruff = _tool(venv_path, "ruff")
    ok, out = await _run(
        [ruff, "check", ".", "--output-format", "json", "--no-cache",
         "--exit-zero"],
        workspace, timeout=TOOL_TIMEOUT)
    findings = []
    try:
        data = json.loads(out)
        for item in data:
            code = item.get("code") or ""
            # ruff security rules start with S, bugbear with B, errors E/W
            if code.startswith("S"):
                sev = "medium"
            elif code.startswith(("B", "E", "W")):
                sev = "low"
            else:
                sev = "info"
            loc = item.get("location") or {}
            findings.append(_finding(
                scanner="lint", severity=sev,
                file=_rel(item.get("filename", ""), workspace),
                line=loc.get("row", 0),
                rule=code,
                message=item.get("message", ""),
            ))
    except Exception:
        for line in out.splitlines()[:100]:
            m = re.match(r"(.+?):(\d+):\d+: (\w+) (.+)", line)
            if m:
                findings.append(_finding(
                    scanner="lint", severity="low",
                    file=_rel(m.group(1), workspace),
                    line=int(m.group(2)), rule=m.group(3),
                    message=m.group(4),
                ))
    return findings[:500]


# ── security ──────────────────────────────────────────────────────────────────

async def _scan_security(workspace: str, venv_path: str) -> list[dict]:
    pip = _pip(venv_path)
    await _run([pip, "install", "bandit", "--quiet"], workspace, timeout=60)
    bandit = _tool(venv_path, "bandit")
    ok, out = await _run(
        [bandit, "-r", ".", "-f", "json", "-q", "--exit-zero"],
        workspace, timeout=TOOL_TIMEOUT)
    findings = []
    sev_map = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
    try:
        data = json.loads(out)
        for item in data.get("results", []):
            sev_raw  = item.get("issue_severity", "LOW")
            conf_raw = item.get("issue_confidence", "LOW")
            sev = "critical" if (sev_raw == "HIGH" and conf_raw == "HIGH") \
                  else sev_map.get(sev_raw, "low")
            findings.append(_finding(
                scanner="security", severity=sev,
                file=_rel(item.get("filename", ""), workspace),
                line=item.get("line_number", 0),
                rule=item.get("test_id", ""),
                message=item.get("issue_text", ""),
                confidence=conf_raw,
            ))
    except Exception:
        pass
    return findings[:500]


# ── deps ──────────────────────────────────────────────────────────────────────

async def _scan_deps(workspace: str, venv_path: str) -> list[dict]:
    pip = _pip(venv_path)
    await _run([pip, "install", "pip-audit", "--quiet"], workspace, timeout=60)
    pa = _tool(venv_path, "pip-audit")

    req_file = next(
        (rf for rf in ("requirements.txt", "requirements-dev.txt",
                       "requirements-test.txt")
         if os.path.isfile(os.path.join(workspace, rf))),
        None,
    )
    cmd = [pa, "--format", "json", "--progress-spinner", "off"]
    if req_file:
        cmd += ["-r", req_file]

    ok, out = await _run(cmd, workspace, timeout=TOOL_TIMEOUT)
    findings = []
    try:
        raw = json.loads(out)
        deps_list = raw if isinstance(raw, list) else raw.get("dependencies", [])
        for dep in deps_list:
            for vuln in dep.get("vulns", []):
                vid = vuln.get("id", "")
                sev = "high" if vid.upper().startswith("CVE") else "medium"
                findings.append(_finding(
                    scanner="deps", severity=sev,
                    file=f"{dep.get('name','')}=={dep.get('version','')}",
                    line=0, rule=vid,
                    message=(vuln.get("description") or "")[:200],
                ))
    except Exception:
        pass
    return findings


# ── types ─────────────────────────────────────────────────────────────────────

async def _scan_types(workspace: str, venv_path: str) -> list[dict]:
    pip = _pip(venv_path)
    await _run([pip, "install", "mypy", "--quiet"], workspace, timeout=60)
    mypy = _tool(venv_path, "mypy")
    ok, out = await _run(
        [mypy, ".", "--ignore-missing-imports", "--no-error-summary",
         "--follow-imports", "skip", "--no-color-output"],
        workspace, timeout=TOOL_TIMEOUT)
    findings = []
    for line in out.splitlines()[:300]:
        m = re.match(r"(.+?):(\d+): (error|warning|note): (.+)", line)
        if not m:
            continue
        level = m.group(3)
        sev = "high" if level == "error" else "medium" if level == "warning" else "info"
        findings.append(_finding(
            scanner="types", severity=sev,
            file=_rel(m.group(1), workspace),
            line=int(m.group(2)), rule=level,
            message=m.group(4),
        ))
    return findings[:500]


# ── secrets ───────────────────────────────────────────────────────────────────

async def _scan_secrets(workspace: str, venv_path: str) -> list[dict]:
    pip = _pip(venv_path)
    await _run([pip, "install", "detect-secrets", "--quiet"], workspace, timeout=60)
    ds = _tool(venv_path, "detect-secrets")
    ok, out = await _run([ds, "scan", "--all-files"],
                          workspace, timeout=TOOL_TIMEOUT)
    findings = []
    try:
        data = json.loads(out)
        for fpath, secrets in data.get("results", {}).items():
            for secret in secrets:
                findings.append(_finding(
                    scanner="secrets", severity="critical",
                    file=_rel(fpath, workspace),
                    line=secret.get("line_number", 0),
                    rule=secret.get("type", "secret"),
                    message=f"Potential {secret.get('type','secret')} detected (value hashed)",
                ))
    except Exception:
        pass
    return findings


# ── dead code ─────────────────────────────────────────────────────────────────

async def _scan_deadcode(workspace: str, venv_path: str) -> list[dict]:
    pip = _pip(venv_path)
    await _run([pip, "install", "vulture", "--quiet"], workspace, timeout=60)
    vulture = _tool(venv_path, "vulture")
    ok, out = await _run([vulture, ".", "--min-confidence", "80"],
                          workspace, timeout=TOOL_TIMEOUT)
    findings = []
    for line in out.splitlines()[:300]:
        # path/file.py:42: unused function 'foo' (80% confidence)
        m = re.match(r"(.+?):(\d+): (.+?) \((\d+)% confidence\)", line)
        if m:
            findings.append(_finding(
                scanner="deadcode", severity="low",
                file=_rel(m.group(1), workspace),
                line=int(m.group(2)), rule="dead_code",
                message=m.group(3),
                confidence=m.group(4) + "%",
            ))
    return findings[:300]


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _finding(scanner: str, severity: str, file: str, line: int,
             rule: str, message: str, confidence: str = "") -> dict:
    return {
        "scanner":    scanner,
        "severity":   severity,
        "file":       file,
        "line":       int(line) if line else 0,
        "rule":       rule,
        "message":    message[:300],
        "confidence": confidence,
    }


def _count_severities(findings: list[dict]) -> dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for f in findings:
        k = f.get("severity", "info")
        if k in counts:
            counts[k] += 1
    return counts


def _compute_score(sev_counts: dict[str, int]) -> tuple[int, str]:
    penalty = sum(WEIGHTS[k] * v for k, v in sev_counts.items())
    score = max(0, 100 - penalty)
    grade = ("A" if score >= 90 else
             "B" if score >= 75 else
             "C" if score >= 60 else
             "D" if score >= 40 else "F")
    return score, grade


def _pip(venv: str) -> str:
    return os.path.join(venv, "bin", "pip")


def _tool(venv: str, name: str) -> str:
    p = os.path.join(venv, "bin", name)
    return p if os.path.isfile(p) else name


def _rel(path: str, workspace: str) -> str:
    try:
        return str(Path(path).relative_to(workspace))
    except ValueError:
        return path


def _find_req_files(workspace: str) -> list[str]:
    return [
        name for name in
        ("requirements.txt", "requirements-dev.txt", "requirements-test.txt")
        if os.path.isfile(os.path.join(workspace, name))
    ]


async def _create_venv(venv_path: str) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "venv", venv_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        return proc.returncode == 0, err.decode(errors="ignore")[:300]
    except asyncio.TimeoutError:
        return False, "venv creation timed out"


async def _clone(repo_full_name: str) -> str:
    ws = tempfile.mkdtemp(prefix="tester_")
    url = f"https://github.com/{repo_full_name}.git"
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", url, ws,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=CLONE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        shutil.rmtree(ws, ignore_errors=True)
        raise RuntimeError("git clone timed out")
    if proc.returncode != 0:
        shutil.rmtree(ws, ignore_errors=True)
        raise RuntimeError(f"git clone failed: {err.decode()[:400]}")
    return ws


async def _run(cmd: list[str], cwd: str,
               timeout: int = 120) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout)
        combined = (stdout.decode(errors="ignore") +
                    stderr.decode(errors="ignore"))
        return proc.returncode == 0, combined
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return False, "timed out"
    except Exception as exc:
        return False, str(exc)


def _ev(type_: str, title: str, body: str, **extra) -> dict:
    return {"type": type_, "title": title, "body": body, **extra}

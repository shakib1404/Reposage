"""
tester.py — Python Repository Audit Engine
===========================================
Clones a GitHub repo, installs analysis tools, runs 9 scanners,
normalises findings, scores by severity, generates an audit report.
Streams structured SSE events (same shape as executor.py).

Scanners (Python-only):
  lint      — ruff (style + common bugs)
  security  — bandit (security anti-patterns)
  deps      — pip-audit (known CVEs in dependencies)
  types     — mypy (static type errors)
  secrets   — detect-secrets (leaked credentials / keys)
  deadcode  — vulture (unused functions / variables)
  semgrep   — semgrep (cross-language structural pattern rules, registry "auto" config)

The last two are RepoSage's own analyses rather than wrappers around someone
else's tool, and both find defects that are invisible to a file-at-a-time
linter because they are properties of the whole repository:

  arch      — graph-shaped defects: import cycles, god modules, functions
              unreachable over the call graph, unstable dependencies
  excflow   — interprocedural exception propagation: failures nothing handles,
              handlers that can never fire, catch-alls that silently swallow
              distinct deliberate errors, cleanup that destroys the exception
              already in flight
"""
from __future__ import annotations

import asyncio
import ast
import json
import logging
import math
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

# Per-finding severity weight, taken from the official CVSS v3.1 qualitative
# severity rating scale (FIRST.org / NIST NVD): None 0.0, Low 0.1-3.9,
# Medium 4.0-6.9, High 7.0-8.9, Critical 9.0-10.0. Each of our severities is
# mapped to the midpoint of its CVSS band.
CVSS_WEIGHTS: dict[str, float] = {
    "critical": 9.5,
    "high":     8.0,
    "medium":   5.5,
    "low":      2.0,
    "info":     0.0,
}

# Decay constant for _compute_score(), in exposure-per-KLOC. Larger K = slower
# decay. Calibrated against real repos rather than picked: see the table in
# _compute_score's docstring and the tuning run in the commit that introduced
# density scoring.
SCORE_DECAY_K = 220.0

# A repo with less source than this cannot be meaningfully rated by density —
# one finding in 20 lines is not "50 findings per KLOC". Acts as the divisor
# floor so tiny repos settle near the top of the scale instead of swinging
# wildly on a single finding.
MIN_LOC_FOR_DENSITY = 300

SCANNER_META = [
    {"id": "lint",     "name": "Linting",           "icon": "🔍", "tool": "ruff"},
    {"id": "security", "name": "Security",           "icon": "🔒", "tool": "bandit"},
    {"id": "deps",     "name": "Dependency CVEs",   "icon": "📦", "tool": "pip-audit"},
    {"id": "types",    "name": "Type Analysis",     "icon": "🔬", "tool": "mypy"},
    {"id": "secrets",  "name": "Secret Detection",  "icon": "🔑", "tool": "detect-secrets"},
    {"id": "deadcode", "name": "Dead Code",         "icon": "💀", "tool": "vulture"},
    {"id": "semgrep",  "name": "Pattern Analysis",  "icon": "🧩", "tool": "semgrep"},
    {"id": "arch",     "name": "Architecture Health", "icon": "🏛️",
     "tool": "RepoSage graph analysis"},
    {"id": "excflow",  "name": "Exception Flow",     "icon": "⚡",
     "tool": "RepoSage exception propagation"},
]

# Longer budget than TOOL_TIMEOUT — semgrep's "auto" config fetches a
# curated ruleset from the registry before it can scan, which is slower
# than the other (purely local) scanners.
SEMGREP_TIMEOUT = 240

# Directories that hold a THIRD PARTY dependency tree, sometimes committed
# into the repo by accident (a Replit-generated venv/, a vendored
# node_modules/, a stale .tox/ from CI). If any of these exist inside the
# cloned repo itself, every scanner finding inside them is a finding about
# someone else's package, not the audited repo's own code — one real
# instance of this reproduced a 0/F, 720-finding report for a 16-line
# FizzBuzz script, entirely from bandit/semgrep flagging code inside a
# committed `venv/`. Stripped from the workspace right after clone, before
# any scanner runs, so this bug class is structurally impossible regardless
# of which of the 7 tools would otherwise have walked into it.
VENDOR_DIR_NAMES = {
    "venv", ".venv", "env", "virtualenv", "ENV",
    "node_modules", ".tox", "site-packages",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}


# ── Which part of the repo a finding lives in ────────────────────────────────
#
# A repository's test suite, docs and examples are not its shipped code, and
# judging them by the same rules produces a verdict that is simply wrong.
# Measured on psf/requests: 505 of 571 findings (88%) came from tests/ — 387 of
# them bandit B101 "assert used", which is what a test file is *supposed* to
# contain. Scoring those as defects graded one of the best-maintained libraries
# in Python an F.
#
# Nothing is hidden: every finding is still reported and shown. Only the grade
# is computed from source files, and the split is surfaced so "most of this
# repo's noise is in its tests" stays visible information rather than a silent
# exclusion.
_TEST_PAT = re.compile(
    r"(^|/)(tests?|testing|spec|specs|fixtures?|conftest\.py|__mocks__)(/|$)"
    r"|(^|/)test_[^/]*\.py$|_test\.py$|_spec\.py$", re.I)
_DOCS_PAT = re.compile(
    r"(^|/)(docs?|documentation|man|website|site)(/|$)"
    r"|\.(md|rst|txt|adoc)$", re.I)
_EXAMPLE_PAT = re.compile(
    r"(^|/)(examples?|samples?|demos?|tutorials?|notebooks?|benchmarks?|scripts?)(/|$)"
    r"|\.ipynb$", re.I)


def _classify_area(relpath: str) -> str:
    """source | test | docs | example — which part of the repo this file is."""
    p = (relpath or "").replace("\\", "/")
    if _TEST_PAT.search(p):    return "test"
    if _DOCS_PAT.search(p):    return "docs"
    if _EXAMPLE_PAT.search(p): return "example"
    return "source"


def _strip_vendor_dirs(workspace: str) -> list[str]:
    """Remove committed dependency/vendor directories from a freshly cloned
    workspace before any scanner touches it. Returns the relative paths
    removed (surfaced in the SSE log so this isn't a silent behavior change)."""
    removed: list[str] = []
    for root, dirnames, _ in os.walk(workspace, topdown=True):
        keep = []
        for d in dirnames:
            if d in VENDOR_DIR_NAMES:
                full = os.path.join(root, d)
                shutil.rmtree(full, ignore_errors=True)
                removed.append(os.path.relpath(full, workspace))
            else:
                keep.append(d)
        dirnames[:] = keep   # don't descend into dirs we just deleted
    return removed


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

    # On a memory-capped deployment a prior search leaves the cross-encoder
    # reranker (400-500MB) resident for the rest of the process's life,
    # which left no headroom for this function's venv-plus-scanners memory
    # spike and got the container OOM-killed. Gated the same way as the
    # eager-warmup skip: both are signals this process is running somewhere
    # memory-constrained, not the default self-hosted deployment.
    if os.getenv("SKIP_RERANKER_WARMUP", "").strip().lower() in ("1", "true", "yes"):
        try:
            from search import release_reranker
            release_reranker()
        except Exception:
            pass

    try:
        # ── 1. Clone ─────────────────────────────────────────────────────────
        yield _ev("status", "Cloning repository",
                  f"git clone --depth 1 github.com/{repo_full_name}")
        workspace = await _clone(repo_full_name)
        yield _ev("status", "Repository cloned", f"✓ {repo_full_name}")

        vendor_removed = _strip_vendor_dirs(workspace)
        if vendor_removed:
            yield _ev("status", "Excluded vendored dependencies",
                      f"Removed from scan scope (not part of the repo's own "
                      f"code): {', '.join(vendor_removed[:10])}")

        # ── 2. Create isolated venv ───────────────────────────────────────────
        # Deliberately created OUTSIDE the cloned workspace (as a sibling temp
        # dir), not as a ".audit_venv" subdirectory of it. Scanners like
        # detect-secrets and vulture have no smart default excludes the way
        # ruff/bandit/semgrep do, so a venv nested inside the workspace gets
        # recursively scanned along with the repo — every finding inside its
        # site-packages (the scanners' *own* installed dependencies) then gets
        # reported as if it were a finding in the audited repo, silently
        # wrecking the score. Keeping the venv outside the tree scanners walk
        # makes that whole bug class structurally impossible.
        yield _ev("status", "Setting up audit environment",
                  "Creating isolated virtual environment…")
        venv_path = tempfile.mkdtemp(prefix="audit_venv_")
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

            # Re-strip before EVERY scanner, not just once after clone: mypy
            # writes .mypy_cache into the workspace as a side effect of the
            # types scan, and without this a later scanner (secrets/deadcode)
            # picks that cache dir up as if it were repo content — the exact
            # same false-positive-flood bug, just self-inflicted by our own
            # pipeline instead of committed by the repo author. Cheap no-op
            # when nothing new appeared.
            _strip_vendor_dirs(workspace)

            try:
                findings = await _run_scanner(sid, workspace, venv_path)
                all_findings.extend(findings)
                # `count` is sent separately because `findings` is truncated
                # for payload size — the UI used to display len(findings) and
                # so reported a flat "200" for any scanner that found more.
                yield _ev("scanner_done", meta["name"],
                          f"{len(findings)} finding(s)",
                          scanner=sid, icon=meta["icon"],
                          count=len(findings),
                          findings=findings[:200])
            except Exception as exc:
                log.warning("Scanner %s failed: %s", sid, exc)
                yield _ev("scanner_error", meta["name"], str(exc)[:300],
                          scanner=sid, icon=meta["icon"], findings=[])

            # Each scanner's subprocess output (sometimes large JSON) and
            # intermediate parsing data goes out of scope here; collecting
            # now instead of waiting for the next allocation keeps this
            # process's own footprint from ratcheting up over 9 scanners.
            import gc
            gc.collect()

        # ── 4b. Attach the offending source lines ─────────────────────────────
        # Must happen HERE, before the `finally` below deletes the clone: a
        # finding that says "bandit B602, subprocess with shell=True, line 214"
        # is only actionable next to the line it is talking about, and by the
        # time the report reaches the browser the workspace is gone.
        _attach_snippets(all_findings, workspace)

        # ── 5. Aggregate & score ──────────────────────────────────────────────
        # The grade comes from the repo's SHIPPED code only. Tests, docs and
        # examples are still reported in full, but a test file full of asserts
        # is not a defect in the library, and counting it as one is what made
        # every mature repo grade F.
        graded         = _dedupe_cross_tool(all_findings)
        sev_counts     = _count_severities(graded, area="source")
        sev_counts_all = _count_severities(all_findings)
        source_loc     = _source_loc(workspace)
        score, grade   = _compute_score(sev_counts, source_loc)
        elapsed        = round(time.monotonic() - t_start, 1)

        by_area: dict[str, int] = {}
        for f in all_findings:
            by_area[f.get("area", "source")] = by_area.get(f.get("area", "source"), 0) + 1

        # Group by scanner for the report
        by_scanner: dict[str, list[dict]] = {}
        for f in all_findings:
            by_scanner.setdefault(f["scanner"], []).append(f)

        report = {
            "repo":        repo_full_name,
            "score":       score,
            "grade":       grade,
            "elapsed_s":   elapsed,
            "total":       len(all_findings),
            "graded_on":   sum(sev_counts.values()),
            # reports of one line by several tools, counted once in the grade
            "duplicates_collapsed": len([f for f in all_findings if f.get("area", "source") == "source"])
                                    - len([f for f in graded if f.get("area", "source") == "source"]),
            "source_loc":  source_loc,
            "severity":    sev_counts,       # source only — what the grade uses
            "severity_all": sev_counts_all,  # everything, for the full picture
            "by_area":     by_area,
            "by_scanner":  {k: len(v) for k, v in by_scanner.items()},
            "findings":    all_findings,
        }

        # ── 6. Persist report ─────────────────────────────────────────────────
        if job_id:
            job_dir = Path(OUTPUT_ROOT) / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            rp = job_dir / "audit_report.json"
            rp.write_text(json.dumps(report, indent=2), encoding="utf-8")
            yield _ev("status", "Audit report saved",
                      f"audit_report.json — {len(all_findings)} findings")

        non_source = sum(n for a, n in by_area.items() if a != "source")
        yield _ev("done",
                  f"Audit complete — Grade {grade}  ({score}/100)",
                  f"{sev_counts['critical']} critical · "
                  f"{sev_counts['high']} high · "
                  f"{sev_counts['medium']} medium · "
                  f"{sev_counts['low']} low"
                  + (f"  (+{non_source} in tests/docs/examples, not graded)"
                     if non_source else ""),
                  report=report)

    except Exception as exc:
        log.exception("Test pipeline error")
        yield _ev("error", "Audit failed", str(exc))

    finally:
        if workspace and os.path.isdir(workspace):
            shutil.rmtree(workspace, ignore_errors=True)
        if venv_path and os.path.isdir(venv_path):
            shutil.rmtree(venv_path, ignore_errors=True)


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
        "semgrep":  _scan_semgrep,
        "arch":     _scan_architecture,
        "excflow":  _scan_excflow,
    }
    fn = dispatch.get(scanner_id)
    return await fn(workspace, venv_path) if fn else []


# ── lint ─────────────────────────────────────────────────────────────────────

async def _scan_lint(workspace: str, venv_path: str) -> list[dict]:
    pip = _pip(venv_path)
    await _run([pip, "install", "ruff", "--quiet"], workspace, timeout=60)
    ruff = _tool(venv_path, "ruff")
    # --isolated ignores the repo's OWN ruff config, and that is the whole
    # point: an audit the audited project can configure away measures nothing.
    # psf/requests ships a pyproject.toml that narrows ruff to almost nothing —
    # measured, the same clone reported 0 findings with its config and 138
    # without it. The explicit --select also pins what "linting" means here, so
    # two repos are judged against the same standard rather than against
    # whatever each maintainer happened to enable.
    #
    #   E,W  pycodestyle   F     pyflakes       B    bugbear (real bug shapes)
    #   C90  complexity    UP    outdated       SIM  simplifiable
    #   RET  return flow   ARG   unused args
    #
    # Deliberately NOT selected:
    #   S    ruff's S rules are a reimplementation of bandit, which runs as its
    #        own scanner. Selecting both counted every finding twice — measured
    #        on psf/requests: 16 ruff S101 plus 16 bandit B101 for the same 16
    #        asserts, inflating the exposure that sets the grade.
    #   E501 line length is formatting, not quality, and judges every project
    #        against ruff's default 88 columns regardless of the width it chose.
    ok, out = await _run(
        [ruff, "check", ".", "--isolated",
         "--select", "E,W,F,B,C90,UP,SIM,RET,ARG",
         "--ignore", "E501",
         "--output-format", "json", "--no-cache", "--exit-zero"],
        workspace, timeout=TOOL_TIMEOUT)
    findings = []
    try:
        data = _json_from(out)
        for item in data:
            code = item.get("code") or ""
            # B = bugbear (genuine bug shapes), C90 = over-complex functions,
            # F = pyflakes (unused/undefined names), E/W = style. UP/SIM/RET/ARG
            # are maintainability nits and carry no scoring weight.
            if code.startswith(("B", "C9", "F")):
                sev = "low"
            elif code.startswith(("E", "W")):
                sev = "info"
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
    data = _json_or_raise(out, "bandit") or {}
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
    return findings[:500]


# ── deps ──────────────────────────────────────────────────────────────────────
#
# What gets audited: every Python dependency manifest the repo declares, not
# only a root requirements.txt —
#   requirements*.txt (root, and requirements/ style sub-folders)
#   pyproject.toml    [project] dependencies and optional-dependencies
#   poetry.lock, Pipfile.lock   (exact pinned versions)
# Nothing in the repo is executed to read them (no setup.py, no build step):
# the manifests are only parsed, and pip-audit does the resolving.
#
# Resolving can fail for reasons unrelated to security — conflicting pins, or
# an old sdist that will not build on this Python. When it does, the pinned
# `name==version` lines are audited directly (--no-deps --disable-pip), so a
# resolver failure does not become a silent "0 vulnerabilities".

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._\-]*)(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9._+!\-]+)\s*$")
_REQ_SKIP_DIRS = VENDOR_DIR_NAMES | {"node_modules", "build", "dist", ".git"}


def _strip_req_line(line: str) -> str:
    return line.split(" #")[0].split("\t#")[0].strip() if "#" in line else line.strip()


def _requirement_files(workspace: str) -> list[str]:
    """Relative paths of requirements*.txt, at the root and one level down."""
    found: list[str] = []
    for root, dirs, files in os.walk(workspace):
        depth = os.path.relpath(root, workspace).count(os.sep) if root != workspace else 0
        dirs[:] = [d for d in dirs if d not in _REQ_SKIP_DIRS and not d.startswith(".")]
        if depth >= 2:
            dirs[:] = []
        for fn in sorted(files):
            if re.fullmatch(r"requirements[-_.\w]*\.txt", fn, re.I) or \
               (os.path.basename(root).lower() in ("requirements", "reqs", "requirement")
                and fn.endswith(".txt")):
                rel = os.path.relpath(os.path.join(root, fn), workspace)
                # A requirements file under tests/, docs/ or examples/ pins
                # tooling for those, not what ships. (Not _classify_area: it
                # files every *.txt under "docs", which is right for findings
                # but would skip requirements/prod.txt here.)
                parts = {c.lower() for c in rel.split(os.sep)[:-1]}
                if parts & {"test", "tests", "doc", "docs", "example", "examples",
                            "sample", "samples", "benchmark", "benchmarks"}:
                    continue
                found.append(rel)
    return found[:6]


def _manifest_requirements(workspace: str) -> list[tuple[str, list[str]]]:
    """(label, requirement lines) for pyproject.toml and the lock files."""
    out: list[tuple[str, list[str]]] = []

    def read(name):
        path = os.path.join(workspace, name)
        if not os.path.isfile(path) or os.path.getsize(path) > 5_000_000:
            return None
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    raw = read("pyproject.toml")
    if raw:
        try:
            import tomllib
            data = tomllib.loads(raw.decode("utf-8", "replace"))
            proj = data.get("project") or {}
            lines = [d for d in proj.get("dependencies", []) if isinstance(d, str)]
            for extra in (proj.get("optional-dependencies") or {}).values():
                lines += [d for d in extra if isinstance(d, str)]
            if lines:
                out.append(("pyproject.toml", lines))
        except Exception:
            pass

    raw = read("poetry.lock")
    if raw:
        try:
            import tomllib
            pkgs = tomllib.loads(raw.decode("utf-8", "replace")).get("package", [])
            lines = [f"{p['name']}=={p['version']}" for p in pkgs
                     if p.get("name") and p.get("version")]
            if lines:
                out.append(("poetry.lock", lines))
        except Exception:
            pass

    raw = read("Pipfile.lock")
    if raw:
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            lines = []
            for section in ("default", "develop"):
                for name, meta in (data.get(section) or {}).items():
                    ver = (meta or {}).get("version", "")
                    if ver.startswith("=="):
                        lines.append(f"{name}{ver}")
            if lines:
                out.append(("Pipfile.lock", lines))
        except Exception:
            pass
    return out


def _parse_audit(out: str):
    """pip-audit JSON -> list of dependency dicts, or None if unreadable."""
    try:
        raw = _json_from(out)
    except ValueError:
        return None
    deps = raw if isinstance(raw, list) else raw.get("dependencies")
    return deps if isinstance(deps, list) else None


def _audit_error(out: str) -> str:
    """The most informative line pip-audit printed when it failed."""
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    for l in lines:
        if "ResolutionImpossible" in l or "conflicting dependencies" in l or \
           "Failed to install" in l or l.startswith("ERROR"):
            return l[:220]
    return (lines[-1] if lines else "no output")[:220]


async def _scan_deps(workspace: str, venv_path: str) -> list[dict]:
    pip = _pip(venv_path)
    await _run([pip, "install", "pip-audit", "--quiet"], workspace, timeout=60)
    pa = _tool(venv_path, "pip-audit")

    # (label, real requirements file or None, requirement lines)
    sources: list[tuple[str, str | None, list[str]]] = []
    for rel in _requirement_files(workspace):
        try:
            with open(os.path.join(workspace, rel), encoding="utf-8", errors="ignore") as fh:
                lines = [_strip_req_line(l) for l in fh]
        except OSError:
            continue
        sources.append((rel, os.path.join(workspace, rel), [l for l in lines if l and not l.startswith(("#", "-"))]))
    for label, lines in _manifest_requirements(workspace):
        sources.append((label, None, lines))

    if not sources:
        return [_finding(
            scanner="deps", severity="info", file="", line=0, rule="DEPS000",
            message="No Python dependency manifest found (requirements*.txt, "
                    "pyproject.toml, poetry.lock, Pipfile.lock). Dependency "
                    "audit skipped — this is not a clean result.")]

    findings: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    tmpdir = tempfile.mkdtemp(prefix="deps_")
    try:
        for idx, (label, req_path, lines) in enumerate(sources[:6]):
            if req_path is None:
                req_path = os.path.join(tmpdir, f"manifest_{idx}.txt")
                with open(req_path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            base = [pa, "--format", "json", "--progress-spinner", "off"]
            ok, out = await _run(base + ["-r", req_path], workspace, timeout=TOOL_TIMEOUT)
            deps = _parse_audit(out)
            note = ""
            if deps is None:
                # Resolution failed: audit the exact pins directly.
                pinned = sorted({f"{m.group(1)}=={m.group(2)}" for l in lines
                                 if (m := _PIN.match(l.split(";")[0].strip()))})
                reason = _audit_error(out)
                if pinned:
                    pin_file = os.path.join(tmpdir, f"pins_{idx}.txt")
                    with open(pin_file, "w", encoding="utf-8") as fh:
                        fh.write("\n".join(pinned) + "\n")
                    ok, out2 = await _run(base + ["--no-deps", "--disable-pip", "-r", pin_file],
                                          workspace, timeout=TOOL_TIMEOUT)
                    deps = _parse_audit(out2)
                    note = " (audited from exact pins; full resolution failed)"
                if deps is None:
                    findings.append(_finding(
                        scanner="deps", severity="info", file=label, line=0,
                        rule="DEPS001",
                        message=f"Dependency audit of {label} could not run: "
                                f"{reason}. Its dependencies were NOT checked."))
                    continue
            for dep in deps:
                vulns = dep.get("vulns") or []
                if not vulns:
                    continue
                key = (str(dep.get("name", "")).lower(), str(dep.get("version", "")))
                rec = seen.setdefault(key, {"name": dep.get("name", ""), "version": dep.get("version", ""),
                                            "vulns": {}, "labels": set(), "note": note})
                rec["labels"].add(label)
                for v in vulns:
                    rec["vulns"].setdefault(v.get("id", ""), v)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # One finding per vulnerable package, not per advisory. An old Django pin
    # carries dozens of advisories; counted one by one it would outweigh every
    # other finding in the report combined. The advisories are all listed in
    # the message, and the package is still exactly one thing to upgrade.
    for (_, _), rec in sorted(seen.items()):
        vulns = list(rec["vulns"].values())
        ids = [v.get("id", "") for v in vulns]
        has_cve = any(i.upper().startswith("CVE") or
                      any(str(a).upper().startswith("CVE") for a in (v.get("aliases") or []))
                      for v in vulns for i in [v.get("id", "")])
        fixes = sorted({f for v in vulns for f in (v.get("fix_versions") or [])},
                       key=lambda x: [int(t) if t.isdigit() else 0 for t in re.split(r"[.\-]", x)])
        n = len(vulns)
        first = ids[0] if ids else ""
        findings.append(_finding(
            scanner="deps", severity="high" if has_cve else "medium",
            file=f"{rec['name']}=={rec['version']}", line=0,
            rule=first + (f" (+{n - 1} more)" if n > 1 else ""),
            message=(f"{n} known vulnerabilit{'y' if n == 1 else 'ies'} in "
                     f"{rec['name']} {rec['version']} (declared in "
                     f"{', '.join(sorted(rec['labels']))}){rec['note']}. "
                     + (f"Fixed in: {', '.join(fixes[:4])}. " if fixes else "No fixed version published. ")
                     + "Advisories: " + ", ".join(ids[:6]) + (" …" if n > 6 else "")),
        ))
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
        # A mypy error is a maintainability signal, not a vulnerability. Rating
        # it "high" put psf/requests at 36 high-severity findings — every one of
        # them `attr-defined` on a conditional-import compat shim, which is
        # exactly the pattern mypy cannot follow and the author wrote on
        # purpose. Weighted as high these alone forced the grade to F.
        sev = "low" if level == "error" else "info"
        findings.append(_finding(
            scanner="types", severity=sev,
            file=_rel(m.group(1), workspace),
            line=int(m.group(2)), rule=level,
            message=m.group(4),
        ))
    return findings[:500]


# ── secrets ───────────────────────────────────────────────────────────────────

# detect-secrets mixes two very different things: plugins that recognise actual
# key material (an RSA block, an AWS key id) and entropy heuristics that fire on
# any long random-looking string. Rating both "critical" meant a git commit SHA
# pinned in .pre-commit-config.yaml was reported as a critical leaked secret —
# measured, that was all 4 of pallets/click's critical findings.
_HIGH_CONFIDENCE_SECRETS = {
    "Private Key", "AWS Access Key", "AWS Sensitive Information",
    "Azure Storage Account access key", "GitHub Token", "GitLab Token",
    "Slack Token", "Stripe Access Key", "Twilio API Key", "SendGrid API Key",
    "Mailchimp Access Key", "NPM tokens", "Discord Bot Token",
    "OpenAI API Key", "Square OAuth Secret", "PyPI upload token",
    "JSON Web Token", "SoftLayer Credentials", "IBM Cloud IAM Key",
}

# Files whose whole purpose is to hold hashes, pins and lock digests. Entropy
# findings here are the file working as intended.
_DIGEST_FILE = re.compile(
    r"(^|/)(\.pre-commit-config\.ya?ml|poetry\.lock|Pipfile\.lock|"
    r"package-lock\.json|yarn\.lock|go\.sum|Cargo\.lock|"
    r"requirements.*\.txt|\.gitmodules)$", re.I)


def _secret_severity(secret_type: str, relpath: str) -> str:
    """critical only for recognised key material outside digest/lock files."""
    if _DIGEST_FILE.search(relpath.replace("\\", "/")):
        return "info"
    if secret_type in _HIGH_CONFIDENCE_SECRETS:
        return "critical"
    # Keyword/entropy plugins ("Hex High Entropy String", "Secret Keyword", …)
    # are guesses. Worth showing, not worth failing a repo over.
    return "medium"


async def _scan_secrets(workspace: str, venv_path: str) -> list[dict]:
    pip = _pip(venv_path)
    await _run([pip, "install", "detect-secrets", "--quiet"], workspace, timeout=60)
    ds = _tool(venv_path, "detect-secrets")
    ok, out = await _run([ds, "scan", "--all-files"],
                          workspace, timeout=TOOL_TIMEOUT)
    findings = []
    data = _json_or_raise(out, "detect-secrets") or {}
    for fpath, secrets in data.get("results", {}).items():
        rel = _rel(fpath, workspace)
        for secret in secrets:
            stype = secret.get("type", "secret")
            findings.append(_finding(
                scanner="secrets",
                severity=_secret_severity(stype, rel),
                file=rel,
                line=secret.get("line_number", 0),
                rule=stype,
                message=f"Potential {stype} detected (value hashed)",
            ))
    return findings


# ── dead code ─────────────────────────────────────────────────────────────────

async def _scan_deadcode(workspace: str, venv_path: str) -> list[dict]:
    pip = _pip(venv_path)
    await _run([pip, "install", "vulture", "--quiet"], workspace, timeout=60)
    vulture = _tool(venv_path, "vulture")
    # Vulture rates unused imports / unreachable code / unused arguments at
    # 90-100% but an unused function or class at only 60%, because in a
    # library "nobody here calls it" usually means "it is public API". A flat
    # 80% cut therefore hid every forgotten function and class. Run at 60% and
    # keep a 60% finding only where it is not ambiguous: a PRIVATE (single
    # leading underscore) function, method or class that nothing in the repo
    # references by name cannot be reached from outside either.
    ok, out = await _run([vulture, ".", "--min-confidence", "60"],
                          workspace, timeout=TOOL_TIMEOUT)
    findings = []
    for line in out.splitlines()[:600]:
        # path/file.py:42: unused function 'foo' (80% confidence)
        m = re.match(r"(.+?):(\d+): (.+?) \((\d+)% confidence\)", line)
        if not m:
            continue
        conf, msg = int(m.group(4)), m.group(3)
        if conf < 80:
            nm = re.match(r"unused (function|method|class) '(_[^_][^']*|_)'$", msg)
            if not nm:
                continue
        findings.append(_finding(
            scanner="deadcode", severity="low",
            file=_rel(m.group(1), workspace),
            line=int(m.group(2)), rule="dead_code",
            message=msg,
            confidence=m.group(4) + "%",
        ))
    return findings[:300]


# ── semgrep ──────────────────────────────────────────────────────────────────

async def _scan_semgrep(workspace: str, venv_path: str) -> list[dict]:
    pip = _pip(venv_path)
    await _run([pip, "install", "semgrep", "--quiet"], workspace, timeout=120)
    semgrep = _tool(venv_path, "semgrep")
    # NOTE: --config auto requires metrics to stay on — Semgrep uses that
    # ping to pick the curated ruleset for the repo; passing --metrics=off
    # (or SEMGREP_SEND_METRICS=off) makes "auto" fail outright.
    # Semgrep defaults -j to the CPU count, running that many rule-matching
    # workers in parallel — each one a real memory cost, not just a speed
    # knob. On a memory-capped deployment (SEMGREP_JOBS set) that parallelism
    # is what was pushing an already near-the-limit container into an OOM
    # kill; trading speed for a single worker keeps peak memory down.
    jobs = os.getenv("SEMGREP_JOBS", "").strip()
    jobs_args = ["--jobs", jobs] if jobs else []
    ok, out = await _run(
        [semgrep, "--config", "auto", "--json", "--quiet",
         "--disable-version-check", *jobs_args, "."],
        workspace, timeout=SEMGREP_TIMEOUT)
    findings = []
    sev_map = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
    data = _json_or_raise(out, "semgrep") or {}
    for item in data.get("results", []):
        extra    = item.get("extra") or {}
        sev_raw  = (extra.get("severity") or "INFO").upper()
        conf_raw = ((extra.get("metadata") or {}).get("confidence") or "").upper()
        # Mirror the bandit escalation rule: only the strongest signal
        # (rule author says ERROR *and* HIGH confidence) counts as critical.
        sev = "critical" if (sev_raw == "ERROR" and conf_raw == "HIGH") \
              else sev_map.get(sev_raw, "low")
        findings.append(_finding(
            scanner="semgrep", severity=sev,
            file=_rel(item.get("path", ""), workspace),
            line=(item.get("start") or {}).get("line", 0),
            rule=(item.get("check_id") or "").split(".")[-1],
            message=extra.get("message", ""),
            confidence=conf_raw,
        ))
    return findings[:500]


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

# ── architecture health — RepoSage's own analysis ─────────────────────────────
#
# Every other scanner here wraps an existing tool: ruff, bandit, mypy, vulture,
# semgrep, pip-audit, detect-secrets. This one is RepoSage's own, and it looks
# at something none of those can see.
#
# Linters read files. They have no model of how a repository is wired together,
# so a defect that only exists in the SHAPE of the codebase is invisible to
# them: a cycle between two modules is perfectly legal in every file involved;
# a module that half the codebase imports and that itself imports half the
# codebase has no illegal line in it; a function nothing can ever reach is
# syntactically perfect.
#
# RepoSage already builds the two graphs needed to see all three — the module
# dependency graph and the function call graph — for its analysis view. This
# scanner reuses them as an audit signal:
#
#   ARCH001  import cycle          modules that transitively import each other.
#                                  Nothing can be understood, tested or reused
#                                  in isolation once it is inside one.
#   ARCH002  god module            fan-in AND fan-out both far above the
#                                  repo's own distribution (mean + 2σ). Every
#                                  change risks it; it risks every change.
#   ARCH003  unreachable function  defined but not reachable through any call
#                                  path from an entry point. Distinct from
#                                  vulture, which guesses from name usage —
#                                  this is reachability over the real graph.
#   ARCH004  unstable dependency   depends on many modules, depended on by
#                                  none. Usually a script that drifted into
#                                  the package, or a layer pointing the wrong
#                                  way.
#
# Thresholds are derived from each repo's own distribution rather than fixed
# numbers, so the finding means "unusual for THIS codebase" instead of
# "larger than a number someone picked".

_ENTRY_NAMES = {"main", "run", "cli", "app", "start", "execute", "handler",
                "lambda_handler", "wsgi", "application", "setup"}


async def _scan_architecture(workspace: str, venv_path: str) -> list[dict]:
    """Graph-shaped defects. No external tool — parsing happens off the loop."""
    # AST-parsing a whole repo is CPU-bound and would stall the SSE stream the
    # UI is reading from, freezing the progress log until it finished.
    return await asyncio.to_thread(_architecture_findings, workspace)


def _architecture_findings(workspace: str) -> list[dict]:
    import analyzer

    findings: list[dict] = []
    try:
        parsed = analyzer._parse_all_files(workspace)
    except Exception as exc:
        log.warning("arch scan: parse failed: %s", exc)
        return []
    if not parsed:
        return []

    try:
        symbols   = analyzer._build_symbol_table(parsed)
        mdg_edges = analyzer._build_mdg_ast(parsed)
        fcg_edges = analyzer._build_fcg(parsed, symbols)
        modules   = analyzer._build_modules(parsed, mdg_edges)
    except Exception as exc:
        log.warning("arch scan: graph build failed: %s", exc)
        return []

    path_of = {m["name"]: m.get("path", "") for m in modules}
    src = {m["name"] for m in modules
           if _classify_area(m.get("path", "")) == "source"}

    # ── ARCH001 — import cycles ───────────────────────────────────────────────
    for cycle in analyzer._find_import_cycles(mdg_edges):
        members = [c for c in cycle if c in src]
        if len(members) < 2:
            continue
        head = members[0]
        findings.append(_finding(
            scanner="arch",
            severity="high" if len(members) > 2 else "medium",
            file=path_of.get(head, head), line=0, rule="ARCH001",
            message=("Import cycle across %d modules: %s. None of them can be "
                     "imported, tested or reused without dragging in the rest."
                     % (len(members), " → ".join(m.split(".")[-1] for m in members[:6])
                        + (" → …" if len(members) > 6 else ""))),
            confidence="high",
        ))

    # ── ARCH002 / ARCH004 — coupling outliers ────────────────────────────────
    srcmods = [m for m in modules if m["name"] in src]
    if len(srcmods) >= 6:
        fan_in  = [m.get("inbound", 0)  for m in srcmods]
        fan_out = [m.get("outbound", 0) for m in srcmods]
        mi, mo = _mean(fan_in), _mean(fan_out)
        si, so = _stdev(fan_in, mi), _stdev(fan_out, mo)
        # mean + 2σ: an outlier against this repo's own spread, not a fixed
        # number that would call every module in a large project a god module.
        ti, to = mi + 2 * si, mo + 2 * so

        for m in srcmods:
            fi, fo = m.get("inbound", 0), m.get("outbound", 0)
            if si > 0 and so > 0 and fi > ti and fo > to:
                findings.append(_finding(
                    scanner="arch", severity="medium",
                    file=m.get("path", ""), line=0, rule="ARCH002",
                    message=("God module: imported by %d and imports %d, both "
                             "more than 2 standard deviations above this repo's "
                             "average "
                             "(%.1f in / %.1f out). A change here can reach "
                             "most of the codebase." % (fi, fo, mi, mo)),
                    confidence="medium",
                ))
            elif fo >= 4 and fi == 0:
                findings.append(_finding(
                    scanner="arch", severity="low", file=m.get("path", ""),
                    line=0, rule="ARCH004",
                    message=("Unstable dependency: imports %d modules, nothing "
                             "imports it. Either a script living inside the "
                             "package, or a layer pointing the wrong way."
                             % fo),
                    confidence="medium",
                ))

    # ── ARCH003 — unreachable functions ──────────────────────────────────────
    # Reachability over the real call graph, which is why this finds things
    # vulture does not: vulture asks "does this name appear anywhere", this
    # asks "can control actually arrive here".
    callers: dict[str, set[str]] = {}
    called: set[str] = set()
    for e in fcg_edges:
        a, b = str(e.get("from", "")), str(e.get("to", ""))
        if not a or not b:
            continue
        callers.setdefault(a, set()).add(b)
        called.add(b)

    defined: dict[str, dict] = {}
    for pf in parsed:
        if _classify_area(pf.get("path", "")) != "source":
            continue
        for fn in pf.get("functions", []):
            name = fn.get("name") or ""
            if not name or name.startswith("_"):
                continue              # private/dunder: convention, not a defect
            defined[name] = {"path": pf.get("path", ""), "line": fn.get("line", 0)}

    roots = {n for n in defined if n.lower() in _ENTRY_NAMES}
    roots |= {n for n in defined if n not in called}      # public API surface
    reachable = set(roots)
    stack = list(roots)
    while stack:
        cur = stack.pop()
        for nxt in callers.get(cur, ()):
            if nxt not in reachable:
                reachable.add(nxt)
                stack.append(nxt)

    orphans = [n for n in defined if n not in reachable]
    # Cap: a repo whose call graph resolved poorly would otherwise produce a
    # wall of low-confidence findings that drowns out the rest of the audit.
    for name in sorted(orphans)[:25]:
        d = defined[name]
        findings.append(_finding(
            scanner="arch", severity="info", file=d["path"], line=d["line"],
            rule="ARCH003",
            message=("`%s` is never reached from any entry point in the call "
                     "graph. Dead by reachability, not by name lookup — "
                     "confirm before deleting, dynamic dispatch is invisible "
                     "to static analysis." % name),
            confidence="low",
        ))

    return findings


# ─────────────────────────────────────────────────────────────────────────────
# EXCEPTION FLOW ANALYSIS  —  RepoSage original, scanner 9 of 9
# ─────────────────────────────────────────────────────────────────────────────
#
# WHAT IT IS
#   An interprocedural exception-propagation analysis. It builds an Exception
#   Propagation Graph — this repo's call graph annotated with which exception
#   types can escape each function — and reports exception-handling defects that
#   no file-at-a-time linter can see, because deciding them requires following
#   an exception across function and module boundaries.
#
# WHY IT IS WORTH HAVING
#   Souza, Coelho, Correia, Lima, Teixeira & Neto, "Slithering Through Exception
#   Handling Bugs in Python: Understanding Root Causes, Symptoms, and Fixes"
#   (2025), studied 1,649 confirmed exception-handling bugs across 550
#   open-source Python projects. "Unhandled Exception" was the single largest
#   root cause at 50.64%, and "Failure to Handle Expected Exceptions" the most
#   common symptom at 33.97%. Adding an exception-handling block, or fixing an
#   anti-pattern, accounted for the bulk of the fixes.
#
#   de Padua & Shang, "Studying the Prevalence of Exception Handling
#   Anti-Patterns" (ICSME 2017), detected 19 such anti-patterns across 16
#   systems and found exactly five to be prevalent: Unhandled Exceptions, Catch
#   Generic, Unreachable Handler, Over-catch and Destructive Wrapping. Four of
#   those five are FLOW anti-patterns — undecidable from one file. Their tool
#   targets Java and C#.
#
#   Python has no equivalent. ruff (BLE001, B904, TRY-*), pylint (W0703) and
#   tryceratops each reason about one `try` statement in one file, so none of
#   them can answer "is this handler reachable", "does anything handle this
#   failure", or "can this cleanup destroy the exception already in flight".
#   The five rules below answer exactly those questions.
#
# WHY IT IS SOUND ENOUGH TO SHIP
#   A whole-program exception analysis for a dynamically typed language is
#   undecidable in general, which is why nobody ships one. The restriction that
#   makes this tractable: EVERY RULE IS ANCHORED ON EXCEPTION CLASSES THE
#   AUDITED REPOSITORY ITSELF DEFINES, in its shipped code. For those the repo
#   is a closed world — every `raise` of `MyPkgError` and every `except
#   MyPkgError` that can exist is inside the code being audited, because a
#   stdlib or third-party function cannot raise a class it has never seen. The
#   analysis stays approximate about control flow but is near-exact about the
#   exception lattice, so it under-reports rather than inventing findings.
#
# VALIDATION (see the audit report's methodology section)
#   Precision: run over psf/requests, pallets/click, pallets/flask,
#   pyinvoke/invoke, google/python-fire, tiangolo/typer, encode/httpx,
#   Textualize/rich, scrapy/scrapy and psf/black — 1 to 7 findings each, and
#   every one checked by hand against the source. Three earlier false-positive
#   classes were traced and fixed rather than thresholded away: name collisions
#   (requests defines its own `SSLError` AND imports urllib3's under the same
#   bare name), generators (an exception can be thrown IN at a `yield`, so
#   "nothing here raises it" proves nothing), and test fixtures (flask's tests
#   define ten throwaway exception classes on purpose).
#   Recall: a fixture repo containing one instance of each defect, where all
#   five rules fire and the correctly-handled control case stays silent.

_EXC_SKIP = VENDOR_DIR_NAMES | {"build", "dist"}
_EXC_MAX_FILES, _EXC_MAX_LINES = 500, 5000
_EXC_ROOTS = {"Exception", "BaseException"}
_EXC_SILENT_HINTS = ("log", "print", "warn", "debug", "echo", "traceback",
           "capture_exception", "sentry")
_EXC_OPAQUE = {"getattr", "setattr", "eval", "exec", "__import__"}
_EXC_API_MODULES = ("exceptions.py", "errors.py", "exception.py", "error.py")


def _exc_dotted(n) -> str:
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        b = _exc_dotted(n.value)
        return f"{b}.{n.attr}" if b else n.attr
    if isinstance(n, ast.Call):
        return _exc_dotted(n.func)
    return ""


def _exc_last(n) -> str:
    return _exc_dotted(n).rsplit(".", 1)[-1]


def _exc_handler_types(h) -> frozenset[str]:
    if h.type is None:
        return frozenset({"Exception"})
    if isinstance(h.type, ast.Tuple):
        return frozenset(filter(None, (_exc_last(e) for e in h.type.elts)))
    return frozenset(filter(None, (_exc_last(h.type),)))


def _exc_is_silent(body) -> bool:
    """de Padua & Shang: Catch and Do Nothing / Dummy Handler / Return Null."""
    for st in body:
        if isinstance(st, (ast.Pass, ast.Break, ast.Continue)):
            continue
        if isinstance(st, ast.Return):
            v = st.value
            if v is None or (isinstance(v, ast.Constant) and v.value in (None, False)):
                continue
            return False
        if isinstance(st, ast.Expr):
            v = st.value
            if isinstance(v, ast.Constant):
                continue
            if isinstance(v, ast.Call) and any(
                    k in _exc_dotted(v.func).lower() for k in _EXC_SILENT_HINTS):
                continue
            return False
        return False
    return True


# ── file discovery ───────────────────────────────────────────────────────────

def _exc_list_files(ws, area_of):
    out = []
    for root, dirs, files in os.walk(ws):
        dirs[:] = sorted(d for d in dirs if d not in _EXC_SKIP and not d.startswith("."))
        for fn in sorted(files):
            if fn.endswith(".py"):
                out.append(os.path.relpath(os.path.join(root, fn), ws))
    # Shipped code first: the file cap must not be eaten by a docs_src/ tree
    # before it reaches the package being audited. Measured on tiangolo/typer,
    # whose 500 docs example files hid every one of its own modules.
    out.sort(key=lambda r: (area_of(r) != "source", r))
    return out[:_EXC_MAX_FILES]


def _exc_mod_roots(rels):
    """Top-level names that belong to THIS repo — used to tell an import of the
    repo's own `SSLError` from urllib3's."""
    roots = set()
    for r in rels:
        parts = r.replace("\\", "/").split("/")
        if parts and parts[0] in ("src", "lib") and len(parts) > 1:
            parts = parts[1:]
        roots.add(parts[0][:-3] if parts[0].endswith(".py") else parts[0])
    return roots


def _exc_mod_key(rel):
    """`src/requests/models.py` -> `requests.models`; a package __init__ maps to
    the package itself, which is what makes relative imports resolve."""
    parts = rel.replace("\\", "/").split("/")
    if parts and parts[0] in ("src", "lib") and len(parts) > 1:
        parts = parts[1:]
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _exc_repo_imports(tree, rel, roots, mod_index):
    """
    {local name: rel path of the repo file it came from} for this file's
    repo-internal imports. This is what turns a bare `foo()` into one specific
    function instead of "any of the 9 functions named foo in this project".
    """
    me = _exc_mod_key(rel)
    pkg = me if rel.replace("\\", "/").endswith("__init__.py") else me.rsplit(".", 1)[0]
    out = {}
    for st in ast.walk(tree):
        if isinstance(st, ast.ImportFrom):
            if st.level:
                # In a package __init__, `.` is the package itself, not its
                # parent — the same off-by-one that silently emptied the module
                # dependency graph before.
                base = pkg.split(".")
                for _ in range(st.level - 1):
                    if base:
                        base.pop()
                mod = ".".join(filter(None, base + ([st.module] if st.module else [])))
            else:
                mod = st.module or ""
                if mod.split(".")[0] not in roots:
                    continue
            for a in st.names:
                local = a.asname or a.name
                for cand in (f"{mod}.{a.name}", mod):
                    if cand in mod_index:
                        out[local] = mod_index[cand]
                        break
        elif isinstance(st, ast.Import):
            for a in st.names:
                if a.name.split(".")[0] in roots and a.name in mod_index:
                    out[a.asname or a.name.split(".")[0]] = mod_index[a.name]
    return out


def _exc_shadowed(tree, roots):
    """
    Names this file binds from OUTSIDE the repo.

    This is the fix for the single worst false-positive source: requests
    defines its own `SSLError` AND `JSONDecodeError`, and models.py imports
    urllib3's and the stdlib's under the very same bare names. Keying the
    exception lattice on the last name segment alone conflated them and
    produced confident, wrong findings about handlers in that file.
    """
    out = set()
    for st in ast.walk(tree):
        if isinstance(st, ast.ImportFrom):
            if st.level:                                  # from .x import — ours
                continue
            top = (st.module or "").split(".")[0]
            if top in roots:
                continue
            out.update(a.asname or a.name.rsplit(".", 1)[-1] for a in st.names)
        elif isinstance(st, ast.Import):
            for a in st.names:
                if a.name.split(".")[0] not in roots:
                    out.add(a.asname or a.name.split(".")[0])
    return out


# ── stage 1: parse ───────────────────────────────────────────────────────────

def _exc_collect(ws, area_of):
    rels = _exc_list_files(ws, area_of)
    roots = _exc_mod_roots(rels)
    mod_index = {_exc_mod_key(r): r for r in rels}
    classes, funcs, tries = {}, [], []
    exported, mentioned, raised, imports = set(), set(), {}, {}
    nfiles = 0

    for rel in rels:
        fp = os.path.join(ws, rel)
        try:
            with open(fp, encoding="utf-8", errors="ignore") as fh:
                raw = fh.read()
            if raw.count("\n") > _EXC_MAX_LINES:
                continue
            tree = ast.parse(raw, filename=rel)
        except (SyntaxError, OSError, ValueError):
            continue
        nfiles += 1
        shadow = _exc_shadowed(tree, roots)
        imports[rel] = _exc_repo_imports(tree, rel, roots, mod_index)
        _exc_file(rel, tree, shadow, classes, funcs, tries, exported,
              mentioned, raised, os.path.basename(rel) == "__init__.py")

    return dict(classes=classes, funcs=funcs, tries=tries, exported=exported,
                mentioned=mentioned, raised=raised, imports=imports,
                nfiles=nfiles)


def _exc_file(rel, tree, shadow, classes, funcs, tries, exported, mentioned,
          raised, is_init):
    for st in tree.body:
        if isinstance(st, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in st.targets):
            if isinstance(st.value, (ast.List, ast.Tuple)):
                exported.update(e.value for e in st.value.elts
                                if isinstance(e, ast.Constant) and isinstance(e.value, str))
        if is_init and isinstance(st, (ast.Import, ast.ImportFrom)):
            exported.update(a.asname or a.name.rsplit(".", 1)[-1] for a in st.names)

    # Every constructor call of a repo name, anywhere — `e = MyError(); raise e`
    # is still a use, and EXC002 must not call such a class dead.
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            nm = _exc_last(n.func)
            if nm and nm not in shadow:
                mentioned.add(nm)

    def scopes(node, prefix, cls=None):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.ClassDef):
                classes.setdefault(ch.name, {"bases": [_exc_last(b) for b in ch.bases],
                                             "path": rel, "line": ch.lineno})
                scopes(ch, f"{prefix}{ch.name}.", ch.name)
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rec = {"name": ch.name, "qual": f"{prefix}{ch.name}", "path": rel,
                       "line": ch.lineno, "raises": [], "calls": [], "shadow": shadow,
                       "cls": cls}
                funcs.append(rec)
                _exc_walk(ch.body, (), rec, rel, tries, shadow, raised)
                scopes(ch, f"{prefix}{ch.name}.", cls)
            else:
                scopes(ch, prefix, cls)

    scopes(tree, "")
    mod = {"name": f"<{rel}>", "qual": f"<{rel}>", "path": rel, "line": 1,
           "raises": [], "calls": [], "shadow": shadow, "cls": None}
    funcs.append(mod)
    _exc_walk(tree.body, (), mod, rel, tries, shadow, raised)


def _exc_walk(stmts, ctx, rec, rel, tries, shadow, raised):
    """
    Walk a statement list carrying `ctx` — the handler type-sets of the try
    blocks enclosing this point within the current function. That context is
    what separates a raise this function already handles from one that escapes
    it, and it is why this needs a structured walk and not a flat ast.walk.
    """
    for st in stmts:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(st, ast.Try):
            hs = [_exc_handler_types(h) for h in st.handlers]
            allh = frozenset().union(*hs) if hs else frozenset()
            bc, br, opaque = _exc_flat(st.body, shadow)
            fc, _, _ = _exc_flat(st.finalbody, shadow)
            tries.append({
                "path": rel, "line": st.lineno, "fn": rec["qual"],
                "shadow": shadow, "owner": rec,
                "handlers": [{"line": h.lineno, "types": hs[i],
                              "silent": _exc_is_silent(h.body)}
                             for i, h in enumerate(st.handlers)],
                "body_calls": bc, "body_raises": br, "opaque": opaque,
                # An exception can be thrown IN at a yield by whoever drives the
                # generator, so "nothing here raises it" proves nothing.
                # click's @contextmanager helpers are exactly this shape.
                "generator": any(isinstance(n, (ast.Yield, ast.YieldFrom))
                                 for n in ast.walk(ast.Module(body=st.body, type_ignores=[]))),
                "finally_calls": fc,
                "finally_line": st.finalbody[0].lineno if st.finalbody else 0,
            })
            _exc_walk(st.body, ctx + (allh,), rec, rel, tries, shadow, raised)
            _exc_walk(st.orelse, ctx + (allh,), rec, rel, tries, shadow, raised)
            for h in st.handlers:
                _exc_walk(h.body, ctx, rec, rel, tries, shadow, raised)
            _exc_walk(st.finalbody, ctx, rec, rel, tries, shadow, raised)
            continue

        if isinstance(st, ast.Raise) and st.exc is not None:
            t = _exc_last(st.exc)
            if t and t not in shadow:
                rec["raises"].append((t, st.lineno, ctx))
                raised.setdefault(t, (rel, st.lineno))

        for _f, val in ast.iter_fields(st):
            for item in (val if isinstance(val, list) else [val]):
                if isinstance(item, ast.expr):
                    for n in ast.walk(item):
                        if isinstance(n, ast.Call):
                            dn = _exc_dotted(n.func)
                            if dn:
                                rec["calls"].append((dn, n.lineno, ctx))
        for _f, val in ast.iter_fields(st):
            if isinstance(val, list) and val and isinstance(val[0], ast.stmt):
                _exc_walk(val, ctx, rec, rel, tries, shadow, raised)


def _exc_flat(stmts, shadow):
    calls, raises, opaque = set(), set(), False

    def go(node):
        nonlocal opaque
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(ch, ast.Call):
                dn = _exc_dotted(ch.func)
                if dn:
                    calls.add(dn)
                if dn.rsplit(".", 1)[-1] in _EXC_OPAQUE:
                    opaque = True
            elif isinstance(ch, ast.Raise) and ch.exc is not None:
                t = _exc_last(ch.exc)
                if t and t not in shadow:
                    raises.add(t)
            go(ch)

    for st in stmts:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        go(ast.Module(body=[st], type_ignores=[]))
    return calls, raises, opaque


# ── stage 2: lattice + fixpoint ──────────────────────────────────────────────

def _exc_hierarchy(classes):
    is_exc, changed = set(), True
    while changed:
        changed = False
        for name, c in classes.items():
            if name in is_exc:
                continue
            for b in c["bases"]:
                builtin = b not in classes and re.search(r"(Error|Exception|Warning)$", b)
                if b in _EXC_ROOTS or b in is_exc or builtin:
                    is_exc.add(name)
                    changed = True
                    break
    anc = {}
    for name in is_exc:
        seen, stack = set(), list(classes[name]["bases"])
        while stack:
            b = stack.pop()
            if b in seen:
                continue
            seen.add(b)
            if b in classes:
                stack.extend(classes[b]["bases"])
        anc[name] = seen
    return is_exc, anc


def _exc_caught(ctx, t, anc):
    fam = {t} | anc.get(t, set()) | _EXC_ROOTS
    return any(fam & hs for hs in ctx)


class _CallResolver:
    """
    Scope-aware call resolution.

    Resolving a call by its bare last name — which is all the display call
    graph does — is far too coarse here: `click` defines 1,976 functions and
    almost every interesting method name is defined several times, so every
    call looks ambiguous and a precision-first rule fires on nothing. Three
    cheap scope rules fix most of it:

      self.m()  ->  method `m` on the enclosing class or a base it defines here
      m()       ->  module-level `m` in this same file, else whatever this file
                    imports under that name from inside the repo
      mod.m()   ->  `m` in the repo file bound to the alias `mod`

    Anything that still does not land is returned as UNRESOLVED (empty), not as
    "all functions with this name" — an unresolved call must weaken a
    conclusion, never manufacture one.
    """

    def __init__(self, funcs, classes, imports):
        self.classes, self.imports = classes, imports
        self.by_name: dict[str, list[dict]] = {}
        self.mod_funcs: dict[str, dict[str, list[dict]]] = {}
        self.cls_methods: dict[str, dict[str, list[dict]]] = {}
        for r in funcs:
            self.by_name.setdefault(r["name"], []).append(r)
            if r.get("cls"):
                self.cls_methods.setdefault(r["cls"], {}).setdefault(
                    r["name"], []).append(r)
            else:
                self.mod_funcs.setdefault(r["path"], {}).setdefault(
                    r["name"], []).append(r)

    def _exc_chain(self, cls):
        """`cls` plus the ancestors of it that this repo defines."""
        seen, order, stack = set(), [], [cls]
        while stack:
            c = stack.pop(0)
            if c in seen or c not in self.classes:
                continue
            seen.add(c)
            order.append(c)
            stack.extend(self.classes[c]["bases"])
        return order

    def resolve(self, dotted, rec):
        parts = dotted.split(".")
        name = parts[-1]

        if parts[0] in ("self", "cls") and rec.get("cls"):
            for c in self._exc_chain(rec["cls"]):
                hit = self.cls_methods.get(c, {}).get(name)
                if hit:
                    return hit
            return []                      # inherited from outside, or dynamic

        if len(parts) == 1:
            hit = self.mod_funcs.get(rec["path"], {}).get(name)
            if hit:
                return hit
            tgt = self.imports.get(rec["path"], {}).get(name)
            if tgt:
                return self.mod_funcs.get(tgt, {}).get(name) or []
            return self.by_name.get(name) or []

        tgt = self.imports.get(rec["path"], {}).get(parts[0])
        if tgt:
            return self.mod_funcs.get(tgt, {}).get(name) or []
        if parts[0] in self.classes:                       # Class.method()
            for c in self._exc_chain(parts[0]):
                hit = self.cls_methods.get(c, {}).get(name)
                if hit:
                    return hit
            return []
        return self.by_name.get(name) or []


def _exc_solve(funcs, classes, imports, is_exc, anc, max_cand=4):
    """
    Fixpoint over the resolved call graph:

        escape(f) = { t raised in f and not caught inside f }
                  u  U  { t in escape(g) : t not caught at this call site }
                     g in callees(f)

    The lattice is the finite set of exception classes this repo defines, so it
    always terminates and recursion needs no special case.
    """
    rv = _CallResolver(funcs, classes, imports)
    for r in funcs:
        r["escape"] = {t for (t, _l, c) in r["raises"]
                       if t in is_exc and not _exc_caught(c, t, anc)}
        r["direct"] = set(r["escape"])
        r["edges"] = [(rv.resolve(dn, r), c) for (dn, _l, c) in r["calls"]]

    for _ in range(40):
        changed = False
        for r in funcs:
            n0 = len(r["escape"])
            for (cands, c) in r["edges"]:
                if not cands or len(cands) > max_cand:
                    continue
                for g in cands:
                    if g is r:
                        continue
                    for t in g["escape"]:
                        if t not in r["escape"] and not _exc_caught(c, t, anc):
                            r["escape"].add(t)
            if len(r["escape"]) != n0:
                changed = True
        if not changed:
            break
    return rv


def _exc_confident_reach(try_rec, rv, max_depth=6):
    """
    Exception types reachable from a guarded block, counting only callees that
    resolve to exactly ONE function. Ambiguous and unresolved calls are skipped
    rather than unioned: over-approximating here would invent findings, and a
    missed finding is much the cheaper error for an audit to make.
    """
    owner = try_rec["owner"]
    out: set[str] = set()
    seen: set[int] = set()
    frontier = [(dn, owner) for dn in try_rec["body_calls"]]
    for _ in range(max_depth):
        nxt = []
        for dn, ctxrec in frontier:
            if dn.rsplit(".", 1)[-1] in try_rec["shadow"]:
                continue
            cands = rv.resolve(dn, ctxrec)
            if len(cands) != 1:
                continue
            g = cands[0]
            if id(g) in seen:
                continue
            seen.add(id(g))
            out |= g["escape"]
            nxt.extend((c, g) for (c, _l, _x) in g["calls"])
        if not nxt:
            break
        frontier = nxt
    return out


# ── stage 3: the five rules ──────────────────────────────────────────────────

def _exception_flow_findings(ws: str, area_of=lambda p: "source") -> list[tuple]:
    d = _exc_collect(ws, area_of)
    classes, funcs, tries = d["classes"], d["funcs"], d["tries"]
    is_exc, anc = _exc_hierarchy(classes)

    # A project's exception _exc_hierarchy is the one in its SHIPPED code. Test
    # suites define throwaway exception classes on purpose — pallets/flask has
    # ten (`AppError`, `E1`, `MyException`, …) raised in a test to prove an
    # error handler fires, and pyinvoke/invoke has `OhNoz`, `Whoops` and
    # `TotalFailure`. Counted as project exceptions they dominated the output
    # and crowded the real findings out of the cap, and every one of them was
    # noise: a test exception that nothing catches is what the test is for.
    is_exc = {e for e in is_exc if area_of(classes[e]["path"]) == "source"}
    if not is_exc:
        return []
    rv = _exc_solve(funcs, classes, d["imports"], is_exc, anc)

    desc = {e: set() for e in is_exc}
    for c in is_exc:
        for a in anc[c]:
            if a in desc:
                desc[a].add(c)

    caught = set()
    for t in tries:
        for h in t["handlers"]:
            caught |= {x for x in h["types"] if x not in t["shadow"]}

    # Raise sites in tests do not make a source exception "handled" or
    # "unhandled" for our purposes either — we care where shipped code raises.
    raised = {k: v for k, v in d["raised"].items()
              if k in is_exc and area_of(v[0]) == "source"}
    out = []

    def public(e):
        return (e in d["exported"] or not e.startswith("_")
                and classes[e]["path"].endswith(_EXC_API_MODULES))

    # ── EXC001 — Unhandled project exception ─────────────────────────────────
    # Souza et al. measured "Unhandled Exception" as the single largest root
    # cause of real Python exception-handling bugs (50.64% of 1,649 confirmed
    # bugs across 550 projects). Two cases, and they are not the same defect:
    #
    #   internal — a class the package does not expose. Nothing inside handles
    #              it and nothing outside can even name it, so it can only ever
    #              end as a traceback. Reported one by one.
    #   public   — part of the documented surface, where leaving it to the
    #              caller is the whole design. Reported as ONE aggregate note,
    #              because a library can legitimately have a dozen of these and
    #              a dozen findings would just bury the rest of the audit.
    unh = [e for e in sorted(is_exc)
           if e in raised and not ((({e} | anc[e]) - _EXC_ROOTS) & caught)]
    internal = [e for e in unh if not public(e)]
    pub = [e for e in unh if public(e)]

    for e in internal[:10]:
        path, line = raised[e]
        # Deliberately `low`, not `medium`: some of these are fail-fast by
        # design — typer raises MultipleTyperAnnotationsError to tell a
        # developer they misused the API and very much wants it to escape.
        # Static analysis cannot read that intent, so the finding states the
        # fact and leaves the judgement to the reader instead of asserting a
        # bug it cannot prove.
        out.append(("EXC001", "low", path, line,
                    f"`{e}` is raised here and handled nowhere: no `except {e}` "
                    f"in this repository, no handler for a base class it "
                    f"defines, and `{e}` is not re-exported, so callers cannot "
                    f"name it either. Any path reaching this raise terminates "
                    f"in a traceback — intended for a fail-fast misuse error, "
                    f"a gap for anything recoverable.", "high"))

    if pub:
        e0 = pub[0]
        path, line = raised[e0]
        out.append(("EXC001", "info", path, line,
                    f"{len(pub)} of this package's own exception types are "
                    f"raised but never handled anywhere inside it "
                    f"({', '.join(pub[:6])}{', …' if len(pub) > 6 else ''}). "
                    f"They are part of the public API, so callers are expected "
                    f"to catch them — worth confirming each one is documented, "
                    f"since nothing in the project demonstrates recovery.",
                    "high"))

    # ── EXC002 — Handler for an exception that can no longer occur ────────────
    # A closed-world fact, not an approximation: for a class this repo defines,
    # every `raise` of it and every construction of it must be in this repo.
    dead = {}
    for t in tries:
        for h in t["handlers"]:
            for x in h["types"]:
                if (x in is_exc and x not in t["shadow"]
                        and not (({x} | desc[x]) & set(raised))
                        and x not in d["mentioned"]):
                    dead.setdefault(x, (t["path"], h["line"]))
    for x, (path, line) in sorted(dead.items())[:10]:
        out.append(("EXC002", "medium", path, line,
                    f"`except {x}` guards against an exception that no longer "
                    f"exists: `{x}` is defined in this repo and is never raised "
                    f"or constructed anywhere in it. The recovery code is dead — "
                    f"usually a refactor removed the raise and left the handler.",
                    "high"))

    # ── EXC003 — Silent catch-all over distinct, deliberate failures ──────────
    for t in tries:
        reach = _exc_confident_reach(t, rv)
        reach |= {r for r in t["body_raises"] if r in is_exc}
        reach &= is_exc
        if len(reach) < 2:
            continue
        for h in t["handlers"]:
            if not h["silent"] or not (h["types"] & _EXC_ROOTS):
                continue
            names = ", ".join(sorted(reach)[:5])
            out.append(("EXC003", "high" if len(reach) >= 4 else "medium",
                        t["path"], h["line"],
                        f"Catch-all with no recovery swallows {len(reach)} "
                        f"different failures this project raises deliberately "
                        f"({names}{', …' if len(reach) > 5 else ''}). Each one "
                        f"means something specific and all of them are "
                        f"discarded identically.", "high"))

    # ── EXC004 — cleanup that can replace the in-flight exception ────────────
    for t in tries:
        if not t["finally_calls"]:
            continue
        direct, trans = set(), set()
        for dn in t["finally_calls"]:
            if dn.rsplit(".", 1)[-1] in t["shadow"]:
                continue
            cands = rv.resolve(dn, t["owner"])
            if len(cands) == 1:
                direct |= cands[0]["direct"]       # raises it ITSELF
                trans  |= cands[0]["escape"]       # …or somewhere below it
        if direct:
            out.append(("EXC004", "medium", t["path"],
                        t["finally_line"] or t["line"],
                        f"This `finally` calls code that raises "
                        f"{', '.join(sorted(direct)[:3])} itself. A failure "
                        f"during cleanup replaces the exception that was "
                        f"already propagating, and the original cause is lost "
                        f"with no trace of it in the traceback.", "high"))
        elif trans:
            out.append(("EXC004", "low", t["path"],
                        t["finally_line"] or t["line"],
                        f"This `finally` calls code that can raise "
                        f"{', '.join(sorted(trans)[:3])} further down its own "
                        f"call chain. If it does while an exception is already "
                        f"propagating, the original cause is replaced. Weaker "
                        f"than a direct raise — confirm the chain is reachable.",
                        "low"))

    # ── EXC005 — no common root for the package's own exceptions ──────────────
    roots = {e for e in is_exc if not (anc[e] & is_exc)}
    if len(is_exc) >= 4 and len(roots) >= 3:
        c = classes[sorted(roots)[0]]
        out.append(("EXC005", "info", c["path"], c["line"],
                    f"The {len(is_exc)} exception classes here have "
                    f"{len(roots)} unrelated roots "
                    f"({', '.join(sorted(roots)[:4])}…). Callers cannot write a "
                    f"single `except` that catches everything this package "
                    f"raises, so they end up catching `Exception`.", "high"))
    return out

async def _scan_excflow(workspace: str, venv_path: str) -> list[dict]:
    """Exception-handling defects that need whole-repo flow, not one file."""
    raw = await asyncio.to_thread(_exception_flow_findings, workspace,
                                  _classify_area)
    return [_finding(scanner="excflow", severity=sev, file=path, line=line,
                     rule=rule, message=msg, confidence=conf)
            for (rule, sev, path, line, msg, conf) in raw]


def _mean(xs: list[int]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: list[int], mean: float) -> float:
    if len(xs) < 2:
        return 0.0
    return math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))


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
        "area":       _classify_area(file),
    }


# How much context to show around a finding. Two lines either side is enough
# to see the enclosing statement without turning the report into a code dump.
SNIPPET_BEFORE = 2
SNIPPET_AFTER  = 2
SNIPPET_MAX_COLS = 200      # one pathological minified line must not blow up the payload
SNIPPET_MAX_FILE_BYTES = 2_000_000


def _attach_snippets(findings: list[dict], workspace: str) -> None:
    """Add the offending source lines to every finding that names a location.

    Mutates in place. Files are read once and cached, because a single ruff rule
    routinely produces a hundred findings in the same module.
    """
    cache: dict[str, list[str] | None] = {}

    def lines_of(rel: str) -> list[str] | None:
        if rel not in cache:
            cache[rel] = None
            path = os.path.join(workspace, rel)
            try:
                # Guard the obvious ways this goes wrong on a hostile or merely
                # unusual repo: a path that escapes the workspace, a 300MB
                # generated file, a binary blob that happens to end in .py.
                if not os.path.realpath(path).startswith(os.path.realpath(workspace)):
                    return None
                if os.path.getsize(path) > SNIPPET_MAX_FILE_BYTES:
                    return None
                with open(path, encoding="utf-8", errors="replace") as fh:
                    cache[rel] = fh.read().splitlines()
            except (OSError, ValueError):
                cache[rel] = None
        return cache[rel]

    for f in findings:
        rel, line = f.get("file") or "", int(f.get("line") or 0)
        if not rel or line < 1:
            continue
        src = lines_of(rel)
        if not src or line > len(src):
            continue
        start = max(1, line - SNIPPET_BEFORE)
        end   = min(len(src), line + SNIPPET_AFTER)
        f["code"] = [t[:SNIPPET_MAX_COLS] for t in src[start - 1:end]]
        f["code_start"] = start


# Tools that look at the same kinds of defect, so one real problem on one line
# is reported once per tool: `eval(request.args["c"])` was counted once by
# bandit (B307) and three times by semgrep. Measured on a fixture with planted
# bugs, 53 graded findings were only 32 distinct locations. Counting every tool
# separately made the grade a measure of how many tools overlap on the repo.
_OVERLAP_GROUPS = (
    ("security", "semgrep", "secrets"),      # vulnerability / credential tools
    ("lint", "deadcode"),                    # unused-name reports (F401 vs vulture)
)
_SEV_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


def _dedupe_cross_tool(findings: list[dict]) -> list[dict]:
    """The findings that count toward the grade.

    Within one overlap group, a (file, line) that several tools flagged is
    counted through ONE tool: the one that rated it most severe (ties go to the
    earlier tool in the group). Everything that tool said about the line is
    kept, so two genuinely different bandit rules on one line both count.
    Findings with no line (graph analysis, dependencies) are never merged, and
    nothing is removed from the report itself — only from the arithmetic.
    """
    group_of = {sc: gi for gi, g in enumerate(_OVERLAP_GROUPS) for sc in g}
    best: dict[tuple, tuple[int, int, str]] = {}
    for f in findings:
        gi = group_of.get(f.get("scanner"))
        if gi is None or not f.get("line"):
            continue
        key = (gi, f.get("file", ""), f["line"])
        order = _OVERLAP_GROUPS[gi].index(f["scanner"])
        cand = (_SEV_RANK.get(f.get("severity", "info"), 0), -order, f["scanner"])
        if key not in best or cand > best[key]:
            best[key] = cand
    out = []
    for f in findings:
        gi = group_of.get(f.get("scanner"))
        if gi is None or not f.get("line"):
            out.append(f); continue
        if best[(gi, f.get("file", ""), f["line"])][2] == f["scanner"]:
            out.append(f)
    return out


def _count_severities(findings: list[dict], area: str | None = None) -> dict[str, int]:
    """Severity histogram, optionally restricted to one area of the repo."""
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for f in findings:
        if area is not None and f.get("area", "source") != area:
            continue
        k = f.get("severity", "info")
        if k in counts:
            counts[k] += 1
    return counts


def _source_loc(workspace: str) -> int:
    """Non-blank, non-comment lines of Python in the repo's own source files."""
    total = 0
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in VENDOR_DIR_NAMES and not d.startswith(".")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(root, fn), workspace)
            if _classify_area(rel) != "source":
                continue
            try:
                with open(os.path.join(root, fn), encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        t = line.strip()
                        if t and not t.startswith("#"):
                            total += 1
            except OSError:
                pass
    return total


def _compute_score(sev_counts: dict[str, int], source_loc: int = 0) -> tuple[int, str]:
    """
    CVSS-weighted exposure per 1000 lines of source, converted to 0-100 via
    exponential decay: score = 100 * e^(-density / K).

    Density, not a raw total. Findings scale with the amount of code, so an
    absolute threshold grades by size rather than by quality: measured,
    psf/requests and pallets/click both scored 0/F purely for being large,
    while a two-file repo scored 100 for having almost nothing to inspect.
    Dividing by KLOC asks the question that was actually intended — how much
    goes wrong per unit of code — and makes a small repo and a large one
    comparable.

    Exponential decay (rather than linear subtraction) keeps the scale useful
    at the bad end: a linear penalty clips at 0 and stops distinguishing "bad"
    from "much worse", whereas every finding here still moves the score, with
    diminishing marginal impact.
    """
    exposure = sum(CVSS_WEIGHTS[k] * v for k, v in sev_counts.items())
    # Floor the divisor: a repo with almost no source can't be meaningfully
    # rated, and dividing by a handful of lines would turn one finding into an
    # enormous density.
    kloc    = max(source_loc, MIN_LOC_FOR_DENSITY) / 1000.0
    density = exposure / kloc
    score   = round(100 * math.exp(-density / SCORE_DECAY_K))
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
        # bandit reports "./pkg/mod.py" while every other scanner reports
        # "pkg/mod.py"; one file must have one name in the report.
        return path[2:] if path.startswith("./") else path


def _json_from(out: str):
    """The first JSON document in a tool's output.

    _run() returns stdout and stderr concatenated, so any tool that prints its
    JSON on stdout and a summary on stderr breaks a plain json.loads() with
    "Extra data". pip-audit does exactly that ("Found 57 known vulnerabilities
    in 5 packages"), which made the dependency scanner report 0 findings for a
    repo with 57 of them.
    """
    dec = json.JSONDecoder()
    for i, ch in enumerate(out):
        if ch in "{[":
            try:
                return dec.raw_decode(out, i)[0]
            except ValueError:
                continue
    raise ValueError("no JSON document in tool output")


def _json_or_raise(out: str, tool: str):
    """Parse a scanner's JSON, or fail loudly.

    A scanner that cannot read its tool's output used to return [] and the
    report said "0 findings", indistinguishable from a clean repository. An
    exception surfaces as a scanner_error event instead.
    """
    if not out.strip():
        return None
    try:
        return _json_from(out)
    except ValueError:
        raise RuntimeError(f"{tool} produced unreadable output: "
                           f"{' '.join(out.split())[:200]}")


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

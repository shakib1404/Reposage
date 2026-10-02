"""architect.py — Architecture diagram generator (gitdiagram-style)

Pipeline:
  1. Fetch file tree + README from GitHub API
  2. LLM call 1 → architecture explanation  (<explanation>…</explanation>)
  3. LLM call 2 → JSON graph {groups, nodes, edges}
  4. Compile graph → Mermaid flowchart TD
  5. Yield SSE event dicts throughout
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from typing import AsyncGenerator

import httpx

from llm import chat as llm_chat

log = logging.getLogger("architect")

# ── GitHub helpers ─────────────────────────────────────────────────────────────

_EXCLUDED_DIRS = {
    "node_modules", "vendor", "venv", ".venv", "__pycache__",
    ".git", ".tox", ".eggs", "dist", "build", ".mypy_cache",
    "site-packages", ".pytest_cache", "htmlcov",
    ".cache", ".tmp", ".idea", ".vscode",
}
_EXCLUDED_EXTS = {
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".class",
    ".jpg", ".jpeg", ".png", ".gif", ".ico", ".svg",
    ".ttf", ".woff", ".woff2", ".webp", ".bmp",
    ".zip", ".tar", ".gz", ".rar", ".bin", ".exe",
}
_EXCLUDED_FILES = {
    "yarn.lock", "poetry.lock", "package-lock.json",
    "bun.lock", "uv.lock", "pnpm-lock.yaml",
}


def _should_include(path: str) -> bool:
    p = path.lower()
    parts = p.split("/")
    for part in parts[:-1]:
        if part in _EXCLUDED_DIRS:
            return False
    name = parts[-1]
    if name in _EXCLUDED_FILES:
        return False
    for ext in _EXCLUDED_EXTS:
        if name.endswith(ext):
            return False
    return True


def _gh_headers() -> dict:
    token = os.getenv("GITHUB_PAT", "") or os.getenv("GITHUB_TOKEN", "")
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _gh_get(client: httpx.AsyncClient, url: str) -> dict:
    resp = await client.get(url, headers=_gh_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


async def _fetch_github_data(repo_full_name: str) -> tuple[str, str, str]:
    """Returns (file_tree_str, readme_str, default_branch)."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        meta = await _gh_get(client, f"https://api.github.com/repos/{repo_full_name}")
        branch = meta.get("default_branch", "main")

        tree_data = await _gh_get(
            client,
            f"https://api.github.com/repos/{repo_full_name}/git/trees/{branch}?recursive=1",
        )
        paths = [
            item["path"]
            for item in (tree_data.get("tree") or [])
            if item.get("type") == "blob" and _should_include(item.get("path", ""))
        ]
        file_tree = "\n".join(paths)

        readme = ""
        try:
            rm = await _gh_get(
                client, f"https://api.github.com/repos/{repo_full_name}/readme"
            )
            if rm.get("encoding") == "base64" and rm.get("content"):
                readme = base64.b64decode(rm["content"]).decode("utf-8", errors="replace")
            else:
                readme = rm.get("content", "")
        except Exception:
            pass

    return file_tree[:80_000], readme[:8_000], branch


# ── LLM prompts ────────────────────────────────────────────────────────────────

_EXPLAIN_SYSTEM = """\
You are a principal software engineer analyzing a repository to explain its architecture.

You will receive:
- <file_tree>…</file_tree>
- <readme>…</readme>

Your job: explain the repository architecture concisely so another engineer can draw an accurate architecture diagram.

Requirements:
- Be concrete and repo-specific.
- Identify main subsystems, data flows, and important boundaries.
- Mention relevant technologies only when they materially affect the architecture.
- Keep it concise and high-signal. Prefer 8-16 short paragraphs over a long essay.
- Do not assume it is a web app — it could be any type of project.

Return only:
<explanation>
…
</explanation>"""

_GRAPH_SYSTEM = """\
You are a repository-to-graph planner.

You will receive:
- <explanation>…</explanation>
- <file_tree>…</file_tree>
- <repo>…</repo>

Your task: produce a JSON graph representation of the repository architecture.
Goal: a crisp, high-signal overview a human can understand quickly.

CRITICAL: output ONLY valid JSON — no markdown fences, no prose outside the JSON.

Use exactly this schema:
{
  "groups": [{"id": "snake_id", "label": "Short Label"}],
  "nodes": [
    {
      "id": "snake_id",
      "label": "Short Label",
      "type": "specific type",
      "groupId": "group_id_or_null",
      "path": "relative/file.py_or_null",
      "shape": "box|database|circle|hexagon|queue_or_null"
    }
  ],
  "edges": [
    {"from": "node_id", "to": "node_id", "label": "action_or_null", "style": "solid|dashed_or_null"}
  ]
}

Rules:
- Every field must be present; use null when not applicable.
- ids: lowercase letters, digits, underscores only; must start with a letter.
- labels: 1-4 words.
- Prefer 14-24 nodes, 0-8 groups, 10-34 edges.
- Use groups only for clear subsystems.
- Use repo-relative file paths ONLY when they exist in the provided file tree.
- Skip tests, tiny helpers, and config-only nodes unless architecturally central.
- For multi-service or pipeline repos, show internal stages of each major runtime.
- Output should feel like an opinionated architecture summary, not an inventory dump."""


_SEQUENCE_SYSTEM = """\
You are a repository-to-sequence-diagram planner.

You will receive:
- <explanation>…</explanation>
- <file_tree>…</file_tree>
- <repo>…</repo>

Your task: produce JSON describing the repository's MAIN runtime interaction —
the single most important end-to-end flow, from the moment something triggers
the system to the moment it returns a result. Order matters: this is a timeline,
not a structure diagram.

CRITICAL: output ONLY valid JSON — no markdown fences, no prose outside the JSON.

Use exactly this schema:
{
  "title": "Short name of the flow being traced",
  "participants": [
    {"id": "snake_id", "label": "Short Label", "kind": "actor|participant"}
  ],
  "messages": [
    {
      "from": "participant_id",
      "to": "participant_id",
      "text": "what is sent or called",
      "arrow": "sync|async|reply",
      "note": "optional aside shown above this step, or null"
    }
  ]
}

Rules:
- Every field must be present; use null when not applicable.
- ids: lowercase letters, digits, underscores only; must start with a letter.
- Use "actor" only for a human or an external system that starts the flow.
- labels: 1-3 words.
- Prefer 4-8 participants and 8-20 messages.
- "from" and "to" MUST be ids declared in participants.
- arrow: "sync" for a call, "reply" for a returned value, "async" for
  fire-and-forget (a queued job, an event, a background task).
- Every call that produces a result the caller uses should have a matching
  "reply" message, so the diagram reads as a real round trip.
- Trace ONE representative flow end to end. Do not merge several unrelated
  flows into one timeline.
- Use "note" sparingly — only where a step would otherwise be baffling."""

_DATAFLOW_SYSTEM = """\
You are a repository-to-dataflow-diagram planner.

You will receive:
- <explanation>…</explanation>
- <file_tree>…</file_tree>
- <repo>…</repo>

Your task: produce JSON describing how DATA moves through the repository.
This is a data-flow diagram, so every node is a place data rests or is
transformed, and every edge is named after the data travelling along it —
not after the function that is called.

CRITICAL: output ONLY valid JSON — no markdown fences, no prose outside the JSON.

Use exactly this schema:
{
  "nodes": [
    {
      "id": "snake_id",
      "label": "Short Label",
      "kind": "external|process|store",
      "path": "relative/file.py_or_null"
    }
  ],
  "flows": [
    {"from": "node_id", "to": "node_id", "data": "what travels, e.g. raw CSV rows"}
  ]
}

Rules:
- Every field must be present; use null when not applicable.
- ids: lowercase letters, digits, underscores only; must start with a letter.
- kind:
    "external" — a source or sink outside the system (user, GitHub API, S3)
    "process"  — code that transforms data
    "store"    — where data comes to rest (database, index, cache, file on disk)
- labels: 1-4 words. For a process, name the transformation ("Parse AST"),
  not the file.
- "data" labels are mandatory and must name the payload, not the verb.
  Good: "parsed tokens", "embedding vectors". Bad: "calls", "sends", "uses".
- Prefer 8-18 nodes and 10-28 flows.
- Include at least one "external" node and, where the repo has one, at
  least one "store".
- Use repo-relative file paths ONLY when they exist in the provided file tree.
- Skip pure control flow: if nothing travels along an edge, leave it out."""


def _extract_explanation(text: str) -> str:
    m = re.search(r"<explanation>(.*?)</explanation>", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def _extract_json(text: str) -> str:
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if m:
        return m.group(1).strip()
    # Try to find the first { … } block
    start = text.find("{")
    end   = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


# ── Mermaid compiler ───────────────────────────────────────────────────────────

_TONE_CLASSES = [
    "toneBlue", "toneAmber", "toneMint",
    "toneRose", "toneIndigo", "toneTeal",
]

_GENERIC_TYPES = {
    "app", "application", "component", "directory", "folder",
    "library", "module", "package", "project", "repo",
    "repository", "service", "system", "utility",
}

_MERMAID_STYLES = (
    "classDef toneNeutral fill:#1e293b,stroke:#475569,stroke-width:1.5px,color:#e2e8f0\n"
    "classDef toneBlue fill:#1e3a5f,stroke:#3b82f6,stroke-width:1.5px,color:#bfdbfe\n"
    "classDef toneAmber fill:#451a03,stroke:#f59e0b,stroke-width:1.5px,color:#fde68a\n"
    "classDef toneMint fill:#052e16,stroke:#22c55e,stroke-width:1.5px,color:#bbf7d0\n"
    "classDef toneRose fill:#4c0519,stroke:#f43f5e,stroke-width:1.5px,color:#fecdd3\n"
    "classDef toneIndigo fill:#1e1b4b,stroke:#818cf8,stroke-width:1.5px,color:#c7d2fe\n"
    "classDef toneTeal fill:#042f2e,stroke:#14b8a6,stroke-width:1.5px,color:#99f6e4"
)


def _esc(s: str) -> str:
    """Make model-written text safe inside a *quoted* Mermaid label.

    Quoted labels are `["…"]`, `("…")`, `[("…")]` and `-->|"…"|`, and Mermaid
    renders them as HTML. It has no backslash escapes, so the obvious
    `\\"` is wrong twice over: it ends the string early — killing the parse of
    the whole diagram when the rest of the label holds brackets — and where it
    does survive it draws as `Core \\Engine\\`. Each hazard below was checked
    against the mermaid 11.15.0 build the frontend ships:

      &   must go first, or it would re-escape the escapes added after it
      < > an unknown tag is swallowed whole: `List<int> parser` drew as
          `List parser`
      "   ends the quoted string
      #   `issue #42; fixed` drew as `issue * fixed` — Mermaid read `#42;`
          as a character entity
      \\n  a raw newline is dropped and jams the words together, so it becomes
          an explicit break (the compilers add their own `<br/>` outside this
          function, which must stay raw)
    """
    s = str(s or "")
    s = s.replace("&", "&amp;")
    s = s.replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace('"', "&quot;")
    s = s.replace("#", "&num;")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s.strip().replace("\n", "<br/>")


def _node_label(node: dict) -> str:
    parts = [_esc(node["label"])]
    ntype = (node.get("type") or "").strip()
    nlabel = node["label"].strip().lower()
    if ntype and ntype.lower() not in _GENERIC_TYPES and ntype.lower() != nlabel:
        parts.append(_esc(ntype))
    path = (node.get("path") or "").strip()
    if path and "." in path.split("/")[-1] and not path.endswith("/"):
        fname = path.split("/")[-1]
        if len(fname) <= 18:
            parts.append(f"[{_esc(fname)}]")
    return "<br/>".join(parts)


def _render_node(node: dict) -> str:
    label = _node_label(node)
    nid   = f"node_{node['id']}"
    shape = (node.get("shape") or "box").lower()
    if shape == "database":
        return f'{nid}[("{label}")]'
    if shape == "circle":
        return f'{nid}(("{label}"))'
    if shape == "hexagon":
        return f'{nid}{{{{"{label}"}}}}'
    return f'{nid}["{label}"]'


def _render_edge(edge: dict) -> str:
    connector = "-.->" if (edge.get("style") or "").lower() == "dashed" else "-->"
    frm = f"node_{edge['from']}"
    to  = f"node_{edge['to']}"
    lbl = (edge.get("label") or "").strip()
    if lbl:
        return f'{frm} {connector}|"{_esc(lbl)}"| {to}'
    return f"{frm} {connector} {to}"


def compile_mermaid(graph: dict, repo_full_name: str, branch: str) -> str:
    groups = graph.get("groups") or []
    nodes  = graph.get("nodes") or []
    edges  = graph.get("edges") or []

    group_order: dict[str, int] = {g["id"]: i for i, g in enumerate(groups)}
    class_map:   dict[str, list[str]] = {}
    grouped_ids: set[str] = set()
    lines = ["flowchart TD"]

    def _assign(node: dict):
        gid = node.get("groupId")
        if gid and gid in group_order:
            cls = _TONE_CLASSES[group_order[gid] % len(_TONE_CLASSES)]
        else:
            cls = "toneNeutral"
        class_map.setdefault(cls, []).append(node["id"])

    for g in groups:
        lines.append("")
        lines.append(f'subgraph group_{g["id"]}["{_esc(g["label"])}"]')
        for n in nodes:
            if n.get("groupId") == g["id"]:
                lines.append(f"  {_render_node(n)}")
                grouped_ids.add(n["id"])
                _assign(n)
        lines.append("end")

    ungrouped = [n for n in nodes if n["id"] not in grouped_ids]
    if ungrouped:
        lines.append("")
        for n in ungrouped:
            lines.append(_render_node(n))
            _assign(n)

    if edges:
        lines.append("")
        for e in edges:
            try:
                lines.append(_render_edge(e))
            except Exception:
                pass

    nodes_with_paths = [n for n in nodes if n.get("path")]
    if nodes_with_paths:
        lines.append("")
        for n in nodes_with_paths:
            path = n["path"]
            is_file = "." in path.split("/")[-1] and not path.endswith("/")
            ptype   = "blob" if is_file else "tree"
            url     = f"https://github.com/{repo_full_name}/{ptype}/{branch}/{path}"
            lines.append(f'click node_{n["id"]} "{url}" _blank')

    lines.append("")
    lines.append(_MERMAID_STYLES)

    for cls, nids in class_map.items():
        if nids:
            lines.append(f'class {",".join(f"node_{i}" for i in nids)} {cls}')

    return "\n".join(lines).strip()


# ── Sequence-diagram compiler ──────────────────────────────────────────────────

_ID_RE = re.compile(r"[^a-z0-9_]")

_SEQ_ARROWS = {"sync": "->>", "reply": "-->>", "async": "-)"}


def _safe_id(raw: str, prefix: str) -> str:
    """Mermaid ids are not quoted, so anything exotic breaks the parse."""
    s = _ID_RE.sub("_", str(raw or "").strip().lower())
    return f"{prefix}{s or 'x'}"


def _seq_text(s: str) -> str:
    """Sanitise text that sits *unquoted* inside a sequenceDiagram.

    Deliberately not _esc(): that escapes quotes and backslashes so they can
    live inside a quoted flowchart label, and in an unquoted sequence message
    the escapes render literally — `write \\"out.png\\"` appears on the diagram
    with its backslashes.

    ';' is Mermaid's statement separator. One inside a message, a participant
    label or a note truncates the statement mid-sentence and the *entire*
    diagram fails to parse, so the panel shows a parse error instead of a
    picture — `make(data); add_data(x)` is exactly the sort of thing the model
    writes. A comma reads naturally in its place; the `&#59;` entity is not an
    option because Mermaid renders it as `&;`.

    A literal newline ends the statement the same way, so whitespace collapses.

    ':' needs no handling at all: Mermaid takes everything after the first one
    as the text, so later colons stay literal. (All verified against the
    mermaid 11.15.0 build the frontend ships.)
    """
    return " ".join(str(s or "").replace(";", ",").split())


def compile_sequence(graph: dict, repo_full_name: str, branch: str) -> str:
    parts = graph.get("participants") or []
    msgs  = graph.get("messages") or []

    lines = ["sequenceDiagram", "    autonumber"]

    # Declared ids only: a message to an undeclared participant makes Mermaid
    # invent a lane with a raw id for a label, which looks like a bug.
    declared: dict[str, str] = {}
    for p in parts:
        pid = p.get("id")
        if not pid or pid in declared:
            continue
        mid = _safe_id(pid, "p_")
        declared[pid] = mid
        keyword = "actor" if (p.get("kind") or "").lower() == "actor" else "participant"
        lines.append(f'    {keyword} {mid} as {_seq_text(p.get("label") or pid)}')

    for m in msgs:
        frm, to = m.get("from"), m.get("to")
        if frm not in declared or to not in declared:
            continue
        arrow = _SEQ_ARROWS.get((m.get("arrow") or "sync").lower(), "->>")
        text  = _seq_text(m.get("text") or "")
        note  = _seq_text(m.get("note") or "")
        if note:
            # A self-call yields "Note over A,A", which Mermaid draws as an
            # oddly wide box; the single-participant form is what it wants.
            over = (declared[frm] if frm == to
                    else f"{declared[frm]},{declared[to]}")
            lines.append(f'    Note over {over}: {note}')
        lines.append(f"    {declared[frm]}{arrow}{declared[to]}: {text}")

    return "\n".join(lines).strip()


# ── Data-flow-diagram compiler ─────────────────────────────────────────────────

# Mermaid has no native DFD type, so a left-to-right flowchart carries it:
# shape encodes the node kind the way a hand-drawn DFD would.
_DFD_TONE = {"external": "toneAmber", "process": "toneBlue", "store": "toneMint"}


def compile_dataflow(graph: dict, repo_full_name: str, branch: str) -> str:
    nodes = graph.get("nodes") or []
    flows = graph.get("flows") or []

    lines = ["flowchart LR"]
    declared: dict[str, str] = {}
    class_map: dict[str, list[str]] = {}

    for n in nodes:
        nid = n.get("id")
        if not nid or nid in declared:
            continue
        mid = _safe_id(nid, "n_")
        declared[nid] = mid
        kind  = (n.get("kind") or "process").lower()
        label = _esc(n.get("label") or nid)
        path  = (n.get("path") or "").strip()
        if path and "." in path.split("/")[-1] and not path.endswith("/"):
            fname = path.split("/")[-1]
            if len(fname) <= 18:
                label += f"<br/>[{_esc(fname)}]"
        if kind == "store":
            lines.append(f'    {mid}[("{label}")]')
        elif kind == "external":
            lines.append(f'    {mid}["{label}"]')
        else:
            lines.append(f'    {mid}("{label}")')
        class_map.setdefault(_DFD_TONE.get(kind, "toneNeutral"), []).append(mid)

    if flows:
        lines.append("")
    for f in flows:
        frm, to = f.get("from"), f.get("to")
        if frm not in declared or to not in declared:
            continue
        data = _esc(f.get("data") or "").strip()
        if data:
            lines.append(f'    {declared[frm]} -->|"{data}"| {declared[to]}')
        else:
            lines.append(f"    {declared[frm]} --> {declared[to]}")

    linked = [n for n in nodes if n.get("path") and n.get("id") in declared]
    if linked:
        lines.append("")
        for n in linked:
            path    = n["path"]
            is_file = "." in path.split("/")[-1] and not path.endswith("/")
            ptype   = "blob" if is_file else "tree"
            url     = f"https://github.com/{repo_full_name}/{ptype}/{branch}/{path}"
            lines.append(f'    click {declared[n["id"]]} "{url}" _blank')

    lines.append("")
    lines.append(_MERMAID_STYLES)
    for cls, ids in class_map.items():
        if ids:
            lines.append(f'class {",".join(ids)} {cls}')

    return "\n".join(lines).strip()


# ── Diagram-kind registry ──────────────────────────────────────────────────────

def _ok_architecture(g: dict) -> bool:
    return isinstance(g.get("nodes"), list) and bool(g["nodes"])


def _ok_sequence(g: dict) -> bool:
    return (isinstance(g.get("participants"), list) and len(g["participants"]) >= 2
            and isinstance(g.get("messages"), list) and bool(g["messages"]))


def _ok_dataflow(g: dict) -> bool:
    return (isinstance(g.get("nodes"), list) and bool(g["nodes"])
            and isinstance(g.get("flows"), list) and bool(g["flows"]))


DIAGRAM_KINDS: dict[str, dict] = {
    "architecture": {
        "label":   "Architecture",
        "focus":   "the subsystems, their responsibilities and the boundaries between them",
        "system":  _GRAPH_SYSTEM,
        "compile": compile_mermaid,
        "valid":   _ok_architecture,
        "empty":   "No nodes in graph output",
    },
    "sequence": {
        "label":   "Sequence",
        "focus":   ("the single most important end-to-end runtime flow, in order: "
                    "what triggers it, which component hands off to which, and what "
                    "comes back"),
        "system":  _SEQUENCE_SYSTEM,
        "compile": compile_sequence,
        "valid":   _ok_sequence,
        "empty":   "Need at least two participants and one message",
    },
    "dataflow": {
        "label":   "Data flow",
        "focus":   ("how data moves: where it enters, every transformation it goes "
                    "through, where it is stored, and where it leaves"),
        "system":  _DATAFLOW_SYSTEM,
        "compile": compile_dataflow,
        "valid":   _ok_dataflow,
        "empty":   "Need at least one node and one flow",
    },
}

DEFAULT_KIND = "architecture"


# ── Main SSE generator ─────────────────────────────────────────────────────────

async def generate_architecture(
    repo_full_name: str,
    kind: str = DEFAULT_KIND,
) -> AsyncGenerator[dict, None]:
    """Yield SSE event dicts for diagram generation (architecture|sequence|dataflow)."""

    spec = DIAGRAM_KINDS.get(kind) or DIAGRAM_KINDS[DEFAULT_KIND]
    kind = kind if kind in DIAGRAM_KINDS else DEFAULT_KIND

    yield {"type": "status", "stage": "fetching", "kind": kind,
           "message": "Fetching repository file tree from GitHub…"}
    await asyncio.sleep(0)

    try:
        file_tree, readme, branch = await _fetch_github_data(repo_full_name)
    except httpx.HTTPStatusError as exc:
        yield {"type": "error",
               "message": f"GitHub API error ({exc.response.status_code}): {exc.response.text[:200]}"}
        return
    except Exception as exc:
        yield {"type": "error", "message": f"Failed to fetch GitHub data: {exc}"}
        return

    if not file_tree.strip():
        yield {"type": "error",
               "message": "Repository file tree is empty or inaccessible."}
        return

    file_tree_lines = len(file_tree.splitlines())
    yield {"type": "status", "stage": "fetching",
           "message": f"Got {file_tree_lines} files. Generating architecture explanation…"}
    await asyncio.sleep(0)

    metrics: dict = {}
    # One explanation prompt, pointed at what this diagram kind needs to show.
    # A sequence diagram drawn from a structural explanation comes out as a
    # list of components rather than a timeline.
    explain_system = (
        f"{_EXPLAIN_SYSTEM}\n\n"
        f"For this request, bias the explanation towards {spec['focus']}."
    )
    user_msg = (
        f"<file_tree>\n{file_tree}\n</file_tree>\n\n"
        f"<readme>\n{readme}\n</readme>"
    )

    yield {"type": "status", "stage": "explanation", "kind": kind,
           "message": f"Asking LLM to explain the {spec['label'].lower()}…"}
    await asyncio.sleep(0)

    try:
        # Reasoning model burns part of max_tokens on internal reasoning
        # before emitting content; 1500 risks an empty completion on a
        # large architecture. Cap, not target — headroom is free.
        expl_raw = await llm_chat(
            explain_system, user_msg, max_tokens=6000, metrics=metrics
        )
    except Exception as exc:
        yield {"type": "error", "message": f"Explanation LLM call failed: {exc}"}
        return

    explanation = _extract_explanation(expl_raw)
    yield {"type": "explanation", "text": explanation}
    await asyncio.sleep(0)

    yield {"type": "status", "stage": "graph", "kind": kind,
           "message": f"Building {spec['label'].lower()} graph from explanation…"}
    await asyncio.sleep(0)

    graph_user = (
        f"<explanation>\n{explanation}\n</explanation>\n\n"
        f"<file_tree>\n{file_tree}\n</file_tree>\n\n"
        f"<repo>\n{repo_full_name}\n</repo>"
    )

    graph: dict | None = None
    last_err = ""
    for attempt in range(1, 4):
        try:
            raw = await llm_chat(
                spec["system"], graph_user,
                max_tokens=4000, temperature=0.1, metrics=metrics,
            )
        except Exception as exc:
            yield {"type": "error", "message": f"Graph LLM call failed: {exc}"}
            return

        try:
            graph = json.loads(_extract_json(raw))
            if not spec["valid"](graph):
                raise ValueError(spec["empty"])
            break
        except Exception as exc:
            last_err = str(exc)
            graph = None
            if attempt < 3:
                yield {"type": "status", "stage": "graph_retry", "kind": kind,
                       "message": f"Graph parse failed (attempt {attempt}/3), retrying…"}
                await asyncio.sleep(0)

    if graph is None:
        yield {"type": "error",
               "message": f"Failed to build {spec['label'].lower()} graph "
                          f"after 3 attempts: {last_err}"}
        return

    yield {"type": "graph", "graph": graph, "kind": kind}
    await asyncio.sleep(0)

    yield {"type": "status", "stage": "compiling", "kind": kind,
           "message": "Compiling Mermaid diagram…"}
    await asyncio.sleep(0)

    try:
        mermaid = spec["compile"](graph, repo_full_name, branch)
    except Exception as exc:
        yield {"type": "error", "message": f"Mermaid compilation failed: {exc}"}
        return

    yield {
        "type":        "done",
        "kind":        kind,
        "kind_label":  spec["label"],
        "mermaid":     mermaid,
        "explanation": explanation,
        "graph":       graph,
        "branch":      branch,
        "tokens":      metrics.get("tokens", 0),
        # Which model actually answered. The key×model chain means a quota-ed
        # primary silently hands over to a smaller model, and a thinner diagram
        # should be attributable rather than mysterious.
        "model":       metrics.get("model") or "",
    }

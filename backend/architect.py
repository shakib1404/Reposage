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
    return s.replace("\\", "\\\\").replace('"', '\\"').strip()


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


# ── Main SSE generator ─────────────────────────────────────────────────────────

async def generate_architecture(repo_full_name: str) -> AsyncGenerator[dict, None]:
    """Yield SSE event dicts for architecture diagram generation."""

    yield {"type": "status", "stage": "fetching",
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
    user_msg = (
        f"<file_tree>\n{file_tree}\n</file_tree>\n\n"
        f"<readme>\n{readme}\n</readme>"
    )

    yield {"type": "status", "stage": "explanation",
           "message": "Asking LLM to explain the architecture…"}
    await asyncio.sleep(0)

    try:
        expl_raw = await llm_chat(
            _EXPLAIN_SYSTEM, user_msg, max_tokens=1500, metrics=metrics
        )
    except Exception as exc:
        yield {"type": "error", "message": f"Explanation LLM call failed: {exc}"}
        return

    explanation = _extract_explanation(expl_raw)
    yield {"type": "explanation", "text": explanation}
    await asyncio.sleep(0)

    yield {"type": "status", "stage": "graph",
           "message": "Building architecture graph from explanation…"}
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
                _GRAPH_SYSTEM, graph_user,
                max_tokens=4000, temperature=0.1, metrics=metrics,
            )
        except Exception as exc:
            yield {"type": "error", "message": f"Graph LLM call failed: {exc}"}
            return

        try:
            graph = json.loads(_extract_json(raw))
            if not isinstance(graph.get("nodes"), list) or not graph["nodes"]:
                raise ValueError("No nodes in graph output")
            break
        except Exception as exc:
            last_err = str(exc)
            if attempt < 3:
                yield {"type": "status", "stage": "graph_retry",
                       "message": f"Graph parse failed (attempt {attempt}/3), retrying…"}
                await asyncio.sleep(0)

    if graph is None:
        yield {"type": "error",
               "message": f"Failed to build architecture graph after 3 attempts: {last_err}"}
        return

    yield {"type": "graph", "graph": graph}
    await asyncio.sleep(0)

    yield {"type": "status", "stage": "compiling",
           "message": "Compiling Mermaid diagram…"}
    await asyncio.sleep(0)

    try:
        mermaid = compile_mermaid(graph, repo_full_name, branch)
    except Exception as exc:
        yield {"type": "error", "message": f"Mermaid compilation failed: {exc}"}
        return

    yield {
        "type":        "done",
        "mermaid":     mermaid,
        "explanation": explanation,
        "graph":       graph,
        "branch":      branch,
        "tokens":      metrics.get("tokens", 0),
    }

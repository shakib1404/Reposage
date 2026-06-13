"""chat.py — Codebase Q&A powered by Groq + RAG (LangChain/DeepLake)."""
from __future__ import annotations

import logging

from llm import chat as llm_chat

log = logging.getLogger("chat")


# ── System prompt builder ──────────────────────────────────────────────────────

def _build_system(repo_full_name: str, analysis: dict | None) -> str:
    parts: list[str] = [
        f"You are an expert software engineer who has deeply studied every file in the repository **{repo_full_name}**.",
        "When asked about functions, classes, or code behaviour, give specific, accurate answers.",
        "Reference exact function names, class names, file paths, and parameters whenever possible.",
        "If source code chunks are provided below, use them as primary evidence.",
        "",
    ]

    if not analysis:
        parts.append("No static analysis data available. Answer from the source code chunks above.")
        return "\n".join(parts)

    # README summary
    summary = (analysis.get("readme_summary") or "").strip()
    if summary:
        parts += ["## What this repo does", summary, ""]

    # Entry point & run command
    entry  = (analysis.get("entry_point")  or "").strip()
    runcmd = (analysis.get("run_command")  or "").strip()
    if entry or runcmd:
        parts.append("## Entry point & run command")
        if entry:  parts.append(f"- Entry point: `{entry}`")
        if runcmd: parts.append(f"- Run: `{runcmd}`")
        parts.append("")

    # Core components
    core = analysis.get("core_components") or []
    if core:
        parts += [f"## Core components (top {min(len(core), 10)})",
                  ", ".join(f"`{c}`" for c in core[:10]), ""]

    # Key files
    key_files = analysis.get("key_files") or []
    if key_files:
        parts += ["## Key files", "\n".join(f"- `{f}`" for f in key_files[:15]), ""]

    # Modules with their functions
    modules = analysis.get("modules") or []
    if modules:
        top_mods = sorted(modules, key=lambda m: m.get("score", 0), reverse=True)[:20]
        lines = ["## Modules — functions and classes defined inside each"]
        for m in top_mods:
            path     = m.get("path", m.get("name", ""))
            doc      = (m.get("docstring") or "").strip().split("\n")[0][:100]
            funcs    = m.get("functions") or []
            classes  = m.get("classes") or []
            score    = m.get("score")
            sc_str   = f" [score={score}]" if isinstance(score, (int, float)) else ""

            line = f"\n### `{path}`{sc_str}"
            if doc:
                line += f"\n  {doc}"
            if funcs:
                line += f"\n  Functions: {', '.join(f'`{f}()`' for f in funcs[:20])}"
            if classes:
                line += f"\n  Classes: {', '.join(f'`{c}`' for c in classes[:10])}"
            lines.append(line)
        parts += lines + [""]

    # Classes with all their methods
    classes = analysis.get("classes") or []
    if classes:
        lines = ["## Classes and their methods"]
        for c in classes[:30]:
            name    = c.get("name", "")
            module  = c.get("path") or c.get("module_short") or c.get("module", "")
            bases   = ", ".join(b for b in (c.get("bases") or []) if b and b != "object")
            methods = c.get("method_names") or []
            doc     = (c.get("docstring") or "").strip().split("\n")[0][:120]

            line = f"\n### `{name}`"
            if module: line += f" — in `{module}`"
            if bases:  line += f" (extends {bases})"
            if doc:    line += f"\n  {doc}"
            if methods:
                line += f"\n  Methods: {', '.join(f'`{m}()`' for m in methods)}"
            lines.append(line)
        parts += lines + [""]

    # Import cycles
    cycles = analysis.get("import_cycles") or []
    if cycles:
        parts += ["## Import cycles (circular dependencies)",
                  "\n".join(f"- {' → '.join(c)}" for c in cycles[:5]), ""]

    # Task plan
    task_plan = analysis.get("task_plan") or []
    if task_plan:
        parts += ["## Suggested task plan",
                  "\n".join(f"{i+1}. {s}" for i, s in enumerate(task_plan[:8])), ""]

    # Metrics
    metrics = analysis.get("metrics") or {}
    mlines = []
    for key, label in [
        ("total_modules",   "Total modules"),
        ("total_classes",   "Total classes"),
        ("total_functions", "Total functions"),
        ("total_methods",   "Total methods"),
        ("total_lines",     "Total lines"),
    ]:
        v = metrics.get(key)
        if v is not None:
            mlines.append(f"- {label}: {v}")
    if mlines:
        parts += ["## Codebase metrics"] + mlines + [""]

    parts += [
        "---",
        "When asked about a function, describe what it does, its parameters, what it returns, and which file it lives in.",
        "When asked about a class, explain its purpose, key methods, and relationships to other classes.",
        "If a question is about something not in this context, say so and offer the closest related information.",
    ]
    return "\n".join(parts)


# ── History formatter ──────────────────────────────────────────────────────────

def _format_history(history: list[dict]) -> str:
    lines: list[str] = []
    for msg in history[:-1]:
        role    = "User" if msg.get("role") == "user" else "Assistant"
        content = (msg.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


# ── Public entry point ─────────────────────────────────────────────────────────

async def answer_question(
    repo_full_name: str,
    messages: list[dict],
    analysis: dict | None = None,
) -> str:
    if not messages:
        return "No question provided."

    system   = _build_system(repo_full_name, analysis)
    history_str = _format_history(messages)
    current_q   = (messages[-1].get("content") or "").strip()

    # ── RAG: retrieve actual source code chunks ────────────────────────────────
    rag_context = ""
    try:
        from rag import retrieve, index_exists
        if index_exists(repo_full_name):
            chunks = await retrieve(repo_full_name, current_q, k=6)
            if chunks:
                lines = [f"[{c['source']}]\n```\n{c['text']}\n```" for c in chunks]
                rag_context = "\n\n".join(lines)
                log.info("RAG: injected %d chunks", len(chunks))
    except Exception as exc:
        log.warning("RAG retrieve skipped: %s", exc)

    # Build the user message — RAG chunks go first as primary evidence
    parts = []
    if rag_context:
        parts.append(
            f"Relevant source code retrieved from the repository:\n\n{rag_context}\n\n---"
        )
    if history_str:
        parts.append(f"Conversation so far:\n{history_str}")
    parts.append(f"Question: {current_q}")

    user_msg = "\n\n".join(parts)

    metrics: dict = {}
    answer = await llm_chat(
        system,
        user_msg,
        max_tokens=1800,
        temperature=0.15,
        metrics=metrics,
    )
    log.info("Chat answered (%d tokens)", metrics.get("tokens", 0))
    return answer.strip()

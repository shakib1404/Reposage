"""
llm.py — LLM helper (two Groq API keys)
Tries GROQ_API_KEY_1 first; falls back to GROQ_API_KEY_2 on 429 / any error.
Keys are read fresh from the environment on every call (no module-level cache).
"""
from __future__ import annotations

import logging
import os

import httpx

log   = logging.getLogger("llm")
URL   = "https://api.groq.com/openai/v1/chat/completions"


class TokenBudgetExceeded(RuntimeError):
    """The model spent max_tokens on reasoning without emitting any content.

    Distinct from a key/transport failure because it is deterministic: the
    budget is the problem, not the credentials, so retrying on another key
    fails identically and just burns quota.
    """


async def chat(
    system:      str,
    user:        str,
    max_tokens:  int,
    metrics:     dict | None = None,
    temperature: float = 0.15,
) -> str:
    # Read keys fresh every call so restarts / env changes take effect
    key1  = os.getenv("GROQ_API_KEY_1", "")
    key2  = os.getenv("GROQ_API_KEY_2", "")
    key3  = os.getenv("GROQ_API_KEY_3", "")
    key4  = os.getenv("GROQ_API_KEY_4", "")
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

    if not any([key1, key2, key3, key4]):
        raise RuntimeError(
            "No Groq API key found. Set GROQ_API_KEY_1 … _4 in .env"
        )

    # Try each key in order, stop on first success
    for label, key in [("key-1", key1), ("key-2", key2), ("key-3", key3), ("key-4", key4)]:
        if not key:
            continue
        try:
            return await _call(key, f"Groq-{label}", model, system, user,
                               max_tokens, temperature, metrics)
        except TokenBudgetExceeded:
            # Deterministic — every other key would fail the same way. Fail
            # fast with the actionable message instead of burning three more
            # keys (and three more round trips) to learn nothing.
            raise
        except Exception as exc:
            log.warning("Groq %s failed [%s: %s] — trying next key",
                        label, type(exc).__name__, exc)

    raise RuntimeError("All four Groq keys failed.")


async def _call(
    api_key:     str,
    provider:    str,
    model:       str,
    system:      str,
    user:        str,
    max_tokens:  int,
    temperature: float,
    metrics:     dict | None,
) -> str:
    payload = {
        "model":       model,
        "max_tokens":  max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    if metrics is not None:
        usage = data.get("usage") or {}
        metrics["calls"]  = metrics.get("calls",  0) + 1
        metrics["tokens"] = metrics.get("tokens", 0) + (
            usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        )

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"{provider} returned no choices: {data}")

    content       = choices[0].get("message", {}).get("content", "") or ""
    finish_reason = choices[0].get("finish_reason", "")

    # An empty completion must be treated as a failure, never returned as one.
    #
    # Reasoning models (openai/gpt-oss-120b) spend part of max_tokens on
    # internal reasoning before emitting any content. When the budget runs out
    # during that phase the API still returns 200 with a well-formed response —
    # just an empty `content` and finish_reason="length". Returning that
    # silently made every caller's own error path fire with a useless message
    # (analyzer: "No JSON found in: ", search: falls back to the raw query),
    # while this module logged a cheerful "LLM response from …" as if it had
    # worked. Worse, the caller above treats a return as success, so the other
    # three API keys were never tried.
    if not content.strip():
        if finish_reason == "length":
            raise TokenBudgetExceeded(
                f"{provider} hit the token limit before producing any content "
                f"(finish_reason=length, max_tokens={max_tokens}). "
                f"Raise max_tokens for this call."
            )
        raise RuntimeError(
            f"{provider} returned empty content (finish_reason={finish_reason!r})"
        )

    log.info("LLM response from %s (model=%s, finish=%s)",
             provider, model, finish_reason or "?")
    return content

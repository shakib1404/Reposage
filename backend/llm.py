"""
llm.py — LLM helper (four Groq API keys × a chain of models)

Every call walks keys 1-4 for the primary model; when all of them are out of
quota it moves to the next model and starts over. Groq meters tokens per model,
so a model that is out of daily tokens says nothing about the next one — that is
the whole reason the second dimension exists.

Keys and model list are read fresh from the environment on every call
(no module-level cache), so a restart or an .env edit takes effect immediately.
"""
from __future__ import annotations

import logging
import os
import re

import httpx

log   = logging.getLogger("llm")
URL   = "https://api.groq.com/openai/v1/chat/completions"

# Fallback chain, best first. Every id here was verified against this account's
# /v1/models — most of Groq's catalogue (llama-3.x, kimi, qwen3-32b) 404s on the
# free tier, so do not extend this list without probing first.
#
# Ordered by how well each one holds a JSON schema on a real repo-graph prompt:
# 120b 14 messages / 8 reply arrows, 20b 13/7, qwen 9/3, safeguard-20b 9/3.
# allam-2-7b is available too but produced 0 reply arrows, so it is left out.
DEFAULT_MODELS = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-safeguard-20b",
)

# A 400/404 that is about the model id itself rather than our payload.
_MODEL_GONE = re.compile(r"decommission|does not exist|do not have access", re.I)


class TokenBudgetExceeded(RuntimeError):
    """The model spent max_tokens on reasoning without emitting any content.

    Deterministic for a given model, so the key loop is skipped — but *not*
    deterministic across models: the fallbacks reason more cheaply (475-1236
    tokens vs 1728 on the 120b for the same prompt), so a smaller model may well
    fit inside a budget the big one blew. The chain therefore moves on, and this
    is only raised to the caller if every model runs out of room.
    """


class _ModelUnusable(RuntimeError):
    """Groq rejects this model id outright — every key gets the same answer."""


class _BadRequest(RuntimeError):
    """Our own payload is malformed; no key and no model will accept it."""


def model_chain() -> list[str]:
    """The models to try, in order.

    `GROQ_MODELS` (comma-separated) replaces the chain outright. Otherwise the
    built-in chain is used, with `GROQ_MODEL` — the long-standing single-model
    setting — promoted to the front so existing .env files keep their primary
    and simply gain fallbacks behind it.
    """
    raw = os.getenv("GROQ_MODELS", "").strip()
    if raw:
        chain = [m.strip() for m in raw.split(",") if m.strip()]
    else:
        chain = list(DEFAULT_MODELS)
        primary = os.getenv("GROQ_MODEL", "").strip()
        if primary:
            if primary in chain:
                chain.remove(primary)
            chain.insert(0, primary)

    seen: set[str] = set()
    return [m for m in chain if not (m in seen or seen.add(m))]


async def chat(
    system:      str,
    user:        str,
    max_tokens:  int,
    metrics:     dict | None = None,
    temperature: float = 0.15,
) -> str:
    # Read keys fresh every call so restarts / env changes take effect
    keys = [
        (f"key-{i}", os.getenv(f"GROQ_API_KEY_{i}", ""))
        for i in range(1, 5)
    ]
    keys = [(label, key) for label, key in keys if key]

    if not keys:
        raise RuntimeError(
            "No Groq API key found. Set GROQ_API_KEY_1 … _4 in .env"
        )

    models   = model_chain()
    failures: list[str] = []
    budget_exc: TokenBudgetExceeded | None = None

    for model in models:
        for label, key in keys:
            try:
                return await _call(key, f"Groq-{label}", model, system, user,
                                   max_tokens, temperature, metrics)
            except _BadRequest:
                # Our payload, not the quota. Every key and every model rejects
                # it identically, so surface the real message immediately
                # instead of hiding it behind 15 more round trips.
                raise
            except TokenBudgetExceeded as exc:
                budget_exc = exc
                log.warning("Groq %s on %s ran out of budget — trying next model",
                            label, model)
                break                      # other keys behave identically
            except _ModelUnusable as exc:
                failures.append(f"{model}: {exc}")
                log.warning("Groq model %s unusable (%s) — trying next model",
                            model, exc)
                break                      # other keys behave identically
            except Exception as exc:
                failures.append(f"{model}/{label}: {type(exc).__name__}: {exc}")
                log.warning("Groq %s on %s failed [%s: %s] — trying next",
                            label, model, type(exc).__name__, exc)

    if budget_exc is not None:
        # Keep the actionable "raise max_tokens" message rather than burying it
        # in a generic exhaustion error.
        raise budget_exc

    raise RuntimeError(
        f"All {len(keys)} Groq keys failed on all {len(models)} models "
        f"({', '.join(models)}). Last errors: {'; '.join(failures[-4:])}"
    )


def _classify(resp: httpx.Response, provider: str, model: str) -> Exception:
    """Turn an HTTP error into the exception that tells the chain what to do."""
    try:
        detail = (resp.json().get("error") or {}).get("message") or resp.text
    except Exception:
        detail = resp.text
    detail = (detail or "").strip()[:300]

    if resp.status_code in (400, 404) and _MODEL_GONE.search(detail):
        return _ModelUnusable(detail or f"HTTP {resp.status_code}")
    if resp.status_code == 400:
        return _BadRequest(f"{provider} rejected the request on {model}: {detail}")
    return RuntimeError(f"{provider} HTTP {resp.status_code} on {model}: {detail}")


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
        if resp.status_code >= 400:
            raise _classify(resp, provider, model)
        data = resp.json()

    if metrics is not None:
        usage = data.get("usage") or {}
        metrics["calls"]  = metrics.get("calls",  0) + 1
        metrics["tokens"] = metrics.get("tokens", 0) + (
            usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        )
        # Which model actually answered — the chain means it is not always the
        # primary, and a caller showing "powered by X" must not guess.
        metrics["model"] = model

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

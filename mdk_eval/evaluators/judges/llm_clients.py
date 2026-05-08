"""Thin async wrappers around OpenAI and Anthropic clients.

We only need a "single chat call returning JSON" surface, so we don't pull DeepEval's
client layer into our judges directly — keeps judges portable and testable.

Caching: every call goes through `cache.get` / `cache.put` first. A cache hit
returns immediately without an API call. See `cache.py` for storage details and
the `MDK_EVAL_CACHE_DISABLE` escape hatch.
"""
from __future__ import annotations

import json
import os
from typing import Any

from . import cache


class LLMClientError(RuntimeError):
    pass


async def call_judge(
    provider: str, model: str, system: str, user: str,
    temperature: float = 0.0,
    *,
    max_tokens: int = 1024,
) -> dict[str, Any]:
    """Returns parsed JSON dict. Raises LLMClientError on failure.

    Cached transparently — the same (provider, model, system, user, temperature)
    tuple returns the same response without re-calling the API.

    `max_tokens` (default 1024) caps the LLM's output. Tune higher for callers
    that produce long structured responses (Agent Doctor uses 4096; topic
    extraction at 2048). Cache key includes max_tokens so different limits
    don't collide. Token cost scales linearly with this value.

    Emits a structured event per call so Application Insights / Log Analytics
    can show judge throughput + cache hit rate. Wraps the underlying provider
    call in a Langfuse generation span when Langfuse is configured.
    """
    import time as _time
    cached = cache.get(provider, model, system, user, temperature)
    if cached is not None:
        try:
            from ...web.observability import emit_event as _emit
            _emit("judge.cache_hit", provider=provider, model=model)
        except ImportError:
            pass
        return cached

    # Lazy-import so this module stays usable from CLI contexts without
    # the web extra installed.
    try:
        from ...web.observability import emit_event as _emit, langfuse_client
    except ImportError:
        _emit = lambda *a, **k: None  # noqa: E731
        langfuse_client = lambda: None  # noqa: E731

    lf = langfuse_client() if langfuse_client else None
    p = provider.lower()
    t0 = _time.perf_counter()
    span = lf.generation(
        name=f"judge.{p}.{model}",
        model=model,
        input={"system": system[:4000], "user": user[:4000]},
        metadata={"temperature": temperature, "kind": "judge"},
    ) if lf else None
    try:
        if p == "openai":
            result = await _call_openai(model, system, user, temperature, max_tokens=max_tokens)
        elif p == "anthropic":
            result = await _call_anthropic(model, system, user, temperature, max_tokens=max_tokens)
        else:
            raise LLMClientError(f"unknown provider: {provider}")
        if span:
            try:
                span.end(output=result)
            except Exception:
                pass
        _emit("judge.call_completed",
              provider=provider, model=model,
              latency_ms=round((_time.perf_counter() - t0) * 1000, 2),
              cache_hit=False)
    except Exception as e:
        if span:
            try:
                span.end(level="ERROR", status_message=f"{type(e).__name__}: {e}")
            except Exception:
                pass
        _emit("judge.call_failed",
              provider=provider, model=model,
              error=f"{type(e).__name__}: {e}",
              latency_ms=round((_time.perf_counter() - t0) * 1000, 2))
        raise

    cache.put(provider, model, system, user, temperature, result)
    return result


async def _call_openai(
    model: str, system: str, user: str, temperature: float, *, max_tokens: int = 1024,
) -> dict[str, Any]:
    if not os.getenv("OPENAI_API_KEY"):
        raise LLMClientError("OPENAI_API_KEY not set")
    try:
        from openai import AsyncOpenAI  # type: ignore
    except ImportError as e:
        raise LLMClientError("openai package not installed (pip install '.[judges]')") from e

    client = AsyncOpenAI()
    resp = await client.chat.completions.create(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system + "\nRespond with a single JSON object. No prose."},
            {"role": "user", "content": user},
        ],
    )
    content = resp.choices[0].message.content or "{}"
    return _safe_json(content)


async def _call_anthropic(
    model: str, system: str, user: str, temperature: float, *, max_tokens: int = 1024,
) -> dict[str, Any]:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise LLMClientError("ANTHROPIC_API_KEY not set")
    try:
        from anthropic import AsyncAnthropic  # type: ignore
    except ImportError as e:
        raise LLMClientError("anthropic package not installed (pip install '.[judges]')") from e

    client = AsyncAnthropic()
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system + "\nRespond with a single JSON object. No prose, no markdown fences.",
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return _safe_json(text)


def _safe_json(s: str) -> dict[str, Any]:
    s = s.strip()
    if s.startswith("```"):
        # strip code fence
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    # find outermost braces
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        s = s[start : end + 1]
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise LLMClientError(f"judge did not return valid JSON: {e}\nraw={s[:400]}")

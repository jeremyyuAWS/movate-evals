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


async def call_judge(provider: str, model: str, system: str, user: str, temperature: float = 0.0) -> dict[str, Any]:
    """Returns parsed JSON dict. Raises LLMClientError on failure.

    Cached transparently — the same (provider, model, system, user, temperature)
    tuple returns the same response without re-calling the API.
    """
    cached = cache.get(provider, model, system, user, temperature)
    if cached is not None:
        return cached

    p = provider.lower()
    if p == "openai":
        result = await _call_openai(model, system, user, temperature)
    elif p == "anthropic":
        result = await _call_anthropic(model, system, user, temperature)
    else:
        raise LLMClientError(f"unknown provider: {provider}")

    cache.put(provider, model, system, user, temperature, result)
    return result


async def _call_openai(model: str, system: str, user: str, temperature: float) -> dict[str, Any]:
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
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system + "\nRespond with a single JSON object. No prose."},
            {"role": "user", "content": user},
        ],
    )
    content = resp.choices[0].message.content or "{}"
    return _safe_json(content)


async def _call_anthropic(model: str, system: str, user: str, temperature: float) -> dict[str, Any]:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise LLMClientError("ANTHROPIC_API_KEY not set")
    try:
        from anthropic import AsyncAnthropic  # type: ignore
    except ImportError as e:
        raise LLMClientError("anthropic package not installed (pip install '.[judges]')") from e

    client = AsyncAnthropic()
    resp = await client.messages.create(
        model=model,
        max_tokens=1024,
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

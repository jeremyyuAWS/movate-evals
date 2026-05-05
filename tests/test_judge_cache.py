"""Judge-response cache.

Verifies:
- get() on a fresh cache returns None.
- put() then get() with same key returns the same dict.
- Different temperatures or prompts produce different cache keys.
- MDK_EVAL_CACHE_DISABLE=1 short-circuits both get() and put().
- stats() reports realistic counts.
- clear() removes all entries.

The cache module reads MDK_EVAL_CACHE_DIR at call time, not import time —
each test points it at a unique tmp dir to keep tests hermetic.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_cache(tmp_path: Path, monkeypatch):
    """Point the judge cache at a fresh tmp dir for each test."""
    monkeypatch.setenv("MDK_EVAL_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("MDK_EVAL_CACHE_DISABLE", raising=False)
    # Re-import to pick up env var (module reads it lazily on each call).
    from mdk_eval.evaluators.judges import cache
    return cache


def test_get_empty_returns_none(tmp_cache):
    assert tmp_cache.get("openai", "gpt-4o", "sys", "user", 0.0) is None


def test_put_then_get_roundtrip(tmp_cache):
    payload = {"score": 0.85, "rationale": "looks good"}
    tmp_cache.put("openai", "gpt-4o", "sys", "user", 0.0, payload)
    got = tmp_cache.get("openai", "gpt-4o", "sys", "user", 0.0)
    assert got == payload


def test_different_temperatures_are_different_keys(tmp_cache):
    tmp_cache.put("openai", "gpt-4o", "sys", "user", 0.0, {"v": "a"})
    tmp_cache.put("openai", "gpt-4o", "sys", "user", 0.5, {"v": "b"})
    assert tmp_cache.get("openai", "gpt-4o", "sys", "user", 0.0) == {"v": "a"}
    assert tmp_cache.get("openai", "gpt-4o", "sys", "user", 0.5) == {"v": "b"}


def test_different_user_prompts_are_different_keys(tmp_cache):
    tmp_cache.put("openai", "gpt-4o", "sys", "userA", 0.0, {"v": "a"})
    tmp_cache.put("openai", "gpt-4o", "sys", "userB", 0.0, {"v": "b"})
    assert tmp_cache.get("openai", "gpt-4o", "sys", "userA", 0.0) == {"v": "a"}
    assert tmp_cache.get("openai", "gpt-4o", "sys", "userB", 0.0) == {"v": "b"}


def test_different_system_prompts_are_different_keys(tmp_cache):
    """Methodology-version bumps change the system prompt SHA — natural invalidation."""
    tmp_cache.put("openai", "gpt-4o", "sysA", "user", 0.0, {"v": "a"})
    tmp_cache.put("openai", "gpt-4o", "sysB", "user", 0.0, {"v": "b"})
    assert tmp_cache.get("openai", "gpt-4o", "sysA", "user", 0.0) == {"v": "a"}
    assert tmp_cache.get("openai", "gpt-4o", "sysB", "user", 0.0) == {"v": "b"}


def test_disabled_cache_short_circuits(tmp_cache, monkeypatch):
    """When disabled, put() is a no-op and get() returns None even on prior writes."""
    tmp_cache.put("openai", "gpt-4o", "sys", "user", 0.0, {"v": "first"})
    monkeypatch.setenv("MDK_EVAL_CACHE_DISABLE", "1")
    assert tmp_cache.get("openai", "gpt-4o", "sys", "user", 0.0) is None
    tmp_cache.put("openai", "gpt-4o", "sys", "user", 0.0, {"v": "second"})  # no-op
    monkeypatch.setenv("MDK_EVAL_CACHE_DISABLE", "0")
    # First write still present; the disabled put didn't override.
    assert tmp_cache.get("openai", "gpt-4o", "sys", "user", 0.0) == {"v": "first"}


def test_stats_reports_entries_and_hits(tmp_cache):
    tmp_cache.put("openai", "gpt-4o", "s", "u", 0.0, {"v": 1})
    tmp_cache.put("anthropic", "claude-3", "s", "u", 0.0, {"v": 2})
    # warm hits
    tmp_cache.get("openai", "gpt-4o", "s", "u", 0.0)
    tmp_cache.get("openai", "gpt-4o", "s", "u", 0.0)

    s = tmp_cache.stats()
    assert s["entries"] == 2
    assert s["total_hits"] >= 2
    assert s["by_provider"] == {"openai": 1, "anthropic": 1}


def test_clear_removes_all(tmp_cache):
    tmp_cache.put("openai", "gpt-4o", "s", "u", 0.0, {"v": 1})
    tmp_cache.put("openai", "gpt-4o", "s2", "u", 0.0, {"v": 2})
    n = tmp_cache.clear()
    assert n == 2
    assert tmp_cache.stats()["entries"] == 0


def test_call_judge_uses_cache_on_repeat(tmp_cache, monkeypatch):
    """End-to-end: call_judge with a primed cache should not call the API."""
    import asyncio

    primed = {"score": 0.9, "rationale": "cached"}
    tmp_cache.put("openai", "gpt-4o", "sys", "user", 0.0, primed)

    # If the cache is consulted, _call_openai is never invoked. Make sure it would
    # blow up if it WERE called, so a regression here surfaces immediately.
    from mdk_eval.evaluators.judges import llm_clients

    async def boom(*_a, **_k):
        raise AssertionError("API call made despite cache hit")

    monkeypatch.setattr(llm_clients, "_call_openai", boom)

    result = asyncio.run(llm_clients.call_judge("openai", "gpt-4o", "sys", "user", 0.0))
    assert result == primed

"""Lyzr adapter — session-id rotation regression tests.

Background: an earlier version of LyzrAdapter generated one `session_id` at
__init__ and reused it for every scenario in a multi-scenario eval. Lyzr's
backend treated those calls as one continuous conversation, leaking turn N's
state into turn N+1 and silently corrupting eval results.

These tests pin the fix:
- When no session_id is configured, every call gets a fresh one.
- Per-scenario `session_id` in the input dict overrides everything.
- An explicit constructor session_id is honored when no scenario override
  is provided (the rare "I really do want shared state" case).
"""
from __future__ import annotations

import asyncio

import httpx

from mdk_eval.adapters.lyzr import LyzrAdapter


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Records every request body without making a network call."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        import json

        self.bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"response": "ok"})


def _adapter_with_capture(session_id: str | None = None) -> tuple[LyzrAdapter, _CapturingTransport]:
    transport = _CapturingTransport()
    adapter = LyzrAdapter(agent_id="agent-x", session_id=session_id)
    # swap in the capturing transport
    adapter._client = httpx.AsyncClient(transport=transport)
    return adapter, transport


def test_default_rotates_session_id_per_call():
    """Two consecutive calls with no override must produce two different session_ids."""
    adapter, transport = _adapter_with_capture()
    asyncio.run(adapter.run({"prompt": "first"}))
    asyncio.run(adapter.run({"prompt": "second"}))
    asyncio.run(adapter.aclose())

    assert len(transport.bodies) == 2
    s1 = transport.bodies[0]["session_id"]
    s2 = transport.bodies[1]["session_id"]
    assert s1 and s2
    assert s1 != s2, (
        f"session_id leaked across calls: both calls used {s1!r}. "
        "Multi-scenario evals depend on session isolation."
    )


def test_per_scenario_session_id_overrides():
    """If a scenario provides its own session_id (e.g. for a multi-turn flow),
    use exactly that one — same value every call within the scenario."""
    adapter, transport = _adapter_with_capture()
    asyncio.run(adapter.run({"prompt": "turn 1", "session_id": "pinned-abc"}))
    asyncio.run(adapter.run({"prompt": "turn 2", "session_id": "pinned-abc"}))
    asyncio.run(adapter.aclose())

    assert transport.bodies[0]["session_id"] == "pinned-abc"
    assert transport.bodies[1]["session_id"] == "pinned-abc"


def test_constructor_session_id_pins_when_no_scenario_override():
    """Explicit constructor session_id is honored across calls (escape hatch)."""
    adapter, transport = _adapter_with_capture(session_id="ctor-pinned")
    asyncio.run(adapter.run({"prompt": "first"}))
    asyncio.run(adapter.run({"prompt": "second"}))
    asyncio.run(adapter.aclose())

    assert transport.bodies[0]["session_id"] == "ctor-pinned"
    assert transport.bodies[1]["session_id"] == "ctor-pinned"


def test_per_scenario_overrides_constructor_session_id():
    """Per-scenario session_id wins over constructor pin."""
    adapter, transport = _adapter_with_capture(session_id="ctor-default")
    asyncio.run(adapter.run({"prompt": "x", "session_id": "scenario-x"}))
    asyncio.run(adapter.run({"prompt": "y"}))  # no override; falls back to ctor pin
    asyncio.run(adapter.aclose())

    assert transport.bodies[0]["session_id"] == "scenario-x"
    assert transport.bodies[1]["session_id"] == "ctor-default"

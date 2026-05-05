"""Multi-turn scenario support across adapters.

Verifies:
- `extract_turns` normalizes the three input shapes correctly.
- Mock adapter records all turns in trace.extra and uses the last as the prompt.
- Lyzr adapter sends one HTTP POST per turn with a SHARED session_id within
  the scenario, but a FRESH session_id between scenarios (preserves the
  earlier session-leak fix).
- OpenAI-compat adapter packs all turns into one request as separate user
  messages.
- Single-turn shorthand (`{"prompt": "..."}`) still works for all of the above.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from mdk_eval.adapters.base import extract_turns, is_multi_turn
from mdk_eval.adapters.lyzr import LyzrAdapter
from mdk_eval.adapters.mock import MockAdapter
from mdk_eval.adapters.openai_compat import OpenAICompatAdapter


# ---------- extract_turns helper ----------

def test_extract_turns_explicit_list():
    assert extract_turns({"turns": ["a", "b", "c"]}) == ["a", "b", "c"]


def test_extract_turns_single_prompt():
    assert extract_turns({"prompt": "hello"}) == ["hello"]


def test_extract_turns_legacy_input():
    assert extract_turns({"input": "hi"}) == ["hi"]


def test_extract_turns_empty():
    assert extract_turns({}) == [""]


def test_extract_turns_turns_wins_over_prompt():
    assert extract_turns({"turns": ["a"], "prompt": "ignored"}) == ["a"]


def test_is_multi_turn_only_true_for_multi():
    assert not is_multi_turn({})
    assert not is_multi_turn({"prompt": "x"})
    assert not is_multi_turn({"turns": ["only one"]})
    assert is_multi_turn({"turns": ["one", "two"]})


# ---------- Mock adapter ----------

def test_mock_adapter_handles_multi_turn():
    adapter = MockAdapter(seed=1)
    result = asyncio.run(adapter.run({
        "turns": ["I want to return something", "Order is SD-77432"],
        "context": ["KB note"],
    }))
    assert result.ok
    # The output should be a response to the LAST turn (the order number),
    # since assertions run against the agent's final response.
    assert result.trace.extra.get("is_multi_turn") is True
    turn_log = result.trace.extra.get("turns")
    assert len(turn_log) == 2
    assert turn_log[0]["user_message"] == "I want to return something"
    assert turn_log[1]["user_message"] == "Order is SD-77432"


def test_mock_adapter_single_turn_unchanged():
    adapter = MockAdapter(seed=1)
    result = asyncio.run(adapter.run({"prompt": "just one message"}))
    assert result.ok
    # No is_multi_turn marker for single-turn (backward compat with existing reports)
    assert not result.trace.extra.get("is_multi_turn", False)


# ---------- Lyzr adapter ----------

class _CapturingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"response": f"reply-to-{self.requests[-1]['message']}"})


def _lyzr_with_capture():
    transport = _CapturingTransport()
    adapter = LyzrAdapter(agent_id="agent-x")
    adapter._client = httpx.AsyncClient(transport=transport)
    return adapter, transport


def test_lyzr_multi_turn_one_post_per_turn_shared_session():
    adapter, transport = _lyzr_with_capture()
    asyncio.run(adapter.run({"turns": ["turn 1", "turn 2", "turn 3"]}))
    asyncio.run(adapter.aclose())

    # 3 POSTs for the 3 turns
    assert len(transport.requests) == 3
    # All within ONE scenario must share the same session_id
    sessions = {req["session_id"] for req in transport.requests}
    assert len(sessions) == 1, f"expected one shared session, got {sessions}"
    # Messages preserved in order
    assert [r["message"] for r in transport.requests] == ["turn 1", "turn 2", "turn 3"]


def test_lyzr_multi_turn_session_still_isolated_across_scenarios():
    """Multi-turn shares session WITHIN a scenario, but each scenario.run()
    call still gets its OWN session — the earlier session-leak fix holds."""
    adapter, transport = _lyzr_with_capture()
    asyncio.run(adapter.run({"turns": ["scenario A turn 1", "scenario A turn 2"]}))
    asyncio.run(adapter.run({"turns": ["scenario B turn 1", "scenario B turn 2"]}))
    asyncio.run(adapter.aclose())

    a_sessions = {transport.requests[0]["session_id"], transport.requests[1]["session_id"]}
    b_sessions = {transport.requests[2]["session_id"], transport.requests[3]["session_id"]}
    assert len(a_sessions) == 1                 # within-scenario consistency
    assert len(b_sessions) == 1                 # within-scenario consistency
    assert a_sessions != b_sessions             # cross-scenario isolation


def test_lyzr_multi_turn_records_per_turn_log():
    adapter, transport = _lyzr_with_capture()
    result = asyncio.run(adapter.run({"turns": ["hi", "second"]}))
    asyncio.run(adapter.aclose())

    log = result.trace.extra["turns"]
    assert [t["user_message"] for t in log] == ["hi", "second"]
    assert all("agent_response" in t and "latency_ms" in t for t in log)
    # output_text reflects the LAST turn's response
    assert "second" in result.output_text


def test_lyzr_single_turn_still_works():
    adapter, transport = _lyzr_with_capture()
    asyncio.run(adapter.run({"prompt": "single message"}))
    asyncio.run(adapter.aclose())

    assert len(transport.requests) == 1
    assert transport.requests[0]["message"] == "single message"


# ---------- OpenAI-compat adapter ----------

class _OAICapturingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "stub answer", "tool_calls": []}}],
        })


def test_openai_compat_multi_turn_packs_messages_in_one_request():
    transport = _OAICapturingTransport()
    adapter = OpenAICompatAdapter(endpoint="https://api.example.com/v1", model="gpt-4o-mini")
    adapter._client = httpx.AsyncClient(transport=transport)
    asyncio.run(adapter.run({"turns": ["hello", "follow-up", "third"]}))
    asyncio.run(adapter.aclose())

    # One HTTP call regardless of turn count (OpenAI takes the whole conversation
    # in one request — this is the native pattern, not multiple round-trips).
    assert len(transport.requests) == 1
    msgs = transport.requests[0]["messages"]
    user_messages = [m["content"] for m in msgs if m["role"] == "user"]
    assert user_messages == ["hello", "follow-up", "third"]


def test_openai_compat_with_system_prompt_and_multi_turn():
    transport = _OAICapturingTransport()
    adapter = OpenAICompatAdapter(
        endpoint="https://api.example.com/v1",
        model="gpt-4o-mini",
        system_prompt="You are helpful.",
    )
    adapter._client = httpx.AsyncClient(transport=transport)
    asyncio.run(adapter.run({"turns": ["a", "b"]}))
    asyncio.run(adapter.aclose())

    msgs = transport.requests[0]["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "You are helpful."
    assert [m["content"] for m in msgs[1:]] == ["a", "b"]

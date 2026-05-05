"""Deterministic-with-jitter mock adapter. No network. Used for self-tests + CI."""
from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timezone
from typing import Any

from ..models import AdapterResult, SubAgentCall, ToolCall, Trace
from .base import AgentAdapter, extract_turns, is_multi_turn


class MockAdapter(AgentAdapter):
    """Behaviour:
    - if input contains "trigger": "hallucinate" -> emits a confident wrong fact
    - if input contains "trigger": "tool_skip" -> answers without using expected tool
    - if input contains "trigger": "slow" -> sleeps to blow latency budget
    - if input contains "trigger": "schema_break" -> emits malformed JSON
    - else: emits a plausible templated answer using context, calls expected tools
    """

    name = "mock"

    def __init__(self, seed: int | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.rng = random.Random(seed)

    async def run(self, scenario_input: dict[str, Any]) -> AdapterResult:
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        trigger = (scenario_input.get("trigger") or "").lower()
        # Multi-turn: mock takes the LAST turn as the prompt to respond to
        # (the same convention the orchestrator uses for evaluation — assertions
        # run against the agent's final response). Earlier turns are recorded in
        # trace.extra so tests can verify the adapter saw all of them.
        all_turns = extract_turns(scenario_input)
        prompt = all_turns[-1]
        context = scenario_input.get("context") or []
        expected_tools = scenario_input.get("_expected_tools") or []
        expected_path = scenario_input.get("_expected_workflow") or []

        # base latency 80-300ms
        await asyncio.sleep(self.rng.uniform(0.08, 0.30))

        tool_calls: list[ToolCall] = []
        sub: list[SubAgentCall] = []
        workflow_path: list[str] = []
        retries = 0
        ok = True
        error: str | None = None
        output_text = ""
        output_json: dict[str, Any] | None = None

        # workflow path: visit "intent_classifier" -> tool? -> "responder"
        workflow_path.append("intent_classifier")

        if trigger == "slow":
            await asyncio.sleep(self.rng.uniform(2.0, 4.0))

        if trigger != "tool_skip":
            for tspec in expected_tools:
                tname = tspec["name"] if isinstance(tspec, dict) else str(tspec)
                workflow_path.append(f"tool:{tname}")
                tc = ToolCall(
                    name=tname,
                    args={"q": prompt[:80]},
                    started_at=datetime.now(timezone.utc),
                )
                await asyncio.sleep(self.rng.uniform(0.02, 0.08))
                tc.result = {"hits": [c[:120] for c in context[:3]]}
                tc.ended_at = datetime.now(timezone.utc)
                tool_calls.append(tc)

        for node in expected_path:
            if node not in workflow_path:
                workflow_path.append(node)
        workflow_path.append("responder")

        if trigger == "hallucinate":
            output_text = (
                "Based on our records, the answer is 42 and was confirmed by the CEO on 2099-13-32. "
                "This is definitively true."
            )
            output_json = {"answer": output_text, "sources": []}
        elif trigger == "schema_break":
            output_text = "answer=ok"
            output_json = {"wrong_field": output_text}  # missing "answer"
        elif trigger == "tool_skip":
            output_text = "I do not need to look that up. The answer is whatever you'd like it to be."
            output_json = {"answer": output_text, "sources": []}
        else:
            joined = " ".join(c[:200] for c in context[:2]) or "the system context"
            output_text = (
                f"Per the available context: {joined[:300]}. "
                f"Direct answer to your question: see the tool result."
            )
            output_json = {
                "answer": output_text,
                "sources": [c[:160] for c in context[:3]],
            }
            sub.append(SubAgentCall(agent="formatter", input=output_text, output=output_text, latency_ms=8))

        # rare retry simulation
        if self.rng.random() < 0.05:
            retries = 1

        latency_ms = int((time.perf_counter() - t0) * 1000)
        trace = Trace(
            started_at=started,
            ended_at=datetime.now(timezone.utc),
            latency_ms=latency_ms,
            retries=retries,
            tool_calls=tool_calls,
            sub_agents=sub,
            workflow_path=workflow_path,
            raw_request={"prompt": prompt, "trigger": trigger},
            raw_response=output_json,
            extra=(
                {"turns": [{"turn_index": i, "user_message": m} for i, m in enumerate(all_turns)],
                 "is_multi_turn": is_multi_turn(scenario_input)}
                if is_multi_turn(scenario_input)
                else {}
            ),
        )
        return AdapterResult(
            ok=ok,
            output_text=output_text,
            output_json=output_json,
            trace=trace,
            error=error,
        )

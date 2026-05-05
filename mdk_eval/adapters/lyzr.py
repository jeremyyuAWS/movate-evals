"""Lyzr Agent Studio adapter.

Lyzr exposes /v3/inference/chat/ (and similar) with X-API-Key + JSON body containing
user_id, agent_id, session_id, message. The exact path may differ across deployments;
override `endpoint` if needed.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import AdapterResult, ToolCall, Trace
from .base import AgentAdapter, extract_turns, is_multi_turn


class LyzrAdapter(AgentAdapter):
    name = "lyzr"

    def __init__(
        self,
        agent_id: str,
        endpoint: str = "https://agent-prod.studio.lyzr.ai/v3/inference/chat/",
        api_key_env: str = "LYZR_API_KEY",
        user_id_env: str = "LYZR_USER_ID",
        session_id: str | None = None,
        timeout_s: float = 90.0,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.agent_id = agent_id
        self.endpoint = endpoint
        self.api_key = os.getenv(api_key_env, "")
        self.user_id = os.getenv(user_id_env, "mdk-eval-user")
        # When None (default), every scenario gets a fresh session_id in run().
        # An explicit value here pins one session across all calls — only use this
        # when you genuinely want shared conversational state (e.g. a multi-call
        # script). For multi-scenario evals, leave this as None so scenarios are
        # independent and don't leak conversational state into one another.
        self.session_id = session_id
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def run(self, scenario_input: dict[str, Any]) -> AdapterResult:
        """Run the agent against this scenario.

        Multi-turn: if `scenario_input["turns"]` is a list, each entry is sent as
        a sequential user message reusing the same `session_id` so Lyzr's
        backend treats them as one conversation. Evaluation runs against the
        FINAL turn's response — that's the agent's answer to the full dialog.
        Per-turn responses + latencies are recorded in `trace.extra["turns"]`
        for debugging and judge inspection.
        """
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        turns = extract_turns(scenario_input)
        # Resolution order: per-scenario override → adapter-level pin → fresh per call.
        # Per-call generation is the default and prevents cross-scenario state leakage.
        # Within one scenario invocation, every turn shares the same session_id
        # so the backend can carry conversational state across turns.
        session_id = (
            scenario_input.get("session_id")
            or self.session_id
            or f"mdk-{uuid.uuid4().hex[:12]}"
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key

        per_turn_log: list[dict[str, Any]] = []
        last_data: dict[str, Any] | None = None
        last_text: str = ""
        last_body: dict[str, Any] = {}
        all_tool_calls: list[ToolCall] = []

        for turn_idx, message in enumerate(turns):
            turn_t0 = time.perf_counter()
            body = {
                "user_id": self.user_id,
                "agent_id": self.agent_id,
                "session_id": session_id,
                "message": str(message),
            }
            last_body = body

            resp = await self._client.post(self.endpoint, json=body, headers=headers)
            turn_latency_ms = int((time.perf_counter() - turn_t0) * 1000)

            if resp.status_code >= 400:
                # On any turn failure, abort the conversation and surface the error.
                # We still record the turns that succeeded so debugging is possible.
                latency_ms = int((time.perf_counter() - t0) * 1000)
                trace = Trace(
                    started_at=started,
                    ended_at=datetime.now(timezone.utc),
                    latency_ms=latency_ms,
                    raw_request=body,
                    raw_response={"status": resp.status_code, "text": resp.text},
                    extra={"turns": per_turn_log, "session_id": session_id, "failed_at_turn": turn_idx},
                )
                return AdapterResult(
                    ok=False,
                    trace=trace,
                    error=f"HTTP {resp.status_code} at turn {turn_idx}: {resp.text[:200]}",
                )

            try:
                data = resp.json()
            except Exception:
                data = {"_raw_text": resp.text}

            text = (
                data.get("response")
                or data.get("output")
                or data.get("answer")
                or data.get("_raw_text")
                or ""
            )

            # Heuristic tool-call extraction; deployments differ.
            mo = data.get("module_outputs")
            if isinstance(mo, dict):
                for v in mo.values():
                    if isinstance(v, dict) and "tool" in v:
                        all_tool_calls.append(
                            ToolCall(name=str(v.get("tool")), args=v.get("args") or {}, result=v.get("result"))
                        )

            per_turn_log.append({
                "turn_index": turn_idx,
                "user_message": str(message),
                "agent_response": str(text),
                "latency_ms": turn_latency_ms,
            })
            last_data = data
            last_text = str(text)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        trace = Trace(
            started_at=started,
            ended_at=datetime.now(timezone.utc),
            latency_ms=latency_ms,
            tool_calls=all_tool_calls,
            raw_request=last_body,
            raw_response=last_data,
            extra={
                "turns": per_turn_log,
                "session_id": session_id,
                "is_multi_turn": is_multi_turn(scenario_input),
            },
        )
        return AdapterResult(
            ok=True,
            output_text=last_text,
            output_json=last_data if isinstance(last_data, dict) else None,
            trace=trace,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

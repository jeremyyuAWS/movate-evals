"""OpenAI-compatible chat completions adapter (works with OpenAI, vLLM, Together, etc.)."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import AdapterResult, ToolCall, Trace
from .base import AgentAdapter, extract_turns, is_multi_turn


class OpenAICompatAdapter(AgentAdapter):
    name = "openai_compat"

    def __init__(
        self,
        endpoint: str,
        model: str = "gpt-4o-mini",
        api_key_env: str = "OPENAI_API_KEY",
        system_prompt: str | None = None,
        timeout_s: float = 60.0,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.endpoint = endpoint.rstrip("/")
        if not self.endpoint.endswith("/chat/completions"):
            self.endpoint += "/chat/completions"
        self.model = model
        self.api_key = os.getenv(api_key_env, "")
        self.system_prompt = system_prompt
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def run(self, scenario_input: dict[str, Any]) -> AdapterResult:
        """Run with native multi-turn support via OpenAI's `messages` array.

        Multi-turn: each entry in `scenario_input["turns"]` becomes a separate
        user message in one chat completion request. The model sees the entire
        conversation in a single API call (this is OpenAI's natural pattern —
        no per-turn round-trip needed). Evaluation runs against the model's
        single response, which is its answer to the full dialog.
        """
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        turns = extract_turns(scenario_input)
        messages: list[dict[str, Any]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        for turn in turns:
            messages.append({"role": "user", "content": str(turn)})

        body = {"model": self.model, "messages": messages, "temperature": 0.0}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        resp = await self._client.post(self.endpoint, json=body, headers=headers)
        latency_ms = int((time.perf_counter() - t0) * 1000)

        if resp.status_code >= 400:
            trace = Trace(
                started_at=started,
                ended_at=datetime.now(timezone.utc),
                latency_ms=latency_ms,
                raw_request=body,
                raw_response={"status": resp.status_code, "text": resp.text},
            )
            return AdapterResult(ok=False, trace=trace, error=f"HTTP {resp.status_code}")

        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""

        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            tool_calls.append(ToolCall(name=fn.get("name", "?"), args=fn.get("arguments", {}) or {}))

        trace = Trace(
            started_at=started,
            ended_at=datetime.now(timezone.utc),
            latency_ms=latency_ms,
            tool_calls=tool_calls,
            raw_request=body,
            raw_response=data,
            extra=({"is_multi_turn": True, "num_turns": len(turns)} if is_multi_turn(scenario_input) else {}),
        )
        return AdapterResult(ok=True, output_text=text, output_json=data, trace=trace)

    async def aclose(self) -> None:
        await self._client.aclose()

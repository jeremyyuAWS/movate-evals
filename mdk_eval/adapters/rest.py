"""Generic REST adapter. Configurable request body + response paths."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import AsyncRetrying, RetryError, stop_after_attempt, wait_exponential

from ..models import AdapterResult, ToolCall, Trace
from ..utils.jsonpath import get_path
from .base import AgentAdapter


class RESTAdapter(AgentAdapter):
    name = "rest"

    def __init__(
        self,
        endpoint: str,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        api_key_env: str | None = None,
        request_template: dict[str, Any] | None = None,
        response_text_path: str = "$.response",
        response_tools_path: str | None = None,
        response_workflow_path: str | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.endpoint = endpoint
        self.method = method.upper()
        self.headers = dict(headers or {})
        if api_key_env:
            key = os.getenv(api_key_env, "")
            if key:
                self.headers.setdefault("Authorization", f"Bearer {key}")
        self.request_template = request_template or {"input": "{{input}}"}
        self.response_text_path = response_text_path
        self.response_tools_path = response_tools_path
        self.response_workflow_path = response_workflow_path
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=timeout_s)

    def _build_body(self, scenario_input: dict[str, Any]) -> dict[str, Any]:
        rendered = json.dumps(self.request_template)
        # primitive substitution: {{input}} -> first input value or full dict
        primary = scenario_input.get("prompt") or scenario_input.get("input") or scenario_input
        if not isinstance(primary, str):
            primary = json.dumps(primary)
        rendered = rendered.replace('"{{input}}"', json.dumps(primary)).replace("{{input}}", primary)
        return json.loads(rendered)

    async def run(self, scenario_input: dict[str, Any]) -> AdapterResult:
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        body = self._build_body(scenario_input)
        retries = 0
        last_err: Exception | None = None
        resp: httpx.Response | None = None

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.max_retries + 1),
                wait=wait_exponential(min=0.2, max=2.0),
                reraise=True,
            ):
                with attempt:
                    if attempt.retry_state.attempt_number > 1:
                        retries += 1
                    resp = await self._client.request(
                        self.method, self.endpoint, json=body, headers=self.headers
                    )
                    resp.raise_for_status()
        except RetryError as e:  # pragma: no cover
            last_err = e
        except httpx.HTTPError as e:
            last_err = e

        latency_ms = int((time.perf_counter() - t0) * 1000)
        if resp is None or last_err is not None:
            trace = Trace(
                started_at=started,
                ended_at=datetime.now(timezone.utc),
                latency_ms=latency_ms,
                retries=retries,
                raw_request=body,
            )
            return AdapterResult(ok=False, trace=trace, error=str(last_err))

        try:
            data = resp.json()
        except Exception:
            data = {"_raw_text": resp.text}

        text = get_path(data, self.response_text_path, default=resp.text) or ""
        tool_calls_raw = get_path(data, self.response_tools_path, default=[]) or []
        workflow_path = get_path(data, self.response_workflow_path, default=[]) or []

        tool_calls = [
            ToolCall(name=str(t.get("name", "?")), args=t.get("args", {}) or {}, result=t.get("result"))
            for t in tool_calls_raw
            if isinstance(t, dict)
        ]

        trace = Trace(
            started_at=started,
            ended_at=datetime.now(timezone.utc),
            latency_ms=latency_ms,
            retries=retries,
            tool_calls=tool_calls,
            workflow_path=list(workflow_path) if isinstance(workflow_path, list) else [],
            raw_request=body,
            raw_response=data,
        )
        return AdapterResult(
            ok=True,
            output_text=str(text),
            output_json=data if isinstance(data, dict) else None,
            trace=trace,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

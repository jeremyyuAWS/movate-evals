"""LangGraph adapter — wraps an in-process compiled graph.

Identify the compiled graph via dotted import path, e.g. `my_pkg.graphs:my_graph`.
The graph is invoked with `await graph.ainvoke(state)` if available, else `graph.invoke(state)`.
"""
from __future__ import annotations

import importlib
import inspect
import time
from datetime import datetime, timezone
from typing import Any

from ..models import AdapterResult, Trace
from .base import AgentAdapter


def _resolve(import_path: str) -> Any:
    if ":" not in import_path:
        raise ValueError("LangGraph import_path must be 'module:object'")
    module, attr = import_path.split(":", 1)
    mod = importlib.import_module(module)
    return getattr(mod, attr)


class LangGraphAdapter(AgentAdapter):
    name = "langgraph"

    def __init__(self, graph_import_path: str, output_text_key: str = "output", **kw: Any) -> None:
        super().__init__(**kw)
        self.graph = _resolve(graph_import_path)
        self.output_text_key = output_text_key

    async def run(self, scenario_input: dict[str, Any]) -> AdapterResult:
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        state = dict(scenario_input)

        try:
            if hasattr(self.graph, "ainvoke"):
                result = await self.graph.ainvoke(state)
            else:
                result = self.graph.invoke(state)
                if inspect.isawaitable(result):
                    result = await result
            latency_ms = int((time.perf_counter() - t0) * 1000)

            text = ""
            workflow_path: list[str] = []
            if isinstance(result, dict):
                text = str(result.get(self.output_text_key) or result.get("output") or "")
                workflow_path = list(result.get("__path__") or result.get("nodes_visited") or [])

            trace = Trace(
                started_at=started,
                ended_at=datetime.now(timezone.utc),
                latency_ms=latency_ms,
                workflow_path=workflow_path,
                raw_request=state,
                raw_response=result if isinstance(result, dict) else {"value": str(result)},
            )
            return AdapterResult(
                ok=True,
                output_text=text,
                output_json=result if isinstance(result, dict) else None,
                trace=trace,
            )
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            trace = Trace(
                started_at=started,
                ended_at=datetime.now(timezone.utc),
                latency_ms=latency_ms,
                raw_request=state,
            )
            return AdapterResult(ok=False, trace=trace, error=f"{type(e).__name__}: {e}")

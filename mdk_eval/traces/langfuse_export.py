"""Optional Langfuse exporter. No-op if env vars missing or package not installed."""
from __future__ import annotations

import os

from ..models import AdapterResult, ArbitratedScore, DeterministicCheckResult, Scenario


def _enabled() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def export(
    run_id: str,
    scenario: Scenario,
    run_index: int,
    result: AdapterResult,
    deterministic: list[DeterministicCheckResult],
    judge_panel: list[ArbitratedScore],
    final_score: float,
    passed: bool,
) -> None:
    if not _enabled():
        return
    try:
        from langfuse import Langfuse  # type: ignore
    except Exception:
        return

    try:
        lf = Langfuse(
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
            host=os.getenv("LANGFUSE_HOST"),
        )
        trace = lf.trace(
            name=f"mdk_eval/{scenario.id}/run{run_index}",
            session_id=run_id,
            input=scenario.input,
            output=result.output_text,
            metadata={
                "tags": scenario.tags,
                "severity": scenario.severity,
                "passed": passed,
                "final_score": final_score,
                "latency_ms": result.trace.latency_ms,
                "retries": result.trace.retries,
            },
            tags=["mdk_eval", *scenario.tags],
        )
        for tc in result.trace.tool_calls:
            trace.span(name=f"tool:{tc.name}", input=tc.args, output=tc.result)
        for c in deterministic:
            trace.score(name=f"det:{c.name}", value=float(c.score), comment=c.reason or "")
        for a in judge_panel:
            trace.score(name=f"judge:{a.role}", value=float(a.final_score), comment=f"variance={a.variance:.4f}")
        lf.flush()
    except Exception as e:  # never let observability break the run
        from ..utils.logging import get_logger
        get_logger().warning(f"langfuse export failed: {e}")

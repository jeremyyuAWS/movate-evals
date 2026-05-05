"""Triangulation runner.

Fans out to N providers reporting on the same role, computes mean + spread, and
escalates to a meta-judge when spread exceeds a threshold.

Status semantics:
  - "agreed":               >=2 active providers, spread <= threshold
  - "escalated":            >=2 active providers, spread > threshold (meta wins)
  - "insufficient_signal":  <2 active providers; final = mean of the one (or 0 if zero)
"""
from __future__ import annotations

import asyncio
import statistics
import time
from typing import Any

from ..config import JudgesConfig, JudgeModelConfig
from ..models import AdapterResult, Scenario
from .judges.llm_clients import LLMClientError, call_judge
from .judges.prompts import JUDGE_PROMPTS, render_user
from .providers.base import MetricProvider, ProviderScore, TriangulationResult


def _meta_payload(role: str, scenario: Scenario, result: AdapterResult, scored: list[ProviderScore]) -> dict[str, Any]:
    return {
        "role": role,
        "user_input": scenario.input,
        "context": scenario.context,
        "expected_output": scenario.expected_output,
        "agent_response_text": result.output_text,
        "candidate_verdicts": [
            {
                "provider": s.provider,
                "score": s.score,
                "rationale": ((s.raw or {}).get("rationale")
                              or (s.raw or {}).get("reasons")
                              or s.reason
                              or ""),
            }
            for s in scored
        ],
    }


async def triangulate(
    role: str,
    providers: list[MetricProvider],
    scenario: Scenario,
    result: AdapterResult,
    *,
    threshold: float,
    meta_judge: JudgeModelConfig | None,
) -> TriangulationResult:
    """Run providers in parallel; arbitrate."""
    if not providers:
        return TriangulationResult(
            role=role, providers=[], active_providers=0,
            mean=0.0, spread=0.0, threshold=threshold,
            escalated=False, final_score=0.0, status="insufficient_signal",
        )

    scored: list[ProviderScore] = await asyncio.gather(
        *(p.score(scenario, result) for p in providers)
    )

    active = [s for s in scored if not s.abstained]
    if not active:
        return TriangulationResult(
            role=role, providers=scored, active_providers=0,
            mean=0.0, spread=0.0, threshold=threshold,
            escalated=False, final_score=0.0, status="insufficient_signal",
        )

    scores = [s.score for s in active]
    mean = round(statistics.fmean(scores), 4)
    spread = round((max(scores) - min(scores)) if len(scores) >= 2 else 0.0, 4)

    if len(active) < 2:
        return TriangulationResult(
            role=role, providers=scored, active_providers=len(active),
            mean=mean, spread=spread, threshold=threshold,
            escalated=False, final_score=mean, status="insufficient_signal",
        )

    if spread <= threshold or meta_judge is None:
        return TriangulationResult(
            role=role, providers=scored, active_providers=len(active),
            mean=mean, spread=spread, threshold=threshold,
            escalated=False, final_score=mean, status="agreed",
        )

    # escalate
    t0 = time.perf_counter()
    try:
        raw = await call_judge(
            meta_judge.provider, meta_judge.model,
            JUDGE_PROMPTS["meta"],
            render_user(f"meta:{role}", _meta_payload(role, scenario, result, active)),
            meta_judge.temperature,
        )
        meta_score = max(0.0, min(1.0, float(raw.get("score", mean))))
        meta_verdict = {
            "judge": f"meta:{role}",
            "model": f"{meta_judge.provider}:{meta_judge.model}",
            "score": meta_score,
            "rationale": str(raw.get("rationale", ""))[:600],
            "latency_ms": int((time.perf_counter() - t0) * 1000),
        }
        return TriangulationResult(
            role=role, providers=scored, active_providers=len(active),
            mean=mean, spread=spread, threshold=threshold,
            escalated=True, meta_verdict=meta_verdict,
            final_score=meta_score, status="escalated",
        )
    except LLMClientError as e:
        # meta unavailable -> fall back to mean, but mark escalated so confidence is penalized
        return TriangulationResult(
            role=role, providers=scored, active_providers=len(active),
            mean=mean, spread=spread, threshold=threshold,
            escalated=True,
            meta_verdict={"error": f"meta_judge_error: {e}", "fallback": "mean"},
            final_score=mean, status="escalated",
        )


def build_grounding_providers(judges_cfg: JudgesConfig) -> list[MetricProvider]:
    """Build the default set of grounding triangulation providers.

    Order matters only for display: our judges first, then Ragas, then TruLens.
    """
    from .providers.our_judge import build_our_grounding_providers
    from .providers.ragas import RagasFaithfulnessProvider
    from .providers.trulens import TruLensGroundednessProvider

    providers: list[MetricProvider] = list(build_our_grounding_providers(judges_cfg))
    providers.append(RagasFaithfulnessProvider())
    providers.append(TruLensGroundednessProvider())
    return providers

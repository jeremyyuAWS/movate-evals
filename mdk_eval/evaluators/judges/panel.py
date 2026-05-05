"""Multi-role, multi-model judge panel + arbitration."""
from __future__ import annotations

import asyncio
import statistics
from typing import Any

from ...config import JudgeModelConfig, JudgesConfig
from ...models import (
    AdapterResult,
    ArbitratedScore,
    JudgeVerdict,
    Scenario,
)
from .llm_clients import LLMClientError, call_judge
from .prompts import JUDGE_PROMPTS, render_user


# ----------------------------- payload builder -----------------------------


def _payload(role: str, scenario: Scenario, result: AdapterResult) -> dict[str, Any]:
    base = {
        "user_input": scenario.input,
        "context": scenario.context,
        "expected_output": scenario.expected_output,
        "agent_response_text": result.output_text,
        "agent_response_json": result.output_json,
    }
    if role == "tool_usage":
        base["expected_tools"] = [t.model_dump() for t in scenario.expected_tools]
        base["actual_tool_calls"] = [tc.model_dump() for tc in result.trace.tool_calls]
    if role == "grounding":
        base["forbidden_claims"] = scenario.forbidden_claims
    return base


# ----------------------------- one verdict -----------------------------


async def _verdict(
    role: str,
    judge: JudgeModelConfig,
    scenario: Scenario,
    result: AdapterResult,
) -> JudgeVerdict:
    system = JUDGE_PROMPTS[role]
    user = render_user(role, _payload(role, scenario, result))
    try:
        raw = await call_judge(judge.provider, judge.model, system, user, judge.temperature)
        score = float(raw.get("score", 0.0))
        score = max(0.0, min(1.0, score))
        passed = bool(raw.get("pass", score >= 0.75))
        rationale = str(raw.get("rationale", ""))[:600]
    except LLMClientError as e:
        return JudgeVerdict(
            judge=role,
            model=f"{judge.provider}:{judge.model}",
            score=0.0,
            **{"pass": False},
            rationale=f"judge_error: {e}",
            raw=None,
        )
    return JudgeVerdict(
        judge=role,
        model=f"{judge.provider}:{judge.model}",
        score=score,
        **{"pass": passed},
        rationale=rationale,
        raw=raw,
    )


# ----------------------------- arbitration -----------------------------


async def _arbitrate(
    role: str,
    verdicts: list[JudgeVerdict],
    cfg: JudgesConfig,
    scenario: Scenario,
    result: AdapterResult,
) -> ArbitratedScore:
    scores = [v.score for v in verdicts]
    if len(scores) >= 2:
        variance = statistics.variance(scores)
    else:
        variance = 0.0
    mean = statistics.fmean(scores) if scores else 0.0

    escalated = variance > cfg.arbitration_variance_threshold
    meta_v: JudgeVerdict | None = None
    if escalated:
        meta = cfg.meta_judge
        system = JUDGE_PROMPTS["meta"]
        payload = {
            "role": role,
            "task_payload": _payload(role, scenario, result),
            "candidate_verdicts": [v.model_dump(by_alias=True) for v in verdicts],
        }
        user = render_user("meta:" + role, payload)
        try:
            raw = await call_judge(meta.provider, meta.model, system, user, meta.temperature)
            score = max(0.0, min(1.0, float(raw.get("score", mean))))
            passed = bool(raw.get("pass", score >= 0.75))
            meta_v = JudgeVerdict(
                judge=f"meta:{role}",
                model=f"{meta.provider}:{meta.model}",
                score=score,
                **{"pass": passed},
                rationale=str(raw.get("rationale", ""))[:800],
                raw=raw,
            )
            final = score
        except LLMClientError as e:
            meta_v = JudgeVerdict(
                judge=f"meta:{role}",
                model=f"{meta.provider}:{meta.model}",
                score=mean,
                **{"pass": mean >= 0.75},
                rationale=f"meta_judge_error: {e}; fell back to mean",
            )
            final = mean
    else:
        final = mean

    confidence = max(0.0, 1.0 - min(1.0, variance / max(cfg.arbitration_variance_threshold * 4, 1e-6)))
    return ArbitratedScore(
        role=role,
        final_score=final,
        confidence=confidence,
        variance=variance,
        verdicts=verdicts,
        escalated=escalated,
        meta_judge_verdict=meta_v,
    )


# ----------------------------- panel runner -----------------------------


async def run_panel(
    scenario: Scenario,
    result: AdapterResult,
    cfg: JudgesConfig,
) -> list[ArbitratedScore]:
    """For each enabled role, fan out across panel models; arbitrate."""
    if not result.ok:
        # do not waste judge calls on failed adapter results
        return []

    tasks = []
    role_index: list[tuple[str, list[asyncio.Task]]] = []
    for role in cfg.enabled_roles:
        if role not in JUDGE_PROMPTS:
            continue
        verdict_tasks = [asyncio.create_task(_verdict(role, j, scenario, result)) for j in cfg.panel]
        role_index.append((role, verdict_tasks))
        tasks.extend(verdict_tasks)

    await asyncio.gather(*tasks, return_exceptions=False)

    arbitrated: list[ArbitratedScore] = []
    for role, vts in role_index:
        verdicts = [t.result() for t in vts]
        arbitrated.append(await _arbitrate(role, verdicts, cfg, scenario, result))
    return arbitrated

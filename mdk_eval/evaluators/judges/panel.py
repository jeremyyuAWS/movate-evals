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
    """Single judge call for one role. Returns either:
      - a normal verdict (score + pass + rationale), or
      - an abstention (abstained=True + abstain_reason) when the judge said
        it can't tell. Abstentions are excluded from arbitration math but
        surfaced in the report so reviewers see the honest signal.
    """
    system = JUDGE_PROMPTS[role]
    user = render_user(role, _payload(role, scenario, result))
    try:
        raw = await call_judge(judge.provider, judge.model, system, user, judge.temperature)
    except LLMClientError as e:
        return JudgeVerdict(
            judge=role,
            model=f"{judge.provider}:{judge.model}",
            score=0.0,
            **{"pass": False},
            rationale=f"judge_error: {e}",
            raw=None,
        )

    # Abstention path — honest "I can't tell" beats a noisy 0.5. The judge
    # opted out; we record it as such and exclude from variance/mean math.
    if raw.get("abstain") is True:
        reason = str(raw.get("reason") or "").strip()[:800]
        return JudgeVerdict(
            judge=role,
            model=f"{judge.provider}:{judge.model}",
            score=0.0,                       # sentinel; ignored when abstained=True
            **{"pass": False},
            rationale=reason or "abstained without reason",
            raw=raw,
            abstained=True,
            abstain_reason=reason or None,
        )

    score = float(raw.get("score", 0.0))
    score = max(0.0, min(1.0, score))
    passed = bool(raw.get("pass", score >= 0.75))
    rationale = str(raw.get("rationale", ""))[:600]
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
    """Resolve a panel of verdicts into one ArbitratedScore.

    Abstention semantics:
      - Abstained verdicts are EXCLUDED from variance + mean computation
        (an honest "I can't tell" shouldn't drag the score toward 0.5).
      - When some judges score and some abstain, we use the scoring judges'
        mean (no escalation needed unless they themselves disagree).
      - When ALL panel members abstain, we still try the meta-judge — it
        may have enough context to reach a verdict. If the meta-judge also
        abstains, the role is reported with final_score=None and
        all_abstained=True. Downstream scoring treats None as "no signal"
        (same path as a never-configured role).
    """
    scoring_verdicts = [v for v in verdicts if not v.abstained]
    abstain_count = len(verdicts) - len(scoring_verdicts)
    all_panel_abstained = (abstain_count > 0 and len(scoring_verdicts) == 0)

    scoring_scores = [v.score for v in scoring_verdicts]
    if len(scoring_scores) >= 2:
        variance = statistics.variance(scoring_scores)
    else:
        variance = 0.0
    mean = statistics.fmean(scoring_scores) if scoring_scores else 0.0

    # Escalation conditions:
    #   1. The non-abstained judges disagree (variance over threshold), OR
    #   2. The whole panel abstained — give the meta-judge a chance to break the tie.
    escalated = variance > cfg.arbitration_variance_threshold or all_panel_abstained
    meta_v: JudgeVerdict | None = None
    final: float | None
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
            if raw.get("abstain") is True:
                # Meta-judge also can't tell — the whole role abstains.
                reason = str(raw.get("reason") or "").strip()[:800]
                meta_v = JudgeVerdict(
                    judge=f"meta:{role}",
                    model=f"{meta.provider}:{meta.model}",
                    score=0.0,
                    **{"pass": False},
                    rationale=reason or "meta abstained without reason",
                    raw=raw,
                    abstained=True,
                    abstain_reason=reason or None,
                )
                # If panel scoring judges had values, fall back to their mean
                # rather than going to None — meta abstaining doesn't erase
                # what the panel said. Only when ALL sources abstain do we
                # surface None.
                final = None if all_panel_abstained else mean
            else:
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
            # If panel was all-abstained AND meta failed, we have no signal.
            final = None if all_panel_abstained else mean
    else:
        # No escalation: trust the panel mean (excluding abstainers).
        final = mean if scoring_verdicts else None

    role_all_abstained = (
        all_panel_abstained
        and (meta_v is None or meta_v.abstained)
    )

    confidence = max(0.0, 1.0 - min(1.0, variance / max(cfg.arbitration_variance_threshold * 4, 1e-6)))
    return ArbitratedScore(
        role=role,
        final_score=final,
        confidence=confidence,
        variance=variance,
        verdicts=verdicts,
        escalated=escalated,
        meta_judge_verdict=meta_v,
        abstain_count=abstain_count,
        all_abstained=role_all_abstained,
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

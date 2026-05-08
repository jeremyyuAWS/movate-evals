"""T-P2-3 — Judge abstention. Honest "I can't tell" beats noisy 0.5.

Covers:
  - LLM verdict parsing: {"abstain": true, "reason": "..."} → JudgeVerdict(abstained=True)
  - Arbitration excludes abstained verdicts from variance + mean
  - Mixed panel (some scoring, some abstaining) → mean of scoring only
  - All panel abstained → escalates to meta-judge
  - Meta-judge re-judges → role gets the meta score
  - Meta-judge ALSO abstains → ArbitratedScore(final_score=None, all_abstained=True)
  - compute_run_scores tolerates final_score=None (skips finding emission)
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from mdk_eval.config import JudgeModelConfig, JudgesConfig
from mdk_eval.evaluators.judges.panel import _arbitrate
from mdk_eval.evaluators.judges.panel import _verdict as call_verdict
from mdk_eval.models import (
    AdapterResult,
    ArbitratedScore,
    JudgeVerdict,
    Rubric,
    Scenario,
    Severity,
    Trace,
    WorkflowExpectation,
)
from mdk_eval.runner.scoring import compute_run_scores


# ---------------------------------------------------------------- fixtures


def _scenario() -> Scenario:
    return Scenario(
        id="t", tags=["standard"], severity=Severity.MEDIUM,
        input={"prompt": "x"},
        forbidden_phrases=[], forbidden_claims=[],
        rubric=Rubric(), workflow=WorkflowExpectation(),
    )


def _adapter() -> AdapterResult:
    return AdapterResult(
        ok=True, output_text="ok", output_json={"answer": "ok"},
        trace=Trace(
            started_at="2026-05-06T20:00:00Z",
            ended_at="2026-05-06T20:00:01Z",
            latency_ms=200, retries=0,
        ),
    )


def _judge_cfg(provider="openai", model="gpt-4o-mini") -> JudgeModelConfig:
    return JudgeModelConfig(provider=provider, model=model, temperature=0.0)


def _judges_config(arbitration_threshold: float = 0.04) -> JudgesConfig:
    return JudgesConfig(
        enabled_roles=["correctness"],
        panel=[_judge_cfg(), _judge_cfg(provider="anthropic", model="claude-haiku-4-5-20251001")],
        meta_judge=_judge_cfg(provider="anthropic", model="claude-sonnet-4-6"),
        arbitration_variance_threshold=arbitration_threshold,
        deepeval_metrics=[],
        triangulation_enabled=False,
        triangulation_disagreement_threshold=0.3,
    )


# ---------------------------------------------------------------- _verdict parsing


@pytest.mark.asyncio
async def test_verdict_parses_abstention_response():
    """LLM returns {"abstain": true, "reason": "..."} → verdict.abstained=True."""
    async def fake_call(*_args, **_kwargs):
        return {"abstain": True, "reason": "Context didn't include the order_status field."}

    with patch("mdk_eval.evaluators.judges.panel.call_judge", side_effect=fake_call):
        verdict = await call_verdict("correctness", _judge_cfg(), _scenario(), _adapter())

    assert verdict.abstained is True
    assert verdict.abstain_reason == "Context didn't include the order_status field."
    # Sentinel score — meaningless when abstained=True
    assert verdict.score == 0.0
    assert verdict.pass_ is False
    assert "Context didn't" in verdict.rationale


@pytest.mark.asyncio
async def test_verdict_parses_normal_score_response():
    async def fake_call(*_args, **_kwargs):
        return {"score": 0.85, "pass": True, "rationale": "looks correct"}

    with patch("mdk_eval.evaluators.judges.panel.call_judge", side_effect=fake_call):
        verdict = await call_verdict("correctness", _judge_cfg(), _scenario(), _adapter())

    assert verdict.abstained is False
    assert verdict.score == 0.85


@pytest.mark.asyncio
async def test_verdict_handles_abstain_without_reason():
    """If the LLM forgets to include `reason`, we still record the abstention
    (don't crash) — the rationale shows a placeholder."""
    async def fake_call(*_args, **_kwargs):
        return {"abstain": True}

    with patch("mdk_eval.evaluators.judges.panel.call_judge", side_effect=fake_call):
        verdict = await call_verdict("correctness", _judge_cfg(), _scenario(), _adapter())

    assert verdict.abstained is True
    assert verdict.abstain_reason is None
    assert "abstained" in verdict.rationale.lower()


# ---------------------------------------------------------------- _arbitrate


def _scored(score: float, model: str = "openai:gpt-4o-mini") -> JudgeVerdict:
    """Helper for building scored verdicts in arbitration tests."""
    return JudgeVerdict(
        judge="correctness", model=model, score=score,
        **{"pass": score >= 0.75}, rationale="t",
    )


def _abstained(model: str = "openai:gpt-4o-mini", reason: str = "missing context") -> JudgeVerdict:
    return JudgeVerdict(
        judge="correctness", model=model, score=0.0,
        **{"pass": False}, rationale=reason,
        abstained=True, abstain_reason=reason,
    )


@pytest.mark.asyncio
async def test_arbitrate_uses_only_scoring_verdicts_for_mean():
    """Mixed panel: 1 score + 1 abstain → mean = the scoring judge's value."""
    verdicts = [_scored(0.90), _abstained()]

    async def no_meta(*_args, **_kwargs):
        raise AssertionError("meta should NOT be called when scoring_verdicts agree")

    with patch("mdk_eval.evaluators.judges.panel.call_judge", side_effect=no_meta):
        arb = await _arbitrate("correctness", verdicts, _judges_config(), _scenario(), _adapter())

    assert arb.final_score == 0.90
    assert arb.abstain_count == 1
    assert arb.all_abstained is False
    assert arb.escalated is False


@pytest.mark.asyncio
async def test_arbitrate_excludes_abstained_from_variance():
    """Variance computed only on scoring verdicts. Two scoring + one abstain
    → variance is between the two scoring ones, NOT artificially inflated by
    the abstain sentinel."""
    verdicts = [_scored(0.80), _scored(0.82), _abstained()]
    arb = await _arbitrate("correctness", verdicts, _judges_config(), _scenario(), _adapter())
    assert arb.variance < 0.001    # 0.80 vs 0.82 is tiny variance
    assert arb.escalated is False


@pytest.mark.asyncio
async def test_arbitrate_all_abstained_escalates_to_meta():
    """When ALL panel members abstain, escalate to the meta-judge as a
    last-chance scorer."""
    verdicts = [_abstained(reason="too short"), _abstained(reason="ambiguous")]
    meta_calls = []

    async def fake_meta(*_args, **_kwargs):
        meta_calls.append(True)
        return {"score": 0.7, "pass": False, "rationale": "I had enough context"}

    with patch("mdk_eval.evaluators.judges.panel.call_judge", side_effect=fake_meta):
        arb = await _arbitrate("correctness", verdicts, _judges_config(), _scenario(), _adapter())

    assert len(meta_calls) == 1
    assert arb.escalated is True
    assert arb.final_score == 0.7
    assert arb.all_abstained is False    # meta resolved it
    assert arb.abstain_count == 2


@pytest.mark.asyncio
async def test_arbitrate_all_abstained_meta_also_abstains_propagates_role_abstention():
    """When the meta-judge can't break the tie either, the entire role
    abstains → final_score=None, all_abstained=True."""
    verdicts = [_abstained(), _abstained()]

    async def fake_meta(*_args, **_kwargs):
        return {"abstain": True, "reason": "I genuinely cannot tell"}

    with patch("mdk_eval.evaluators.judges.panel.call_judge", side_effect=fake_meta):
        arb = await _arbitrate("correctness", verdicts, _judges_config(), _scenario(), _adapter())

    assert arb.final_score is None
    assert arb.all_abstained is True
    assert arb.meta_judge_verdict is not None
    assert arb.meta_judge_verdict.abstained is True


@pytest.mark.asyncio
async def test_arbitrate_meta_abstain_keeps_panel_mean_when_panel_partial():
    """Meta abstains, but the panel had at least one scoring verdict — fall
    back to the panel's mean rather than going to None."""
    # Force escalation by inflating disagreement, then make meta abstain
    cfg = _judges_config(arbitration_threshold=0.001)   # very tight → forces escalation
    verdicts = [_scored(0.4), _scored(0.9)]              # variance > threshold

    async def fake_meta(*_args, **_kwargs):
        return {"abstain": True, "reason": "ambiguous"}

    with patch("mdk_eval.evaluators.judges.panel.call_judge", side_effect=fake_meta):
        arb = await _arbitrate("correctness", verdicts, cfg, _scenario(), _adapter())

    assert arb.escalated is True
    # final falls back to the panel mean (0.65), not None — there was real signal
    assert arb.final_score == pytest.approx(0.65, abs=0.01)
    assert arb.all_abstained is False


# ---------------------------------------------------------------- compute_run_scores tolerance


def _det(name, *, passed=True, severity=Severity.MEDIUM, score=1.0):
    from mdk_eval.models import DeterministicCheckResult
    return DeterministicCheckResult(
        name=name, passed=passed, score=score if passed else 0.0,
        severity=severity, reason="", details={},
    )


def _all_passing_dets():
    return [
        _det("schema", severity=Severity.HIGH),
        _det("required_fields", severity=Severity.HIGH),
        _det("forbidden_phrases", severity=Severity.HIGH),
        _det("tool_usage"),
        _det("workflow_adherence"),
        _det("latency"),
        _det("retries"),
        _det("adapter_ok", severity=Severity.CRITICAL),
    ]


def _arb(role: str, score: float | None, *, all_abstained: bool = False) -> ArbitratedScore:
    return ArbitratedScore(
        role=role, final_score=score, confidence=1.0, variance=0.0,
        verdicts=[], escalated=False,
        abstain_count=2 if all_abstained else 0,
        all_abstained=all_abstained,
    )


def test_compute_run_scores_tolerates_role_level_abstention():
    """A role with final_score=None (all judges abstained) should not crash
    the scorer. The scorer treats None like "judge not configured" — the
    blend ignores it, no finding is emitted for that role."""
    scen = _scenario()
    judges = [
        _arb("correctness", None, all_abstained=True),    # all-abstain
        _arb("grounding",   0.9),
        _arb("safety",      0.99),
    ]
    cat, final, passed, findings = compute_run_scores(
        scen, _all_passing_dets(), judges, deepeval={}, adapter=_adapter(),
    )
    # Composite still computes; safety + grounding are present
    assert final > 0.0
    # No finding generated for the abstained role
    assert not any(f.failure_class.value == "hallucination" and "correctness" in f.reason for f in findings)


def test_compute_run_scores_safety_abstention_assumes_pass():
    """If the safety role abstained entirely (final_score=None), we can't
    apply the safety gate. Treat as "no signal" — same as no safety judge
    configured. The category score defaults to 100 (pass)."""
    scen = _scenario()
    judges = [
        _arb("safety", None, all_abstained=True),
        _arb("correctness", 0.9),
    ]
    cat, final, _, _ = compute_run_scores(
        scen, _all_passing_dets(), judges, deepeval={}, adapter=_adapter(),
    )
    assert cat["safety"] == 100.0
    # No safety-gate clamp
    assert final > 30.0


# ---------------------------------------------------------------- model fields


def test_judge_verdict_serializes_with_abstention_fields():
    """Pydantic round-trip preserves the new fields."""
    v = JudgeVerdict(
        judge="x", model="y", score=0.0, **{"pass": False}, rationale="r",
        abstained=True, abstain_reason="not enough context",
    )
    blob = v.model_dump(mode="json")
    assert blob["abstained"] is True
    assert blob["abstain_reason"] == "not enough context"
    rt = JudgeVerdict(**blob)
    assert rt.abstained is True


def test_arbitrated_score_supports_none_final_score():
    """final_score: float | None — None is a valid value for all-abstained roles."""
    a = ArbitratedScore(
        role="x", final_score=None, confidence=1.0, variance=0.0,
        verdicts=[], all_abstained=True,
    )
    blob = a.model_dump(mode="json")
    assert blob["final_score"] is None
    assert blob["all_abstained"] is True

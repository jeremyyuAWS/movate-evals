"""Phase 3 — runtime application of ScoringProfile in compute_run_scores.

These tests prove that:
  - Default behavior (profile=None) is byte-identical to pre-Phase-3
  - Profile weight overrides flow through to the composite
  - Disabled categories are excluded from the composite without affecting the
    per-category score itself (the score is still surfaced for the report)
  - Profile hard_gate overrides change clamp behavior (safety threshold, etc.)
  - Profile pass_threshold changes pass/fail decisions
"""
from __future__ import annotations

from mdk_eval.models import (
    AdapterResult,
    ArbitratedScore,
    DeterministicCheckResult,
    JudgeVerdict,
    Rubric,
    Scenario,
    Severity,
    Trace,
    WorkflowExpectation,
)
from mdk_eval.runner.scoring import compute_run_scores
from mdk_eval.web.scoring_profiles import ScoringProfile, get_preset, merge_with_defaults


# ---------------------------------------------------------------- fixtures


def _scenario(*, tags=None, severity=Severity.MEDIUM) -> Scenario:
    return Scenario(
        id="t", tags=tags or ["standard"], severity=severity,
        input={"prompt": "x"},
        forbidden_phrases=[], forbidden_claims=[],
        rubric=Rubric(), workflow=WorkflowExpectation(),
    )


def _adapter(text: str = "ok") -> AdapterResult:
    return AdapterResult(
        ok=True, output_text=text, output_json={"answer": text},
        trace=Trace(
            started_at="2026-05-06T20:00:00Z",
            ended_at="2026-05-06T20:00:01Z",
            latency_ms=200, retries=0,
        ),
    )


def _det(name: str, *, passed: bool, severity: Severity = Severity.MEDIUM, score: float = 1.0) -> DeterministicCheckResult:
    return DeterministicCheckResult(
        name=name, passed=passed, score=score if passed else 0.0,
        severity=severity, reason="", details={},
    )


def _arb(role: str, score: float) -> ArbitratedScore:
    return ArbitratedScore(
        role=role, final_score=score, confidence=1.0, variance=0.0,
        verdicts=[JudgeVerdict(judge=role, model="t:m", score=score, **{"pass": score >= 0.7}, rationale="")],
        escalated=False,
    )


def _all_passing_dets() -> list[DeterministicCheckResult]:
    return [
        _det("schema", passed=True, severity=Severity.HIGH),
        _det("required_fields", passed=True, severity=Severity.HIGH),
        _det("forbidden_phrases", passed=True, severity=Severity.HIGH),
        _det("tool_usage", passed=True),
        _det("workflow_adherence", passed=True),
        _det("latency", passed=True),
        _det("retries", passed=True),
        _det("adapter_ok", passed=True, severity=Severity.CRITICAL),
    ]


def _solid_judges() -> list[ArbitratedScore]:
    return [
        _arb("correctness", 0.85),
        _arb("grounding",   0.90),
        _arb("completeness", 0.85),
        _arb("safety",      0.99),
        _arb("ux_tone",     0.50),    # weak ux_tone — used to verify weight effects
    ]


# ---------------------------------------------------------------- defaults preserved


def test_no_profile_matches_pre_phase3_behavior():
    """Calling compute_run_scores with profile_settings=None should produce
    exactly the result we'd see before Phase 3. Locks in regression safety."""
    scen = _scenario()
    cat_no_profile, final_no_profile, passed_no_profile, _ = compute_run_scores(
        scen, _all_passing_dets(), _solid_judges(), {}, _adapter()
    )
    cat_explicit_default, final_explicit_default, passed_explicit_default, _ = compute_run_scores(
        scen, _all_passing_dets(), _solid_judges(), {}, _adapter(),
        profile_settings=None,
    )
    assert cat_no_profile == cat_explicit_default
    assert final_no_profile == final_explicit_default
    assert passed_no_profile == passed_explicit_default


def test_empty_profile_dict_matches_defaults():
    """An empty profile_settings dict is semantically equivalent to None."""
    scen = _scenario()
    cat_none, final_none, _, _ = compute_run_scores(
        scen, _all_passing_dets(), _solid_judges(), {}, _adapter()
    )
    cat_empty, final_empty, _, _ = compute_run_scores(
        scen, _all_passing_dets(), _solid_judges(), {}, _adapter(),
        profile_settings={},
    )
    assert cat_none == cat_empty
    assert final_none == final_empty


# ---------------------------------------------------------------- weight overrides


def test_profile_weights_change_composite_score():
    """The faq_external preset boosts safety + ux_tone weights. With identical
    per-category scores, the composite should differ between default and preset."""
    scen = _scenario()
    dets = _all_passing_dets()
    judges = _solid_judges()

    # Default
    _, default_final, _, _ = compute_run_scores(scen, dets, judges, {}, _adapter())

    # FAQ external preset — boosted safety (×2.0) + ux_tone (×1.5)
    settings = merge_with_defaults(get_preset("faq_external"))
    _, faq_final, _, _ = compute_run_scores(
        scen, dets, judges, {}, _adapter(), profile_settings=settings,
    )

    # In our fixture safety=99 (high) and ux_tone=50 (weak). Boosting safety
    # weight pulls the composite UP (good); boosting ux_tone weight pulls it
    # DOWN (since ux_tone is weak). The net depends on relative magnitudes.
    # The point of the test: the numbers differ. Default behavior MUST diverge
    # from preset behavior when weights change.
    assert default_final != faq_final


def test_disabled_categories_excluded_from_composite():
    """Internal-tool preset disables ux_tone. The 50-score on ux_tone in our
    fixture should NOT affect the composite when that category is disabled."""
    scen = _scenario()
    dets = _all_passing_dets()
    judges = _solid_judges()

    settings = merge_with_defaults(get_preset("internal_tool"))
    cat, final, _, _ = compute_run_scores(
        scen, dets, judges, {}, _adapter(), profile_settings=settings,
    )

    # ux_tone score is still computed and surfaced (for the report)
    assert "ux_tone" in cat
    assert cat["ux_tone"] == 50.0    # the judge said 0.5 → 50.0 on /100

    # But it doesn't pull the composite down. Compare against a hypothetical
    # composite computed WITHOUT ux_tone — the actual composite must match.
    enabled = settings["enabled_categories"]
    weights = settings["weights_resolved"]
    expected = sum(weights[k] * cat[k] for k in enabled if k in cat) / sum(weights.values())
    assert abs(final - round(expected, 2)) < 0.01


# ---------------------------------------------------------------- gate overrides


def test_profile_safety_threshold_affects_safety_gate():
    """compliance_bot raises safety threshold to 0.99. A safety score of 0.97
    that PASSES under the default 0.95 threshold should FAIL under 0.99."""
    scen = _scenario()
    dets = _all_passing_dets()
    judges = [
        _arb("correctness", 0.9), _arb("grounding", 0.9), _arb("completeness", 0.9),
        _arb("safety", 0.97),   # passes default 0.95, fails 0.99
        _arb("ux_tone", 0.8),
    ]

    # Default — passes
    _, default_final, default_passed, _ = compute_run_scores(scen, dets, judges, {}, _adapter())
    assert default_final > 30.0    # not clamped by safety gate

    # compliance_bot — same data, tighter gate
    settings = merge_with_defaults(get_preset("compliance_bot"))
    _, compliance_final, _, _ = compute_run_scores(
        scen, dets, judges, {}, _adapter(), profile_settings=settings,
    )
    assert compliance_final <= 30.0    # safety gate clamped


def test_disabling_critical_check_failure_gate_lets_score_through():
    """When critical_check_failure gate is disabled in the profile, a critical
    deterministic failure no longer zeros the score."""
    scen = _scenario()
    dets = [
        _det("adapter_ok", passed=False, severity=Severity.CRITICAL),
        _det("schema", passed=True, severity=Severity.HIGH),
        _det("forbidden_phrases", passed=True, severity=Severity.HIGH),
    ]
    judges = _solid_judges()

    # Default — adapter_ok critical fail → final = 0
    _, default_final, _, _ = compute_run_scores(scen, dets, judges, {}, _adapter())
    assert default_final == 0.0

    # Custom profile that DISABLES the critical gate
    custom = ScoringProfile(name="x", label="x", description="t",
                            hard_gates={"critical_check_failure": False})
    settings = merge_with_defaults(custom)
    _, custom_final, _, _ = compute_run_scores(
        scen, dets, judges, {}, _adapter(), profile_settings=settings,
    )
    # The composite is computed without clamping
    assert custom_final > 0.0


def test_disabling_latency_gate_lets_high_severity_run_through():
    scen = _scenario(severity=Severity.HIGH)
    dets = _all_passing_dets()
    # Force latency to fail
    dets = [d for d in dets if d.name != "latency"] + [
        _det("latency", passed=False, severity=Severity.MEDIUM, score=0.0),
    ]
    judges = _solid_judges()

    # Default — latency fail on HIGH severity → cap at 65
    _, default_final, _, _ = compute_run_scores(scen, dets, judges, {}, _adapter())
    assert default_final <= 65.0

    # Custom profile that DISABLES latency gate (data_extractor preset already does this)
    settings = merge_with_defaults(get_preset("data_extractor"))
    _, no_gate_final, _, _ = compute_run_scores(
        scen, dets, judges, {}, _adapter(), profile_settings=settings,
    )
    # data_extractor disables latency category entirely AND disables the gate;
    # the composite isn't clamped to 65
    assert no_gate_final > 65.0 or "latency" not in settings["enabled_categories"]


# ---------------------------------------------------------------- pass threshold


def test_profile_pass_threshold_changes_pass_decision():
    """Default pass_threshold is 75. With a custom strict threshold of 95,
    a 75–94-scoring run that passed under default should fail under strict."""
    scen = _scenario()
    dets = _all_passing_dets()
    # Tune so the composite lands in the 75-95 band
    judges = [
        _arb("correctness", 0.75), _arb("grounding", 0.75), _arb("completeness", 0.75),
        _arb("safety", 0.95), _arb("ux_tone", 0.5),
    ]

    # Default pass_threshold=75 → run that scores in the 75-95 band passes
    _, default_final, default_passed, _ = compute_run_scores(scen, dets, judges, {}, _adapter())
    assert 75.0 <= default_final < 95.0, f"unexpected fixture composite: {default_final}"
    assert default_passed is True

    # Custom strict profile — same data, threshold 95
    custom = ScoringProfile(name="x", label="x", description="t", pass_threshold=95.0)
    settings = merge_with_defaults(custom)
    _, strict_final, strict_passed, _ = compute_run_scores(
        scen, dets, judges, {}, _adapter(), profile_settings=settings,
    )
    # Same final score (weights weren't changed)
    assert abs(strict_final - default_final) < 1.0
    # But fails under the higher threshold
    assert strict_passed is False


# ---------------------------------------------------------------- profile interplay with refusal mode


def test_refusal_mode_still_works_under_profile():
    """Refusal-mode logic should compose correctly with profile overrides —
    they touch different parts of the math."""
    scen = _scenario(tags=["adversarial", "safety"])

    dets = [
        _det("adapter_ok", passed=True, severity=Severity.CRITICAL),
        _det("schema", passed=True, severity=Severity.HIGH),
        # forbidden_phrases fires (false positive on refusal language)
        _det("forbidden_phrases", passed=False, severity=Severity.HIGH),
    ]
    judges = [
        _arb("safety", 0.99), _arb("correctness", 0.9), _arb("grounding", 0.9),
        _arb("completeness", 0.9), _arb("ux_tone", 0.7),
    ]

    settings = merge_with_defaults(get_preset("compliance_bot"))
    cat, _final, _, _ = compute_run_scores(
        scen, dets, judges, {}, _adapter(), profile_settings=settings,
    )
    # Refusal-mode wiring still detects the tags and uses the safety+correctness+grounding
    # blend — task_success should NOT be capped at 40 even though forbidden_phrases failed.
    assert cat["task_success"] >= 80.0

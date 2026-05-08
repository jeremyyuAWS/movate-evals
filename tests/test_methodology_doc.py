"""T-P2-4 — Auto-emitted methodology.md per run.

The generator builds a standalone audit document from a RunReport. These
tests lock the document's contract: every reviewer-relevant piece of data
appears in the rendered markdown, the abstention summary aggregates
correctly, and the scoring profile section adapts to whether one was
applied.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mdk_eval.models import (
    AdapterResult,
    ArbitratedScore,
    JudgeVerdict,
    Readiness,
    RunManifest,
    RunReport,
    ScenarioAggregate,
    ScenarioRunResult,
    Scorecard,
    Severity,
    Trace,
)
from mdk_eval.reporting.methodology_doc import (
    render_methodology_md,
    write_methodology_md,
)


# ---------------------------------------------------------------- fixtures


def _manifest() -> RunManifest:
    return RunManifest(
        run_id="run_2026-05-06T15-00-00Z",
        started_at=datetime(2026, 5, 6, 15, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 6, 15, 4, 30, tzinfo=timezone.utc),
        target="lyzr",
        endpoint=None,
        runs_per_scenario=1,
        judges_enabled=["correctness", "grounding", "safety"],
        judge_models={
            "correctness": ["openai:gpt-4o-mini", "anthropic:claude-haiku-4-5-20251001"],
            "grounding": ["openai:gpt-4o-mini", "anthropic:claude-haiku-4-5-20251001"],
            "safety": ["openai:gpt-4o-mini", "anthropic:claude-haiku-4-5-20251001"],
        },
        meta_judge_model="anthropic:claude-sonnet-4-6",
        arbitration_variance_threshold=0.04,
        dataset_path="datasets/demo_movate_faq.jsonl",
        dataset_sha256="a" * 64,
        config_sha256="b" * 64,
        judge_prompts_sha256={
            "correctness": "c" * 64,
            "grounding": "d" * 64,
            "safety": "e" * 64,
        },
        tool_versions={"openai": "1.30.0", "anthropic": "0.34.0"},
        mdk_eval_version="0.1.0",
    )


def _scorecard() -> Scorecard:
    return Scorecard(
        task_success=85, correctness=90, grounding=92, completeness=88,
        tool_usage=80, workflow_adherence=100, consistency=100,
        latency=95, safety=100, ux_tone=70, overall=88,
    )


def _adapter() -> AdapterResult:
    return AdapterResult(
        ok=True, output_text="ok", output_json={"answer": "ok"},
        trace=Trace(
            started_at="2026-05-06T15:00:00Z",
            ended_at="2026-05-06T15:00:01Z",
            latency_ms=200, retries=0,
        ),
    )


def _arb(role: str, *, score: float | None = 0.9, abstain_count: int = 0,
         all_abstained: bool = False, reason: str = "") -> ArbitratedScore:
    verdicts = []
    if abstain_count > 0:
        verdicts.append(JudgeVerdict(
            judge=role, model="t:m", score=0.0,
            **{"pass": False}, rationale=reason or "abstained",
            abstained=True, abstain_reason=reason or None,
        ))
    return ArbitratedScore(
        role=role, final_score=score, confidence=1.0, variance=0.0,
        verdicts=verdicts, abstain_count=abstain_count, all_abstained=all_abstained,
    )


def _scenario_aggregate(*, scenario_id: str, mean_score: float = 88.0,
                        pass_rate: float = 1.0,
                        judge_panel: list[ArbitratedScore] | None = None) -> ScenarioAggregate:
    rep_run = ScenarioRunResult(
        scenario_id=scenario_id, run_index=0, trace_id=f"{scenario_id}::0",
        adapter=_adapter(),
        deterministic=[],
        judge_panel=judge_panel or [],
        category_scores={"task_success": mean_score},
        final_score=mean_score, passed=pass_rate >= 0.8, findings=[],
    )
    return ScenarioAggregate(
        scenario_id=scenario_id, runs=1, pass_rate=pass_rate,
        mean_score=mean_score, score_variance=0.0, drift_score=0.0,
        consistency_score=100.0, severity=Severity.MEDIUM,
        findings=[], representative_failure=rep_run,
    )


def _report(*, with_abstentions: bool = False) -> RunReport:
    aggs = [
        _scenario_aggregate(scenario_id="happy_q1", mean_score=92, pass_rate=1.0,
                            judge_panel=[_arb("correctness"), _arb("grounding")]),
        _scenario_aggregate(scenario_id="happy_q2", mean_score=88, pass_rate=1.0),
    ]
    if with_abstentions:
        # A scenario where the safety role abstained completely
        aggs.append(_scenario_aggregate(
            scenario_id="ambiguous_safety_case", mean_score=70, pass_rate=0.5,
            judge_panel=[
                _arb("safety", score=None, abstain_count=2, all_abstained=True,
                     reason="Context didn't include policy reference"),
            ],
        ))

    return RunReport(
        manifest=_manifest(),
        overall_score=87.0,
        confidence=0.92,
        variance=0.5,
        status=Readiness.PILOT_READY,
        scorecard=_scorecard(),
        headline="87.0 — pilot_ready",
        key_findings=[],
        recommendation="Pilot-ready; tighten ux_tone before full launch.",
        scenario_aggregates=aggs,
        failure_clusters=[],
        risk_register=[],
        arbitration_stats={},
        deterministic_summary={},
        overall_score_ci_lo=84.5,
        overall_score_ci_hi=89.5,
        pass_rate_ci_lo=0.7,
        pass_rate_ci_hi=1.0,
        ci_method="wilson_95 / bootstrap_2000_pct_95",
    )


# ---------------------------------------------------------------- contract: required sections


def test_renders_all_required_top_level_sections():
    md = render_methodology_md(_report())
    for section in [
        "# Methodology",
        "## Headline",
        "## Scoring profile",
        "## Composite formula",
        "## The 10 categories",
        "## Judges used",
        "## Abstention summary",
        "## Provenance & reproducibility",
    ]:
        assert section in md, f"missing section: {section}"


def test_headline_includes_score_and_status_and_ci_bracket():
    md = render_methodology_md(_report())
    assert "87.0 / 100" in md
    assert "pilot_ready" in md
    # Bootstrap CI bracket
    assert "[84.5, 89.5]" in md


def test_provenance_shows_dataset_and_config_shas():
    md = render_methodology_md(_report())
    assert ("a" * 64) in md       # dataset SHA
    assert ("b" * 64) in md       # config SHA
    # Judge prompt SHAs (truncated to 8 chars)
    assert "correctness=cccccccc" in md


def test_replay_command_present_for_audit():
    md = render_methodology_md(_report())
    assert "mdk-eval replay" in md


# ---------------------------------------------------------------- profile section


def test_no_profile_renders_default_message():
    md = render_methodology_md(_report())
    assert "no profile" in md.lower() or "framework defaults" in md.lower()


def test_profile_renders_per_category_overrides_with_deltas():
    """When a profile is supplied, the doc shows per-category weight overrides
    with delta vs defaults."""
    profile = {
        "name": "faq_external", "label": "FAQ — External", "description": "Customer-facing.",
        "enabled_categories": ["task_success", "correctness", "grounding", "completeness",
                               "tool_usage", "workflow_adherence", "consistency", "latency",
                               "safety", "ux_tone"],
        "weights_resolved": {
            "task_success": 2.0, "correctness": 1.5, "grounding": 1.8,
            "completeness": 1.2, "tool_usage": 1.2, "workflow_adherence": 1.0,
            "consistency": 1.0, "latency": 0.8, "safety": 2.0, "ux_tone": 1.5,
        },
        "hard_gates_resolved": {"safety_threshold": 0.97, "critical_check_failure": True,
                                 "latency_on_high_severity": True},
        "pass_threshold_resolved": 75.0,
    }
    md = render_methodology_md(_report(), profile_settings=profile)
    assert "faq_external" in md
    assert "0.97" in md   # safety_threshold override
    # Delta for safety (1.5 default → 2.0 override) shown as +0.50
    assert "+0.50" in md


def test_profile_disabled_categories_marked_disabled():
    profile = {
        "name": "internal_tool", "label": "Internal", "description": "...",
        "enabled_categories": ["task_success", "correctness", "grounding", "completeness",
                               "tool_usage", "workflow_adherence", "consistency", "latency",
                               "safety"],   # ux_tone DISABLED
        "weights_resolved": {},
        "hard_gates_resolved": {},
    }
    md = render_methodology_md(_report(), profile_settings=profile)
    assert "ux_tone" in md
    # Look for the disabled marker on ux_tone's row
    assert "_disabled_" in md or "(disabled)" in md or "—  _(disabled)_" in md


# ---------------------------------------------------------------- abstention section


def test_no_abstentions_renders_clean_message():
    md = render_methodology_md(_report(with_abstentions=False))
    assert "No abstentions recorded" in md or "no abstentions" in md.lower()


def test_abstentions_aggregated_per_role():
    md = render_methodology_md(_report(with_abstentions=True))
    # Table header
    assert "Role | Verdicts seen | Abstained" in md or "| safety |" in md
    assert "safety" in md
    # Role-level abstention listing
    assert "ambiguous_safety_case" in md


def test_abstention_explanation_is_present():
    md = render_methodology_md(_report(with_abstentions=False))
    # The explanatory paragraph must be in the doc even when no abstentions
    # occurred — reviewers need to know what abstention means
    assert "Judges may abstain" in md
    assert "noisy 0.5" in md


# ---------------------------------------------------------------- judges section


def test_judge_section_lists_each_role_with_models():
    md = render_methodology_md(_report())
    assert "correctness" in md
    assert "openai:gpt-4o-mini" in md
    assert "anthropic:claude-haiku-4-5-20251001" in md


def test_judge_section_calls_out_meta_judge():
    md = render_methodology_md(_report())
    assert "Meta-judge" in md
    assert "claude-sonnet-4-6" in md


# ---------------------------------------------------------------- categories section


def test_each_of_10_categories_has_a_row():
    md = render_methodology_md(_report())
    for cat in [
        "task_success", "correctness", "grounding", "completeness",
        "tool_usage", "workflow_adherence", "consistency", "latency",
        "safety", "ux_tone",
    ]:
        assert cat in md


# ---------------------------------------------------------------- gate thresholds


def test_default_safety_threshold_in_doc():
    md = render_methodology_md(_report())
    assert "0.95" in md       # default safety_threshold


def test_profile_safety_threshold_override_in_doc():
    profile = {
        "name": "compliance_bot", "label": "Compliance Bot", "description": "...",
        "enabled_categories": list(["task_success", "correctness", "grounding", "completeness",
                                     "tool_usage", "workflow_adherence", "consistency", "latency",
                                     "safety", "ux_tone"]),
        "weights_resolved": {},
        "hard_gates_resolved": {"safety_threshold": 0.99},
        "pass_threshold_resolved": 75.0,
    }
    md = render_methodology_md(_report(), profile_settings=profile)
    assert "0.99" in md


# ---------------------------------------------------------------- write_methodology_md (file IO)


def test_write_methodology_md_creates_file_in_run_dir():
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        target = write_methodology_md(run_dir, _report())
        assert target == run_dir / "methodology.md"
        assert target.exists()
        body = target.read_text()
        assert "# Methodology" in body
        assert "87.0 / 100" in body


def test_write_methodology_idempotent_on_rerun():
    """Re-running the writer should overwrite cleanly without raising."""
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        write_methodology_md(run_dir, _report())
        write_methodology_md(run_dir, _report())   # must not raise
        assert (run_dir / "methodology.md").exists()

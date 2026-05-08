"""mdk-eval ab — diff + markdown rendering tests.

The orchestration layer (`run_ab`) is exercised by integration tests and the
existing run-orchestrator tests. Here we lock down the diff math and renderer
contract: per-scenario classification, headline, status counts, and the
verdict line a downstream PR-bot might parse.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mdk_eval.cli.ab import (
    ABDiff,
    build_diff,
    render_ab_markdown,
)
from mdk_eval.models import (
    Readiness,
    RunManifest,
    RunReport,
    ScenarioAggregate,
    Scorecard,
    Severity,
)


def _make_report(
    *,
    run_id: str,
    overall: float,
    status: Readiness,
    scorecard: dict,
    aggregates: list[dict],
) -> RunReport:
    """Build a minimal RunReport from positional dicts. Avoids creating
    on-disk runs."""
    manifest = RunManifest(
        run_id=run_id,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        target="mock",
        endpoint=None,
        runs_per_scenario=1,
        judges_enabled=[],
        judge_models={},
        meta_judge_model=None,
        arbitration_variance_threshold=0.2,
        dataset_path="dataset.jsonl",
        dataset_sha256="0" * 64,
        config_sha256="0" * 64,
        judge_prompts_sha256={},
        tool_versions={},
        mdk_eval_version="0.0.0",
    )
    aggs = [ScenarioAggregate(**{
        "scenario_id": a["id"],
        "runs": 1,
        "pass_rate": a.get("pass_rate", 1.0),
        "mean_score": a["mean_score"],
        "score_variance": 0.0,
        "drift_score": 0.0,
        "consistency_score": 100.0,
        "severity": Severity.MEDIUM,
        "findings": [],
    }) for a in aggregates]
    return RunReport(
        manifest=manifest,
        overall_score=overall,
        confidence=1.0,
        variance=0.0,
        status=status,
        scorecard=Scorecard(**scorecard),
        headline="",
        key_findings=[],
        recommendation="",
        scenario_aggregates=aggs,
        failure_clusters=[],
        risk_register=[],
        arbitration_stats={},
        deterministic_summary={},
    )


SCORECARD_FULL = {
    "task_success": 80, "correctness": 80, "grounding": 80,
    "completeness": 80, "tool_usage": 80, "workflow_adherence": 80,
    "consistency": 80, "latency": 80, "safety": 80, "ux_tone": 80,
    "overall": 80,
}


# ---------------------------------------------------------------- per-scenario classification


def test_regression_flagged_when_b_drops_more_than_band():
    a = _make_report(run_id="A", overall=85.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "s1", "mean_score": 90.0}])
    b = _make_report(run_id="B", overall=70.0, status=Readiness.NEEDS_IMPROVEMENT,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "s1", "mean_score": 70.0}])  # -20 → regression
    diff = build_diff(Path("/x"), a, Path("/y"), b)
    assert diff.scenarios[0].status == "REGRESSION"
    assert diff.scenarios[0].delta == -20.0
    assert diff.regressions == 1
    assert diff.improvements == 0


def test_improvement_flagged_when_b_climbs_more_than_band():
    a = _make_report(run_id="A", overall=70.0, status=Readiness.NEEDS_IMPROVEMENT,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "s1", "mean_score": 70.0}])
    b = _make_report(run_id="B", overall=85.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "s1", "mean_score": 90.0}])
    diff = build_diff(Path("/x"), a, Path("/y"), b)
    assert diff.scenarios[0].status == "improvement"
    assert diff.improvements == 1
    assert diff.regressions == 0


def test_stable_when_within_band():
    """Δ inside ±5 is 'stable' — within noise."""
    a = _make_report(run_id="A", overall=80.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "s1", "mean_score": 80.0}])
    b = _make_report(run_id="B", overall=82.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "s1", "mean_score": 83.0}])
    diff = build_diff(Path("/x"), a, Path("/y"), b)
    assert diff.scenarios[0].status == "stable"
    assert diff.regressions == 0
    assert diff.improvements == 0


def test_added_and_removed_scenarios_classified():
    a = _make_report(run_id="A", overall=80.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "only_in_a", "mean_score": 80.0}])
    b = _make_report(run_id="B", overall=80.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "only_in_b", "mean_score": 80.0}])
    diff = build_diff(Path("/x"), a, Path("/y"), b)
    by_id = {r.scenario_id: r for r in diff.scenarios}
    assert by_id["only_in_a"].status == "removed"
    assert by_id["only_in_b"].status == "added"
    assert diff.added == 1
    assert diff.removed == 1


def test_band_threshold_at_exactly_5_is_classified_strictly():
    """Δ == -5 is exactly on the boundary → REGRESSION (≥band, not >)."""
    a = _make_report(run_id="A", overall=80.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "s1", "mean_score": 80.0}])
    b = _make_report(run_id="B", overall=75.0, status=Readiness.NEEDS_IMPROVEMENT,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "s1", "mean_score": 75.0}])
    diff = build_diff(Path("/x"), a, Path("/y"), b)
    assert diff.scenarios[0].delta == -5.0
    assert diff.scenarios[0].status == "REGRESSION"


# ---------------------------------------------------------------- composite + scorecard


def test_overall_delta_sign_convention_b_minus_a():
    a = _make_report(run_id="A", overall=70.0, status=Readiness.NEEDS_IMPROVEMENT,
                     scorecard=SCORECARD_FULL,
                     aggregates=[])
    b = _make_report(run_id="B", overall=85.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL,
                     aggregates=[])
    diff = build_diff(Path("/x"), a, Path("/y"), b)
    assert diff.overall_delta == 15.0   # B is better → positive


def test_scorecard_delta_per_category():
    a = _make_report(run_id="A", overall=70.0, status=Readiness.NEEDS_IMPROVEMENT,
                     scorecard={**SCORECARD_FULL, "safety": 60, "grounding": 80},
                     aggregates=[])
    b = _make_report(run_id="B", overall=80.0, status=Readiness.PILOT_READY,
                     scorecard={**SCORECARD_FULL, "safety": 90, "grounding": 80},
                     aggregates=[])
    diff = build_diff(Path("/x"), a, Path("/y"), b)
    assert diff.scorecard_delta["safety"] == 30.0
    assert diff.scorecard_delta["grounding"] == 0.0


# ---------------------------------------------------------------- markdown renderer


def test_markdown_includes_verdict_line_for_regression():
    a = _make_report(run_id="A", overall=85.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "s1", "mean_score": 90.0}])
    b = _make_report(run_id="B", overall=70.0, status=Readiness.NEEDS_IMPROVEMENT,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "s1", "mean_score": 70.0}])
    diff = build_diff(Path("/x"), a, Path("/y"), b, label_a="prompt_v1", label_b="prompt_v2")
    md = render_ab_markdown(diff)
    assert "REGRESSION" in md
    assert "prompt_v1" in md
    assert "prompt_v2" in md
    assert "-15.0" in md     # composite delta in headline


def test_markdown_includes_no_clear_winner_when_within_band():
    a = _make_report(run_id="A", overall=80.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "s1", "mean_score": 80.0}])
    b = _make_report(run_id="B", overall=80.5, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "s1", "mean_score": 80.5}])
    md = render_ab_markdown(build_diff(Path("/x"), a, Path("/y"), b))
    assert "No clear winner" in md


def test_markdown_renders_per_scenario_table_in_stable_format():
    a = _make_report(run_id="A", overall=80.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "scenario_x", "mean_score": 80.0}])
    b = _make_report(run_id="B", overall=80.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL,
                     aggregates=[{"id": "scenario_x", "mean_score": 90.0}])
    md = render_ab_markdown(build_diff(Path("/x"), a, Path("/y"), b))
    # Scenario row appears
    assert "scenario_x" in md
    # Improvement gets the +10.0 delta
    assert "+10.0" in md
    # Per-scenario table header
    assert "| Scenario |" in md


def test_markdown_includes_provenance():
    a = _make_report(run_id="run-aaa", overall=80.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL, aggregates=[])
    b = _make_report(run_id="run-bbb", overall=80.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL, aggregates=[])
    md = render_ab_markdown(build_diff(Path("/path/to/A"), a, Path("/path/to/B"), b))
    assert "run-aaa" in md
    assert "run-bbb" in md
    assert "/path/to/A" in md
    assert "/path/to/B" in md


def test_diff_is_pydantic_serializable():
    """Critical for --json-out: the diff must round-trip cleanly through JSON."""
    import json as _json

    a = _make_report(run_id="A", overall=80.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL, aggregates=[{"id": "s1", "mean_score": 80.0}])
    b = _make_report(run_id="B", overall=85.0, status=Readiness.PILOT_READY,
                     scorecard=SCORECARD_FULL, aggregates=[{"id": "s1", "mean_score": 85.0}])
    diff = build_diff(Path("/x"), a, Path("/y"), b)
    blob = diff.model_dump(mode="json")
    s = _json.dumps(blob, default=str)
    parsed = _json.loads(s)
    # Round-trip back into the model
    rt = ABDiff(**parsed)
    assert rt.overall_delta == diff.overall_delta

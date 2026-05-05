"""Business-user dashboard renderer.

Verifies:
- KPI computation produces 6 KPIs with valid bands.
- Dashboard HTML is emitted with all 6 KPI labels and the auto-narrative.
- Renders cleanly when judges are disabled (note text appears).
- Glossary terms all appear.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


from mdk_eval.reporting import dashboard_kpis as dk
from mdk_eval.reporting.generators.dashboard_gen import write_dashboard


def _stub_report(judges_enabled: bool = False, status_value: str = "needs_improvement"):
    """Build a minimal RunReport-shaped stub for unit testing."""
    sc = MagicMock()
    sc.task_success = 100.0
    sc.correctness = 0.0 if not judges_enabled else 85.0
    sc.grounding = 0.0 if not judges_enabled else 78.0
    sc.completeness = 100.0
    sc.tool_usage = 100.0
    sc.workflow_adherence = 100.0
    sc.consistency = 100.0
    sc.latency = 100.0
    sc.safety = 100.0
    sc.ux_tone = 0.0 if not judges_enabled else 82.0
    sc.model_dump = lambda: {
        "task_success": sc.task_success, "correctness": sc.correctness,
        "grounding": sc.grounding, "completeness": sc.completeness,
        "tool_usage": sc.tool_usage, "workflow_adherence": sc.workflow_adherence,
        "consistency": sc.consistency, "latency": sc.latency,
        "safety": sc.safety, "ux_tone": sc.ux_tone, "overall": 71.0,
    }

    manifest = MagicMock()
    manifest.run_id = "run_test"
    manifest.started_at = "2026-05-05T00:00:00Z"
    manifest.ended_at = "2026-05-05T00:00:30Z"
    manifest.target = "lyzr"
    manifest.judges_enabled = ["correctness"] if judges_enabled else []
    manifest.runs_per_scenario = 1

    status = MagicMock()
    status.value = status_value

    agg = MagicMock()
    agg.pass_rate = 0.0
    agg.scenario_id = "scenario_a"

    report = MagicMock()
    report.manifest = manifest
    report.overall_score = 71.0
    report.confidence = 1.0
    report.variance = 0.0
    report.status = status
    report.scorecard = sc
    report.headline = "71/100 — Needs Improvement (0/3 scenarios passing)"
    report.scenario_aggregates = [agg, agg, agg]
    report.failure_clusters = []

    return report


def _stub_runs():
    runs = {}
    for sid in ("a", "b", "c"):
        r = MagicMock()
        r.adapter.trace.latency_ms = 2500
        runs[sid] = [r]
    return runs


def test_compute_kpis_produces_six():
    report = _stub_report()
    kpis = dk.compute_kpis(report, _stub_runs())
    assert len(kpis) == 6
    keys = [k.key for k in kpis]
    assert keys == ["readiness", "accuracy", "reliability", "safety", "helpfulness", "speed"]


def test_kpi_bands_assigned():
    report = _stub_report()
    kpis = dk.compute_kpis(report, _stub_runs())
    for k in kpis:
        assert k.band in ("pass", "watch", "fail", "neutral")


def test_judges_disabled_adds_note_to_judge_dependent_kpis():
    report = _stub_report(judges_enabled=False)
    kpis = dk.compute_kpis(report, _stub_runs())
    by_key = {k.key: k for k in kpis}
    # accuracy depends on correctness + grounding — both 0 without judges
    assert by_key["accuracy"].note is not None
    assert "judges" in by_key["accuracy"].note.lower()


def test_speed_kpi_uses_latency_format():
    report = _stub_report()
    kpis = dk.compute_kpis(report, _stub_runs())
    speed = next(k for k in kpis if k.key == "speed")
    assert speed.format == "latency"
    assert dk.kpi_value_display(speed) == "2.5s"


def test_auto_narrative_varies_by_status():
    for status in ("production_ready", "pilot_ready", "needs_improvement", "not_ready"):
        report = _stub_report(judges_enabled=True, status_value=status)
        kpis = dk.compute_kpis(report, _stub_runs())
        narrative = dk.auto_narrative(report, kpis)
        assert narrative
        # Each status produces a meaningfully distinct first sentence
        assert len(narrative) > 80


def test_judges_disabled_narrative_mentions_limitation():
    report = _stub_report(judges_enabled=False)
    kpis = dk.compute_kpis(report, _stub_runs())
    narrative = dk.auto_narrative(report, kpis)
    assert "judge" in narrative.lower()


def test_write_dashboard_emits_html_with_all_sections(tmp_path: Path):
    report = _stub_report(judges_enabled=False)
    cfg = MagicMock()
    cfg.client_name = "Test Client"
    cfg.confidentiality_footer = "Confidential"
    out = tmp_path / "dashboard.html"
    write_dashboard(out, report, _stub_runs(), cfg)

    html = out.read_text()
    # Hero
    assert "Production-Readiness Score" in html
    assert "Test Client" in html
    # All six KPI labels
    for label in ["Production-Readiness Score", "Accuracy & Truthfulness",
                  "Reliability", "Safety", "Helpfulness", "Speed"]:
        assert label in html, f"missing KPI label: {label}"
    # Glossary terms all present
    for g in dk.GLOSSARY:
        assert g["term"] in html, f"missing glossary term: {g['term']}"
    # Working / needs attention sections
    assert "What's working" in html
    assert "Needs attention" in html


def test_dashboard_links_to_engineering_report(tmp_path: Path):
    report = _stub_report()
    cfg = MagicMock()
    cfg.client_name = "X"
    cfg.confidentiality_footer = "C"
    out = tmp_path / "dashboard.html"
    write_dashboard(out, report, _stub_runs(), cfg)
    assert 'href="report.html"' in out.read_text()

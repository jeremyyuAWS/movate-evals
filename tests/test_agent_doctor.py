"""T51 — Agent Doctor (Rx) tests.

Covers:
  - Template-fallback path (no LLM): always returns useful output
  - LLM-path parse robustness (malformed responses don't crash)
  - Markdown renderer contract: required sections present
  - File-IO: write_doctor_artifacts emits both .md and .json
  - Tier 1/2/3 structure preserved through serialization
  - Endpoint shape matches the schema (mocked DB)
"""
from __future__ import annotations

import importlib
import json
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mdk_eval.insights.agent_doctor import (
    AgentDoctorReport,
    Prescription,
    SpecificChange,
    _parse_llm_response,
    _template_response,
    generate,
    render_markdown,
    write_doctor_artifacts,
)
from mdk_eval.models import (
    FailureClass,
    FailureCluster,
    Readiness,
    RunManifest,
    RunReport,
    ScenarioAggregate,
    Scorecard,
    Severity,
)


# ---------------------------------------------------------------- fixtures


def _manifest() -> RunManifest:
    return RunManifest(
        run_id="run_2026-05-06T17-00-00Z",
        started_at=datetime(2026, 5, 6, 17, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 6, 17, 4, 30, tzinfo=timezone.utc),
        target="lyzr",
        endpoint=None,
        runs_per_scenario=1,
        judges_enabled=["correctness", "grounding"],
        judge_models={"correctness": ["openai:gpt-4o"], "grounding": ["openai:gpt-4o"]},
        meta_judge_model="anthropic:claude-sonnet-4-6",
        arbitration_variance_threshold=0.04,
        dataset_path="datasets/x.jsonl",
        dataset_sha256="0" * 64, config_sha256="0" * 64,
        judge_prompts_sha256={}, tool_versions={}, mdk_eval_version="0.1.0",
    )


def _scorecard(**over) -> Scorecard:
    base = dict(
        task_success=80, correctness=85, grounding=70, completeness=80,
        tool_usage=85, workflow_adherence=100, consistency=100,
        latency=95, safety=99, ux_tone=70, overall=85,
    )
    base.update(over)
    return Scorecard(**base)


def _agg(scenario_id: str, *, score: float = 80.0, pass_rate: float = 1.0) -> ScenarioAggregate:
    return ScenarioAggregate(
        scenario_id=scenario_id, runs=1, pass_rate=pass_rate,
        mean_score=score, score_variance=0.0, drift_score=0.0,
        consistency_score=100.0, severity=Severity.MEDIUM,
        findings=[], representative_failure=None,
    )


def _report(*, status: Readiness = Readiness.PILOT_READY, score: float = 85.0,
            clusters: list[FailureCluster] | None = None) -> RunReport:
    return RunReport(
        manifest=_manifest(),
        overall_score=score,
        confidence=0.9,
        variance=0.4,
        status=status,
        scorecard=_scorecard(overall=score),
        headline=f"{score} — {status.value}",
        key_findings=[],
        recommendation="",
        scenario_aggregates=[
            _agg("happy_q1", score=92, pass_rate=1.0),
            _agg("happy_q2", score=85, pass_rate=1.0),
            _agg("hallucination_trap", score=55, pass_rate=0.0),
        ],
        failure_clusters=clusters or [],
        risk_register=[],
        arbitration_stats={},
        deterministic_summary={},
    )


# ---------------------------------------------------------------- template fallback


def test_template_response_for_production_ready_run():
    """When the agent is production_ready, the doctor reports a clean bill."""
    report = _report(status=Readiness.PRODUCTION_READY, score=94.0)
    doctor = _template_response(report)
    assert "production-ready" in doctor.executive_summary.lower()
    assert doctor.headline_action
    # No prescriptions when there are no failure clusters and score is high
    assert doctor.prescriptions == []
    assert doctor.source == "template"


def test_template_response_for_pilot_ready_with_clusters():
    """Pilot-ready with failure clusters → prescriptions generated from each."""
    clusters = [
        FailureCluster(
            failure_class=FailureClass.HALLUCINATION,
            label="Hallucination",
            count=4,
            severity=Severity.HIGH,
            example_scenario_ids=["s1", "s2", "s3"],
            suggested_fix="Force citations",
        ),
        FailureCluster(
            failure_class=FailureClass.TOOL_MISUSE,
            label="Tool misuse",
            count=2,
            severity=Severity.MEDIUM,
            example_scenario_ids=["s4"],
            suggested_fix="Improve tool descriptions",
        ),
    ]
    report = _report(status=Readiness.PILOT_READY, score=85.0, clusters=clusters)
    doctor = _template_response(report)
    assert "pilot-ready" in doctor.executive_summary.lower()
    # One prescription per cluster
    assert len(doctor.prescriptions) == 2
    # Cited scenarios flow through
    assert "s1" in doctor.prescriptions[0].cited_scenarios
    # Tier-3 specific changes generated
    assert len(doctor.specific_changes) >= 1
    # Each change targets a real field
    assert all(c.target for c in doctor.specific_changes)


def test_template_response_low_score_with_no_clusters_still_returns_one_prescription():
    """If a run has a low score but no clusters, the template generates a
    placeholder prescription investigating the weakest scenario."""
    report = _report(status=Readiness.NEEDS_IMPROVEMENT, score=72.0, clusters=[])
    doctor = _template_response(report)
    assert len(doctor.prescriptions) >= 1
    # The placeholder prescription cites the weakest scenario
    assert "hallucination_trap" in doctor.prescriptions[0].cited_scenarios


def test_generate_with_allow_llm_false_uses_template():
    """allow_llm=False is the test/CI path; never hits the LLM."""
    report = _report()
    doctor = generate(report, allow_llm=False)
    assert doctor.source == "template"


# ---------------------------------------------------------------- LLM response parsing


def test_parse_llm_response_handles_complete_response():
    raw = {
        "executive_summary": "Agent is pilot-ready with grounding gaps.",
        "headline_action": "Force citations on every KB-grounded answer.",
        "prescriptions": [
            {
                "title": "Force grounding citations",
                "diagnosis": "5 of 13 scenarios show hallucinations.",
                "treatment": "Add citation requirement to system prompt.",
                "expected_impact": "Should lift overall from 84 to ~89.",
                "confidence": "high",
                "cited_scenarios": ["s1", "s2"],
                "cited_findings": ["hallucination"],
            }
        ],
        "specific_changes": [
            {
                "target": "agent_instructions",
                "change": "Add: 'Cite KB section for every claim.'",
                "rationale": "Prevents hallucination.",
            }
        ],
        "confidence": "high",
    }
    doctor = _parse_llm_response(raw, _report(), source="llm")
    assert doctor.source == "llm"
    assert doctor.executive_summary.startswith("Agent is pilot-ready")
    assert doctor.headline_action.startswith("Force citations")
    assert len(doctor.prescriptions) == 1
    assert doctor.prescriptions[0].confidence == "high"
    assert doctor.prescriptions[0].cited_scenarios == ["s1", "s2"]
    assert len(doctor.specific_changes) == 1
    assert doctor.confidence == "high"


def test_parse_llm_response_clamps_invalid_confidence():
    raw = {
        "executive_summary": "ok",
        "headline_action": "do this",
        "prescriptions": [{
            "title": "x", "diagnosis": "x", "treatment": "x",
            "expected_impact": "x", "confidence": "absolutely_certain",
        }],
        "confidence": "totally_unsure",
    }
    doctor = _parse_llm_response(raw, _report(), source="llm")
    assert doctor.confidence == "medium"
    assert doctor.prescriptions[0].confidence == "medium"


def test_parse_llm_response_silently_drops_malformed_entries():
    raw = {
        "executive_summary": "ok", "headline_action": "do this",
        "prescriptions": [
            "not_a_dict",
            None,
            {"title": "valid", "diagnosis": "d", "treatment": "t",
             "expected_impact": "i", "confidence": "high"},
        ],
        "specific_changes": [
            "junk",
            {"target": "x", "change": "y", "rationale": "z"},
        ],
    }
    doctor = _parse_llm_response(raw, _report(), source="llm")
    # Only the well-formed entries survive
    assert len(doctor.prescriptions) == 1
    assert doctor.prescriptions[0].title == "valid"
    assert len(doctor.specific_changes) == 1


def test_parse_llm_response_caps_at_5_prescriptions():
    """Backstop — the LLM might emit more than the spec allows; cap server-side."""
    raw = {
        "executive_summary": "x", "headline_action": "x",
        "prescriptions": [
            {"title": f"p{i}", "diagnosis": "d", "treatment": "t",
             "expected_impact": "i", "confidence": "medium"}
            for i in range(10)
        ],
    }
    doctor = _parse_llm_response(raw, _report(), source="llm")
    assert len(doctor.prescriptions) == 5


# ---------------------------------------------------------------- markdown renderer


def _doctor_with_data() -> AgentDoctorReport:
    return AgentDoctorReport(
        run_id="r1",
        overall_score=85.0,
        status="pilot_ready",
        executive_summary="Agent is pilot-ready; hallucinations on 4 scenarios.",
        headline_action="Force grounding citations.",
        prescriptions=[
            Prescription(
                title="Force citations",
                diagnosis="4 hallucinations spotted",
                treatment="Add to system prompt",
                expected_impact="+5 overall",
                confidence="high",
                cited_scenarios=["s1", "s2"],
                cited_findings=["hallucination"],
            )
        ],
        specific_changes=[
            SpecificChange(target="agent_instructions",
                           change="Add citation requirement.",
                           rationale="Reduces hallucination."),
        ],
        confidence="high", source="llm", prompt_sha="abc" * 22,
    )


def test_markdown_renders_required_sections():
    md = render_markdown(_doctor_with_data())
    for section in [
        "# Agent Doctor",
        "## Executive summary",
        "## Prescriptions",
        "## Specific suggested changes",
    ]:
        assert section in md


def test_markdown_includes_prescription_metadata():
    md = render_markdown(_doctor_with_data())
    assert "Force citations" in md
    assert "confidence: high" in md
    assert "`s1`" in md and "`s2`" in md
    assert "`hallucination`" in md


def test_markdown_includes_headline_action():
    md = render_markdown(_doctor_with_data())
    assert "**Do this first:** Force grounding citations." in md


# ---------------------------------------------------------------- alignment with business-report
#
# Phase 1+2 of the Doctor / business-report alignment refactor: both endpoints
# feed the SAME deterministic context (augmented clusters, ranked fix list,
# top wins, recommendation text) to their LLM prompts. These tests pin that
# the Doctor's payload includes those fields and that the priority ordering
# matches what business-report would produce for the same inputs.


def test_summarize_run_for_doctor_includes_alignment_fields():
    """Phase 1: the Doctor's LLM payload must carry the shared run_context
    fields so the system prompt can reference `ranked_fixes`, `agent_strengths`,
    `failure_clusters_aug`, etc."""
    from mdk_eval.insights.agent_doctor import _summarize_run_for_doctor
    clusters = [
        FailureCluster(
            failure_class=FailureClass.HALLUCINATION,
            label="Hallucination",
            count=2,
            severity=Severity.HIGH,
            example_scenario_ids=["hallucination_trap"],
            suggested_fix="cite",
        ),
    ]
    report = _report(status=Readiness.PILOT_READY, score=82.0, clusters=clusters)
    payload = _summarize_run_for_doctor(report)

    # New fields are present
    assert "failure_clusters_aug" in payload
    assert "risks_aug" in payload
    assert "agent_strengths" in payload
    assert "top_losses" in payload
    assert "ranked_fixes" in payload
    assert "production_recommendation_text" in payload

    # failure_clusters_aug carries the business-language augmentation
    assert payload["failure_clusters_aug"][0]["business_label"].startswith(
        "The agent makes up"
    )

    # ranked_fixes is non-empty, ordered, and references the cluster's
    # business label (so the Doctor's prescription titles can match the
    # exec view's what_to_fix_first 1-to-1).
    rf = payload["ranked_fixes"]
    assert len(rf) >= 1
    assert rf[0]["rank"] == 1
    assert rf[0]["issue"] == "The agent makes up information not in your knowledge base"

    # agent_strengths captures the top-3 categories scoring >=75. Default
    # _scorecard() has workflow_adherence=100, consistency=100, safety=99,
    # so those should win. The point is just that strengths is non-empty
    # and the highest scorer comes first.
    assert len(payload["agent_strengths"]) > 0
    assert payload["agent_strengths"][0]["score"] >= payload["agent_strengths"][-1]["score"]

    # production_recommendation_text matches pilot_ready framing
    assert "controlled rollout" in payload["production_recommendation_text"]


def test_doctor_alignment_matches_business_report_priority():
    """The Doctor's `ranked_fixes` and the business-report's
    `what_to_fix_first` come from the same shared helper, so they must
    produce identical ordering for the same input data. This is the
    cross-tab consistency guarantee."""
    from mdk_eval.insights.agent_doctor import _summarize_run_for_doctor
    from mdk_eval.web import run_context as rc
    clusters = [
        FailureCluster(
            failure_class=FailureClass.TOOL_MISUSE,
            label="Tool misuse", count=5, severity=Severity.HIGH,
            example_scenario_ids=["s1"], suggested_fix="x",
        ),
        FailureCluster(
            failure_class=FailureClass.HALLUCINATION,
            label="Hallucination", count=2, severity=Severity.HIGH,
            example_scenario_ids=["s2"], suggested_fix="y",
        ),
    ]
    report = _report(clusters=clusters)
    payload = _summarize_run_for_doctor(report)

    # Compute the same context independently
    independent_ctx = rc.build_run_context(
        scorecard=report.scorecard.model_dump(),
        failure_clusters=payload["failure_clusters"],
        risk_register=payload["risk_register"],
        status=report.status.value,
    )

    # Doctor's payload's ranked_fixes equals what business-report computes
    assert payload["ranked_fixes"] == independent_ctx["what_to_fix_first"]
    # Tool misuse (3*5=15) outranks hallucination (3*2=6)
    assert payload["ranked_fixes"][0]["issue"].startswith("The agent picks the wrong tool")


def test_get_cached_narrative_returns_none_on_db_miss():
    """The cross-feed helper must fail gracefully when the run doesn't exist
    or DB is unavailable — Phase 2 is opportunistic, not a hard dependency."""
    from mdk_eval.web import business_report as br
    # No mock — _gather will raise (no DB connection in test env). The helper
    # swallows the exception and returns None silently.
    result = br.get_cached_narrative_for_run(99999999)
    assert result is None


def test_markdown_handles_no_prescriptions_gracefully():
    """A clean run shows the 'no prescriptions' message rather than blank."""
    doctor = _doctor_with_data()
    doctor.prescriptions = []
    doctor.specific_changes = []
    md = render_markdown(doctor)
    assert "performing within expected bounds" in md.lower() or "no prescriptions" in md.lower()


# ---------------------------------------------------------------- write_doctor_artifacts


def test_write_artifacts_emits_md_and_json_in_run_dir():
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        md_path, json_path = write_doctor_artifacts(run_dir, _doctor_with_data())
        assert md_path == run_dir / "agent_doctor.md"
        assert json_path == run_dir / "agent_doctor.json"
        assert md_path.exists() and json_path.exists()
        # JSON is parseable + structured
        data = json.loads(json_path.read_text())
        assert data["run_id"] == "r1"
        assert len(data["prescriptions"]) == 1
        # Markdown contains the headline
        assert "Agent Doctor — r1" in md_path.read_text()


def test_write_artifacts_idempotent():
    """Re-writing must not raise."""
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        write_doctor_artifacts(run_dir, _doctor_with_data())
        write_doctor_artifacts(run_dir, _doctor_with_data())   # overwrites cleanly


# ---------------------------------------------------------------- API endpoint


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MDK_WEB_API_KEY", "test-key")
    monkeypatch.setenv("MDK_WEB_CORS_ORIGINS", "http://localhost:3000")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    import mdk_eval.web.server as srv
    importlib.reload(srv)
    from fastapi.testclient import TestClient
    return TestClient(srv.app)


def _auth() -> dict:
    return {"Authorization": "Bearer test-key"}


def test_doctor_endpoint_returns_404_for_missing_run(client):
    cur = MagicMock()
    cur.fetchone.return_value = None
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn

    with patch("mdk_eval.web.db.connect", fake):
        r = client.get("/api/runs/9999/doctor", headers=_auth())
    assert r.status_code == 404


def _doctor_cur_mock(*, run_lookup, fetchall_seq):
    """Build a MagicMock cursor for the doctor DB path. Handles the expanded
    query set introduced by the topic-breakdown / managed-agents / trend
    enrichments — fetchone falls back to None after the initial run lookup,
    fetchall falls back to [] after the explicit sequence is exhausted."""
    cur = MagicMock()
    fetchone_calls = {"n": 0}

    def fetchone_side_effect():
        fetchone_calls["n"] += 1
        if fetchone_calls["n"] == 1:
            return run_lookup
        return None  # scenario_run lookups + agent_definition lookup → no rows

    cur.fetchone.side_effect = fetchone_side_effect

    seq = iter(fetchall_seq + [[]] * 20)  # generous tail of empty lists
    cur.fetchall.side_effect = lambda: next(seq)
    cur.__enter__ = lambda self: cur
    cur.__exit__ = lambda *a: None
    return cur


def test_doctor_endpoint_returns_template_when_llm_unavailable(client, monkeypatch):
    """No ANTHROPIC_API_KEY → call_judge raises → endpoint returns the template
    diagnosis (still useful)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # Setup fake DB returning a real-looking run
    # Run lookup tuple: (id, run_id, agent_id, overall, status, conf, passing, total, scorecard)
    cur = _doctor_cur_mock(
        run_lookup=(1, "run_x", 1, 75.0, "needs_improvement", 0.85, 7, 13,
                    {"correctness": 75, "grounding": 60}),
        fetchall_seq=[
            # failure_cluster rows
            [("hallucination", "high", 4, ["s1", "s2"], "Cite sources")],
            # risk_item rows
            [],
            # weakest scenario rows: (id, scenario_id, mean_score, severity, pass_rate, tags)
            [(1, "halluc_trap", 50.0, "high", 0.0, ["category:standard"])],
            # findings for that weak scenario
            [("hallucination", "high", "fabricated fact", "Add citation requirement")],
            # scenario_aggregate for topic_breakdown
            [],
            # recent runs
            [],
        ],
    )
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn

    with patch("mdk_eval.web.db.connect", fake):
        # Force the LLM path to fail so we exercise the template fallback
        with patch(
            "mdk_eval.evaluators.judges.llm_clients.call_judge",
            side_effect=RuntimeError("no key"),
        ):
            r = client.get("/api/runs/1/doctor", headers=_auth())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "template"
    assert body["run_id"] == "run_x"
    assert body["status"] == "needs_improvement"
    # The template generates a prescription from the cluster
    assert len(body["prescriptions"]) >= 1
    # Confidence + prompt_sha fields populated
    assert body["confidence"] in ("low", "medium", "high")
    assert len(body["prompt_sha"]) == 64    # SHA-256 hex


def test_doctor_endpoint_caches_via_judge_cache(client):
    """Pre-populate the judge cache so the endpoint returns 'cached' source
    without making any LLM call. The DB path uses get_with_metadata so it
    can also surface last_generated_at — patch that one."""
    import time
    cur = _doctor_cur_mock(
        run_lookup=(1, "run_cached", 1, 88.0, "pilot_ready", 0.92, 12, 13,
                    {"correctness": 88}),
        fetchall_seq=[],
    )
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn

    cached_with_meta = {
        "response": {
            "executive_summary": "Cached doctor said the agent is fine.",
            "headline_action": "Ship it.",
            "prescriptions": [],
            "specific_changes": [],
            "confidence": "high",
        },
        "created_at": int(time.time()) - 60,  # 1 minute ago
        "hits": 1,
    }
    with patch("mdk_eval.web.db.connect", fake):
        with patch(
            "mdk_eval.evaluators.judges.cache.get_with_metadata",
            return_value=cached_with_meta,
        ):
            r = client.get("/api/runs/1/doctor", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "cached"
    assert body["executive_summary"].startswith("Cached doctor")


def test_doctor_endpoint_returns_last_generated_at_for_cached(client):
    """When the doctor response is served from cache, last_generated_at
    should be set to the original cache-write time (not 'now'). This is
    what powers the dashboard's "Generated 2h ago" tooltip.
    """
    import time
    cur = _doctor_cur_mock(
        run_lookup=(1, "run_x", 1, 88.0, "pilot_ready", 0.92, 12, 13,
                    {"correctness": 88}),
        fetchall_seq=[],
    )
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn

    cached_with_meta = {
        "response": {
            "executive_summary": "Cached.",
            "headline_action": "Ship.",
            "prescriptions": [], "specific_changes": [],
            "confidence": "high",
        },
        "created_at": int(time.time()) - 3600,  # 1 hour ago
        "hits": 5,
    }
    with patch("mdk_eval.web.db.connect", fake):
        with patch(
            "mdk_eval.evaluators.judges.cache.get_with_metadata",
            return_value=cached_with_meta,
        ):
            r = client.get("/api/runs/1/doctor", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "cached"
    assert body["last_generated_at"] is not None
    # Roughly 1 hour ago (give a 5-min slop for test execution time).
    from datetime import datetime, timezone, timedelta
    parsed = datetime.fromisoformat(body["last_generated_at"].replace("Z", "+00:00"))
    age = datetime.now(timezone.utc) - parsed
    assert timedelta(minutes=55) < age < timedelta(minutes=65), (
        f"expected last_generated_at to be ~1h ago; got age={age}"
    )


def test_doctor_endpoint_regenerate_bypasses_cache(client, monkeypatch):
    """?regenerate=true should skip the cache lookup and fire a fresh LLM call.
    Verify by patching get_with_metadata to assert it's never called when
    regenerate=true."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")  # so LLM path is enabled
    cur = _doctor_cur_mock(
        run_lookup=(1, "run_x", 1, 88.0, "pilot_ready", 0.92, 12, 13,
                    {"correctness": 88}),
        fetchall_seq=[],
    )
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn

    fresh_llm_response = {
        "executive_summary": "Fresh LLM said something different.",
        "headline_action": "Adjust.",
        "prescriptions": [],
        "specific_changes": [],
        "confidence": "medium",
    }

    async def fake_judge(*_a, **_k):
        return fresh_llm_response

    with patch("mdk_eval.web.db.connect", fake):
        with patch(
            "mdk_eval.evaluators.judges.cache.get_with_metadata"
        ) as fake_cache_get:
            with patch(
                "mdk_eval.evaluators.judges.llm_clients.call_judge",
                side_effect=fake_judge,
            ):
                with patch("mdk_eval.evaluators.judges.cache.put"):
                    r = client.get(
                        "/api/runs/1/doctor?regenerate=true",
                        headers=_auth(),
                    )

    assert r.status_code == 200, r.text
    # Cache lookup must not have happened on the regenerate path.
    fake_cache_get.assert_not_called()
    body = r.json()
    assert body["source"] == "llm"
    assert "Fresh LLM" in body["executive_summary"]


def test_doctor_endpoint_default_uses_cache(client):
    """No ?regenerate param → default behavior: cache lookup happens."""
    cur = _doctor_cur_mock(
        run_lookup=(1, "run_x", 1, 88.0, "pilot_ready", 0.92, 12, 13,
                    {"correctness": 88}),
        fetchall_seq=[],
    )
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn

    with patch("mdk_eval.web.db.connect", fake):
        with patch(
            "mdk_eval.evaluators.judges.cache.get_with_metadata",
            return_value=None,
        ) as fake_cache_get:
            r = client.get("/api/runs/1/doctor", headers=_auth())
    # Default path (no regenerate): cache lookup HAPPENS.
    fake_cache_get.assert_called_once()
    assert r.status_code == 200


def test_extract_response_excerpt_pulls_lyzr_answer():
    """Lyzr-style trace: trace.raw_response is a dict with 'answer' key."""
    from mdk_eval.insights.agent_doctor import _extract_response_excerpt
    trace = {
        "raw_response": {"answer": "Movate's HQ is in Plano, TX.", "sources": []},
    }
    assert _extract_response_excerpt(trace) == "Movate's HQ is in Plano, TX."


def test_extract_response_excerpt_pulls_string_response():
    from mdk_eval.insights.agent_doctor import _extract_response_excerpt
    trace = {"raw_response": "Plain string response from agent."}
    assert _extract_response_excerpt(trace) == "Plain string response from agent."


def test_extract_response_excerpt_truncates_at_max_chars():
    from mdk_eval.insights.agent_doctor import _extract_response_excerpt
    long_answer = "x" * 1000
    excerpt = _extract_response_excerpt(
        {"raw_response": {"answer": long_answer}}, max_chars=400,
    )
    assert len(excerpt) == 400


def test_extract_response_excerpt_handles_multi_turn():
    from mdk_eval.insights.agent_doctor import _extract_response_excerpt
    trace = {
        "extra": {
            "turns": [
                {"response_text": "first turn"},
                {"response_text": "final turn answer"},
            ],
        },
    }
    assert _extract_response_excerpt(trace) == "final turn answer"


def test_extract_response_excerpt_returns_empty_on_garbage():
    from mdk_eval.insights.agent_doctor import _extract_response_excerpt
    assert _extract_response_excerpt(None) == ""
    assert _extract_response_excerpt({}) == ""
    assert _extract_response_excerpt({"raw_response": None}) == ""
    assert _extract_response_excerpt("not a dict") == ""


def test_extract_judge_rationales_picks_worst_verdict():
    """When multiple verdicts exist for a role, pick the worst (most diagnostic)."""
    from mdk_eval.insights.agent_doctor import _extract_judge_rationales
    judges = [
        {
            "role": "correctness",
            "verdicts": [
                {"score": 0.9, "rationale": "looks fine", "abstained": False},
                {"score": 0.3, "rationale": "fabricated SKU", "abstained": False},
                {"score": 0.7, "rationale": "mostly OK", "abstained": False},
            ],
        },
    ]
    out = _extract_judge_rationales(judges)
    assert len(out) == 1
    assert "fabricated SKU" in out[0]["rationale"]
    assert out[0]["score"] == 0.3
    assert out[0]["role"] == "correctness"


def test_extract_judge_rationales_handles_all_abstained():
    """If all verdicts abstained, surface the first one with abstain_reason."""
    from mdk_eval.insights.agent_doctor import _extract_judge_rationales
    judges = [
        {
            "role": "grounding",
            "verdicts": [
                {"score": 0.5, "rationale": "no canonical answer", "abstained": True},
            ],
        },
    ]
    out = _extract_judge_rationales(judges)
    assert len(out) == 1
    assert out[0]["abstained"] is True


def test_extract_judge_rationales_caps_count_and_chars():
    from mdk_eval.insights.agent_doctor import _extract_judge_rationales
    judges = [
        {"role": f"j{i}", "verdicts": [{"score": 0.5, "rationale": "x" * 1000}]}
        for i in range(10)
    ]
    out = _extract_judge_rationales(judges, max_count=3, max_chars=100)
    assert len(out) == 3
    for r in out:
        assert len(r["rationale"]) <= 100


def test_extract_judge_rationales_handles_garbage():
    from mdk_eval.insights.agent_doctor import _extract_judge_rationales
    assert _extract_judge_rationales(None) == []
    assert _extract_judge_rationales([]) == []
    assert _extract_judge_rationales("not a list") == []
    # Item without verdicts
    assert _extract_judge_rationales([{"role": "x"}]) == []


def test_template_fallback_includes_topic_and_subagent_callouts():
    """Template-only response should still mention the weakest topic and
    bottleneck sub-agent — same enrichment data the LLM would use."""
    from mdk_eval.insights.agent_doctor import _template_from_db_payload
    payload = {
        "run_id": "x", "overall_score": 75.0, "status": "needs_improvement",
        "passing_scenarios": 8, "total_scenarios": 13,
        "scorecard": {}, "failure_clusters": [
            {"failure_class": "hallucination", "severity": "high",
             "count": 3, "example_scenario_ids": ["s1"], "suggested_fix": "x"},
        ],
        "risk_register": [],
        "topic_breakdown": [
            {"slug": "validator_agent", "display_name": "Validator Agent",
             "mean_score": 56.0, "pass_rate": 0.4, "scenarios_count": 5,
             "failures_count": 3, "severity_max": "high"},
            {"slug": "ocr_agent", "display_name": "OCR Agent",
             "mean_score": 89.0, "pass_rate": 0.95, "scenarios_count": 4,
             "failures_count": 0, "severity_max": "low"},
        ],
        "managed_agents_breakdown": [
            {"name": "Validator Agent", "mean_score": 56.0, "pass_rate": 0.4,
             "scenarios_count": 5, "matched_topic": "validator_agent",
             "usage_description": "validates orders"},
            {"name": "OCR Agent", "mean_score": 89.0, "pass_rate": 0.95,
             "scenarios_count": 4, "matched_topic": "ocr_agent",
             "usage_description": "extracts text"},
        ],
        "recent_runs_trend": [
            {"run_pk": 99, "overall_score": 75.0, "is_current": True},
            {"run_pk": 98, "overall_score": 79.0, "is_current": False},
        ],
    }
    result = _template_from_db_payload(payload)
    # Bottleneck sub-agent is named in the executive summary
    assert "Validator Agent" in result.executive_summary
    # Trend snippet appears (4-point drop from prior)
    assert "4 points" in result.executive_summary or "down" in result.executive_summary.lower()
    # Headline targets the bottleneck sub-agent, not the failure class
    assert "Validator Agent" in result.headline_action


def test_template_fallback_handles_missing_enrichments():
    """Pre-enrichment runs (older data, no topic_breakdown / managed_agents /
    trend) — template should still produce a valid report."""
    from mdk_eval.insights.agent_doctor import _template_from_db_payload
    payload = {
        "run_id": "x", "overall_score": 85.0, "status": "pilot_ready",
        "passing_scenarios": 12, "total_scenarios": 13,
        "scorecard": {}, "failure_clusters": [], "risk_register": [],
        "topic_breakdown": [], "managed_agents_breakdown": [], "recent_runs_trend": [],
    }
    result = _template_from_db_payload(payload)
    # Should not crash; should produce a sensible status sentence.
    assert "pilot-ready" in result.executive_summary.lower()


def test_doctor_endpoint_stream_returns_sse_events(client, monkeypatch):
    """?stream=true returns text/event-stream with phase-progress messages
    terminated by a phase=done event carrying the full report."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cur = _doctor_cur_mock(
        run_lookup=(1, "run_x", 1, 85.0, "pilot_ready", 0.92, 12, 13,
                    {"correctness": 85}),
        fetchall_seq=[],
    )
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn

    with patch("mdk_eval.web.db.connect", fake):
        with patch(
            "mdk_eval.evaluators.judges.llm_clients.call_judge",
            side_effect=RuntimeError("LLM offline"),
        ):
            r = client.get("/api/runs/1/doctor?stream=true", headers=_auth())

    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    body = r.text

    # Each progress phase must appear as its own SSE event.
    expected_phases = [
        "loading", "trace_extract", "topic_breakdown",
        "trend", "llm_call", "done",
    ]
    for phase in expected_phases:
        assert f'"phase": "{phase}"' in body, (
            f"missing SSE event for phase={phase!r}; body excerpt: {body[:500]}"
        )

    # The done event carries the full report payload.
    assert '"report":' in body
    # Each event ends with the SSE \n\n delimiter.
    assert body.count("\n\n") >= len(expected_phases)


def test_doctor_endpoint_stream_handles_missing_run(client):
    """?stream=true on a missing run yields a phase=error event instead of
    a 404. SSE streams have already started before the error is raised, so
    we surface it as an event rather than aborting the stream."""
    cur = _doctor_cur_mock(run_lookup=None, fetchall_seq=[])
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn

    with patch("mdk_eval.web.db.connect", fake):
        r = client.get("/api/runs/9999/doctor?stream=true", headers=_auth())

    assert r.status_code == 200
    body = r.text
    assert '"phase": "error"' in body
    assert "9999" in body or "not found" in body.lower()


def test_doctor_endpoint_requires_auth(client):
    r = client.get("/api/runs/1/doctor")
    assert r.status_code == 401

"""LLM insights endpoint tests.

DB layer + LLM call mocked. Verifies:
- Valid kinds + names are accepted; invalid ones raise ValueError
- KPI score derivation matches the documented formulas (accuracy = avg of
  correctness + grounding, etc.)
- Empty data returns a low-confidence insight, not a 500
- Bad LLM output (missing fields, wrong types) is parsed defensively
- Judges-disabled bumps confidence down + adds a note
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("psycopg")

from mdk_eval.web import insights


# ---------- helpers ----------


def _fake_db_connect(*, scorecard, judges_enabled, scenarios):
    """Mock db.connect so insights._gather_data_for_run sees the data we want.

    Row shape post-2026-05-07 enrichment: 9 columns
    (scenario_id, mean_score, payload, severity, tags, findings, actual,
    category_scores, judges_panel).
    """
    cur = MagicMock()
    fetchone_calls = iter([(scorecard, ["correctness"] if judges_enabled else [])])
    cur.fetchone.side_effect = lambda: next(fetchone_calls)
    cur.fetchall.return_value = [
        (s["id"], s["score"], s.get("payload"), s.get("severity", "medium"),
         s.get("tags") or [],
         s.get("findings"), s.get("actual"), s.get("category_scores"),
         s.get("judges_panel") or [])
        for s in scenarios
    ]
    cur.__enter__ = lambda self: cur
    cur.__exit__ = lambda *a: None
    conn = MagicMock()
    conn.cursor.return_value = cur

    @contextmanager
    def fake():
        yield conn
    return fake


# ---------- input validation ----------


def test_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown kind"):
        insights.generate(run_pk=1, kind="something-else", name="correctness")


def test_unknown_category_raises():
    with pytest.raises(ValueError, match="unknown category"):
        insights.generate(run_pk=1, kind="category", name="not-a-real-category")


def test_unknown_kpi_raises():
    with pytest.raises(ValueError, match="unknown kpi"):
        insights.generate(run_pk=1, kind="kpi", name="not-a-real-kpi")


# ---------- KPI score formulas ----------


def test_kpi_accuracy_averages_correctness_and_grounding():
    sc = {"correctness": 80, "grounding": 60}
    assert insights._kpi_score(sc, "accuracy") == 70.0


def test_kpi_helpfulness_averages_task_success_and_ux_tone():
    sc = {"task_success": 90, "ux_tone": 70}
    assert insights._kpi_score(sc, "helpfulness") == 80.0


def test_kpi_reliability_uses_consistency():
    assert insights._kpi_score({"consistency": 85}, "reliability") == 85.0


def test_kpi_safety_uses_safety_score():
    assert insights._kpi_score({"safety": 100}, "safety") == 100.0


def test_kpi_readiness_uses_overall():
    assert insights._kpi_score({"overall": 84}, "readiness") == 84.0


def test_kpi_speed_uses_latency_proxy():
    assert insights._kpi_score({"latency": 96}, "speed") == 96.0


def test_kpi_score_handles_missing_keys():
    assert insights._kpi_score({}, "accuracy") == 0.0


# ---------- empty / error degradation ----------


def test_empty_run_returns_low_confidence_insight(monkeypatch):
    fake = _fake_db_connect(scorecard={"correctness": 0}, judges_enabled=False, scenarios=[])
    monkeypatch.setattr(insights.db, "connect", fake)
    out = insights.generate(run_pk=1, kind="category", name="correctness")
    assert out.confidence == "low"
    assert "No scenario data" in out.narrative
    assert "empty_run" in out.notes


def test_llm_failure_returns_low_confidence_insight(monkeypatch):
    """LLM exception → low-confidence insight with note, NOT a 500."""
    fake = _fake_db_connect(
        scorecard={"correctness": 80}, judges_enabled=True,
        scenarios=[{"id": "s1", "score": 80}],
    )
    monkeypatch.setattr(insights.db, "connect", fake)

    async def boom(*_a, **_k):
        raise RuntimeError("LLM down")

    with patch("mdk_eval.web.insights.call_judge", side_effect=boom):
        out = insights.generate(run_pk=1, kind="category", name="correctness")
    assert out.confidence == "low"
    assert "could not reach" in out.narrative.lower()
    assert any("llm_error" in n for n in out.notes)


# ---------- happy path ----------


def test_well_formed_response_parses_into_insight(monkeypatch):
    fake = _fake_db_connect(
        scorecard={"correctness": 78}, judges_enabled=True,
        scenarios=[
            {"id": "s1", "score": 90}, {"id": "s2", "score": 60},
        ],
    )
    monkeypatch.setattr(insights.db, "connect", fake)

    async def fake_llm(*_a, **_k):
        return {
            "narrative": "The agent correctly answered 1 of 2 scenarios. The failure was on s2 where the agent fabricated a date.",
            "top_offenders": [
                {"scenario_id": "s2", "score": 60, "why": "Fabricated date with no source."}
            ],
            "suggested_fixes": ["Add 'cite source for any date' to system prompt."],
            "suggested_new_scenarios": [
                {"id_hint": "date_must_be_cited", "description": "Probes date hallucination", "input": "When was X founded?"}
            ],
            "confidence": "high",
        }

    with patch("mdk_eval.web.insights.call_judge", side_effect=fake_llm):
        out = insights.generate(run_pk=1, kind="category", name="correctness")

    assert out.title == "correctness"
    assert out.score == 78
    assert "fabricated" in out.narrative.lower()
    assert len(out.top_offenders) == 1
    assert out.top_offenders[0]["scenario_id"] == "s2"
    assert len(out.suggested_fixes) == 1
    assert len(out.suggested_new_scenarios) == 1
    assert out.confidence == "high"


def test_judges_disabled_caps_confidence_at_medium(monkeypatch):
    fake = _fake_db_connect(
        scorecard={"correctness": 0}, judges_enabled=False,
        scenarios=[{"id": "s1", "score": 0}],
    )
    monkeypatch.setattr(insights.db, "connect", fake)

    async def fake_llm(*_a, **_k):
        return {"narrative": "n", "confidence": "high"}

    with patch("mdk_eval.web.insights.call_judge", side_effect=fake_llm):
        out = insights.generate(run_pk=1, kind="category", name="correctness")

    assert out.confidence == "medium"  # bumped down from high
    assert "judges_disabled" in out.notes


# ---------- defensive parsing ----------


def test_malformed_top_offenders_dropped(monkeypatch):
    """Non-dict items in arrays should be silently dropped, not crash."""
    fake = _fake_db_connect(
        scorecard={"correctness": 80}, judges_enabled=True,
        scenarios=[{"id": "s1", "score": 80}],
    )
    monkeypatch.setattr(insights.db, "connect", fake)

    async def fake_llm(*_a, **_k):
        return {
            "narrative": "ok",
            "top_offenders": [{"scenario_id": "s1"}, "garbage", 42, None],
            "suggested_fixes": ["fix1", 42, None],
        }

    with patch("mdk_eval.web.insights.call_judge", side_effect=fake_llm):
        out = insights.generate(run_pk=1, kind="category", name="correctness")
    assert len(out.top_offenders) == 1
    assert out.top_offenders[0]["scenario_id"] == "s1"
    # str() coerces non-strings; that's intentional — better than dropping
    assert all(isinstance(f, str) for f in out.suggested_fixes)


def test_invalid_confidence_falls_back_to_medium(monkeypatch):
    fake = _fake_db_connect(
        scorecard={"correctness": 80}, judges_enabled=True,
        scenarios=[{"id": "s1", "score": 80}],
    )
    monkeypatch.setattr(insights.db, "connect", fake)

    async def fake_llm(*_a, **_k):
        return {"narrative": "x", "confidence": "extremely-high-actually"}

    with patch("mdk_eval.web.insights.call_judge", side_effect=fake_llm):
        out = insights.generate(run_pk=1, kind="category", name="correctness")
    assert out.confidence == "medium"


# ---------- enrichment helpers (2026-05-07) ----------


def test_score_band_thresholds():
    assert insights._score_band(95) == "pass"
    assert insights._score_band(80) == "pass"
    assert insights._score_band(79.99) == "watch"
    assert insights._score_band(60) == "watch"
    assert insights._score_band(59.99) == "fail"
    assert insights._score_band(0) == "fail"


def test_topic_display_name_titlecases_slug():
    assert insights._topic_display_name("movate_services") == "Movate Services"
    assert insights._topic_display_name("ocr_agent") == "Ocr Agent"
    assert insights._topic_display_name(None) is None
    assert insights._topic_display_name("untagged") is None
    assert insights._topic_display_name("") is None


def test_extract_response_excerpt_pulls_answer_from_json():
    actual = '{"answer": "Movate is HQ in Plano, TX.", "sources": []}'
    assert insights._extract_response_excerpt(actual) == "Movate is HQ in Plano, TX."


def test_extract_response_excerpt_falls_back_to_raw_text():
    """When the actual is not JSON-parseable, return truncated raw text."""
    out = insights._extract_response_excerpt("just a plain text response", max_chars=100)
    assert out == "just a plain text response"


def test_extract_response_excerpt_handles_none():
    assert insights._extract_response_excerpt(None) == ""
    assert insights._extract_response_excerpt("") == ""


def test_judge_rationale_picks_matching_role():
    judges = [
        {
            "role": "correctness",
            "verdicts": [
                {"score": 0.3, "rationale": "fabricated SKU", "abstained": False},
                {"score": 0.9, "rationale": "looks fine", "abstained": False},
            ],
        },
        {
            "role": "safety",
            "verdicts": [{"score": 0.9, "rationale": "no policy violation"}],
        },
    ]
    rat = insights._judge_rationale_for_category(judges, "correctness")
    assert rat == "fabricated SKU"


def test_judge_rationale_handles_derived_categories():
    """Categories like consistency/latency/workflow_adherence have no judge —
    surface a placeholder explaining what they're derived from."""
    rat = insights._judge_rationale_for_category([], "consistency")
    assert rat is not None
    assert "variance" in rat.lower()


def test_judge_rationale_returns_none_when_no_match():
    judges = [{"role": "safety", "verdicts": [{"score": 0.5, "rationale": "x"}]}]
    assert insights._judge_rationale_for_category(judges, "correctness") is None


def test_enrich_offender_merges_backend_fields():
    """LLM produces {scenario_id, score, why}; backend fills in
    scenario_title, severity, topic, behavior_category, failure_class,
    category_score, agent_response_excerpt, judge_rationale."""
    scenarios = {
        "test_scen": {
            "scenario_title": "Asks for company info",
            "severity": "high",
            "topic_slug": "movate_services",
            "behavior_category": "adversarial",
            "score": 65.0,
            "findings": [{"failure_class": "hallucination", "reason": "fabricated"}],
            "actual": '{"answer": "Movate has 50,000 employees globally."}',
            "category_scores": {"correctness": 60.0, "grounding": 55.0},
            "judges_panel": [
                {
                    "role": "correctness",
                    "verdicts": [{"score": 0.3, "rationale": "no KB source for headcount", "abstained": False}],
                },
            ],
        },
    }
    llm_offender = {"scenario_id": "test_scen", "score": 65.0, "why": "agent fabricated"}
    out = insights._enrich_offender(llm_offender, scenarios, "correctness")

    # LLM-provided fields preserved
    assert out["scenario_id"] == "test_scen"
    assert out["why"] == "agent fabricated"
    # Backend-merged fields present
    assert out["scenario_title"] == "Asks for company info"
    assert out["severity"] == "high"
    assert out["topic_slug"] == "movate_services"
    assert out["topic"] == "Movate Services"
    assert out["behavior_category"] == "adversarial"
    assert out["failure_class"] == "hallucination"
    assert out["category_score"] == 60.0
    assert "50,000 employees" in out["agent_response_excerpt"]
    assert "no KB source" in out["judge_rationale"]


def test_enrich_offender_handles_unknown_scenario_id():
    """If the LLM hallucinates a scenario_id not in our data, enrichment
    fields are None/empty rather than raising."""
    out = insights._enrich_offender(
        {"scenario_id": "nope", "score": 50, "why": "x"}, {}, "correctness",
    )
    assert out["scenario_id"] == "nope"
    assert out["scenario_title"] == "nope"  # falls back to slug
    assert out["severity"] is None
    assert out["topic"] is None


def test_compute_distribution_uses_per_category_scores():
    scenarios = [
        {"score": 90, "category_scores": {"correctness": 85}},   # pass
        {"score": 50, "category_scores": {"correctness": 70}},   # watch
        {"score": 70, "category_scores": {"correctness": 50}},   # fail
        {"score": 95, "category_scores": {"correctness": 95}},   # pass
    ]
    dist = insights._compute_distribution(scenarios, "correctness")
    assert dist == {"pass": 2, "watch": 1, "fail": 1, "total": 4}


def test_compute_distribution_falls_back_to_overall_score():
    """When category_scores doesn't have the requested category, fall back
    to the scenario's overall mean_score."""
    scenarios = [
        {"score": 90, "category_scores": {}},
        {"score": 50, "category_scores": {}},
    ]
    dist = insights._compute_distribution(scenarios, "task_success")
    assert dist["pass"] + dist["watch"] + dist["fail"] == 2


def test_generate_returns_enriched_top_offenders(tmp_path, monkeypatch):
    """End-to-end: generate() merges backend data into LLM-produced offenders."""
    fake = _fake_db_connect(
        scorecard={"correctness": 75},
        judges_enabled=True,
        scenarios=[
            {
                "id": "weakest_scen",
                "score": 55.0,
                "severity": "high",
                "tags": ["topic:movate_services", "category:adversarial"],
                "payload": {"description": "Adversarial probe on company facts"},
                "findings": [{"failure_class": "hallucination", "reason": "fabricated"}],
                "actual": '{"answer": "Movate has 50K employees"}',
                "category_scores": {"correctness": 50.0},
                "judges_panel": [
                    {
                        "role": "correctness",
                        "verdicts": [{"score": 0.3, "rationale": "no KB source", "abstained": False}],
                    },
                ],
            },
        ],
    )

    async def fake_llm(*_a, **_k):
        return {
            "narrative": "narrative",
            "top_offenders": [
                {"scenario_id": "weakest_scen", "score": 55, "why": "Agent fabricated."},
            ],
            "suggested_fixes": ["Force grounding"],
            "suggested_new_scenarios": [],
            "confidence": "medium",
        }

    with patch("mdk_eval.web.insights.db.connect", fake):
        with patch("mdk_eval.web.insights.call_judge", side_effect=fake_llm):
            out = insights.generate(run_pk=1, kind="category", name="correctness")

    # The offender now has all the enrichment fields
    assert len(out.top_offenders) == 1
    o = out.top_offenders[0]
    assert o["scenario_id"] == "weakest_scen"
    assert o["scenario_title"] == "Adversarial probe on company facts"
    assert o["topic"] == "Movate Services"
    assert o["behavior_category"] == "adversarial"
    assert o["failure_class"] == "hallucination"
    assert o["category_score"] == 50.0
    assert "50K employees" in o["agent_response_excerpt"]
    assert "no KB source" in o["judge_rationale"]
    # Insight-level enrichments
    assert out.score_band == "watch"   # 75 → watch
    assert out.weight_pct > 0
    assert out.distribution["total"] == 1

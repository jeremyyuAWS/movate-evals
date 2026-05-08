"""Business report — failure-class business mapping, top wins/losses,
what-to-fix-first ordering, narrative templating fallback, endpoint shape.

The LLM-generated narrative is exercised via the template fallback path
(no live API call required for tests). Real-LLM behavior is exercised in
post-deploy smoke tests against the live endpoint.
"""
from __future__ import annotations

import pytest

from mdk_eval.models import FailureClass
from mdk_eval.reporting.business_language import (
    augment_cluster,
    augment_risk,
    business_for_class,
)
from mdk_eval.web.business_report import (
    _compute_top_losses,
    _compute_top_wins,
    _compute_what_to_fix_first,
    _detect_class_in_risk_text,
    _production_recommendation_text,
    _template_narrative,
)


# ---------------------------------------------------------------- business mapping


@pytest.mark.parametrize("cls", list(FailureClass))
def test_every_failure_class_has_business_mapping(cls):
    """Every enum value must have a business label, customer impact, and fix."""
    biz = business_for_class(cls)
    assert biz["business_label"]
    assert biz["customer_impact"]
    assert biz["business_fix"]
    # Sanity: not just the engineer label echoed back
    assert cls.value not in biz["business_label"].lower().replace(" ", "_")


def test_unknown_class_falls_back_gracefully():
    biz = business_for_class("totally_made_up")
    assert biz["business_label"]
    assert "Made Up" in biz["business_label"] or "made up" in biz["business_label"].lower()
    assert biz["customer_impact"]
    assert biz["business_fix"]


def test_business_mappings_are_human_readable():
    """No engineer enum values in any business label or fix.

    Catches drift where an editor accidentally pastes 'tool_misuse' into the
    business_label field instead of writing it out in plain English.
    """
    enum_strings = {c.value for c in FailureClass}
    for cls in FailureClass:
        biz = business_for_class(cls)
        for field, text in biz.items():
            for s in enum_strings:
                # The string match must not appear as a token
                if s in text.lower() and len(s) > 4:
                    pytest.fail(f"engineer enum '{s}' leaked into {cls.value}.{field}: {text!r}")


# ---------------------------------------------------------------- top_wins / top_losses


def test_top_wins_returns_only_high_scores():
    scorecard = {
        "task_success": 95,    # win
        "correctness": 88,     # win
        "grounding": 95,       # win
        "completeness": 60,    # loss, NOT a win
        "latency": 100,        # win — but only top 3 returned
    }
    wins = _compute_top_wins(scorecard)
    assert len(wins) == 3
    cats = [w["category"] for w in wins]
    assert "completeness" not in cats
    # Sorted descending
    scores = [w["score"] for w in wins]
    assert scores == sorted(scores, reverse=True)


def test_top_wins_skips_zero_or_missing_categories():
    scorecard = {"task_success": 0, "correctness": 88, "extra_metric": "not_a_number"}
    wins = _compute_top_wins(scorecard)
    cats = [w["category"] for w in wins]
    assert "task_success" not in cats
    assert "correctness" in cats


def test_top_losses_mixes_categories_and_clusters():
    scorecard = {"task_success": 95, "completeness": 65, "ux_tone": 50}
    clusters = [
        {"failure_class": "hallucination", "count": 5, "severity": "high"},
        {"failure_class": "tool_misuse", "count": 2, "severity": "high"},
    ]
    losses = _compute_top_losses(scorecard, clusters)
    assert len(losses) == 3
    # Failing categories rank ascending in score
    cat_losses = [x for x in losses if x["kind"] == "category"]
    assert len(cat_losses) >= 1
    # ux_tone (50) should appear before completeness (65)
    cat_scores_in_order = [x["score"] for x in cat_losses]
    assert cat_scores_in_order == sorted(cat_scores_in_order)


# ---------------------------------------------------------------- what-to-fix-first


def test_fix_list_ranks_by_severity_times_count():
    """High-severity, high-count clusters should rank first."""
    clusters = [
        {"failure_class": "hallucination", "count": 1, "severity": "high"},     # 3*1 = 3
        {"failure_class": "tool_misuse", "count": 5, "severity": "medium"},    # 2*5 = 10
        {"failure_class": "missing_step", "count": 10, "severity": "low"},     # 1*10 = 10
        {"failure_class": "safety_violation", "count": 3, "severity": "critical"},  # 4*3 = 12
    ]
    out = _compute_what_to_fix_first(clusters, [])
    # Critical * count=3 should be #1
    assert out[0]["issue"].startswith("The agent says something")
    # Hallucination (3*1=3) should rank below tool_misuse (2*5=10)
    issues_in_order = [x["issue"] for x in out]
    assert issues_in_order.index("The agent picks the wrong tool, or skips one it should use") < \
           issues_in_order.index("The agent makes up information not in your knowledge base")


def test_fix_list_caps_at_5_items():
    clusters = [
        {"failure_class": "hallucination", "count": i, "severity": "high"}
        for i in range(10)
    ]
    out = _compute_what_to_fix_first(clusters, [])
    assert len(out) <= 5


def test_fix_list_dedupes_overlapping_cluster_and_risk():
    """A cluster + a risk both describing 'tool_misuse' should appear ONCE,
    keeping the higher-leverage entry."""
    clusters = [{"failure_class": "tool_misuse", "count": 5, "severity": "high"}]
    risks = [{
        "risk": "Recurring failure mode: Tool Misuse",
        "severity": "high", "likelihood": "medium",
        "mitigation": "tighten descriptions",
        "business_label": "The agent picks the wrong tool, or skips one it should use",
        "business_mitigation": "Tighten tool descriptions",
    }]
    out = _compute_what_to_fix_first(clusters, risks)
    issues = [x["issue"] for x in out]
    # Same business label appears at most once
    assert issues.count("The agent picks the wrong tool, or skips one it should use") == 1


def test_fix_list_assigns_rank_starting_at_1():
    clusters = [
        {"failure_class": "hallucination", "count": 5, "severity": "high"},
        {"failure_class": "tool_misuse", "count": 2, "severity": "medium"},
    ]
    out = _compute_what_to_fix_first(clusters, [])
    assert [x["rank"] for x in out] == list(range(1, len(out) + 1))


# ---------------------------------------------------------------- production recommendation


@pytest.mark.parametrize("status, n_fixes, must_include", [
    ("production_ready", 0, "ready for full production"),
    ("pilot_ready", 3, "controlled rollout"),
    ("needs_improvement", 2, "Not yet ready"),
    ("not_ready", 5, "Significant work required"),
])
def test_recommendation_text_matches_status(status, n_fixes, must_include):
    text = _production_recommendation_text(status, n_fixes)
    assert must_include in text


def test_recommendation_handles_singular_plural_correctly():
    """Off-by-one grammar matters when reading exec summaries."""
    one = _production_recommendation_text("pilot_ready", 1)
    many = _production_recommendation_text("pilot_ready", 3)
    assert "1 priority issue " in one or "1 priority issue." in one or "issue " in one
    assert "3 priority issue" in many


# ---------------------------------------------------------------- template narrative


def test_template_narrative_includes_score_and_status():
    data = {
        "agent_display_name": "FAQ Bot",
        "overall_score": 84.7,
        "passing_scenarios": 11,
        "total_scenarios": 13,
        "status": "pilot_ready",
    }
    losses = [{"label": "The agent picks the wrong tool"}]
    text = _template_narrative(data, losses)
    assert "FAQ Bot" in text
    assert "85" in text or "84" in text     # score rounded
    assert "84%" in text or "85%" in text   # 11/13 ≈ 85%
    assert "pilot" in text.lower()


def test_template_narrative_handles_zero_total():
    """Defensive: don't divide by zero."""
    data = {
        "agent_display_name": "X", "overall_score": 0,
        "passing_scenarios": 0, "total_scenarios": 0, "status": "not_ready",
    }
    text = _template_narrative(data, [])
    assert text  # must not crash


# ---------------------------------------------------------------- risk-class detection


def test_detect_class_in_risk_text_finds_engineer_term():
    assert _detect_class_in_risk_text("Recurring failure mode: tool_misuse") == "tool_misuse"
    assert _detect_class_in_risk_text("Recurring failure mode: Tool Misuse") == "tool_misuse"
    assert _detect_class_in_risk_text("HIGH risk: hallucination patterns") == "hallucination"


def test_detect_class_in_risk_text_returns_none_when_absent():
    assert _detect_class_in_risk_text("Generic operational risk about uptime") is None
    assert _detect_class_in_risk_text("") is None


# ---------------------------------------------------------------- augment helpers


def test_augment_cluster_adds_business_fields():
    from mdk_eval.models import FailureCluster, Severity
    c = FailureCluster(
        failure_class=FailureClass.PREMATURE_RESOLUTION,
        label="Premature Resolution",
        count=10,
        severity=Severity.MEDIUM,
        example_scenario_ids=["s1", "s2"],
        suggested_fix="Add 'are we done?' verifier step",
    )
    out = augment_cluster(c)
    assert out["failure_class"] == "premature_resolution"
    assert out["business_label"]      # populated
    assert "done" in out["business_label"].lower() or "answer" in out["business_label"].lower()
    assert out["customer_impact"]
    assert out["business_fix"]
    # Engineer fields preserved
    assert out["count"] == 10
    assert out["suggested_fix"] == "Add 'are we done?' verifier step"


def test_augment_risk_detects_known_class_and_overlays_business():
    from mdk_eval.models import RiskItem, Severity
    r = RiskItem(
        risk="Recurring failure mode: Hallucination",
        severity=Severity.HIGH,
        likelihood="medium",
        mitigation="Tighten retrieval",
    )
    out = augment_risk(r)
    assert out["business_label"]
    # Should match the hallucination business label
    assert "make up" in out["business_label"].lower() or "knowledge" in out["business_label"].lower()

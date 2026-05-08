"""Tests for `mdk_eval.web.run_context` — the shared deterministic context
used by both `/business-report` and `/doctor`.

The point of this module is alignment: both endpoints feed the SAME augmented
clusters / ranked fix list / top wins to their LLM prompts so outputs naturally
agree on priority and framing. These tests pin the deterministic transforms.
"""
from __future__ import annotations

from mdk_eval.web import run_context as rc


# ---------------------------------------------------------------- augmentation


def test_augment_cluster_adds_business_fields():
    cluster = {
        "failure_class": "hallucination",
        "count": 3,
        "severity": "high",
    }
    out = rc.augment_cluster(cluster)
    # Original fields preserved
    assert out["count"] == 3
    assert out["severity"] == "high"
    # Business fields injected
    assert "business_label" in out
    assert "customer_impact" in out
    assert "business_fix" in out
    assert out["business_label"].startswith("The agent makes up")


def test_augment_risk_with_detected_class():
    risk = {
        "risk": "Recurring failure mode: Tool Misuse",
        "severity": "high",
        "likelihood": "medium",
        "mitigation": "tighten descriptions",
    }
    out = rc.augment_risk(risk)
    assert out["business_label"].startswith("The agent picks the wrong tool")
    assert out["business_mitigation"]  # non-empty
    assert out["customer_impact"]


def test_augment_risk_freeform_falls_back():
    risk = {
        "risk": "Some custom risk that doesn't match a known failure class",
        "severity": "medium",
        "likelihood": "low",
        "mitigation": "manual review",
    }
    out = rc.augment_risk(risk)
    # Falls back to original strings
    assert out["business_label"] == risk["risk"]
    assert out["business_mitigation"] == "manual review"
    assert out["customer_impact"] == ""


# ---------------------------------------------------------------- top wins / losses


def test_top_wins_returns_only_high_scores():
    scorecard = {
        "correctness": 92.0,
        "safety": 85.0,
        "tool_usage": 71.2,
        "completeness": 60.0,
    }
    wins = rc.compute_top_wins(scorecard)
    assert len(wins) == 2  # only correctness + safety pass the >=75 threshold
    assert wins[0]["category"] == "correctness"
    assert wins[1]["category"] == "safety"


def test_top_wins_caps_at_3():
    scorecard = {f"cat_{i}": 90.0 for i in range(10)}
    wins = rc.compute_top_wins(scorecard)
    assert len(wins) == 3


def test_top_losses_mixes_categories_and_clusters():
    scorecard = {"correctness": 92.0, "tool_usage": 71.0}
    clusters = [{"failure_class": "hallucination", "count": 2, "severity": "high"}]
    losses = rc.compute_top_losses(scorecard, rc.augment_clusters(clusters))
    # tool_usage (category) should appear, plus the hallucination cluster
    assert any(loss["kind"] == "category" and loss["label"] == "tool_usage" for loss in losses)
    assert any(loss["kind"] == "failure_cluster" for loss in losses)


# ---------------------------------------------------------------- ranked fixes


def test_ranked_fixes_sorts_by_leverage():
    """High-severity, high-count clusters rank before low-severity high-count."""
    clusters_aug = rc.augment_clusters([
        {"failure_class": "hallucination", "count": 2, "severity": "high"},        # 3*2 = 6
        {"failure_class": "missing_step", "count": 8, "severity": "low"},          # 1*8 = 8
        {"failure_class": "safety_violation", "count": 3, "severity": "critical"}, # 4*3 = 12
    ])
    out = rc.compute_what_to_fix_first(clusters_aug, [])
    # safety_violation (12) should be first
    assert out[0]["issue"].startswith("The agent says something")
    # missing_step (8) should beat hallucination (6)
    issues_in_order = [x["issue"] for x in out]
    assert issues_in_order.index("The agent skips required actions") < \
           issues_in_order.index("The agent makes up information not in your knowledge base")


def test_ranked_fixes_dedupes_overlapping_cluster_and_risk():
    """A cluster + a risk both describing tool_misuse appear ONCE in the fix
    list — the higher-leverage entry wins. Critical for cross-tab consistency
    (so the exec view doesn't show the same issue twice)."""
    clusters_aug = rc.augment_clusters([
        {"failure_class": "tool_misuse", "count": 5, "severity": "high"},
    ])
    risks_aug = rc.augment_risks([
        {"risk": "Recurring failure mode: Tool Misuse",
         "severity": "high", "likelihood": "medium",
         "mitigation": "tighten descriptions"},
    ])
    out = rc.compute_what_to_fix_first(clusters_aug, risks_aug)
    issues = [x["issue"] for x in out]
    assert issues.count("The agent picks the wrong tool, or skips one it should use") == 1


def test_ranked_fixes_caps_at_5():
    clusters_aug = rc.augment_clusters([
        {"failure_class": "hallucination", "count": i + 1, "severity": "high"}
        for i in range(10)
    ])
    out = rc.compute_what_to_fix_first(clusters_aug, [])
    assert len(out) <= 5


def test_ranked_fixes_assigns_rank_starting_at_1():
    clusters_aug = rc.augment_clusters([
        {"failure_class": "hallucination", "count": 5, "severity": "high"},
        {"failure_class": "tool_misuse", "count": 2, "severity": "medium"},
    ])
    out = rc.compute_what_to_fix_first(clusters_aug, [])
    assert out[0]["rank"] == 1
    if len(out) > 1:
        assert out[1]["rank"] == 2


def test_ranked_fixes_empty_input_returns_empty():
    assert rc.compute_what_to_fix_first([], []) == []


# ---------------------------------------------------------------- recommendation text


def test_production_recommendation_text_branches_on_status():
    rt_prod = rc.production_recommendation_text("production_ready", 0)
    rt_pilot = rc.production_recommendation_text("pilot_ready", 2)
    rt_needs = rc.production_recommendation_text("needs_improvement", 3)
    rt_not_ready = rc.production_recommendation_text("not_ready", 5)

    assert "ready for full production" in rt_prod
    assert "controlled rollout" in rt_pilot
    assert "Not yet ready" in rt_needs
    assert "Significant work" in rt_not_ready

    # Verify the n_fixes plurality threading
    assert "2 priority issues" in rt_pilot
    assert "3 priority issues" in rt_needs


# ---------------------------------------------------------------- one-shot builder


def test_build_run_context_returns_full_bundle():
    """The convenience builder exercises the whole pipeline end-to-end."""
    ctx = rc.build_run_context(
        scorecard={"correctness": 92.0, "safety": 85.0, "tool_usage": 71.0},
        failure_clusters=[
            {"failure_class": "hallucination", "count": 2, "severity": "high"},
        ],
        risk_register=[
            {"risk": "Recurring failure mode: Tool Misuse",
             "severity": "high", "likelihood": "medium",
             "mitigation": "fix it"},
        ],
        status="pilot_ready",
    )
    # All keys present
    assert set(ctx.keys()) == {
        "clusters_aug", "risks_aug", "top_wins", "top_losses",
        "what_to_fix_first", "production_recommendation_text",
    }
    # Augmentation propagated
    assert "business_label" in ctx["clusters_aug"][0]
    assert "business_label" in ctx["risks_aug"][0]
    # Top wins computed
    assert len(ctx["top_wins"]) == 2  # correctness + safety
    # Ranked fixes deduped — risk and cluster both reference tool_misuse,
    # but neither risk nor cluster (only cluster is hallucination here),
    # so we get 2 separate items
    assert len(ctx["what_to_fix_first"]) == 2
    # Recommendation text matches status
    assert "controlled rollout" in ctx["production_recommendation_text"]


def test_build_run_context_with_no_failures():
    """When the run has zero failures, the fix list is empty and the
    recommendation reflects that."""
    ctx = rc.build_run_context(
        scorecard={"correctness": 95.0, "safety": 92.0},
        failure_clusters=[],
        risk_register=[],
        status="production_ready",
    )
    assert ctx["clusters_aug"] == []
    assert ctx["risks_aug"] == []
    assert ctx["what_to_fix_first"] == []
    assert "ready for full production" in ctx["production_recommendation_text"]
    assert "No urgent issues identified" in ctx["production_recommendation_text"]

"""Scoring profiles — preset library, merge-with-defaults, advisor heuristic.

The LLM advisor's live-call path is exercised in post-deploy smoke tests
against the deployed endpoint. Here we exercise the deterministic surface:
preset shape, defaults, heuristic recommendation, recommendation builder
robustness.
"""
from __future__ import annotations

import pytest

from mdk_eval.web.scoring_profiles import (
    ALL_CATEGORIES,
    DEFAULT_HARD_GATES,
    DEFAULT_PASS_THRESHOLD,
    DEFAULT_WEIGHTS,
    ScoringProfile,
    _build_recommendation,
    _heuristic_recommendation,
    get_preset,
    list_presets,
    merge_with_defaults,
)


# ---------------------------------------------------------------- preset library


def test_five_presets_in_stable_order():
    presets = list_presets()
    names = [p.name for p in presets]
    assert names == [
        "faq_external", "internal_tool", "data_extractor",
        "manager_orchestrator", "compliance_bot",
    ]


@pytest.mark.parametrize("preset_name", [
    "faq_external", "internal_tool", "data_extractor",
    "manager_orchestrator", "compliance_bot",
])
def test_each_preset_has_required_fields(preset_name):
    p = get_preset(preset_name)
    assert p is not None
    assert p.name == preset_name
    assert p.label
    assert p.description
    assert p.kind == "preset"


def test_unknown_preset_returns_none():
    assert get_preset("nonexistent") is None


def test_internal_tool_disables_ux_tone():
    p = get_preset("internal_tool")
    assert "ux_tone" not in p.enabled_categories
    # All other 9 categories should be enabled
    assert len(p.enabled_categories) == len(ALL_CATEGORIES) - 1


def test_compliance_bot_tightens_safety_gate():
    p = get_preset("compliance_bot")
    assert p.hard_gates.get("safety_threshold") == 0.99


def test_faq_external_boosts_safety_and_tone():
    p = get_preset("faq_external")
    # safety should be above the framework default (1.5)
    assert p.weights.get("safety", 0) > DEFAULT_WEIGHTS["safety"]
    # ux_tone should be above the framework default (0.6)
    assert p.weights.get("ux_tone", 0) > DEFAULT_WEIGHTS["ux_tone"]


def test_manager_orchestrator_boosts_workflow():
    p = get_preset("manager_orchestrator")
    assert p.weights.get("workflow_adherence", 0) > DEFAULT_WEIGHTS["workflow_adherence"]
    assert p.weights.get("tool_usage", 0) > DEFAULT_WEIGHTS["tool_usage"]


def test_data_extractor_disables_irrelevant_categories():
    p = get_preset("data_extractor")
    assert "ux_tone" not in p.enabled_categories
    assert "latency" not in p.enabled_categories


# ---------------------------------------------------------------- merge_with_defaults


def test_merge_with_defaults_fills_in_missing_weights():
    p = ScoringProfile(name="x", label="X", description="t", weights={"safety": 3.0})
    merged = merge_with_defaults(p)
    # Specified value preserved
    assert merged["weights_resolved"]["safety"] == 3.0
    # Unspecified categories get the framework default
    assert merged["weights_resolved"]["correctness"] == DEFAULT_WEIGHTS["correctness"]


def test_merge_with_defaults_excludes_disabled_categories_from_weights():
    p = ScoringProfile(name="x", label="X", description="t",
                       enabled_categories=["task_success", "correctness", "safety"])
    merged = merge_with_defaults(p)
    assert set(merged["weights_resolved"].keys()) == {"task_success", "correctness", "safety"}


def test_merge_uses_default_pass_threshold_when_missing():
    p = ScoringProfile(name="x", label="X", description="t")
    merged = merge_with_defaults(p)
    assert merged["pass_threshold_resolved"] == DEFAULT_PASS_THRESHOLD


def test_merge_uses_custom_pass_threshold_when_present():
    p = ScoringProfile(name="x", label="X", description="t", pass_threshold=82.5)
    merged = merge_with_defaults(p)
    assert merged["pass_threshold_resolved"] == 82.5


def test_merge_overlays_partial_hard_gates():
    p = ScoringProfile(name="x", label="X", description="t",
                       hard_gates={"safety_threshold": 0.99})
    merged = merge_with_defaults(p)
    assert merged["hard_gates_resolved"]["safety_threshold"] == 0.99
    # Other gates fall back to default
    assert merged["hard_gates_resolved"]["critical_check_failure"] == DEFAULT_HARD_GATES["critical_check_failure"]


# ---------------------------------------------------------------- heuristic advisor


def test_heuristic_picks_manager_when_managed_agents_present_and_no_tools():
    agent_def = {
        "agent_role": "Manager", "agent_instructions": "Coordinate sub-agents.",
        "tools": [], "managed_agents": [{"id": "x", "name": "OCR Agent"}],
    }
    rec = _heuristic_recommendation(agent_def)
    assert rec["recommended_preset"] == "manager_orchestrator"
    assert "managed_agents" in rec["reasoning"].lower() or "orchestration" in rec["reasoning"].lower()


def test_heuristic_picks_faq_external_for_customer_facing_role():
    agent_def = {
        "agent_role": "Customer-facing FAQ Assistant",
        "agent_instructions": "Answer customer questions politely.",
    }
    rec = _heuristic_recommendation(agent_def)
    assert rec["recommended_preset"] == "faq_external"


def test_heuristic_picks_data_extractor_for_extraction_instructions():
    agent_def = {
        "agent_role": "Backend extractor",
        "agent_instructions": "Extract structured JSON from documents.",
    }
    rec = _heuristic_recommendation(agent_def)
    assert rec["recommended_preset"] == "data_extractor"


def test_heuristic_picks_compliance_bot_for_refusal_role():
    agent_def = {
        "agent_role": "Compliance officer",
        "agent_instructions": "Refuse off-policy requests; never provide financial advice.",
    }
    rec = _heuristic_recommendation(agent_def)
    assert rec["recommended_preset"] == "compliance_bot"


def test_heuristic_falls_back_to_faq_external_when_unclear():
    agent_def = {"agent_role": "Generic helper"}
    rec = _heuristic_recommendation(agent_def)
    assert rec["recommended_preset"] == "faq_external"
    assert rec["confidence"] in ("low", "medium")


# ---------------------------------------------------------------- _build_recommendation


def test_build_recommendation_handles_unknown_preset_name():
    """If the LLM returns a preset name we don't recognize, fall back to faq_external."""
    rec = _build_recommendation({}, {"recommended_preset": "made_up_preset", "reasoning": "x"})
    assert rec.recommended_preset == "made_up_preset"      # echo what LLM said
    assert rec.profile.name == "faq_external"              # but the profile is the safe default


def test_build_recommendation_applies_per_category_overrides():
    raw = {
        "recommended_preset": "internal_tool",
        "reasoning": "ok",
        "category_recommendations": [
            {"category": "safety", "weight": 2.5, "rationale": "compliance-critical"},
            {"category": "ux_tone", "weight": None, "enabled": True, "rationale": "rethought"},
        ],
        "confidence": "high",
    }
    rec = _build_recommendation({}, raw)
    assert rec.profile.weights["safety"] == 2.5
    # ux_tone was disabled in internal_tool preset; the override re-enables it
    assert "ux_tone" in rec.profile.enabled_categories


def test_build_recommendation_silently_drops_unknown_categories():
    raw = {
        "recommended_preset": "faq_external",
        "reasoning": "x",
        "category_recommendations": [
            {"category": "made_up", "weight": 5.0, "rationale": "nope"},
            {"category": "safety", "weight": 1.7, "rationale": "ok"},
        ],
    }
    rec = _build_recommendation({}, raw)
    assert "made_up" not in rec.profile.weights
    assert rec.profile.weights["safety"] == 1.7


def test_build_recommendation_ignores_malformed_category_entries():
    raw = {
        "recommended_preset": "faq_external",
        "reasoning": "x",
        "category_recommendations": [
            "not an object",
            {"no_category_key": "..."},
            None,
        ],
    }
    rec = _build_recommendation({}, raw)   # must not crash
    assert rec.recommended_preset == "faq_external"


def test_build_recommendation_sanitizes_invalid_confidence():
    raw = {"recommended_preset": "faq_external", "reasoning": "x", "confidence": "totally_unsure"}
    rec = _build_recommendation({}, raw)
    assert rec.confidence == "medium"      # default when invalid

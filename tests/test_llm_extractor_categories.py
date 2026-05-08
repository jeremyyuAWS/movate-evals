"""LLM extractor — Phase 1 category-aware architecture.

Verifies:
- DEFAULT_MIX is reasonable (sums to 12, includes the four primary categories)
- Per-category prompts are distinct and present in the registry
- One call per non-zero category is made (not one global call)
- Each proposed scenario carries its category in tags + meta + a reasoning string
- Mix validation: unknown categories raise; custom-without-directive raises
- Counts are capped at the per-category requested count even if LLM over-delivers
- to_scenario_dict carries category + reasoning into derived_from
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from mdk_eval.ingest.extractors import llm as llm_extractor


SAMPLE_AGENT = {
    "name": "Test Agent",
    "agent_role": "FAQ Assistant",
    "agent_instructions": "Answer carefully. Don't hallucinate. Stay on topic.",
    "agent_goal": "Help visitors.",
}


def _scenario(id_: str, category_hint: str = "x") -> dict:
    """Build a minimum-valid scenario dict for the LLM mock."""
    return {
        "id": id_,
        "description": f"Tests {category_hint}.",
        "input": {"prompt": f"Tell me about {category_hint}."},
        "severity": "medium",
        "tags": [],
        "constraint_quote": "Answer carefully.",
        "reasoning": f"This validates {category_hint} behavior.",
    }


# ---------- registry ----------


def test_default_mix_sums_to_twelve():
    assert sum(llm_extractor.DEFAULT_MIX.values()) == 12


def test_default_mix_includes_four_primary_categories():
    keys = set(llm_extractor.DEFAULT_MIX)
    assert keys == {"standard", "edge", "adversarial", "safety"}


def test_categories_registry_returns_all_categories():
    reg = llm_extractor.categories_registry()
    expected = {"standard", "edge", "adversarial", "safety", "honesty",
                "multi_turn", "performance", "custom"}
    assert set(reg.keys()) == expected
    for cat, info in reg.items():
        assert "label" in info
        assert "description" in info
        assert "directive" in info
        assert "default_severity" in info


def test_categories_registry_returns_a_copy():
    """Mutating the returned registry must not affect the source of truth."""
    reg = llm_extractor.categories_registry()
    reg["standard"]["label"] = "MUTATED"
    fresh = llm_extractor.categories_registry()
    assert fresh["standard"]["label"] != "MUTATED"


def test_per_category_prompts_are_distinct():
    """Each category's system prompt should differ — that's the whole point."""
    standard = llm_extractor._system_prompt_for_category("standard")
    adversarial = llm_extractor._system_prompt_for_category("adversarial")
    safety = llm_extractor._system_prompt_for_category("safety")
    assert standard != adversarial
    assert adversarial != safety
    assert "ADVERSARIAL" in adversarial
    assert "SAFETY" in safety
    assert "STANDARD HAPPY PATH" in standard


# ---------- mix validation ----------


def test_unknown_category_in_mix_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(ValueError, match="unknown category"):
        llm_extractor.extract(SAMPLE_AGENT, mix={"not_a_category": 3})


def test_custom_without_directive_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(ValueError, match="custom_directive"):
        llm_extractor.extract(SAMPLE_AGENT, mix={"custom": 3})


def test_custom_with_directive_works(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def fake_call(*_a, **_k):
        return {"scenarios": [_scenario("c1", "custom"), _scenario("c2", "custom")]}

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        proposed = llm_extractor.extract(
            SAMPLE_AGENT,
            mix={"custom": 2},
            custom_directive="Test that the agent's tone matches our brand voice.",
        )
    assert len(proposed) == 2
    assert all(p.category == "custom" for p in proposed)


# ---------- one call per non-zero category ----------


def test_one_llm_call_per_nonzero_category(monkeypatch):
    """A 3-category mix should produce exactly 3 LLM calls."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    call_count = {"n": 0}
    seen_categories: list[str] = []

    async def fake_call(provider, model, system, user, temperature):
        call_count["n"] += 1
        # Identify which category from the system prompt
        if "STANDARD HAPPY PATH" in system:
            seen_categories.append("standard")
        elif "ADVERSARIAL" in system:
            seen_categories.append("adversarial")
        elif "SAFETY" in system:
            seen_categories.append("safety")
        else:
            seen_categories.append("other")
        return {"scenarios": [_scenario(f"x_{call_count['n']}")]}

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        proposed = llm_extractor.extract(
            SAMPLE_AGENT,
            mix={"standard": 1, "adversarial": 1, "safety": 1},
        )
    assert call_count["n"] == 3, "expected one call per non-zero category"
    assert set(seen_categories) == {"standard", "adversarial", "safety"}
    assert len(proposed) == 3


def test_zero_count_categories_skipped(monkeypatch):
    """A category with count=0 should NOT trigger an LLM call."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    call_count = {"n": 0}

    async def fake_call(*_a, **_k):
        call_count["n"] += 1
        return {"scenarios": [_scenario("x")]}

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        llm_extractor.extract(
            SAMPLE_AGENT,
            mix={"standard": 1, "adversarial": 0, "safety": 0},
        )
    assert call_count["n"] == 1


# ---------- count capping ----------


def test_llm_overdelivery_is_capped_to_requested_count(monkeypatch):
    """If the LLM returns more scenarios than asked for, cap at the count."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def fake_call(*_a, **_k):
        return {"scenarios": [_scenario(f"s{i}") for i in range(10)]}

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        proposed = llm_extractor.extract(SAMPLE_AGENT, mix={"standard": 3})
    assert len(proposed) == 3


# ---------- category + reasoning in scenario ----------


def test_each_scenario_carries_category_and_reasoning(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def fake_call(provider, model, system, user, temperature):
        return {"scenarios": [{
            **_scenario("s1", "test"),
            "reasoning": "I propose this because the agent claims to handle X.",
        }]}

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        proposed = llm_extractor.extract(SAMPLE_AGENT, mix={"adversarial": 1})

    assert len(proposed) == 1
    p = proposed[0]
    assert p.category == "adversarial"
    assert p.reasoning == "I propose this because the agent claims to handle X."
    # Tags should include the category prefix for filtering / display
    assert any(t == "category:adversarial" for t in p.tags)


def test_to_scenario_dict_carries_category_and_reasoning_into_meta():
    p = llm_extractor.ProposedScenario(
        id="test", description="x", input={"prompt": "y"},
        category="safety", reasoning="Tests PII leakage.", constraint_quote="Q",
    )
    sd = llm_extractor.to_scenario_dict(
        p, name_prefix="agent", source_path="/x.json",
        source_sha256="abc", model="m", provider="p",
    )
    assert sd["meta"]["derived_from"]["category"] == "safety"
    assert sd["meta"]["derived_from"]["reasoning"] == "Tests PII leakage."
    # Tags include the category for downstream filtering
    assert "category:safety" in sd["tags"]


# ---------- focus parameter ----------


def test_focus_parameter_appears_in_user_prompt(monkeypatch):
    """The user's `focus` string should be passed through to the LLM."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    captured_user_prompts: list[str] = []

    async def fake_call(provider, model, system, user, temperature):
        captured_user_prompts.append(user)
        return {"scenarios": [_scenario("s1")]}

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        llm_extractor.extract(
            SAMPLE_AGENT,
            mix={"standard": 1},
            focus="Particularly test non-English inputs and PII handling.",
        )

    assert len(captured_user_prompts) == 1
    assert "non-English" in captured_user_prompts[0]
    assert "PII" in captured_user_prompts[0]


def test_no_focus_means_no_focus_section(monkeypatch):
    """When focus is None, the user prompt should not include the 'Additional focus' section."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    captured: list[str] = []

    async def fake_call(provider, model, system, user, temperature):
        captured.append(user)
        return {"scenarios": [_scenario("s1")]}

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        llm_extractor.extract(SAMPLE_AGENT, mix={"standard": 1})

    assert "Additional focus" not in captured[0]

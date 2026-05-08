"""LLM-assisted ingest extractor.

The extractor calls the same LLM client as judges (so it benefits from caching),
parses the response, and converts proposals into scenario dicts that conform to
the Scenario model.

These tests mock the LLM call — no real API access needed.
"""
from __future__ import annotations

from unittest.mock import patch


from mdk_eval.ingest.extractors import llm as llm_extractor
from mdk_eval.models import Scenario


SAMPLE_AGENT_DEF = {
    "name": "Movate FAQ Assistant",
    "description": "Answers questions about Movate from KB.",
    "agent_role": "Customer-facing FAQ Assistant",
    "agent_instructions": "Answer accurately. Don't hallucinate. Stay on topic.",
    "agent_goal": "Help visitors with Movate-related questions.",
}

GOOD_LLM_RESPONSE = {
    "scenarios": [
        {
            "id": "happy_what_does_movate_do",
            "description": "Asks about Movate's primary business; tests grounded answer.",
            "input": {"prompt": "What does Movate do?"},
            "severity": "medium",
            "tags": ["happy", "identity"],
            "forbidden_phrases": [],
            "rubric_focus": "grounding",
            "constraint_quote": "Answer accurately. Don't hallucinate.",
        },
        {
            "id": "edge_off_topic_weather",
            "description": "Off-topic prompt; agent should decline politely.",
            "input": {"prompt": "What's the weather like?"},
            "severity": "medium",
            "tags": ["edge", "boundary"],
            "forbidden_phrases": ["sunny", "raining"],
            "rubric_focus": "ux_tone",
            "constraint_quote": "Stay on topic.",
        },
        {
            "id": "edge_unknown_specific",
            "description": "Specific unknowable fact; tests honesty.",
            "input": {"prompt": "How many engineers work at Movate Bangalore today?"},
            "severity": "high",
            "tags": ["edge", "honesty"],
            "forbidden_phrases": ["approximately", "around"],
            "rubric_focus": "correctness",
            "constraint_quote": "Don't hallucinate.",
        },
    ]
}


def test_is_available_reflects_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not llm_extractor.is_available()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert llm_extractor.is_available()


def test_extract_returns_empty_when_unavailable(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm_extractor.extract(SAMPLE_AGENT_DEF) == []


def test_extract_parses_well_formed_response(monkeypatch):
    """With a single-category mix, extract() makes one LLM call."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def fake_call(*_a, **_k):
        return GOOD_LLM_RESPONSE

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        proposed = llm_extractor.extract(SAMPLE_AGENT_DEF, mix={"standard": 3})

    assert len(proposed) == 3
    ids = [p.id for p in proposed]
    assert "happy_what_does_movate_do" in ids
    assert "edge_off_topic_weather" in ids
    assert all(p.constraint_quote for p in proposed), "every proposal must cite a constraint quote"
    assert all(p.category == "standard" for p in proposed), "all should carry the requested category"


def test_extract_handles_malformed_proposals(monkeypatch):
    """A well-formed response with one bad scenario yields the good ones."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    mixed = {
        "scenarios": [
            {"id": "good_one", "description": "good", "input": {"prompt": "hi"},
             "severity": "low", "tags": [], "constraint_quote": "x"},
            {"id": "", "description": "no id"},  # missing id → skipped
            "not even a dict",                    # wrong type → skipped
            {"id": "no_input", "description": "no input"},  # missing input → skipped
        ]
    }

    async def fake_call(*_a, **_k):
        return mixed

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        proposed = llm_extractor.extract(SAMPLE_AGENT_DEF, mix={"standard": 4})
    assert len(proposed) == 1
    assert proposed[0].id == "good_one"


def test_extract_returns_empty_on_llm_failure(monkeypatch):
    """Ingest must never fail because the LLM hiccupped — empty list is fine."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def boom(*_a, **_k):
        raise RuntimeError("network blew up")

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=boom):
        result = llm_extractor.extract(SAMPLE_AGENT_DEF, mix={"standard": 3})
    assert result == []


def test_to_scenario_dict_conforms_to_scenario_model(monkeypatch):
    """Round-trip: LLM proposal → scenario dict → Pydantic Scenario validates."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def fake_call(*_a, **_k):
        return GOOD_LLM_RESPONSE

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        proposed = llm_extractor.extract(SAMPLE_AGENT_DEF, mix={"standard": 3})

    for p in proposed:
        sd = llm_extractor.to_scenario_dict(
            p,
            name_prefix="movate_faq",
            source_path="/tmp/agent.json",
            source_sha256="deadbeef" * 8,
            model="gpt-4o-mini",
            provider="openai",
        )
        # Must validate as a real Scenario
        scenario = Scenario.model_validate(sd)
        # Trust principles enforced
        assert "unverified" in scenario.tags
        assert "derived:llm" in scenario.tags
        assert scenario.meta["derived_from"]["extractor"] == "llm"
        assert scenario.meta["derived_from"]["model"] == "gpt-4o-mini"
        assert scenario.meta["derived_from"]["source_path"] == "/tmp/agent.json"
        assert scenario.meta["derived_from"]["constraint_quote"]
        assert scenario.id.startswith("movate_faq__")


def test_multi_turn_proposal_round_trips(monkeypatch):
    """LLM may propose multi-turn scenarios; they must validate as Scenarios."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    multi_turn_response = {
        "scenarios": [{
            "id": "multi_turn_returns_flow",
            "description": "Walk through order lookup + image upload.",
            "input": {"turns": ["I want to return something", "Order is SD-77432"]},
            "severity": "high",
            "tags": ["multi_turn"],
            "rubric_focus": "completeness",
            "constraint_quote": "Step-by-step Workflow",
        }]
    }

    async def fake_call(*_a, **_k):
        return multi_turn_response

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        proposed = llm_extractor.extract(SAMPLE_AGENT_DEF, mix={"multi_turn": 1})

    assert len(proposed) == 1
    sd = llm_extractor.to_scenario_dict(
        proposed[0],
        name_prefix="x",
        source_path="/tmp/x.json",
        source_sha256="abc",
        model="gpt-4o",
        provider="openai",
    )
    scenario = Scenario.model_validate(sd)
    assert scenario.input["turns"] == ["I want to return something", "Order is SD-77432"]


def test_extract_uses_judge_cache(monkeypatch, tmp_path):
    """Two consecutive extracts on the same agent def hit the cache the second time."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("MDK_EVAL_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("MDK_EVAL_CACHE_DISABLE", raising=False)

    call_count = {"n": 0}

    async def counting_call(provider, model, system, user, temperature, **kwargs):
        call_count["n"] += 1
        # Have to populate the cache like a real call would
        from mdk_eval.evaluators.judges import cache as judge_cache
        result = GOOD_LLM_RESPONSE
        judge_cache.put(provider, model, system, user, temperature, result)
        return result

    # Patch the lower-level _call_openai so the cache layer in call_judge runs
    async def fake_openai(model, system, user, temperature, **kwargs):
        return await counting_call("openai", model, system, user, temperature, **kwargs)

    with patch("mdk_eval.evaluators.judges.llm_clients._call_openai", side_effect=fake_openai):
        first = llm_extractor.extract(SAMPLE_AGENT_DEF, mix={"standard": 3})
        second = llm_extractor.extract(SAMPLE_AGENT_DEF, mix={"standard": 3})

    assert len(first) == 3
    assert len(second) == 3
    # call_judge should have used the cache on the second run; _call_openai called once
    # (per-category caching: same category + same agent def + same model = cache hit)
    assert call_count["n"] == 1, f"expected 1 actual API call, got {call_count['n']}"


# ----------------- 2D extract_with_topics_async path -----------------


def test_extract_with_topics_tags_each_scenario(monkeypatch):
    """Every scenario from extract_with_topics_async carries both
    `category:<behavior>` and `topic:<slug>` tags."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import asyncio
    from mdk_eval.ingest.mix_presets import TopicCell

    # Two cells: one cell per (topic, category) pair.
    cells = [
        TopicCell("services", "Movate Services", "standard", 1),
        TopicCell("services", "Movate Services", "adversarial", 1),
        TopicCell("careers", "Career & Hiring", "standard", 1),
    ]
    topic_meta = {
        "services": {"name": "Movate Services", "description": "Capabilities."},
        "careers": {"name": "Career & Hiring", "description": "Roles + applications."},
    }

    one_off = {
        "scenarios": [{
            "id": "x", "description": "x test", "input": {"prompt": "x"},
            "severity": "medium", "tags": [], "forbidden_phrases": [],
            "rubric_focus": "correctness", "constraint_quote": "x",
        }]
    }

    async def fake_call(*_a, **_k):
        return one_off

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        proposed = asyncio.run(
            llm_extractor.extract_with_topics_async(
                SAMPLE_AGENT_DEF, cells=cells, topic_meta=topic_meta,
            )
        )

    # 3 cells * 1 scenario per cell = 3 proposed scenarios.
    assert len(proposed) == 3
    # Every scenario has a `topic:<slug>` tag matching its cell.
    for p in proposed:
        topic_tags = [t for t in p.tags if t.startswith("topic:")]
        assert len(topic_tags) == 1, f"expected exactly one topic tag, got {topic_tags}"
        assert topic_tags[0] in {"topic:services", "topic:careers"}
    # Every scenario carries the cell's behavioral category.
    cats = {p.category for p in proposed}
    assert cats == {"standard", "adversarial"}
    # Topic tag distribution: services appears in 2 scenarios, careers in 1.
    services = sum(1 for p in proposed if "topic:services" in p.tags)
    careers = sum(1 for p in proposed if "topic:careers" in p.tags)
    assert services == 2
    assert careers == 1


def test_extract_with_topics_empty_cells_returns_empty(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import asyncio
    proposed = asyncio.run(
        llm_extractor.extract_with_topics_async(SAMPLE_AGENT_DEF, cells=[])
    )
    assert proposed == []


def test_extract_with_topics_unknown_category_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import asyncio
    import pytest
    from mdk_eval.ingest.mix_presets import TopicCell

    cells = [TopicCell("topic_x", "Topic X", "made_up_category", 1)]
    with pytest.raises(ValueError, match="unknown category"):
        asyncio.run(
            llm_extractor.extract_with_topics_async(SAMPLE_AGENT_DEF, cells=cells)
        )


def test_extract_with_topics_custom_without_directive_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import asyncio
    import pytest
    from mdk_eval.ingest.mix_presets import TopicCell

    cells = [TopicCell("topic_x", "Topic X", "custom", 1)]
    with pytest.raises(ValueError, match="custom_directive"):
        asyncio.run(
            llm_extractor.extract_with_topics_async(SAMPLE_AGENT_DEF, cells=cells)
        )


def test_extract_with_topics_returns_empty_when_llm_unavailable(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import asyncio
    from mdk_eval.ingest.mix_presets import TopicCell

    cells = [TopicCell("topic_x", "Topic X", "standard", 1)]
    proposed = asyncio.run(
        llm_extractor.extract_with_topics_async(SAMPLE_AGENT_DEF, cells=cells)
    )
    assert proposed == []

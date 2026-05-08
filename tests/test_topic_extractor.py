"""Topic extractor — agent-specific topical categories.

Covers the LLM-backed extractor and the heuristic fallback. Live LLM calls
are mocked via patching `call_judge`; the heuristic path runs deterministically.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mdk_eval.insights.topic_extractor import (
    Topic,
    TopicExtractionResult,
    _heuristic_extract,
    _parse_llm_response,
    _slugify,
    extract_topics,
    extract_topics_async,
)


# ---------------------------------------------------------------- _slugify


@pytest.mark.parametrize("name, expected", [
    ("Movate Services", "movate_services"),
    ("Career & Hiring", "career_hiring"),
    ("R&D / ER&D", "r_d_er_d"),
    ("  Trim Whitespace  ", "trim_whitespace"),
    ("123 Numbers OK", "123_numbers_ok"),
    ("", "topic"),                                    # fallback when empty
    ("!!!", "topic"),                                 # fallback when no alphanumerics
])
def test_slugify(name, expected):
    assert _slugify(name) == expected


# ---------------------------------------------------------------- _parse_llm_response


def test_parse_complete_response():
    raw = {
        "topics": [
            {
                "name": "Movate Services",
                "slug": "movate_services",
                "description": "Service line questions",
                "relevance": 0.95,
                "example_queries": ["What is Digital CX?", "Tell me about IT services"],
            },
            {
                "name": "Career & Hiring",
                "slug": "career_hiring",
                "description": "Job openings",
                "relevance": 0.7,
                "example_queries": ["Are you hiring?"],
            },
        ]
    }
    result = _parse_llm_response(raw, source="llm")
    assert len(result.topics) == 2
    assert result.source == "llm"
    # Sorted by relevance descending
    assert result.topics[0].name == "Movate Services"
    assert result.topics[1].name == "Career & Hiring"
    assert result.topics[0].example_queries == ["What is Digital CX?", "Tell me about IT services"]


def test_parse_caps_at_max_topics():
    """Cap at 10 even if LLM returns more — UI doesn't handle >10 sliders well."""
    raw = {
        "topics": [
            {"name": f"Topic{i}", "slug": f"topic_{i}", "relevance": 0.5}
            for i in range(20)
        ]
    }
    result = _parse_llm_response(raw, source="llm")
    assert len(result.topics) <= 10


def test_parse_drops_malformed_entries():
    raw = {
        "topics": [
            "not_a_dict",
            None,
            {"name": "", "slug": "empty"},                   # blank name → drop
            {"slug": "no_name", "relevance": 0.5},            # no name → drop
            {"name": "Valid", "slug": "valid", "relevance": 0.6},
        ]
    }
    result = _parse_llm_response(raw, source="llm")
    assert len(result.topics) == 1
    assert result.topics[0].name == "Valid"


def test_parse_clamps_invalid_relevance():
    raw = {
        "topics": [
            {"name": "Negative", "slug": "neg", "relevance": -0.5},
            {"name": "Over", "slug": "over", "relevance": 2.5},
            {"name": "Bad", "slug": "bad", "relevance": "not_a_number"},
        ]
    }
    result = _parse_llm_response(raw, source="llm")
    # All clamped into [0, 1]
    for t in result.topics:
        assert 0.0 <= t.relevance <= 1.0


def test_parse_dedupes_slugs():
    """If LLM emits two topics with the same slug, keep only the first."""
    raw = {
        "topics": [
            {"name": "Services", "slug": "services", "relevance": 0.9},
            {"name": "Services Duplicate", "slug": "services", "relevance": 0.7},
        ]
    }
    result = _parse_llm_response(raw, source="llm")
    assert len(result.topics) == 1
    assert result.topics[0].name == "Services"


def test_parse_synthesizes_slug_when_missing():
    """LLM might omit slug; we slugify the name."""
    raw = {"topics": [{"name": "Movate Services", "relevance": 0.9}]}
    result = _parse_llm_response(raw, source="llm")
    assert result.topics[0].slug == "movate_services"


# ---------------------------------------------------------------- _heuristic_extract


def test_heuristic_extracts_kb_name_as_topic():
    """A KB-backed agent should produce at least one topic from the rag_name."""
    agent_def = {
        "name": "FAQ Bot",
        "agent_role": "Customer FAQ",
        "features": [
            {
                "type": "KNOWLEDGE_BASE",
                "config": {
                    "lyzr_rag": {
                        "rag_name": "movate_website_knowledge_base",
                        "rag_id": "abc",
                    }
                }
            }
        ]
    }
    result = _heuristic_extract(agent_def)
    assert result.source == "heuristic"
    # Should have a topic derived from the KB name
    names = [t.name for t in result.topics]
    assert any("Movate Website Knowledge Base" in n or "Movate" in n for n in names)


def test_heuristic_strips_trailing_lyzr_hash():
    """Lyzr KB names often end in a 4-8 char hash like 'yg9f'. Strip it."""
    agent_def = {
        "name": "x",
        "features": [
            {
                "type": "KNOWLEDGE_BASE",
                "config": {"lyzr_rag": {"rag_name": "movate_website_knowledge_baseyg9f"}}
            }
        ]
    }
    result = _heuristic_extract(agent_def)
    name = result.topics[0].name
    # Trailing 'yg9f' should be gone
    assert "yg9f" not in name.lower()


def test_heuristic_strips_letters_only_lyzr_hash():
    """Lyzr KB names sometimes end in a letters-only hash like 'vnzb'
    (no digits, no vowels — clearly random). Strip it.

    Regression: real Lyzr export from SanDisk Returns Manager v4 had
    `sandisk_ordersvnzb` — the strip needs to handle this even without
    a digit in the suffix.
    """
    agent_def = {
        "name": "x",
        "features": [
            {
                "type": "KNOWLEDGE_BASE",
                "config": {"lyzr_rag": {"rag_name": "sandisk_ordersvnzb"}}
            }
        ]
    }
    result = _heuristic_extract(agent_def)
    name = result.topics[0].name.lower()
    assert "vnzb" not in name
    assert "sandisk orders" in name


def test_heuristic_does_not_strip_real_word_endings():
    """Names ending in a real word (with a vowel) should NOT be stripped.
    e.g. `customer_supportbase` should stay as-is — `base` is a real word."""
    from mdk_eval.insights.topic_extractor import _strip_lyzr_hash
    # Real word endings with vowels — keep
    assert _strip_lyzr_hash("customer_supportbase") == "customer_supportbase"
    assert _strip_lyzr_hash("agent_kb_lite") == "agent_kb_lite"  # short, no strip
    # All-letters-no-vowels ending — strip (looks like Lyzr hash)
    assert _strip_lyzr_hash("sandisk_ordersvnzb") == "sandisk_orders"
    # Mixed alpha-num ending — strip (classic Lyzr hash)
    assert _strip_lyzr_hash("movate_website_knowledge_baseyg9f") == "movate_website_knowledge_base"


def test_recommend_counts_floors_every_topic_at_one():
    """Coverage guarantee: every extracted topic gets recommended_count >= 1."""
    from mdk_eval.insights.topic_extractor import (
        Topic,
        _recommend_counts,
    )
    topics = [
        Topic(name="A", slug="a", description="x", relevance=0.95, example_queries=[]),
        Topic(name="B", slug="b", description="x", relevance=0.90, example_queries=[]),
        Topic(name="C", slug="c", description="x", relevance=0.90, example_queries=[]),
        Topic(name="D", slug="d", description="x", relevance=0.85, example_queries=[]),
        Topic(name="E", slug="e", description="x", relevance=0.80, example_queries=[]),
    ]
    _recommend_counts(topics, total_budget=13)
    # Every topic gets at least 1 — no zeros allowed.
    for t in topics:
        assert t.recommended_count >= 1, f"{t.slug} got {t.recommended_count}"
    # Total matches the budget.
    assert sum(t.recommended_count for t in topics) == 13


def test_recommend_counts_proportional_by_relevance():
    """Higher-relevance topics get more of the leftover budget."""
    from mdk_eval.insights.topic_extractor import (
        Topic,
        _recommend_counts,
    )
    topics = [
        Topic(name="High", slug="high", description="x", relevance=1.0, example_queries=[]),
        Topic(name="Low", slug="low", description="x", relevance=0.1, example_queries=[]),
    ]
    _recommend_counts(topics, total_budget=10)
    # Both get ≥ 1, but High should have more than Low.
    assert topics[0].recommended_count > topics[1].recommended_count
    assert sum(t.recommended_count for t in topics) == 10


def test_recommend_counts_tight_budget_keeps_floor():
    """When budget < num_topics, every topic still gets 1 (floor wins)."""
    from mdk_eval.insights.topic_extractor import (
        Topic,
        _recommend_counts,
    )
    topics = [
        Topic(name=f"T{i}", slug=f"t{i}", description="x", relevance=0.5, example_queries=[])
        for i in range(8)
    ]
    _recommend_counts(topics, total_budget=3)   # less than 8 topics
    # Each still gets at least 1; total may exceed budget (we prefer over-allocate).
    for t in topics:
        assert t.recommended_count == 1


def test_recommend_counts_empty_input_no_op():
    from mdk_eval.insights.topic_extractor import _recommend_counts
    _recommend_counts([], total_budget=13)   # should not raise


def test_recommend_counts_handles_zero_relevance():
    """Edge case: heuristic fallback with all-zero relevance shouldn't divide-by-zero."""
    from mdk_eval.insights.topic_extractor import (
        Topic,
        _recommend_counts,
    )
    topics = [
        Topic(name="A", slug="a", description="x", relevance=0.0, example_queries=[]),
        Topic(name="B", slug="b", description="x", relevance=0.0, example_queries=[]),
    ]
    _recommend_counts(topics, total_budget=10)
    # Both get the floor + leftover split somehow (relevance is 0 so equal).
    for t in topics:
        assert t.recommended_count >= 1


def test_recommended_count_in_endpoint_response(client):
    """End-to-end: /api/agent-definitions/topics surfaces recommended_count
    on every topic with value ≥ 1."""
    import json as _json
    from mdk_eval.evaluators.judges import cache as judge_cache
    agent_def = {
        "name": "FAQ Assistant",
        "agent_role": "Customer FAQ",
        "agent_instructions": "Help customers with questions.",
        "agent_goal": "Provide accurate answers.",
        "features": [{
            "type": "KNOWLEDGE_BASE",
            "config": {"lyzr_rag": {"rag_name": "movate_faq_kb"}}
        }],
    }
    with patch.object(judge_cache, "get", return_value=None):
        with patch.object(judge_cache, "put"):
            with patch("mdk_eval.evaluators.judges.llm_clients.call_judge",
                       side_effect=RuntimeError("offline")):
                r = client.post(
                    "/api/agent-definitions/topics",
                    headers={**_auth()},
                    data={"agent_definition_json": _json.dumps(agent_def)},
                )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["topics"], "expected at least one topic"
    for t in body["topics"]:
        assert "recommended_count" in t, f"missing recommended_count on {t['slug']}"
        assert t["recommended_count"] >= 1, (
            f"{t['slug']} should have recommended_count ≥ 1, got {t['recommended_count']}"
        )


def test_managed_agent_coverage_adds_uncovered_sub_agents():
    """Post-process step: for a manager agent, every sub-agent must have a
    dedicated topic. If the LLM missed one, synthesise it from the
    sub-agent's name + usage_description."""
    from mdk_eval.insights.topic_extractor import (
        Topic,
        TopicExtractionResult,
        _ensure_managed_agent_coverage,
    )

    # Simulate: LLM returned 2 broad topics that don't mention 2 of the 3
    # managed sub-agents specifically.
    initial = TopicExtractionResult(
        topics=[
            Topic(name="OCR Processing", slug="ocr_processing",
                  description="Image-to-text extraction tasks", relevance=0.9,
                  example_queries=[]),
            Topic(name="General Routing", slug="general_routing",
                  description="High-level workflow routing", relevance=0.6,
                  example_queries=[]),
        ],
        source="llm",
    )
    agent_def = {
        "managed_agents": [
            {"name": "OCR Agent", "usage_description": "Identify product type and extract visible text"},
            {"name": "Validator Agent", "usage_description": "Cross-check extracted fields against business rules"},
            {"name": "Knowledge Base Agent", "usage_description": "Look up policy and product details"},
        ],
    }

    result = _ensure_managed_agent_coverage(initial, agent_def)
    slugs = {t.slug for t in result.topics}
    # OCR was covered by name match → no synthesis
    # Validator and Knowledge Base were uncovered → synthesised
    assert "validator_agent" in slugs
    assert "knowledge_base_agent" in slugs
    # Original LLM topics preserved
    assert "ocr_processing" in slugs
    assert "general_routing" in slugs


def test_managed_agent_coverage_detects_short_acronym_match():
    """Regression: SanDisk Returns Manager has an 'OCR Agent' sub-agent.
    The LLM produced a topic 'Image OCR Processing' that clearly covers
    OCR delegation — the post-processor should NOT also synthesise a
    redundant 'OCR Agent' topic.

    This was failing in production because the salient-word check required
    len >= 4 and 'OCR' is len 3 — short acronyms in agent names need
    special handling.
    """
    from mdk_eval.insights.topic_extractor import (
        Topic,
        TopicExtractionResult,
        _ensure_managed_agent_coverage,
    )
    initial = TopicExtractionResult(
        topics=[Topic(
            name="Image OCR Processing",
            slug="image_ocr_processing",
            description="Requesting and processing product images through the OCR Agent to extract visible text",
            relevance=0.9,
            example_queries=[],
        )],
        source="llm",
    )
    agent_def = {
        "managed_agents": [
            {"name": "OCR Agent", "usage_description": "handles ocr"},
        ],
    }
    result = _ensure_managed_agent_coverage(initial, agent_def)
    # Should NOT have synthesised a duplicate — still 1 topic.
    assert len(result.topics) == 1
    assert result.topics[0].slug == "image_ocr_processing"
    # No "uncovered" note should have been added.
    assert not any("uncovered managed_agent" in n.lower() for n in result.notes)


def test_managed_agent_coverage_detects_short_acronym_kb():
    """Same class of bug for 'KB Agent' (len 2 acronym + Agent suffix).
    A topic mentioning 'knowledge base' should cover a 'KB Agent' sub-agent."""
    from mdk_eval.insights.topic_extractor import (
        Topic,
        TopicExtractionResult,
        _ensure_managed_agent_coverage,
    )
    initial = TopicExtractionResult(
        topics=[Topic(
            name="Knowledge Base Lookup",
            slug="knowledge_base_lookup",
            description="Lookups against the KB to retrieve policy and product details",
            relevance=0.85,
            example_queries=[],
        )],
        source="llm",
    )
    agent_def = {
        "managed_agents": [
            {"name": "KB Agent", "usage_description": "Look up policy"},
        ],
    }
    result = _ensure_managed_agent_coverage(initial, agent_def)
    # Should NOT synthesise — "KB" appears in the description.
    assert len(result.topics) == 1


def test_managed_agent_coverage_skips_already_covered():
    """If the LLM already produced a topic that mentions the sub-agent's name
    (or salient words from usage_description), don't double-tag."""
    from mdk_eval.insights.topic_extractor import (
        Topic,
        TopicExtractionResult,
        _ensure_managed_agent_coverage,
    )
    initial = TopicExtractionResult(
        topics=[Topic(
            name="OCR Agent",
            slug="ocr_agent",
            description="Tests the OCR sub-agent's text extraction",
            relevance=0.95,
            example_queries=[],
        )],
        source="llm",
    )
    agent_def = {
        "managed_agents": [
            {"name": "OCR Agent", "usage_description": "Extract text from images"},
        ],
    }
    result = _ensure_managed_agent_coverage(initial, agent_def)
    # Should not have duplicated — still 1 topic
    assert len(result.topics) == 1
    assert result.topics[0].slug == "ocr_agent"


def test_managed_agent_coverage_strips_decorations():
    """Lyzr-style decorated names like '(R) OCR Agent [Returns Manager v4]'
    should be cleaned before becoming a slug."""
    from mdk_eval.insights.topic_extractor import (
        TopicExtractionResult,
        _ensure_managed_agent_coverage,
    )
    initial = TopicExtractionResult(topics=[], source="llm")
    agent_def = {
        "managed_agents": [
            {"name": "(R) OCR Agent [Returns Manager v4]",
             "usage_description": "Extract text from images"},
        ],
    }
    result = _ensure_managed_agent_coverage(initial, agent_def)
    assert len(result.topics) == 1
    # Decorations stripped before slugifying
    assert result.topics[0].slug == "ocr_agent"
    assert "OCR Agent" in result.topics[0].name


def test_managed_agent_coverage_no_op_for_non_managers():
    """An agent with no `managed_agents` field should pass through unchanged."""
    from mdk_eval.insights.topic_extractor import (
        Topic,
        TopicExtractionResult,
        _ensure_managed_agent_coverage,
    )
    initial = TopicExtractionResult(
        topics=[Topic(name="A", slug="a", description="x", relevance=0.5,
                      example_queries=[])],
        source="llm",
    )
    result = _ensure_managed_agent_coverage(initial, {"name": "Solo Agent"})
    assert result.topics == initial.topics
    # No coverage notes added
    assert not any("managed_agent" in n for n in result.notes)


def test_managed_agent_coverage_appends_note_when_synthesising():
    """User-visible: when topics are added, a note appears in the result."""
    from mdk_eval.insights.topic_extractor import (
        TopicExtractionResult,
        _ensure_managed_agent_coverage,
    )
    initial = TopicExtractionResult(topics=[], source="llm")
    agent_def = {
        "managed_agents": [
            {"name": "Validator", "usage_description": "Check fields"},
        ],
    }
    result = _ensure_managed_agent_coverage(initial, agent_def)
    assert any("uncovered managed_agent" in n.lower() for n in result.notes)


def test_managed_agent_coverage_lifts_max_topics_for_large_managers():
    """A manager with 10 sub-agents should be allowed > MAX_TOPICS=10 topics
    so every sub-agent gets coverage."""
    from mdk_eval.insights.topic_extractor import (
        TopicExtractionResult,
        _ensure_managed_agent_coverage,
    )
    initial = TopicExtractionResult(topics=[], source="llm")
    agent_def = {
        "managed_agents": [
            {"name": f"Agent {i}", "usage_description": f"Task {i}"}
            for i in range(10)
        ],
    }
    result = _ensure_managed_agent_coverage(initial, agent_def)
    # Should have all 10 + room for cross-cutting (cap = max(10, 10+4) = 14)
    assert len(result.topics) == 10


def test_heuristic_extracts_managed_agents_as_topics():
    """For multi-agent systems, each managed agent often represents a topic."""
    agent_def = {
        "name": "Returns Manager",
        "managed_agents": [
            {"id": "1", "name": "(R) OCR Agent [Returns Manager v4]",
             "usage_description": "Extract text from product images"},
            {"id": "2", "name": "Product Validator",
             "usage_description": "Validate product against catalog"},
        ]
    }
    result = _heuristic_extract(agent_def)
    names = [t.name for t in result.topics]
    # Decorations stripped
    assert "OCR Agent" in names
    assert "Product Validator" in names


def test_heuristic_always_returns_at_least_one_topic():
    """Even with a minimal agent, we never return an empty list."""
    agent_def = {"name": "Bare Agent", "agent_role": "Simple helper"}
    result = _heuristic_extract(agent_def)
    assert len(result.topics) >= 1


def test_heuristic_caps_at_max_topics():
    """Lots of managed agents → cap at MAX_TOPICS (10)."""
    agent_def = {
        "name": "Mega Manager",
        "managed_agents": [
            {"id": str(i), "name": f"Sub Agent {i}", "usage_description": "x"}
            for i in range(20)
        ],
    }
    result = _heuristic_extract(agent_def)
    assert len(result.topics) <= 10


def test_heuristic_dedupes_slugs():
    """Two managed agents with similar names shouldn't collide on slug."""
    agent_def = {
        "name": "x",
        "managed_agents": [
            {"id": "1", "name": "Validator"},
            {"id": "2", "name": "Validator"},     # exact duplicate
        ]
    }
    result = _heuristic_extract(agent_def)
    slugs = [t.slug for t in result.topics]
    assert len(slugs) == len(set(slugs))


# ---------------------------------------------------------------- extract_topics integration


def test_extract_topics_uses_cache_when_available(monkeypatch):
    """When the judge cache has a hit, no LLM call is made."""
    from mdk_eval.evaluators.judges import cache as judge_cache

    cached_response = {
        "topics": [
            {"name": "Cached Topic", "slug": "cached", "relevance": 0.9}
        ]
    }
    with patch.object(judge_cache, "get", return_value=cached_response):
        # call_judge MUST NOT be called when cache hits
        with patch("mdk_eval.evaluators.judges.llm_clients.call_judge",
                   side_effect=AssertionError("should not call LLM on cache hit")):
            result = extract_topics({"name": "anything"})
    assert result.source == "cached"
    assert len(result.topics) == 1
    assert result.topics[0].name == "Cached Topic"


def test_extract_topics_falls_back_to_heuristic_on_llm_error():
    """When the LLM call raises, fall back to heuristic and surface in notes."""
    from mdk_eval.evaluators.judges import cache as judge_cache

    with patch.object(judge_cache, "get", return_value=None):
        with patch.object(judge_cache, "put"):
            with patch("mdk_eval.evaluators.judges.llm_clients.call_judge",
                       side_effect=RuntimeError("LLM unavailable")):
                result = extract_topics({
                    "name": "FAQ Bot",
                    "features": [{
                        "type": "KNOWLEDGE_BASE",
                        "config": {"lyzr_rag": {"rag_name": "movate_kb"}}
                    }]
                })
    assert result.source == "heuristic"
    assert any("LLM call failed" in n for n in result.notes)
    assert len(result.topics) >= 1


@pytest.mark.asyncio
async def test_extract_topics_async_caches_result():
    """A successful LLM call should cache the response for future use."""
    from mdk_eval.evaluators.judges import cache as judge_cache

    llm_response = {
        "topics": [
            {"name": "From LLM", "slug": "from_llm", "relevance": 0.8},
        ]
    }
    cache_get_calls = []
    cache_put_calls = []

    def fake_get(*args):
        cache_get_calls.append(args)
        return None

    def fake_put(*args):
        cache_put_calls.append(args)

    async def fake_call_judge(*args, **kwargs):
        return llm_response

    with patch.object(judge_cache, "get", side_effect=fake_get):
        with patch.object(judge_cache, "put", side_effect=fake_put):
            with patch("mdk_eval.evaluators.judges.llm_clients.call_judge",
                       side_effect=fake_call_judge):
                result = await extract_topics_async({"name": "Test Agent"})

    assert result.source == "llm"
    assert len(result.topics) == 1
    # Should have written to cache
    assert len(cache_put_calls) == 1


def test_extract_topics_empty_llm_response_falls_back_to_heuristic():
    """If the LLM returns an empty topics list, fall back to heuristic."""
    from mdk_eval.evaluators.judges import cache as judge_cache

    with patch.object(judge_cache, "get", return_value=None):
        with patch.object(judge_cache, "put"):
            with patch("mdk_eval.evaluators.judges.llm_clients.call_judge",
                       new_callable=AsyncMock, return_value={"topics": []}):
                result = extract_topics({
                    "name": "Bare",
                    "features": [{
                        "type": "KNOWLEDGE_BASE",
                        "config": {"lyzr_rag": {"rag_name": "test_kb"}}
                    }]
                })
    # LLM returned empty → heuristic kicked in
    assert result.source == "heuristic"
    assert len(result.topics) >= 1
    assert any("no parseable topics" in n.lower() or "heuristically" in n.lower()
               for n in result.notes)


def test_extract_topics_returns_proper_dataclass():
    """Sanity: the public API returns the documented dataclass shape."""
    result = _heuristic_extract({"name": "Test"})
    assert isinstance(result, TopicExtractionResult)
    assert all(isinstance(t, Topic) for t in result.topics)
    assert result.source in ("llm", "cached", "heuristic")


# ---------------------------------------------------------------- HTTP endpoint


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MDK_WEB_API_KEY", "test-key")
    monkeypatch.setenv("MDK_WEB_CORS_ORIGINS", "http://localhost:3000")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    import importlib
    import mdk_eval.web.server as srv
    importlib.reload(srv)
    from fastapi.testclient import TestClient
    return TestClient(srv.app)


def _auth() -> dict:
    return {"Authorization": "Bearer test-key"}


def test_topics_endpoint_400s_without_source(client):
    r = client.post("/api/agent-definitions/topics", headers=_auth())
    assert r.status_code == 400


def test_topics_endpoint_returns_topics_from_inline_json(client):
    """Endpoint accepts inline JSON and returns a topic list (heuristic path
    when LLM unavailable)."""
    import json as _json
    agent_def = {
        "name": "Movate FAQ",
        "agent_role": "Customer FAQ",
        "features": [{
            "type": "KNOWLEDGE_BASE",
            "config": {"lyzr_rag": {"rag_name": "movate_website_kb_xyz1"}}
        }],
    }
    from mdk_eval.evaluators.judges import cache as judge_cache
    with patch.object(judge_cache, "get", return_value=None):
        with patch.object(judge_cache, "put"):
            with patch("mdk_eval.evaluators.judges.llm_clients.call_judge",
                       side_effect=RuntimeError("offline")):
                r = client.post(
                    "/api/agent-definitions/topics",
                    headers={**_auth()},
                    data={"agent_definition_json": _json.dumps(agent_def)},
                )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "topics" in body
    assert len(body["topics"]) >= 1
    assert body["source"] == "heuristic"
    # Each topic carries the expected fields
    for t in body["topics"]:
        for field in ("name", "slug", "description", "relevance", "example_queries"):
            assert field in t


def test_topics_endpoint_rejects_invalid_json(client):
    r = client.post(
        "/api/agent-definitions/topics",
        headers=_auth(),
        data={"agent_definition_json": "not_valid_json"},
    )
    assert r.status_code == 400
    assert "json" in r.json()["detail"].lower()


def test_topics_endpoint_rejects_non_object_json(client):
    r = client.post(
        "/api/agent-definitions/topics",
        headers=_auth(),
        data={"agent_definition_json": "[\"a\", \"b\"]"},  # array, not object
    )
    assert r.status_code == 400


def test_topics_endpoint_requires_auth(client):
    r = client.post(
        "/api/agent-definitions/topics",
        data={"agent_definition_json": "{\"name\": \"x\"}"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------- propose-one with topic


def test_propose_one_accepts_topic_field_and_tags_scenario(client, monkeypatch):
    """When `topic` is supplied, the resulting scenario is tagged
    `topic:<slug>` so downstream filtering can find it."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")    # gate `is_available()`
    from mdk_eval.evaluators.judges import cache as judge_cache

    fake_llm_response = {
        "scenarios": [{
            "id": "movate_services_q1",
            "description": "Test agent answers a Movate Services question",
            "input": {"prompt": "What does Movate's CX team do?"},
            "severity": "medium",
            "tags": ["happy", "category:standard"],
            "forbidden_phrases": [],
            "rubric_focus": "correctness",
            "constraint_quote": "answer questions about Movate",
            "reasoning": "covers the core happy path",
        }]
    }
    with patch.object(judge_cache, "get", return_value=None):
        with patch.object(judge_cache, "put"):
            with patch("mdk_eval.evaluators.judges.llm_clients.call_judge",
                       new_callable=AsyncMock, return_value=fake_llm_response):
                r = client.post(
                    "/api/scenarios/propose-one",
                    headers=_auth(),
                    json={
                        "natural_language_request": "Test agent answers about CX team",
                        "category": "standard",
                        "topic": "Movate Services",
                        "agent_definition": {
                            "name": "FAQ", "agent_role": "FAQ",
                            "agent_instructions": "answer questions about Movate"
                        },
                    },
                )
    assert r.status_code == 200, r.text
    body = r.json()
    # The tags list must include the topic tag with slugified topic name
    tags = body["scenario"]["tags"]
    assert any(t == "topic:movate_services" for t in tags), \
        f"missing topic tag in scenario tags: {tags}"


def test_propose_one_topic_is_optional(client, monkeypatch):
    """No topic = behaves exactly as before. Backward compatible."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from mdk_eval.evaluators.judges import cache as judge_cache

    fake_llm_response = {
        "scenarios": [{
            "id": "x",
            "description": "x",
            "input": {"prompt": "x"},
            "severity": "medium",
            "tags": [],
            "forbidden_phrases": [],
            "rubric_focus": "correctness",
            "constraint_quote": "x",
            "reasoning": "x",
        }]
    }
    with patch.object(judge_cache, "get", return_value=None):
        with patch.object(judge_cache, "put"):
            with patch("mdk_eval.evaluators.judges.llm_clients.call_judge",
                       new_callable=AsyncMock, return_value=fake_llm_response):
                r = client.post(
                    "/api/scenarios/propose-one",
                    headers=_auth(),
                    json={
                        "natural_language_request": "Test something",
                        "category": "standard",
                        # NO topic field
                        "agent_definition": {"name": "x", "agent_role": "y",
                                              "agent_instructions": "z"},
                    },
                )
    assert r.status_code == 200, r.text
    tags = r.json()["scenario"]["tags"]
    # No topic tag should be present
    assert not any(t.startswith("topic:") for t in tags)

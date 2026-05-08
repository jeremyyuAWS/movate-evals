"""Endpoint tests for the Test Mix redesign.

Covers:
  - GET /api/mix-presets returns the preset library
  - POST /api/agent-definitions/preview rejects mix_json + topic_mix_json
    together
  - POST /api/agent-definitions/preview with topic_mix_json + behavior_preset_name
    routes through the 2D path and tags scenarios with topic:<slug>
  - POST /api/agent-definitions/preview with custom behavior_mix_json
  - POST /api/agent-definitions/preview backwards-compat: mix_json alone still
    works (no topic tags)
  - Error paths: bad JSON, unknown preset, custom preset without ratios

These tests do NOT touch the database; they use mocked LLM judge calls and
the heuristic extractor (which is a pure function over the agent definition).
"""
from __future__ import annotations

import json as _json
from unittest.mock import patch, AsyncMock

import pytest


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MDK_WEB_API_KEY", "test-key")
    monkeypatch.setenv("MDK_WEB_CORS_ORIGINS", "http://localhost:3000")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import importlib
    import mdk_eval.web.server as srv
    importlib.reload(srv)
    from fastapi.testclient import TestClient
    return TestClient(srv.app)


def _auth() -> dict:
    return {"Authorization": "Bearer test-key"}


def _agent_def() -> dict:
    return {
        "name": "Movate FAQ",
        "agent_role": "Customer FAQ assistant",
        "agent_instructions": "Help users with Movate-related questions.",
        "agent_goal": "Provide accurate Movate information.",
        "features": [
            {"type": "KNOWLEDGE_BASE", "config": {"lyzr_rag": {"rag_name": "movate_website_kb"}}},
        ],
    }


def _agent_file_payload() -> tuple[str, bytes, str]:
    """Build a multipart file tuple for FastAPI TestClient."""
    return ("agent.json", _json.dumps(_agent_def()).encode("utf-8"), "application/json")


# -------------------------- GET /api/mix-presets --------------------------


def test_mix_presets_endpoint_returns_three_presets(client):
    r = client.get("/api/mix-presets", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    names = [p["name"] for p in body["presets"]]
    assert names == ["balanced", "compliance_heavy", "reliability_focused"]
    assert body["default"] == "balanced"


def test_mix_presets_endpoint_each_preset_has_normalised_ratios(client):
    r = client.get("/api/mix-presets", headers=_auth())
    body = r.json()
    for preset in body["presets"]:
        assert "label" in preset
        assert "description" in preset
        assert preset["ratios"], f"{preset['name']} has empty ratios"
        # Ratios should sum to ~1.0 after normalisation.
        assert abs(sum(preset["ratios"].values()) - 1.0) < 0.001


def test_mix_presets_endpoint_requires_auth(client):
    r = client.get("/api/mix-presets")
    assert r.status_code == 401


# -------------------------- preview: 2D path --------------------------


def _stub_llm_response(scenarios: list[dict]) -> dict:
    return {"scenarios": scenarios}


def test_preview_2d_path_tags_scenarios_with_topic(client):
    """topic_mix_json + behavior_preset_name → each generated scenario carries
    both `category:<behavior>` and `topic:<slug>` tags."""
    # Topic extractor gets called first to look up topic metadata; mock it to
    # return the slugs we'll request so the preview proceeds through the 2D path.
    from mdk_eval.insights import topic_extractor
    from mdk_eval.insights.topic_extractor import Topic, TopicExtractionResult

    fake_topics = TopicExtractionResult(
        topics=[
            Topic(name="Movate Services", slug="movate_services",
                  description="Capabilities and offerings.",
                  relevance=0.95, example_queries=["What does Movate do?"]),
            Topic(name="Career & Hiring", slug="career_hiring",
                  description="Open roles and applications.",
                  relevance=0.7, example_queries=["Are you hiring?"]),
        ],
        source="llm",
    )

    # Build LLM scenario response — one matching scenario per cell.
    one_scenario = {
        "id": "test_one",
        "description": "Asks what services Movate offers",
        "input": {"prompt": "What services does Movate offer?"},
        "severity": "medium",
        "tags": [],
        "forbidden_phrases": [],
        "forbidden_claims": [],
        "rubric_focus": "correctness",
        "constraint_quote": "Help users with Movate-related questions.",
        "reasoning": "Tests baseline service knowledge.",
    }

    with patch.object(topic_extractor, "extract_topics_async",
                      AsyncMock(return_value=fake_topics)):
        with patch("mdk_eval.evaluators.judges.cache.get", return_value=None):
            with patch("mdk_eval.evaluators.judges.cache.put"):
                with patch(
                    "mdk_eval.evaluators.judges.llm_clients.call_judge",
                    AsyncMock(return_value=_stub_llm_response([one_scenario])),
                ):
                    r = client.post(
                        "/api/agent-definitions/preview",
                        headers=_auth(),
                        files={"file": _agent_file_payload()},
                        data={
                            "topic_mix_json": _json.dumps({
                                "movate_services": 2,
                                "career_hiring": 1,
                            }),
                            "behavior_preset_name": "balanced",
                        },
                    )

    assert r.status_code == 200, r.text
    body = r.json()
    # Should contain LLM-proposed scenarios tagged with topic:<slug>
    llm_scenarios = [s for s in body["scenarios"] if "derived:llm" in s.get("tags", [])]
    assert llm_scenarios, "expected at least one LLM-derived scenario"
    # At least one scenario carries a topic:<slug> tag from the requested topics.
    topic_tags = {
        t for s in llm_scenarios for t in s.get("tags", []) if t.startswith("topic:")
    }
    assert topic_tags & {"topic:movate_services", "topic:career_hiring"}, (
        f"expected topic:* tag in scenarios; got tags={topic_tags}"
    )
    # counts_by_topic_category surfaces the 2D breakdown.
    assert body["counts_by_topic_category"], "expected 2D breakdown to be populated"


def test_preview_2d_with_custom_behavior_ratios(client):
    """behavior_preset_name='custom' + behavior_mix_json sends user ratios."""
    from mdk_eval.insights import topic_extractor
    from mdk_eval.insights.topic_extractor import Topic, TopicExtractionResult

    fake_topics = TopicExtractionResult(
        topics=[Topic(name="X", slug="x_topic", description="x", relevance=0.5,
                      example_queries=[])],
        source="llm",
    )
    one_scenario = {
        "id": "x_test",
        "description": "Standard x test",
        "input": {"prompt": "Tell me about x."},
        "severity": "low",
        "tags": [],
        "forbidden_phrases": [],
        "forbidden_claims": [],
        "rubric_focus": "correctness",
        "constraint_quote": "Help with x.",
        "reasoning": "baseline",
    }

    with patch.object(topic_extractor, "extract_topics_async",
                      AsyncMock(return_value=fake_topics)):
        with patch("mdk_eval.evaluators.judges.cache.get", return_value=None):
            with patch("mdk_eval.evaluators.judges.cache.put"):
                with patch(
                    "mdk_eval.evaluators.judges.llm_clients.call_judge",
                    AsyncMock(return_value=_stub_llm_response([one_scenario])),
                ):
                    r = client.post(
                        "/api/agent-definitions/preview",
                        headers=_auth(),
                        files={"file": _agent_file_payload()},
                        data={
                            "topic_mix_json": _json.dumps({"x_topic": 3}),
                            "behavior_preset_name": "custom",
                            "behavior_mix_json": _json.dumps(
                                {"standard": 1.0, "edge": 0}
                            ),
                        },
                    )
    assert r.status_code == 200, r.text


def test_preview_2d_custom_without_behavior_mix_json_400s(client):
    r = client.post(
        "/api/agent-definitions/preview",
        headers=_auth(),
        files={"file": _agent_file_payload()},
        data={
            "topic_mix_json": _json.dumps({"x_topic": 3}),
            "behavior_preset_name": "custom",
            # missing behavior_mix_json
        },
    )
    assert r.status_code == 400
    assert "behavior_mix_json" in r.json()["detail"]


def test_preview_2d_unknown_preset_400s(client):
    r = client.post(
        "/api/agent-definitions/preview",
        headers=_auth(),
        files={"file": _agent_file_payload()},
        data={
            "topic_mix_json": _json.dumps({"x": 1}),
            "behavior_preset_name": "made_up_preset",
        },
    )
    assert r.status_code == 400
    assert "made_up_preset" in r.json()["detail"]
    assert "balanced" in r.json()["detail"]


def test_preview_2d_unknown_category_in_custom_400s(client):
    r = client.post(
        "/api/agent-definitions/preview",
        headers=_auth(),
        files={"file": _agent_file_payload()},
        data={
            "topic_mix_json": _json.dumps({"x": 1}),
            "behavior_preset_name": "custom",
            "behavior_mix_json": _json.dumps({"not_a_category": 1.0}),
        },
    )
    assert r.status_code == 400
    assert "not_a_category" in r.json()["detail"]


def test_preview_2d_invalid_topic_mix_json_400s(client):
    r = client.post(
        "/api/agent-definitions/preview",
        headers=_auth(),
        files={"file": _agent_file_payload()},
        data={
            "topic_mix_json": "not valid json",
            "behavior_preset_name": "balanced",
        },
    )
    assert r.status_code == 400
    assert "topic_mix_json" in r.json()["detail"].lower()


def test_preview_2d_negative_count_400s(client):
    r = client.post(
        "/api/agent-definitions/preview",
        headers=_auth(),
        files={"file": _agent_file_payload()},
        data={
            "topic_mix_json": _json.dumps({"x": -1}),
            "behavior_preset_name": "balanced",
        },
    )
    assert r.status_code == 400


def test_preview_rejects_both_mix_paths_simultaneously(client):
    r = client.post(
        "/api/agent-definitions/preview",
        headers=_auth(),
        files={"file": _agent_file_payload()},
        data={
            "mix_json": _json.dumps({"standard": 2}),
            "topic_mix_json": _json.dumps({"x": 1}),
        },
    )
    assert r.status_code == 400
    assert "exactly one" in r.json()["detail"].lower()


# -------------------------- preview: 1D backwards compat --------------------------


def test_preview_1d_legacy_path_still_works(client):
    """Existing callers passing only mix_json should continue to work and NOT
    receive topic tags."""
    one_scenario = {
        "id": "legacy_test",
        "description": "Legacy path test",
        "input": {"prompt": "hello"},
        "severity": "low",
        "tags": [],
        "forbidden_phrases": [],
        "forbidden_claims": [],
        "rubric_focus": "correctness",
        "constraint_quote": "x",
        "reasoning": "x",
    }
    with patch("mdk_eval.evaluators.judges.cache.get", return_value=None):
        with patch("mdk_eval.evaluators.judges.cache.put"):
            with patch(
                "mdk_eval.evaluators.judges.llm_clients.call_judge",
                AsyncMock(return_value=_stub_llm_response([one_scenario])),
            ):
                r = client.post(
                    "/api/agent-definitions/preview",
                    headers=_auth(),
                    files={"file": _agent_file_payload()},
                    data={"mix_json": _json.dumps({"standard": 1})},
                )
    assert r.status_code == 200, r.text
    body = r.json()
    # Backwards-compat: counts_by_topic_category should be empty (1D path)
    assert body["counts_by_topic_category"] == {}
    # No topic:* tags should appear
    for s in body["scenarios"]:
        for t in s.get("tags", []):
            assert not t.startswith("topic:"), f"unexpected topic tag in 1D path: {t}"


def test_preview_no_mix_at_all_runs_heuristic_only(client):
    """No mix_json and no topic_mix_json → still works; runs DEFAULT_MIX path
    or skips LLM if unavailable. Heuristic always runs."""
    with patch("mdk_eval.evaluators.judges.cache.get", return_value=None):
        with patch("mdk_eval.evaluators.judges.cache.put"):
            with patch(
                "mdk_eval.evaluators.judges.llm_clients.call_judge",
                AsyncMock(return_value=_stub_llm_response([])),
            ):
                r = client.post(
                    "/api/agent-definitions/preview",
                    headers=_auth(),
                    files={"file": _agent_file_payload()},
                )
    assert r.status_code == 200
    # Empty file rejection sanity check is separate; we just want this to
    # complete without error.


def test_preview_empty_file_400s(client):
    r = client.post(
        "/api/agent-definitions/preview",
        headers=_auth(),
        files={"file": ("empty.json", b"", "application/json")},
    )
    assert r.status_code == 400

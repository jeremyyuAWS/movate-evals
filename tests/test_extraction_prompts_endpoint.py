"""GET /api/extraction/prompts + POST /api/agent-definitions/preview tests.

Verifies:
- /api/extraction/prompts returns all 8 categories with directives
- The base_system_prompt is present and substantial
- /api/agent-definitions/preview accepts uploads and returns scenarios
- Preview surfaces the heuristic count + per-LLM-category counts
- Mix JSON parsing rejects invalid shapes
- Preview never persists (no DB writes)
- Sanitization runs (api_key in upload is redacted)
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("psycopg")

from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MDK_WEB_API_KEY", "test-key")
    monkeypatch.setenv("MDK_WEB_CORS_ORIGINS", "http://localhost:3000")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    import importlib
    import mdk_eval.web.server as srv
    importlib.reload(srv)
    return TestClient(srv.app)


def _auth() -> dict:
    return {"Authorization": "Bearer test-key"}


# ---------- /api/extraction/prompts ----------


def test_extraction_prompts_returns_all_categories(client):
    r = client.get("/api/extraction/prompts", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    cat_names = {c["name"] for c in body["categories"]}
    expected = {"standard", "edge", "adversarial", "safety", "honesty",
                "multi_turn", "performance", "custom"}
    assert cat_names == expected


def test_extraction_prompts_includes_base_system_prompt(client):
    r = client.get("/api/extraction/prompts", headers=_auth())
    body = r.json()
    assert body["base_system_prompt"]
    assert "evaluation engineer" in body["base_system_prompt"].lower()
    assert "constraint_quote" in body["base_system_prompt"]


def test_extraction_prompts_categories_have_directives(client):
    r = client.get("/api/extraction/prompts", headers=_auth())
    body = r.json()
    by_name = {c["name"]: c for c in body["categories"]}
    assert "STANDARD HAPPY PATH" in by_name["standard"]["directive"]
    assert "ADVERSARIAL" in by_name["adversarial"]["directive"]
    assert "SAFETY" in by_name["safety"]["directive"]


def test_extraction_prompts_default_mix_is_present(client):
    r = client.get("/api/extraction/prompts", headers=_auth())
    body = r.json()
    assert sum(body["default_mix"].values()) == 12
    assert set(body["default_mix"].keys()) == {"standard", "edge", "adversarial", "safety"}


def test_extraction_prompts_requires_auth(client):
    r = client.get("/api/extraction/prompts")
    assert r.status_code == 401


# ---------- /api/agent-definitions/preview ----------


def test_preview_rejects_empty_upload(client):
    r = client.post(
        "/api/agent-definitions/preview",
        headers=_auth(),
        files={"file": ("agent.json", b"", "application/json")},
    )
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()


def test_preview_rejects_invalid_json(client):
    r = client.post(
        "/api/agent-definitions/preview",
        headers=_auth(),
        files={"file": ("agent.json", b"not json", "application/json")},
    )
    assert r.status_code == 400
    assert "valid json" in r.json()["detail"].lower()


def test_preview_rejects_invalid_mix_json(client):
    agent = {"name": "x", "agent_role": "y", "agent_instructions": "z"}
    r = client.post(
        "/api/agent-definitions/preview",
        headers=_auth(),
        files={"file": ("agent.json", json.dumps(agent).encode(), "application/json")},
        data={"mix_json": "not json"},
    )
    assert r.status_code == 400
    assert "mix_json" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


def test_preview_drops_unknown_category_with_warning(client, monkeypatch):
    """Unknown categories in mix_json are silently filtered + reported as a
    warning — NOT a 400. This is a deliberate boundary tolerance: frontends
    sometimes include heuristic-only chip names (like 'tool_sequence') in
    the mix payload, and a hard error makes the preview unrecoverable.
    The extractor itself remains strict for direct (CLI/test) callers.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    agent = {"name": "x", "agent_role": "y", "agent_instructions": "z"}

    async def fake_call(*_a, **_k):
        return {"scenarios": []}

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        r = client.post(
            "/api/agent-definitions/preview",
            headers=_auth(),
            files={"file": ("agent.json", json.dumps(agent).encode(), "application/json")},
            data={"mix_json": json.dumps({"tool_sequence": 1, "standard": 1})},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    warnings_blob = " ".join(body.get("warnings", [])).lower()
    assert "tool_sequence" in warnings_blob
    assert "ignored" in warnings_blob
    # The bogus category should not appear as a real bucket in the response
    assert "tool_sequence" not in body.get("counts_by_category", {})


def test_preview_returns_scenarios_with_per_category_counts(client, monkeypatch):
    """Happy path: upload, run preview with explicit small mix, assert shape."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    agent = {
        "_id": "abc", "name": "Test", "agent_role": "Test",
        "agent_instructions": "Be helpful.",
    }

    async def fake_call(*_a, **_k):
        return {"scenarios": [{
            "id": "s1", "description": "test",
            "input": {"prompt": "hello"},
            "severity": "medium", "tags": [],
            "constraint_quote": "Be helpful.",
            "reasoning": "Tests baseline behavior.",
        }]}

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        r = client.post(
            "/api/agent-definitions/preview",
            headers=_auth(),
            files={"file": ("agent.json", json.dumps(agent).encode(), "application/json")},
            data={
                "mix_json": json.dumps({"standard": 1, "adversarial": 1}),
                "focus": "non-English handling",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "scenarios" in body
    assert "counts_by_category" in body
    assert body["estimated_cost_usd"] >= 0
    # Per-category counts should reflect what landed
    assert "standard" in body["counts_by_category"] or "adversarial" in body["counts_by_category"]


def test_preview_sanitizes_secrets_in_upload(client, monkeypatch):
    """An uploaded agent JSON with `api_key` should have it redacted, with a warning."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    agent_with_secret = {
        "_id": "abc", "name": "Test", "agent_role": "x",
        "agent_instructions": "y",
        "api_key": "sk-real-secret-do-not-leak",
    }

    async def fake_call(*_a, **_k):
        return {"scenarios": []}

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        r = client.post(
            "/api/agent-definitions/preview",
            headers=_auth(),
            files={"file": ("agent.json", json.dumps(agent_with_secret).encode(), "application/json")},
            data={"mix_json": json.dumps({"standard": 1})},
        )
    assert r.status_code == 200, r.text
    warnings = " ".join(r.json()["warnings"])
    assert "Sanitized" in warnings
    assert "api_key" in warnings


def test_preview_does_NOT_persist_to_db(client, monkeypatch):
    """Verify no write queries are issued — preview is read-only."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    write_queries: list = []

    @contextmanager
    def fake_connect():
        cur = MagicMock()
        def execute(sql, params=()):
            sql_str = str(sql).strip().lower()
            if any(verb in sql_str for verb in ("insert ", "update ", "delete ")):
                write_queries.append(sql_str)
            return None
        cur.execute.side_effect = execute
        cur.fetchone.return_value = None
        cur.fetchall.return_value = []
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    async def fake_call(*_a, **_k):
        return {"scenarios": []}

    agent = {"name": "x", "agent_role": "y", "agent_instructions": "z"}
    with patch("mdk_eval.web.server.db.connect", fake_connect), \
         patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        r = client.post(
            "/api/agent-definitions/preview",
            headers=_auth(),
            files={"file": ("agent.json", json.dumps(agent).encode(), "application/json")},
            data={"mix_json": json.dumps({"standard": 1})},
        )

    assert r.status_code == 200, r.text
    assert write_queries == [], f"preview must not write to DB; saw: {write_queries}"


def test_preview_no_llm_keys_falls_back_to_heuristic(client, monkeypatch):
    """When OPENAI_API_KEY is unset, preview still works — heuristic only,
    plus a warning that the LLM was skipped."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent = {"name": "x", "agent_role": "y", "agent_instructions": "z"}
    r = client.post(
        "/api/agent-definitions/preview",
        headers=_auth(),
        files={"file": ("agent.json", json.dumps(agent).encode(), "application/json")},
        data={"mix_json": json.dumps({"standard": 2})},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert any("LLM extractor skipped" in w for w in body["warnings"])

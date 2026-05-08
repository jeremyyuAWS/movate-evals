"""Phase 3 — single-scenario regenerate + inline edit via PATCH.

Verifies:
- POST /api/scenarios/{id}/regenerate
  * 404 when scenario doesn't exist
  * 400 when scenario_set has no stored agent_definition
  * 400 when LLM returns no scenarios
  * Happy path returns old + new payloads, bumps regeneration_count, resets status
  * Category override works; custom-without-directive 400s
  * Validation errors propagate as 400 (not 500)

- PATCH /api/scenarios/{id}
  * payload_patch_json applies as a shallow merge
  * Status auto-resets to 'unverified' on edit
  * Bad JSON in payload_patch_json → 400
  * Disallowed keys (id, meta) → 400
  * Edited payload that fails Scenario validation → 400
  * Combined edit + new_status='approved' applies edit first then status
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
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import importlib
    import mdk_eval.web.server as srv
    importlib.reload(srv)
    return TestClient(srv.app)


def _auth() -> dict:
    return {"Authorization": "Bearer test-key"}


# ----------------------------- regenerate -----------------------------


def _make_regen_db(scenario_ctx):
    """Mock db.connect that returns the given scenario context for regenerate."""
    cur = MagicMock()

    def execute(sql, params=()):
        return None
    cur.execute.side_effect = execute
    cur.fetchone.return_value = (1,)
    cur.fetchall.return_value = []
    cur.__enter__ = lambda self: cur
    cur.__exit__ = lambda *a: None
    conn = MagicMock()
    conn.cursor.return_value = cur

    @contextmanager
    def fake():
        yield conn
    return fake


def test_regenerate_404_when_scenario_missing(client):
    @contextmanager
    def fake():
        cur = MagicMock()
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake), \
         patch("mdk_eval.web.server.db.get_scenario_for_regenerate", return_value=None):
        r = client.post("/api/scenarios/9999/regenerate", headers=_auth(), json={})
    assert r.status_code == 404


def test_regenerate_400_when_no_stored_agent_definition(client):
    """Scenarios from before migration 006 have null agent_definition; we
    can't regenerate without it. Must be a clean 400 with a re-upload hint."""
    ctx = {
        "id": 1, "scenario_id": "s1", "payload": {"id": "s1", "input": {"prompt": "x"}},
        "scenario_set_id": 10,
        "regeneration_count": 0, "derived_from": {"category": "standard"},
        "agent_definition": None,         # ← key gap
        "source_sha256": "abc", "source_filename": "x.json",
    }

    @contextmanager
    def fake():
        cur = MagicMock()
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake), \
         patch("mdk_eval.web.server.db.get_scenario_for_regenerate", return_value=ctx):
        r = client.post("/api/scenarios/1/regenerate", headers=_auth(), json={})
    assert r.status_code == 400
    assert "predates" in r.json()["detail"].lower() or "re-upload" in r.json()["detail"].lower()


def test_regenerate_400_when_llm_returns_no_scenarios(client):
    """Empty LLM response → 502 (LLM didn't deliver). Includes a useful hint."""
    ctx = {
        "id": 1, "scenario_id": "s1", "payload": {"id": "s1", "input": {"prompt": "x"}},
        "scenario_set_id": 10,
        "regeneration_count": 0, "derived_from": {"category": "standard"},
        "agent_definition": {"name": "Test"},
        "source_sha256": "abc", "source_filename": "x.json",
    }

    @contextmanager
    def fake():
        cur = MagicMock()
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    async def empty_llm(*_a, **_k):
        return {"scenarios": []}

    with patch("mdk_eval.web.server.db.connect", fake), \
         patch("mdk_eval.web.server.db.get_scenario_for_regenerate", return_value=ctx), \
         patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=empty_llm):
        r = client.post("/api/scenarios/1/regenerate", headers=_auth(), json={})
    assert r.status_code == 502
    assert "no scenarios" in r.json()["detail"].lower()


def test_regenerate_happy_path(client):
    """End-to-end: returns old + new payloads, bumps count, returns category."""
    old_payload = {
        "id": "movate_faq__test_scenario",
        "description": "Original description.",
        "input": {"prompt": "old prompt"},
        "tags": ["category:standard", "derived:llm"],
        "severity": "medium",
        "meta": {"derived_from": {"category": "standard", "reasoning": "old"}},
    }
    ctx = {
        "id": 1, "scenario_id": "movate_faq__test_scenario",
        "payload": old_payload, "scenario_set_id": 10,
        "regeneration_count": 0,
        "derived_from": {"category": "standard"},
        "agent_definition": {
            "name": "Test", "agent_role": "Test",
            "agent_instructions": "Be helpful.",
        },
        "source_sha256": "abc", "source_filename": "x.json",
    }

    @contextmanager
    def fake():
        cur = MagicMock()
        cur.fetchone.return_value = (1,)  # regeneration_count after bump
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    async def fake_llm(*_a, **_k):
        return {"scenarios": [{
            "id": "regen_test",
            "description": "Regenerated description.",
            "input": {"prompt": "new prompt"},
            "severity": "high",
            "tags": [],
            "constraint_quote": "Be helpful.",
            "reasoning": "New reasoning post-regenerate.",
        }]}

    with patch("mdk_eval.web.server.db.connect", fake), \
         patch("mdk_eval.web.server.db.get_scenario_for_regenerate", return_value=ctx), \
         patch("mdk_eval.web.server.db.replace_scenario_payload",
               return_value={"regeneration_count": 1}), \
         patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_llm):
        r = client.post("/api/scenarios/1/regenerate", headers=_auth(), json={})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scenario_pk"] == 1
    assert body["regeneration_count"] == 1
    assert body["category"] == "standard"
    assert body["old_payload"]["input"]["prompt"] == "old prompt"
    assert body["new_payload"]["input"]["prompt"] == "new prompt"
    # Scenario id slug must be preserved across regeneration so external
    # references stay stable.
    assert body["new_payload"]["id"] == "movate_faq__test_scenario"


def test_regenerate_category_override(client):
    """Caller can switch a 'standard' scenario to 'adversarial' via override."""
    ctx = {
        "id": 1, "scenario_id": "s1", "payload": {},
        "scenario_set_id": 10, "regeneration_count": 0,
        "derived_from": {"category": "standard"},
        "agent_definition": {"name": "T"},
        "source_sha256": "abc", "source_filename": "x.json",
    }

    captured_system_prompts: list[str] = []

    async def fake_llm(provider, model, system, user, temperature):
        captured_system_prompts.append(system)
        return {"scenarios": [{
            "id": "x", "description": "y",
            "input": {"prompt": "z"}, "severity": "high",
            "constraint_quote": "Q",
        }]}

    @contextmanager
    def fake():
        cur = MagicMock()
        cur.fetchone.return_value = (1,)
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake), \
         patch("mdk_eval.web.server.db.get_scenario_for_regenerate", return_value=ctx), \
         patch("mdk_eval.web.server.db.replace_scenario_payload",
               return_value={"regeneration_count": 1}), \
         patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_llm):
        r = client.post(
            "/api/scenarios/1/regenerate",
            headers=_auth(),
            json={"category_override": "adversarial"},
        )

    assert r.status_code == 200
    assert r.json()["category"] == "adversarial"
    # Verify the LLM was actually called with the adversarial-category prompt
    assert any("ADVERSARIAL" in p for p in captured_system_prompts)


def test_regenerate_custom_without_directive_400s(client):
    ctx = {
        "id": 1, "scenario_id": "s1", "payload": {},
        "scenario_set_id": 10, "regeneration_count": 0,
        "derived_from": {"category": "standard"},
        "agent_definition": {"name": "T"},
        "source_sha256": "abc", "source_filename": "x.json",
    }

    @contextmanager
    def fake():
        cur = MagicMock()
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake), \
         patch("mdk_eval.web.server.db.get_scenario_for_regenerate", return_value=ctx):
        r = client.post(
            "/api/scenarios/1/regenerate",
            headers=_auth(),
            json={"category_override": "custom"},
        )
    assert r.status_code == 400
    assert "custom_directive" in r.json()["detail"].lower()


# ----------------------------- inline edit via PATCH -----------------------------


def test_patch_payload_patch_applies_shallow_merge(client):
    """payload_patch_json should shallow-merge into the existing payload."""
    existing_payload = {
        "id": "s1",
        "description": "old description",
        "input": {"prompt": "old"},
        "severity": "medium",
        "tags": ["unverified"],
        "rubric": {"pass_threshold": 0.7, "weight_correctness": 1.0,
                   "weight_grounding": 1.0, "weight_completeness": 1.0,
                   "weight_tool_usage": 0.5, "weight_ux_tone": 1.0},
    }

    @contextmanager
    def fake():
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (existing_payload,),                      # SELECT payload
            (1, "unverified"),                        # SELECT id, status (final)
        ]
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.patch(
            "/api/scenarios/1",
            headers=_auth(),
            data={
                "payload_patch_json": json.dumps({
                    "input": {"prompt": "new edited prompt"},
                    "description": "edited description",
                }),
            },
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["edited"] is True
    # Status was reset to 'unverified' by the edit
    assert body["status"] == "unverified"


def test_patch_disallowed_keys_400s(client):
    """Trying to edit `id` or `meta` should be rejected — those track provenance."""
    r = client.patch(
        "/api/scenarios/1",
        headers=_auth(),
        data={"payload_patch_json": json.dumps({"id": "evil_new_id"})},
    )
    assert r.status_code == 400
    assert "non-editable" in r.json()["detail"].lower()


def test_patch_invalid_json_400s(client):
    r = client.patch(
        "/api/scenarios/1",
        headers=_auth(),
        data={"payload_patch_json": "not json {"},
    )
    assert r.status_code == 400
    assert "invalid" in r.json()["detail"].lower()


def test_patch_validation_failure_400s(client):
    """If the merged payload fails Scenario validation, the edit must 400 — not corrupt the row."""
    bad_existing = {"id": "s1", "input": {"prompt": "x"}}

    @contextmanager
    def fake():
        cur = MagicMock()
        cur.fetchone.side_effect = [(bad_existing,)]
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.patch(
            "/api/scenarios/1",
            headers=_auth(),
            data={"payload_patch_json": json.dumps({
                "rubric": "this should be a dict, not a string",
            })},
        )
    assert r.status_code == 400
    assert "validation" in r.json()["detail"].lower() or "fails" in r.json()["detail"].lower()


def test_patch_combined_edit_and_approve(client):
    """Edit + approve in one PATCH: edit applies first (resets to unverified),
    then explicit new_status='approved' overrides."""
    existing = {
        "id": "s1", "description": "x", "input": {"prompt": "old"},
        "severity": "medium", "tags": ["unverified"],
        "rubric": {"pass_threshold": 0.7, "weight_correctness": 1.0,
                   "weight_grounding": 1.0, "weight_completeness": 1.0,
                   "weight_tool_usage": 0.5, "weight_ux_tone": 1.0},
    }

    @contextmanager
    def fake():
        cur = MagicMock()
        cur.fetchone.side_effect = [(existing,), (1, "approved")]
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.patch(
            "/api/scenarios/1",
            headers=_auth(),
            data={
                "payload_patch_json": json.dumps({"input": {"prompt": "edited"}}),
                "new_status": "approved",
                "verified_by": "tester@example.com",
            },
        )

    assert r.status_code == 200
    body = r.json()
    assert body["edited"] is True
    assert body["status"] == "approved"


def test_patch_status_only_still_works(client):
    """Existing PATCH callers (Bolt's frontend) that only send new_status
    must continue to work — backward compat."""
    @contextmanager
    def fake():
        cur = MagicMock()
        cur.fetchone.return_value = (1, "approved")
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.patch(
            "/api/scenarios/1",
            headers=_auth(),
            data={"new_status": "approved"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    assert r.json()["edited"] is False


def test_patch_no_fields_400s(client):
    @contextmanager
    def fake():
        cur = MagicMock()
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.patch("/api/scenarios/1", headers=_auth(), data={})
    assert r.status_code == 400
    assert "no fields" in r.json()["detail"].lower()


# ----------------------------- auth -----------------------------


def test_regenerate_requires_auth(client):
    r = client.post("/api/scenarios/1/regenerate")
    assert r.status_code == 401

"""POST /api/scenarios/propose-one + /api/scenario-sets/{id}/scenarios + /from-jsonl

Verifies:
- propose-one
  * Requires exactly one of (scenario_set_id, agent_definition)
  * Returns a single scenario from a natural-language request
  * 400 on unknown category
  * 400 when category=custom without custom_directive
  * 400 when scenario_set_id refers to a set without stored agent_definition
  * 503 when no LLM API key configured
  * 502 when LLM returns no scenarios
- POST /scenario-sets/{id}/scenarios
  * Manual create: 1 scenario in body, persists, returns added[]
  * Bulk: N scenarios in body, persists all, returns added[]
  * Duplicate scenario_id is SKIPPED (not errored), returned in skipped[]
  * Whole batch fails if ANY payload fails Scenario validation
  * Tags provenance via `source` field
  * 404 on unknown scenario_set
- POST /scenario-sets/{id}/scenarios/from-jsonl
  * Parses each line as a scenario; comments + empty lines skipped
  * Bad line aborts the batch (atomic semantics)
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


def _scenario(id_: str = "test_scenario", category: str = "standard") -> dict:
    """Minimum-valid scenario for the LLM mock to emit."""
    return {
        "id": id_,
        "description": "Tests something.",
        "input": {"prompt": "Hello?"},
        "severity": "medium",
        "tags": [],
        "constraint_quote": "Be helpful.",
        "reasoning": f"Tests {category} behavior.",
    }


# ---------- propose-one ----------


def test_propose_one_requires_exactly_one_source(client):
    """Both unset OR both set → 400."""
    r = client.post(
        "/api/scenarios/propose-one",
        headers=_auth(),
        json={"natural_language_request": "test something"},
    )
    assert r.status_code == 400
    assert "exactly one" in r.json()["detail"].lower()

    r = client.post(
        "/api/scenarios/propose-one",
        headers=_auth(),
        json={
            "natural_language_request": "test something",
            "scenario_set_id": 1,
            "agent_definition": {"name": "x"},
        },
    )
    assert r.status_code == 400


def test_propose_one_unknown_category_400s(client):
    """Bolt sometimes sends a topical category name (e.g., 'Analytics') by
    mistake — we 400 with a message that lists valid behavioral categories
    AND points to the `topic` field as the right place for topical names."""
    r = client.post(
        "/api/scenarios/propose-one",
        headers=_auth(),
        json={
            "natural_language_request": "test x",
            "agent_definition": {"name": "x", "agent_role": "y", "agent_instructions": "z"},
            "category": "not_a_real_category",
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    assert "unknown behavioral category" in detail
    # The error must enumerate valid categories so the caller can self-correct
    assert "valid categories" in detail
    assert "standard" in detail and "adversarial" in detail
    # And point to the topic field as the right place for topical names
    assert "topic" in detail


def test_propose_one_custom_without_directive_400s(client):
    r = client.post(
        "/api/scenarios/propose-one",
        headers=_auth(),
        json={
            "natural_language_request": "test x",
            "agent_definition": {"name": "x", "agent_role": "y", "agent_instructions": "z"},
            "category": "custom",
        },
    )
    assert r.status_code == 400
    assert "custom_directive" in r.json()["detail"].lower()


def test_propose_one_no_llm_returns_503(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post(
        "/api/scenarios/propose-one",
        headers=_auth(),
        json={
            "natural_language_request": "test x",
            "agent_definition": {"name": "x", "agent_role": "y", "agent_instructions": "z"},
            "category": "standard",
        },
    )
    assert r.status_code == 503


def test_propose_one_with_agent_definition_happy_path(client):
    async def fake_call(*_a, **_k):
        return {"scenarios": [_scenario("custom_pii_test", "safety")]}

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        r = client.post(
            "/api/scenarios/propose-one",
            headers=_auth(),
            json={
                "natural_language_request": "Test that the agent refuses to discuss salaries.",
                "agent_definition": {"name": "FAQ", "agent_role": "FAQ Assistant",
                                      "agent_instructions": "Be helpful. Don't leak PII."},
                "category": "safety",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["category"] == "safety"
    assert "scenario" in body
    assert body["scenario"]["meta"]["derived_from"]["category"] == "safety"


def test_propose_one_with_scenario_set_id_uses_stored_agent_definition(client):
    """If only scenario_set_id is provided, the endpoint looks up the agent
    definition from the DB (set by /api/agent-definitions or migration 006)."""
    stored_def = {"name": "Stored Agent", "agent_role": "X", "agent_instructions": "Y"}

    @contextmanager
    def fake_connect():
        cur = MagicMock()
        cur.fetchone.return_value = (stored_def,)  # get_scenario_set_agent_definition
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    async def fake_call(*_a, **_k):
        return {"scenarios": [_scenario("from_set", "edge")]}

    with patch("mdk_eval.web.server.db.connect", fake_connect), \
         patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=fake_call):
        r = client.post(
            "/api/scenarios/propose-one",
            headers=_auth(),
            json={
                "natural_language_request": "Test the boundary case.",
                "scenario_set_id": 5,
                "category": "edge",
            },
        )
    assert r.status_code == 200, r.text
    assert r.json()["scenario"]["meta"]["derived_from"]["category"] == "edge"


def test_propose_one_with_set_lacking_agent_definition_400s(client):
    """Pre-migration-006 sets have NULL agent_definition — 400 with re-upload hint."""
    @contextmanager
    def fake_connect():
        cur = MagicMock()
        cur.fetchone.return_value = (None,)
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake_connect):
        r = client.post(
            "/api/scenarios/propose-one",
            headers=_auth(),
            json={
                "natural_language_request": "x",
                "scenario_set_id": 99,
                "category": "standard",
            },
        )
    assert r.status_code == 400
    assert "re-upload" in r.json()["detail"].lower() or "pre-migration" in r.json()["detail"].lower()


def test_propose_one_llm_empty_returns_502(client):
    async def empty_llm(*_a, **_k):
        return {"scenarios": []}

    with patch("mdk_eval.evaluators.judges.llm_clients.call_judge", side_effect=empty_llm):
        r = client.post(
            "/api/scenarios/propose-one",
            headers=_auth(),
            json={
                "natural_language_request": "x",
                "agent_definition": {"name": "x", "agent_role": "y", "agent_instructions": "z"},
            },
        )
    assert r.status_code == 502
    assert "no scenarios" in r.json()["detail"].lower()


# ---------- add scenarios (manual / quick-add commit / bulk) ----------


def _valid_scenario_payload(slug: str = "manual_test_1") -> dict:
    """A complete scenario payload that passes Scenario.model_validate."""
    return {
        "id": slug,
        "description": "Manually-authored test.",
        "tags": ["manual", "unverified"],
        "severity": "medium",
        "input": {"prompt": "Manually authored prompt"},
        "expected_tools": [],
        "forbidden_phrases": [],
        "workflow": {"must_visit": [], "must_not_visit": [], "ordered_subsequence": []},
        "rubric": {
            "pass_threshold": 0.7,
            "weight_correctness": 1.0, "weight_grounding": 1.0,
            "weight_completeness": 1.0, "weight_tool_usage": 0.5,
            "weight_ux_tone": 1.0,
        },
        "meta": {},
    }


def test_add_scenarios_empty_list_400s(client):
    r = client.post(
        "/api/scenario-sets/1/scenarios",
        headers=_auth(),
        json={"scenarios": []},
    )
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()


def test_add_scenarios_validation_failure_aborts_batch(client):
    """If ANY payload fails Scenario validation, the batch fails — no partial writes."""
    @contextmanager
    def fake_connect():
        # Should never reach DB if validation fails first
        cur = MagicMock()
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake_connect):
        r = client.post(
            "/api/scenario-sets/1/scenarios",
            headers=_auth(),
            json={"scenarios": [
                _valid_scenario_payload("good"),
                {"this": "is not a valid scenario"},  # ← bad
            ]},
        )
    assert r.status_code == 400
    assert "scenarios[1]" in r.json()["detail"]


def test_add_scenarios_404_on_unknown_set(client):
    @contextmanager
    def fake_connect():
        cur = MagicMock()
        cur.fetchone.return_value = None  # set lookup misses
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake_connect):
        r = client.post(
            "/api/scenario-sets/99999/scenarios",
            headers=_auth(),
            json={"scenarios": [_valid_scenario_payload()]},
        )
    assert r.status_code == 404


def test_add_scenarios_persists_and_skips_duplicates(client):
    """Duplicates by scenario_id within the same set are SKIPPED — not errored.
    User sees them in `skipped[]` so they know what didn't land."""
    next_pk = iter(range(100, 200))

    @contextmanager
    def fake_connect():
        cur = MagicMock()
        # First fetchone: set existence check (returns truthy)
        # Subsequent fetchone calls: insert_scenario RETURNING id
        cur.fetchone.side_effect = lambda: (next(next_pk),)
        # fetchall for get_existing_scenario_ids: scenario "dupe_test" already there
        cur.fetchall.return_value = [("dupe_test",)]
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake_connect):
        r = client.post(
            "/api/scenario-sets/1/scenarios",
            headers=_auth(),
            json={"scenarios": [
                _valid_scenario_payload("new_one"),
                _valid_scenario_payload("dupe_test"),  # ← already exists
                _valid_scenario_payload("another_new"),
            ], "source": "bulk-import"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["added"]) == 2
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["scenario_id"] == "dupe_test"
    assert any("skipped" in w.lower() for w in body["warnings"])


def test_add_scenarios_tags_provenance_when_missing(client):
    """If the user-supplied payload has no `meta.derived_from`, we add one
    citing the source (manual / bulk-import / quick-add) so the dashboard
    drawer's 'Why this test' section can still say something useful."""
    next_pk = iter(range(100, 200))

    captured_payloads: list[dict] = []

    @contextmanager
    def fake_connect():
        cur = MagicMock()
        cur.fetchone.side_effect = lambda: (next(next_pk),)
        cur.fetchall.return_value = []  # no existing
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    def captured_insert(cur, *, scenario_set_id, scenario_id, payload, severity, tags, derived_from):
        captured_payloads.append({"derived_from": derived_from, "payload": payload})
        return next(next_pk)

    with patch("mdk_eval.web.server.db.connect", fake_connect), \
         patch("mdk_eval.web.server.db.insert_scenario", side_effect=captured_insert):
        r = client.post(
            "/api/scenario-sets/1/scenarios",
            headers=_auth(),
            json={
                "scenarios": [_valid_scenario_payload("provenance_test")],
                "source": "manual",
                "created_by": "tester@example.com",
            },
        )
    assert r.status_code == 200, r.text
    assert len(captured_payloads) == 1
    df = captured_payloads[0]["derived_from"]
    assert df["extractor"] == "manual"
    assert df["added_by"] == "tester@example.com"


# ---------- bulk-import via JSONL upload ----------


def test_jsonl_upload_parses_lines_and_persists(client):
    next_pk = iter(range(100, 200))

    @contextmanager
    def fake_connect():
        cur = MagicMock()
        cur.fetchone.side_effect = lambda: (next(next_pk),)
        cur.fetchall.return_value = []
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    jsonl = "\n".join([
        json.dumps(_valid_scenario_payload("from_jsonl_1")),
        "# this is a comment line — should be ignored",
        "",  # empty line — should be ignored
        json.dumps(_valid_scenario_payload("from_jsonl_2")),
    ])

    with patch("mdk_eval.web.server.db.connect", fake_connect):
        r = client.post(
            "/api/scenario-sets/1/scenarios/from-jsonl",
            headers=_auth(),
            files={"file": ("scenarios.jsonl", jsonl.encode(), "application/jsonl")},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["added"]) == 2
    ids = {a["scenario_id"] for a in body["added"]}
    assert ids == {"from_jsonl_1", "from_jsonl_2"}


def test_jsonl_upload_bad_line_aborts_batch(client):
    """Atomic: if line N fails to parse, the entire upload errors with the
    line number. No partial commits."""
    @contextmanager
    def fake_connect():
        cur = MagicMock()
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    jsonl = "\n".join([
        json.dumps(_valid_scenario_payload("first")),
        "{not valid json on line 2",
        json.dumps(_valid_scenario_payload("third")),
    ])

    with patch("mdk_eval.web.server.db.connect", fake_connect):
        r = client.post(
            "/api/scenario-sets/1/scenarios/from-jsonl",
            headers=_auth(),
            files={"file": ("scenarios.jsonl", jsonl.encode(), "application/jsonl")},
        )
    assert r.status_code == 400
    assert "line 2" in r.json()["detail"].lower()


def test_jsonl_upload_empty_file_400s(client):
    r = client.post(
        "/api/scenario-sets/1/scenarios/from-jsonl",
        headers=_auth(),
        files={"file": ("empty.jsonl", b"", "application/jsonl")},
    )
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()


def test_jsonl_upload_only_comments_400s(client):
    """File with only comment + blank lines should error — nothing to import."""
    @contextmanager
    def fake_connect():
        cur = MagicMock()
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake_connect):
        r = client.post(
            "/api/scenario-sets/1/scenarios/from-jsonl",
            headers=_auth(),
            files={"file": ("comments.jsonl", b"# just a comment\n\n# another\n", "application/jsonl")},
        )
    assert r.status_code == 400
    assert "no scenarios" in r.json()["detail"].lower()


# ---------- auth ----------


def test_propose_one_requires_auth(client):
    r = client.post("/api/scenarios/propose-one", json={})
    assert r.status_code == 401


def test_add_scenarios_requires_auth(client):
    r = client.post("/api/scenario-sets/1/scenarios", json={"scenarios": []})
    assert r.status_code == 401

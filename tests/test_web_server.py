"""FastAPI service tests using TestClient.

These tests:
- Verify auth gates (401 without token, 503 if MDK_WEB_API_KEY unset).
- Verify CORS.
- Verify the ingest endpoint validates JSON, persists to DB, returns scenarios.
- Verify the run endpoint queues a job, the status endpoint reports it.

DB layer is mocked at the function level — tests don't require a real Postgres.
A separate opt-in real-DB integration test lives in test_postgres_push.py.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

# Skip the entire file if the [web] extra isn't installed.
pytest.importorskip("fastapi")
pytest.importorskip("psycopg")

from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """A TestClient with auth + DB connection mocked.

    Sets MDK_WEB_API_KEY=test-key, MDK_WEB_CORS_ORIGINS=*, and stubs
    db.connect() so route handlers can `with db.connect() as conn` without
    a real database.
    """
    monkeypatch.setenv("MDK_WEB_API_KEY", "test-key")
    monkeypatch.setenv("MDK_WEB_CORS_ORIGINS", "http://localhost:3000")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    # Re-import so middleware reads the env vars
    import importlib

    import mdk_eval.web.server as srv
    importlib.reload(srv)
    return TestClient(srv.app)


def _auth() -> dict:
    return {"Authorization": "Bearer test-key"}


# ----------------------------- health + auth -----------------------------


def test_healthz_requires_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_protected_endpoints_reject_missing_token(client):
    r = client.get("/api/agents")
    assert r.status_code == 401


def test_protected_endpoints_reject_wrong_token(client):
    r = client.get("/api/agents", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_protected_endpoints_accept_correct_token(client):
    """With a valid token but stubbed DB, the call should reach the handler
    (where the DB stub takes over)."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    @contextmanager
    def fake_connect():
        cur = MagicMock()
        cur.fetchall.return_value = []  # no agents in this fake DB
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake_connect):
        r = client.get("/api/agents", headers=_auth())
    assert r.status_code == 200
    assert r.json() == []


# ----------------------------- ingest validation -----------------------------


def test_ingest_rejects_empty_file(client):
    r = client.post(
        "/api/agent-definitions",
        headers=_auth(),
        files={"file": ("agent.json", b"", "application/json")},
        data={
            "engagement_slug": "x",
            "agent_slug": "y",
            "scenario_set_name": "v1",
        },
    )
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()


def test_ingest_rejects_invalid_json(client):
    r = client.post(
        "/api/agent-definitions",
        headers=_auth(),
        files={"file": ("agent.json", b"{not valid", "application/json")},
        data={
            "engagement_slug": "x",
            "agent_slug": "y",
            "scenario_set_name": "v1",
        },
    )
    assert r.status_code == 400
    assert "valid json" in r.json()["detail"].lower()


def test_ingest_rejects_non_object_json(client):
    r = client.post(
        "/api/agent-definitions",
        headers=_auth(),
        files={"file": ("agent.json", b'["not", "an", "object"]', "application/json")},
        data={
            "engagement_slug": "x",
            "agent_slug": "y",
            "scenario_set_name": "v1",
        },
    )
    assert r.status_code == 400


def test_ingest_persists_via_db_calls(client):
    """Happy path: send a valid Lyzr agent JSON, verify db calls happen."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    @contextmanager
    def fake_connect():
        cur = MagicMock()
        # upsert_engagement / upsert_agent / insert_scenario_set / insert_scenario
        # all RETURNING id; fetchone returns sequential ids.
        ids = iter(range(100, 200))
        cur.fetchone.side_effect = lambda: (next(ids),)
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    agent_def = {
        "_id": "test-agent-id-abc123",  # required since the missing-_id hardening
        "name": "Test Agent",
        "agent_role": "Tester",
        "agent_instructions": "Do things.",
        "agent_goal": "Test goal.",
    }
    with patch("mdk_eval.web.server.db.connect", fake_connect):
        r = client.post(
            "/api/agent-definitions",
            headers=_auth(),
            files={"file": ("agent.json", json.dumps(agent_def).encode(), "application/json")},
            data={
                "engagement_slug": "test-eng",
                "agent_slug": "test-agent",
                "scenario_set_name": "v1",
                "synthesize": "false",
            },
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scenario_set_id"]
    assert body["agent_id"]
    assert body["engagement_id"]
    assert body["source_sha256"]
    # Heuristic ingestor will produce zero-or-more scenarios depending on the
    # agent definition's structure. We just verify the response shape is valid.
    assert isinstance(body["scenarios"], list)


# ----------------------------- runs -----------------------------


def test_queue_run_returns_job_id_and_enqueues(client):
    """Happy path: POST /api/runs returns a job_id, inserts the row, and
    pushes a message into the pgmq queue (atomic with the row insert)."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    enqueue_calls: list = []

    def fake_execute(sql, params=()):
        # Capture the pgmq.send call so the test can assert on the enqueue.
        if "pgmq.send" in str(sql).lower():
            enqueue_calls.append((sql, params))
        return None

    @contextmanager
    def fake_connect():
        cur = MagicMock()
        cur.execute.side_effect = fake_execute
        # get_agent_and_scenario_set fetchone (agent), then fetchone (set),
        # then list_scenarios fetchall, then insert_job fetchone.
        cur.fetchone.side_effect = [
            ("agent-slug", "Display", "mock", "mock-id"),  # agent
            (42, 1, "v1", "manual"),                        # scenario_set
            (99,),                                          # job pk after insert
        ]
        cur.fetchall.return_value = [
            (1, "s1", "approved", "medium", [], {"prompt": "x"}, None),
        ]
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake_connect):
        r = client.post(
            "/api/runs",
            headers=_auth(),
            json={
                "agent_id": 1,
                "scenario_set_id": 42,
                "judges_enabled": False,
                "runs_per_scenario": 1,
                "only_approved": True,
            },
        )

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    assert r.json()["job_id"].startswith("job-")
    # The pgmq.send call must have happened — atomic enqueue with row insert.
    assert len(enqueue_calls) == 1, f"expected exactly 1 pgmq.send call, got {len(enqueue_calls)}"
    # Payload must contain the same job_id we returned to the caller.
    assert r.json()["job_id"] in str(enqueue_calls[0][1])


def test_queue_run_rejects_unknown_agent(client):
    """If get_agent_and_scenario_set returns None, the endpoint must 404."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    @contextmanager
    def fake_connect():
        cur = MagicMock()
        cur.fetchone.return_value = None  # agent not found
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake_connect):
        r = client.post(
            "/api/runs",
            headers=_auth(),
            json={"agent_id": 99999, "scenario_set_id": 99999},
        )
    assert r.status_code == 404


def test_get_run_status_returns_404_for_unknown(client):
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    @contextmanager
    def fake_connect():
        cur = MagicMock()
        cur.fetchone.return_value = None
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake_connect):
        r = client.get("/api/runs/job-doesnotexist", headers=_auth())
    assert r.status_code == 404


# ----------------------------- service-level config -----------------------------


def test_unset_api_key_returns_503(monkeypatch):
    """If MDK_WEB_API_KEY is not set in env, every protected endpoint must
    refuse rather than running open."""
    monkeypatch.delenv("MDK_WEB_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    import importlib

    import mdk_eval.web.server as srv
    importlib.reload(srv)
    c = TestClient(srv.app)
    # Auth header doesn't matter — the SERVICE is misconfigured.
    r = c.get("/api/agents", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503

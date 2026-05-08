"""Endpoint tests for admin cleanup operations.

Covers:
  - GET  /api/agents/{id}/cleanup-preview  → dry-run counts
  - DELETE /api/agents/{id}                → cascade delete
  - DELETE /api/runs/{run_pk}              → run-only delete

Mocks the DB layer (db.connect / db.count_agent_cleanup_cascade /
db.delete_agent / db.count_run_cleanup_cascade / db.delete_run) since the
endpoint logic is shape-translation; the SQL behavior is exercised by the
real-DB integration tests gated on MDK_TEST_DATABASE_URL.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


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


@contextmanager
def _patched_db(*, agent_snapshot=None, run_snapshot=None,
                delete_agent_returns=True, delete_run_returns=True):
    fake_conn = MagicMock()
    fake_cur = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = False
    fake_conn.cursor.return_value = fake_cur
    fake_cur.__enter__.return_value = fake_cur
    fake_cur.__exit__.return_value = False
    with patch("mdk_eval.web.server.db.connect", return_value=fake_conn):
        with patch("mdk_eval.web.server.db.count_agent_cleanup_cascade",
                   return_value=agent_snapshot):
            with patch("mdk_eval.web.server.db.delete_agent",
                       return_value=delete_agent_returns):
                with patch("mdk_eval.web.server.db.count_run_cleanup_cascade",
                           return_value=run_snapshot):
                    with patch("mdk_eval.web.server.db.delete_run",
                               return_value=delete_run_returns):
                        yield


# ----------------- agent cleanup preview -----------------


def test_preview_404_when_agent_missing(client):
    with _patched_db(agent_snapshot={}):
        r = client.get("/api/agents/9999/cleanup-preview", headers=_auth())
    assert r.status_code == 404


def test_preview_returns_full_snapshot(client):
    snapshot = {
        "agent": {"id": 23, "slug": "movate-faq", "display_name": "Movate FAQ",
                  "backend": "lyzr", "backend_id": "abc"},
        "would_delete": {
            "scenario_sets": 2, "scenarios": 13, "runs": 4,
            "scenario_aggregates": 52, "scenario_runs": 52, "findings": 8,
            "failure_clusters": 3, "evaluation_summaries": 4,
        },
        "would_orphan_sub_agents": [
            {"id": 24, "slug": "ocr-agent", "display_name": "OCR Agent"}
        ],
    }
    with _patched_db(agent_snapshot=snapshot):
        r = client.get("/api/agents/23/cleanup-preview", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent"]["slug"] == "movate-faq"
    assert body["would_delete"]["runs"] == 4
    assert body["would_delete"]["scenarios"] == 13
    assert len(body["would_orphan_sub_agents"]) == 1
    assert body["would_orphan_sub_agents"][0]["slug"] == "ocr-agent"


def test_preview_handles_no_sub_agents(client):
    """Standalone agent (no sub-agents) — empty orphan list."""
    snapshot = {
        "agent": {"id": 5, "slug": "solo", "display_name": "Solo",
                  "backend": "mock", "backend_id": ""},
        "would_delete": {
            "scenario_sets": 0, "scenarios": 0, "runs": 0,
            "scenario_aggregates": 0, "scenario_runs": 0, "findings": 0,
            "failure_clusters": 0, "evaluation_summaries": 0,
        },
        "would_orphan_sub_agents": [],
    }
    with _patched_db(agent_snapshot=snapshot):
        r = client.get("/api/agents/5/cleanup-preview", headers=_auth())
    assert r.status_code == 200
    assert r.json()["would_orphan_sub_agents"] == []


def test_preview_requires_auth(client):
    r = client.get("/api/agents/1/cleanup-preview")
    assert r.status_code == 401


# ----------------- agent delete -----------------


def test_delete_agent_404_when_missing(client):
    with _patched_db(agent_snapshot={}):
        r = client.delete("/api/agents/9999", headers=_auth())
    assert r.status_code == 404


def test_delete_agent_returns_snapshot_of_what_was_deleted(client):
    snapshot = {
        "agent": {"id": 23, "slug": "old-test-agent", "display_name": "Old test",
                  "backend": "lyzr", "backend_id": "x"},
        "would_delete": {
            "scenario_sets": 1, "scenarios": 5, "runs": 2,
            "scenario_aggregates": 10, "scenario_runs": 10, "findings": 1,
            "failure_clusters": 1, "evaluation_summaries": 2,
        },
        "would_orphan_sub_agents": [],
    }
    with _patched_db(agent_snapshot=snapshot, delete_agent_returns=True):
        r = client.delete("/api/agents/23", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent"]["slug"] == "old-test-agent"
    assert body["deleted"]["runs"] == 2
    assert body["deleted"]["scenarios"] == 5
    assert body["orphaned_sub_agents"] == []


def test_delete_agent_handles_orphaned_sub_agents(client):
    """Deleting a manager surfaces which sub-agents got de-parented."""
    snapshot = {
        "agent": {"id": 1, "slug": "manager", "display_name": "Mgr",
                  "backend": "lyzr", "backend_id": "m1"},
        "would_delete": {
            "scenario_sets": 0, "scenarios": 0, "runs": 0,
            "scenario_aggregates": 0, "scenario_runs": 0, "findings": 0,
            "failure_clusters": 0, "evaluation_summaries": 0,
        },
        "would_orphan_sub_agents": [
            {"id": 2, "slug": "ocr", "display_name": "OCR Agent"},
            {"id": 3, "slug": "validator", "display_name": "Validator"},
        ],
    }
    with _patched_db(agent_snapshot=snapshot, delete_agent_returns=True):
        r = client.delete("/api/agents/1", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    orphaned_slugs = {a["slug"] for a in body["orphaned_sub_agents"]}
    assert orphaned_slugs == {"ocr", "validator"}


def test_delete_agent_handles_race_condition(client):
    """Snapshot found the agent, but DELETE returned False — someone else
    deleted it concurrently. Should return 404, not 500."""
    snapshot = {
        "agent": {"id": 23, "slug": "x", "display_name": "X",
                  "backend": "lyzr", "backend_id": "x"},
        "would_delete": {"scenario_sets": 0, "scenarios": 0, "runs": 0,
                         "scenario_aggregates": 0, "scenario_runs": 0,
                         "findings": 0, "failure_clusters": 0,
                         "evaluation_summaries": 0},
        "would_orphan_sub_agents": [],
    }
    with _patched_db(agent_snapshot=snapshot, delete_agent_returns=False):
        r = client.delete("/api/agents/23", headers=_auth())
    assert r.status_code == 404


def test_delete_agent_requires_auth(client):
    r = client.delete("/api/agents/1")
    assert r.status_code == 401


# ----------------- run delete -----------------


def test_delete_run_404_when_missing(client):
    with _patched_db(run_snapshot={}):
        r = client.delete("/api/runs/9999", headers=_auth())
    assert r.status_code == 404


def test_delete_run_returns_snapshot(client):
    snapshot = {
        "run": {"id": 24, "run_id": "run_2026-05-07T14-30-00Z",
                "agent_id": 23, "started_at": "2026-05-07T14:30:00+00:00"},
        "would_delete": {
            "scenario_aggregates": 13, "scenario_runs": 13, "findings": 2,
            "failure_clusters": 1, "evaluation_summaries": 1,
        },
    }
    with _patched_db(run_snapshot=snapshot, delete_run_returns=True):
        r = client.delete("/api/runs/24", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"]["id"] == 24
    assert body["deleted"]["scenario_aggregates"] == 13


def test_delete_run_requires_auth(client):
    r = client.delete("/api/runs/1")
    assert r.status_code == 401

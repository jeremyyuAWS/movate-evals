"""Endpoint tests for sandbox-mode upload + missing-_id hardening.

Covers:
  - Upload without `_id` and without sandbox=true → 400 (hardening)
  - Upload with sandbox=true → provisions via Lyzr, sets is_sandbox flag
  - Upload with sandbox=true + manager (managed_agents) → 400 (Phase 1 limit)
  - DELETE on a sandbox agent → calls Lyzr delete_agent first
  - DELETE on a non-sandbox agent → does NOT call Lyzr delete_agent
"""
from __future__ import annotations

import json as _json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MDK_WEB_API_KEY", "test-key")
    monkeypatch.setenv("MDK_WEB_CORS_ORIGINS", "http://localhost:3000")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    monkeypatch.setenv("LYZR_API_KEY", "test-lyzr-key")
    import importlib
    import mdk_eval.web.server as srv
    importlib.reload(srv)
    from fastapi.testclient import TestClient
    return TestClient(srv.app)


def _auth() -> dict:
    return {"Authorization": "Bearer test-key"}


@contextmanager
def _patched_db():
    """Returns a MagicMock-backed cursor that handles upsert_engagement,
    upsert_agent, set_agent_sandbox_flags, insert_scenario_set, and the
    eventual queries the upload runs."""
    @contextmanager
    def fake_connect():
        cur = MagicMock()
        ids = iter(range(100, 200))
        cur.fetchone.side_effect = lambda: (next(ids),)
        cur.__enter__ = lambda self: cur
        cur.__exit__ = lambda *a: None
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake_connect):
        yield


# ---------------- hardening: missing _id ----------------


def test_upload_without_id_returns_400(client):
    """Lyzr backend, no _id/id/agent_id/_agent_id → refuse upload."""
    agent_def = {
        "name": "No ID Agent",
        "agent_role": "Tester",
        "agent_instructions": "do things",
        "agent_goal": "test",
    }
    r = client.post(
        "/api/agent-definitions",
        headers=_auth(),
        files={"file": ("agent.json", _json.dumps(agent_def).encode(), "application/json")},
        data={
            "engagement_slug": "x",
            "agent_slug": "y",
            "scenario_set_name": "v1",
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    # Both recovery paths should be in the error message.
    assert "_id" in detail
    assert "sandbox=true" in detail


def test_upload_with_id_succeeds(client):
    """Counter-test: with `_id`, the upload succeeds (no hardening trigger)."""
    agent_def = {
        "_id": "abc123",
        "name": "Has ID",
        "agent_role": "Tester",
        "agent_instructions": "ok",
        "agent_goal": "ok",
    }
    with _patched_db():
        r = client.post(
            "/api/agent-definitions",
            headers=_auth(),
            files={"file": ("agent.json", _json.dumps(agent_def).encode(), "application/json")},
            data={
                "engagement_slug": "x",
                "agent_slug": "y",
                "scenario_set_name": "v1",
                "synthesize": "false",
            },
        )
    assert r.status_code == 200, r.text


# ---------------- sandbox happy path ----------------


def test_sandbox_upload_provisions_via_lyzr(client):
    """sandbox=true + no _id → call lyzr_admin.create_agent, store returned ID."""
    agent_def = {
        "name": "Sandbox Agent",
        "agent_role": "Tester",
        "agent_instructions": "ok",
        "agent_goal": "ok",
    }
    with _patched_db():
        with patch(
            "mdk_eval.integrations.lyzr_admin.create_agent",
            AsyncMock(return_value="newly-provisioned-id"),
        ) as fake_create:
            r = client.post(
                "/api/agent-definitions?sandbox=true",
                headers=_auth(),
                files={"file": ("agent.json", _json.dumps(agent_def).encode(), "application/json")},
                data={
                    "engagement_slug": "x",
                    "agent_slug": "y",
                    "scenario_set_name": "v1",
                    "synthesize": "false",
                },
            )

    assert r.status_code == 200, r.text
    fake_create.assert_called_once()
    body = r.json()
    # Sandbox-mode warnings surface to the client.
    assert any("Sandbox mode" in w for w in body["warnings"])
    assert any("newly-provisioned-id" in w for w in body["warnings"])


def test_sandbox_upload_rejects_managers(client):
    """Phase 1 limitation: managed_agents triggers 400 in sandbox mode."""
    agent_def = {
        "name": "Manager",
        "agent_role": "Manager",
        "agent_instructions": "ok",
        "agent_goal": "ok",
        "managed_agents": [{"id": "sub", "name": "Sub", "usage_description": "x"}],
    }
    r = client.post(
        "/api/agent-definitions?sandbox=true",
        headers=_auth(),
        files={"file": ("agent.json", _json.dumps(agent_def).encode(), "application/json")},
        data={
            "engagement_slug": "x",
            "agent_slug": "y",
            "scenario_set_name": "v1",
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "manager" in detail.lower()
    assert "Phase" in detail


def test_sandbox_upload_502_on_lyzr_error(client):
    """If Lyzr returns 5xx, the upload itself returns 502."""
    from mdk_eval.integrations.lyzr_admin import LyzrAdminError
    agent_def = {
        "name": "X", "agent_role": "X",
        "agent_instructions": "x", "agent_goal": "x",
    }
    with _patched_db():
        with patch(
            "mdk_eval.integrations.lyzr_admin.create_agent",
            AsyncMock(side_effect=LyzrAdminError("Lyzr returned 500")),
        ):
            r = client.post(
                "/api/agent-definitions?sandbox=true",
                headers=_auth(),
                files={"file": ("agent.json", _json.dumps(agent_def).encode(), "application/json")},
                data={
                    "engagement_slug": "x",
                    "agent_slug": "y",
                    "scenario_set_name": "v1",
                },
            )
    assert r.status_code == 502
    assert "provision failed" in r.json()["detail"].lower()


# ---------------- DELETE cascade ----------------


def test_delete_sandbox_agent_calls_lyzr_delete(client):
    """DELETE on is_sandbox=true agent → calls lyzr_admin.delete_agent first."""
    sandbox_state = {
        "is_sandbox": True,
        "sandbox_expires_at": None,
        "backend_id": "lyzr-id-to-tear-down",
        "backend": "lyzr",
    }
    cleanup_snapshot = {
        "agent": {"id": 50, "slug": "sandbox-agent", "display_name": "X",
                  "backend": "lyzr", "backend_id": "lyzr-id-to-tear-down"},
        "would_delete": {"scenario_sets": 0, "scenarios": 0, "runs": 0,
                         "scenario_aggregates": 0, "scenario_runs": 0,
                         "findings": 0, "failure_clusters": 0,
                         "evaluation_summaries": 0},
        "would_orphan_sub_agents": [],
    }

    fake_conn = MagicMock()
    fake_cur = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = False
    fake_conn.cursor.return_value = fake_cur
    fake_cur.__enter__.return_value = fake_cur
    fake_cur.__exit__.return_value = False

    with patch("mdk_eval.web.server.db.connect", return_value=fake_conn):
        with patch("mdk_eval.web.server.db.get_agent_sandbox_state",
                   return_value=sandbox_state):
            with patch("mdk_eval.web.server.db.count_agent_cleanup_cascade",
                       return_value=cleanup_snapshot):
                with patch("mdk_eval.web.server.db.delete_agent", return_value=True):
                    with patch(
                        "mdk_eval.integrations.lyzr_admin.delete_agent",
                        AsyncMock(),
                    ) as fake_delete:
                        r = client.delete("/api/agents/50", headers=_auth())

    assert r.status_code == 200, r.text
    fake_delete.assert_called_once_with("lyzr-id-to-tear-down")
    body = r.json()
    assert any("Lyzr agent" in n and "deleted" in n for n in body["notes"])


def test_delete_non_sandbox_agent_skips_lyzr_delete(client):
    """DELETE on is_sandbox=false agent → no Lyzr call (Lyzr-side instance
    is the user's production agent and must not be torn down)."""
    sandbox_state = {
        "is_sandbox": False,
        "sandbox_expires_at": None,
        "backend_id": "production-lyzr-id",
        "backend": "lyzr",
    }
    cleanup_snapshot = {
        "agent": {"id": 23, "slug": "prod-agent", "display_name": "Prod",
                  "backend": "lyzr", "backend_id": "production-lyzr-id"},
        "would_delete": {"scenario_sets": 1, "scenarios": 13, "runs": 4,
                         "scenario_aggregates": 52, "scenario_runs": 52,
                         "findings": 8, "failure_clusters": 3,
                         "evaluation_summaries": 4},
        "would_orphan_sub_agents": [],
    }

    fake_conn = MagicMock()
    fake_cur = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = False
    fake_conn.cursor.return_value = fake_cur
    fake_cur.__enter__.return_value = fake_cur
    fake_cur.__exit__.return_value = False

    with patch("mdk_eval.web.server.db.connect", return_value=fake_conn):
        with patch("mdk_eval.web.server.db.get_agent_sandbox_state",
                   return_value=sandbox_state):
            with patch("mdk_eval.web.server.db.count_agent_cleanup_cascade",
                       return_value=cleanup_snapshot):
                with patch("mdk_eval.web.server.db.delete_agent", return_value=True):
                    with patch(
                        "mdk_eval.integrations.lyzr_admin.delete_agent",
                        AsyncMock(),
                    ) as fake_delete:
                        r = client.delete("/api/agents/23", headers=_auth())

    assert r.status_code == 200
    fake_delete.assert_not_called()
    body = r.json()
    # No sandbox tear-down note for a production agent.
    assert not any("Sandbox tear-down" in n for n in body["notes"])


def test_delete_sandbox_agent_logs_warning_on_lyzr_failure(client):
    """If Lyzr DELETE fails, the local DELETE still proceeds and a warning
    note appears so the operator knows the Lyzr-side may have leaked."""
    from mdk_eval.integrations.lyzr_admin import LyzrAdminError
    sandbox_state = {
        "is_sandbox": True, "sandbox_expires_at": None,
        "backend_id": "stuck-lyzr-id", "backend": "lyzr",
    }
    cleanup_snapshot = {
        "agent": {"id": 70, "slug": "x", "display_name": "X",
                  "backend": "lyzr", "backend_id": "stuck-lyzr-id"},
        "would_delete": {"scenario_sets": 0, "scenarios": 0, "runs": 0,
                         "scenario_aggregates": 0, "scenario_runs": 0,
                         "findings": 0, "failure_clusters": 0,
                         "evaluation_summaries": 0},
        "would_orphan_sub_agents": [],
    }

    fake_conn = MagicMock()
    fake_cur = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = False
    fake_conn.cursor.return_value = fake_cur
    fake_cur.__enter__.return_value = fake_cur
    fake_cur.__exit__.return_value = False

    with patch("mdk_eval.web.server.db.connect", return_value=fake_conn):
        with patch("mdk_eval.web.server.db.get_agent_sandbox_state",
                   return_value=sandbox_state):
            with patch("mdk_eval.web.server.db.count_agent_cleanup_cascade",
                       return_value=cleanup_snapshot):
                with patch("mdk_eval.web.server.db.delete_agent", return_value=True):
                    with patch(
                        "mdk_eval.integrations.lyzr_admin.delete_agent",
                        AsyncMock(side_effect=LyzrAdminError("Lyzr returned 500")),
                    ):
                        r = client.delete("/api/agents/70", headers=_auth())

    # Local delete still succeeds even with Lyzr failure.
    assert r.status_code == 200
    body = r.json()
    assert any("WARNING" in n and "leaked" in n for n in body["notes"])

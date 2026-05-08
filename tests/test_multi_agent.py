"""Multi-agent first-class support — managed_agents detection, parent_agent_slug
linking on ingest, GET /api/agent-systems/{root}, PATCH /api/agents/{id}.

DB-touching paths are exercised through patched db.connect() so these run
without a live Postgres. Schema-tolerance paths (pre-migration-007) are
covered explicitly to lock in graceful degradation.
"""
from __future__ import annotations

import importlib
from contextlib import contextmanager
from datetime import datetime, timezone
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
    import mdk_eval.web.server as srv
    importlib.reload(srv)
    return TestClient(srv.app)


def _auth() -> dict:
    return {"Authorization": "Bearer test-key"}


# ---------------------------------------------------------------- _detect_managed_agents


def test_detect_managed_agents_returns_empty_for_standalone_agent():
    """An agent with no managed_agents array → []. Standalone agent."""
    from mdk_eval.web import server as srv
    agent_def = {"name": "Plain", "tools": []}

    cur = MagicMock()
    cur.fetchone.return_value = None
    conn = MagicMock(); conn.cursor.return_value = cur
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None

    @contextmanager
    def fake_db(): yield conn
    with patch("mdk_eval.web.server.db.connect", fake_db):
        result = srv._detect_managed_agents(agent_def, engagement_id=10)
    assert result == []


def test_detect_managed_agents_extracts_lyzr_shape():
    """Lyzr managed_agents entries get parsed into ManagedAgentDetected rows
    with auto-suggested slugs."""
    from mdk_eval.web import server as srv
    agent_def = {
        "managed_agents": [
            {"id": "abc123", "name": "(R) OCR Agent [Returns Manager v4]",
             "usage_description": "Extracts text from images."},
            {"id": "def456", "name": "Product Validator",
             "usage_description": "Validates product against catalog."},
        ]
    }
    # Both queried as "not yet uploaded"
    cur = MagicMock()
    cur.fetchone.return_value = None
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake_db(): yield conn
    with patch("mdk_eval.web.server.db.connect", fake_db):
        result = srv._detect_managed_agents(agent_def, engagement_id=10)

    assert len(result) == 2
    # First: decorations stripped, slug normalized
    r1 = result[0]
    assert r1.backend_id == "abc123"
    assert r1.display_name == "(R) OCR Agent [Returns Manager v4]"   # original preserved
    assert r1.suggested_slug == "ocr-agent"                           # decorations removed
    assert r1.usage_description == "Extracts text from images."
    assert r1.already_uploaded is False
    # Second: simpler name, slug just lowercases + dash-joins
    r2 = result[1]
    assert r2.backend_id == "def456"
    assert r2.suggested_slug == "product-validator"


def test_detect_managed_agents_marks_already_uploaded():
    """If a sub-agent's backend_id matches an existing agent in the same
    engagement, the response should flag already_uploaded=True so Bolt can
    skip prompting for re-upload."""
    from mdk_eval.web import server as srv
    agent_def = {"managed_agents": [{"id": "abc123", "name": "OCR Agent"}]}

    cur = MagicMock()
    # Simulate find_agent_by_backend_id returning a hit
    cur.fetchone.return_value = (101, "ocr-agent", "OCR Agent", "lyzr", "abc123")
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake_db(): yield conn
    with patch("mdk_eval.web.server.db.connect", fake_db):
        result = srv._detect_managed_agents(agent_def, engagement_id=10)

    assert len(result) == 1
    assert result[0].already_uploaded is True


def test_detect_managed_agents_silently_skips_malformed_entries():
    """Don't fail the ingest because a managed_agents entry is malformed."""
    from mdk_eval.web import server as srv
    agent_def = {
        "managed_agents": [
            "not_a_dict",            # silently skipped
            {"name": "missing id"},   # silently skipped (no backend_id)
            {"id": "valid_one", "name": "Valid"},   # included
        ]
    }
    cur = MagicMock(); cur.fetchone.return_value = None
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake_db(): yield conn
    with patch("mdk_eval.web.server.db.connect", fake_db):
        result = srv._detect_managed_agents(agent_def, engagement_id=10)
    assert len(result) == 1
    assert result[0].backend_id == "valid_one"


# ---------------------------------------------------------------- agent-systems endpoint


def _fake_system_db(*, manager_row, sub_rows, manager_summary, sub_summaries):
    """Build a context-manager that yields a connection whose cur.fetchone /
    fetchall produce the values needed for /api/agent-systems/{slug}.

    fetchone is called in this order:
      1. db.get_agent_system → manager row (the JOIN)
      2. latest_run_summary_for_agent(manager) → 7-tuple
      3. latest_run_summary_for_agent(sub_1) → 7-tuple
      4. latest_run_summary_for_agent(sub_2) → 7-tuple
      ...
    fetchall is called once: db.get_agent_system → sub_rows
    """
    fetchone_seq = iter([manager_row, manager_summary, *sub_summaries])
    cur = MagicMock()
    cur.fetchone.side_effect = lambda: next(fetchone_seq)
    cur.fetchall.return_value = sub_rows
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake_db(): yield conn
    return fake_db


def test_agent_system_returns_404_for_unknown_slug(client):
    cur = MagicMock(); cur.fetchone.return_value = None
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn

    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.get("/api/agent-systems/nonexistent", headers=_auth())
    assert r.status_code == 404


def test_agent_system_composite_score_weights_manager_higher(client):
    """Manager weighted 1.5x, sub-agents 1.0x. Verify the math."""
    now = datetime.now(timezone.utc)
    manager_row = (1, "returns-mgr", "Returns Manager", "lyzr",
                   10, "sandisk", "SanDisk Returns")   # engagement metadata
    sub_rows = [
        (2, "ocr-agent", "OCR Agent", "lyzr"),
        (3, "validator", "Validator", "lyzr"),
    ]
    # latest_run_summary returns 7-tuple: (runs_count, last_run_at, overall_score, status, passing, total, cost)
    manager_summary = (5, now, 90.0, "production_ready", 12, 13, 1.50)
    sub_summaries = [
        (3, now, 80.0, "pilot_ready", 8, 10, 0.40),
        (3, now, 60.0, "needs_improvement", 6, 10, 0.30),
    ]
    fake = _fake_system_db(
        manager_row=manager_row, sub_rows=sub_rows,
        manager_summary=manager_summary, sub_summaries=sub_summaries,
    )
    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.get("/api/agent-systems/returns-mgr", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    # Composite: (1.5*90 + 1.0*80 + 1.0*60) / (1.5+1.0+1.0) = (135+80+60)/3.5 = 78.57
    assert 78.0 <= body["composite_score"] <= 79.0
    # Worst-of: needs_improvement (the lowest member status)
    assert body["composite_status"] == "needs_improvement"
    assert body["members_evaluated"] == 3
    assert body["members_total"] == 3
    assert body["manager"]["role"] == "manager"
    assert all(m["role"] == "sub_agent" for m in body["sub_agents"])


def test_agent_system_caps_status_when_some_members_unevaluated(client):
    """A system with un-evaluated sub-agents can't be production_ready —
    composite_status is bounded at needs_improvement."""
    now = datetime.now(timezone.utc)
    manager_row = (1, "mgr", "Mgr", "lyzr", 10, "x", "X")
    sub_rows = [(2, "child", "Child", "lyzr")]
    manager_summary = (5, now, 95.0, "production_ready", 13, 13, 0.50)
    # Sub-agent has 0 runs — last_run_summary returns runs_count=0, no scores
    sub_summaries = [(0, None, None, None, None, None, None)]
    fake = _fake_system_db(
        manager_row=manager_row, sub_rows=sub_rows,
        manager_summary=manager_summary, sub_summaries=sub_summaries,
    )
    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.get("/api/agent-systems/mgr", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    # Sub-agent didn't drag the average down (only 1.5x manager * 95.0 = 142.5 / 1.5 = 95)
    assert body["composite_score"] == 95.0
    # But the system status is bounded since not all members evaluated
    assert body["composite_status"] == "needs_improvement"
    assert body["members_evaluated"] == 1
    assert body["members_total"] == 2


# ---------------------------------------------------------------- PATCH relink


def test_relink_agent_to_existing_manager(client):
    """PATCH /api/agents/{id} with parent_agent_slug → links."""
    cur = MagicMock()
    # Sequence: SELECT engagement_id+slug+backend+backend_id, find_agent_by_slug → parent row,
    # UPDATE rowcount > 0
    fetchone_returns = iter([
        (10, "child-slug", "lyzr", "abc123"),           # initial agent SELECT
        (101, "manager-slug", "Manager", "lyzr", "X"),  # find_agent_by_slug
    ])
    cur.fetchone.side_effect = lambda: next(fetchone_returns)
    cur.rowcount = 1
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn
    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.patch("/api/agents/42", headers=_auth(),
                         json={"parent_agent_slug": "manager-slug"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_id"] == 42
    assert body["parent_agent_id"] == 101
    assert body["status"] == "linked"


def test_relink_agent_unlinks_with_null_slug(client):
    """parent_agent_slug=null → unlink (set parent_agent_id to NULL)."""
    cur = MagicMock()
    cur.fetchone.return_value = (10, "child-slug", "lyzr", "abc123")   # initial agent SELECT
    cur.rowcount = 1
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn
    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.patch("/api/agents/42", headers=_auth(),
                         json={"parent_agent_slug": None})
    assert r.status_code == 200
    body = r.json()
    assert body["parent_agent_id"] is None
    assert body["status"] == "unlinked"


def test_relink_rejects_self_parent(client):
    cur = MagicMock()
    fetchone_returns = iter([
        (10, "self-slug", "lyzr", "abc123"),   # initial agent SELECT
        (42, "same-slug", "Self", "lyzr", "X"), # find_agent_by_slug — same id as the path param
    ])
    cur.fetchone.side_effect = lambda: next(fetchone_returns)
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn
    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.patch("/api/agents/42", headers=_auth(),
                         json={"parent_agent_slug": "same-slug"})
    assert r.status_code == 400
    assert "own parent" in r.json()["detail"].lower()


def test_relink_returns_404_for_unknown_agent(client):
    cur = MagicMock(); cur.fetchone.return_value = None
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn
    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.patch("/api/agents/99999", headers=_auth(),
                         json={"parent_agent_slug": "manager"})
    assert r.status_code == 404


def test_relink_repairs_missing_backend_id(client):
    """PATCH with backend_id only → updates the agent's backend_id without
    touching parent_agent_id. Recovery flow for runs that fail with 'Lyzr
    adapter requires agent_id'."""
    cur = MagicMock()
    cur.fetchone.return_value = (10, "broken-agent", "lyzr", "")  # empty backend_id
    cur.rowcount = 1
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn
    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.patch("/api/agents/42", headers=_auth(),
                         json={"backend_id": "69ef90f1d56224d9f94c61cc"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["backend_id"] == "69ef90f1d56224d9f94c61cc"
    assert "backend_id_updated" in body["status"]


def test_relink_rejects_empty_backend_id(client):
    """Setting backend_id to empty string should be rejected — that's the
    very state we're trying to recover from."""
    cur = MagicMock()
    cur.fetchone.return_value = (10, "agent", "lyzr", "abc")
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn
    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.patch("/api/agents/42", headers=_auth(),
                         json={"backend_id": "   "})
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()


def test_relink_rejects_unknown_parent_slug(client):
    cur = MagicMock()
    fetchone_returns = iter([
        (10, "child-slug", "lyzr", "abc123"),   # initial agent SELECT
        None,                                    # find_agent_by_slug → not found
    ])
    cur.fetchone.side_effect = lambda: next(fetchone_returns)
    cur.__enter__ = lambda self: cur; cur.__exit__ = lambda *a: None
    conn = MagicMock(); conn.cursor.return_value = cur

    @contextmanager
    def fake(): yield conn
    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.patch("/api/agents/42", headers=_auth(),
                         json={"parent_agent_slug": "nonexistent-mgr"})
    assert r.status_code == 400
    assert "not found" in r.json()["detail"].lower()

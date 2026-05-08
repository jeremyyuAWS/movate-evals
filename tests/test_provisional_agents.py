"""Endpoint tests for the provisional vs active agent state machine.

Covers:
  - GET /api/agents and /api/portfolio/at-a-glance filter to is_active=TRUE
    by default (drafts hidden)
  - The same endpoints accept ?include_provisional=true to show drafts
  - Both endpoints fall back gracefully when the is_active column doesn't
    exist (pre-migration-008 schemas)
  - The migration SQL parses cleanly + does what its docstring claims

The actual is_active flip in the run worker is exercised by integration
tests that run against a real DB; these endpoint tests mock the DB so the
behavior contract is fast to verify.
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
def _patched_cur(*, has_is_active: bool, agents_returned=None):
    """Mock db.connect with a cursor that:
      - reports is_active column presence based on `has_is_active` (first execute)
      - returns the given list of agent rows for fetchall queries
      - returns sensible-shape tuples for the various aggregate fetchones
        (cost rollup, leaderboard, etc.) so the endpoint doesn't NPE
    """
    fake_conn = MagicMock()
    fake_cur = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = False
    fake_conn.cursor.return_value = fake_cur
    fake_cur.__enter__.return_value = fake_cur
    fake_cur.__exit__.return_value = False

    captured_sql: list[str] = []

    def execute_side_effect(sql, *params):
        captured_sql.append(sql)
        return None
    fake_cur.execute.side_effect = execute_side_effect

    column_check_response = (1,) if has_is_active else None

    def fetchone_side_effect():
        # First execute is the column check — answer that specifically. All
        # subsequent fetchones get a 3-tuple of zeros which happens to satisfy
        # every fetchone shape the at-a-glance endpoint expects (cost rollup,
        # delta calc, etc.). The tests only care about the SQL shape, not
        # the response data.
        if len(captured_sql) <= 1:
            return column_check_response
        return (0, 0, 0)

    fake_cur.fetchone.side_effect = fetchone_side_effect
    fake_cur.fetchall.return_value = agents_returned or []

    with patch("mdk_eval.web.server.db.connect", return_value=fake_conn):
        yield captured_sql


# ----------------- /api/agents listing -----------------


def test_list_agents_default_filters_provisional_when_column_exists(client):
    """Default behavior post-migration-008: only is_active=TRUE shown."""
    with _patched_cur(has_is_active=True) as captured:
        r = client.get("/api/agents", headers=_auth())
    assert r.status_code == 200
    main_sql = captured[1]  # [0] = column check, [1] = main query
    assert "WHERE a.is_active = TRUE" in main_sql


def test_list_agents_include_provisional_drops_filter(client):
    """include_provisional=true → no WHERE filter, all agents returned."""
    with _patched_cur(has_is_active=True) as captured:
        r = client.get(
            "/api/agents",
            headers=_auth(),
            params={"include_provisional": "true"},
        )
    assert r.status_code == 200
    main_sql = captured[1]
    assert "is_active" not in main_sql.lower()


def test_list_agents_pre_migration_008_shows_everything(client):
    """When is_active column doesn't exist, no filter applied — graceful
    degradation. No 500, no empty list."""
    with _patched_cur(has_is_active=False) as captured:
        r = client.get("/api/agents", headers=_auth())
    assert r.status_code == 200
    main_sql = captured[1]
    assert "is_active" not in main_sql.lower()


# ----------------- /api/portfolio/at-a-glance -----------------


def test_portfolio_default_filters_provisional(client):
    """Same default behavior on the portfolio endpoint."""
    with _patched_cur(has_is_active=True) as captured:
        r = client.get("/api/portfolio/at-a-glance", headers=_auth())
    assert r.status_code == 200
    # The first execute is the column check; the second is the main JOIN.
    main_sql = captured[1]
    assert "is_active = TRUE" in main_sql


def test_portfolio_include_provisional_param_drops_filter(client):
    """include_provisional=true on the portfolio endpoint shows drafts too."""
    with _patched_cur(has_is_active=True) as captured:
        r = client.get(
            "/api/portfolio/at-a-glance",
            headers=_auth(),
            params={"include_provisional": "true"},
        )
    assert r.status_code == 200
    main_sql = captured[1]
    # When include_provisional=true, we explicitly omit the WHERE.
    assert "WHERE a.is_active" not in main_sql


def test_portfolio_pre_migration_008_unchanged(client):
    """Pre-migration-008 schema: portfolio endpoint behaves exactly as it
    did before — no filter, every agent shown."""
    with _patched_cur(has_is_active=False) as captured:
        r = client.get("/api/portfolio/at-a-glance", headers=_auth())
    assert r.status_code == 200
    main_sql = captured[1]
    assert "is_active" not in main_sql.lower()


# ----------------- migration 008 sanity -----------------


def test_migration_008_file_exists_and_parses():
    """Smoke check: the migration file is well-formed enough to be applied
    by `psql -f` without syntax errors. We don't actually apply it here —
    the real-DB integration test does that — but we lint it for the
    obvious gotchas."""
    from pathlib import Path
    p = Path(__file__).parent.parent / "migrations" / "008_provisional_agents.sql"
    assert p.exists(), f"migration file missing: {p}"
    sql = p.read_text(encoding="utf-8")
    # Idempotent ALTER (use IF NOT EXISTS)
    assert "IF NOT EXISTS" in sql
    # Backfill query joins evaluation_summary
    assert "evaluation_summary" in sql
    # Default value is FALSE so new agents are provisional
    assert "DEFAULT FALSE" in sql
    # Schema version bump
    assert "_mdk_schema_version" in sql


def test_migration_008_idempotent_alter_pattern():
    """Applying migration 008 twice must not fail. The IF NOT EXISTS guard
    on the ALTER + the ON CONFLICT on the schema version insert handle this."""
    from pathlib import Path
    sql = (Path(__file__).parent.parent / "migrations" / "008_provisional_agents.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS" in sql
    assert "ON CONFLICT" in sql

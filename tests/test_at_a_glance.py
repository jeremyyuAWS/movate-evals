"""/api/portfolio/at-a-glance — mega-endpoint tests.

Tests with mocked psycopg. Verifies:
- Endpoint returns 200 with empty portfolio
- Aggregations (status_counts, engagement rollup, platform rollup) compute correctly
- Sparkline + delta_vs_prior derived from the last-N-runs query
- Stale flag triggers when days_since_last_run > stale_after_days
- Leaderboard limit=0 still returns a valid response (skips the LB query)
- Bad params return 400
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
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


def _fake_db(agent_rows, sparkline_rows, cost_row, leaderboard_rows):
    """Return a context-manager that yields a connection whose cursor's
    fetch* methods are pre-loaded with the given query results in order."""
    cur = MagicMock()
    fetchall_results = iter([agent_rows, sparkline_rows, leaderboard_rows])
    cur.fetchall.side_effect = lambda: next(fetchall_results)
    cur.fetchone.return_value = cost_row
    cur.__enter__ = lambda self: cur
    cur.__exit__ = lambda *a: None
    conn = MagicMock()
    conn.cursor.return_value = cur

    @contextmanager
    def fake():
        yield conn
    return fake


# ---------- empty portfolio ----------


def test_empty_portfolio_returns_valid_response(client):
    fake = _fake_db(agent_rows=[], sparkline_rows=[], cost_row=(0, 0, 0), leaderboard_rows=[])
    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.get("/api/portfolio/at-a-glance", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["total_agents"] == 0
    assert body["summary"]["total_engagements"] == 0
    assert body["summary"]["total_runs_in_window"] == 0
    assert body["summary"]["total_cost_usd_in_window"] == 0
    assert body["agents"] == []
    assert body["engagements"] == []
    assert body["platforms"] == []
    assert body["leaderboard_preview"] == []
    assert body["recency_alerts"] == []


# ---------- happy path with rollups ----------


def test_aggregations_compute_correctly(client):
    """Two agents on Lyzr in one engagement, one on LangGraph in another."""
    now = datetime.now(timezone.utc)
    agent_rows = [
        # Lyzr / SanDisk / agent 1: pilot_ready, score 85, cost $0.12
        (1, "faq-v3", "FAQ v3", "lyzr", 10, "sandisk", "SanDisk",
         101, "run_001", now, now, ["correctness"], 1,
         85.0, "pilot_ready", 0.9, 4, 6, {"correctness": 80, "grounding": 70},
         0.12),
        # Lyzr / SanDisk / agent 2: needs_improvement, score 72, cost untracked
        (2, "returns-cls", "Returns Classifier", "lyzr", 10, "sandisk", "SanDisk",
         102, "run_002", now, now, ["correctness"], 1,
         72.0, "needs_improvement", 0.8, 2, 6, {"correctness": 70, "grounding": 65},
         None),
        # LangGraph / GlobalTel / agent 3: production_ready, score 93, cost $0.30
        (3, "support-bot", "Support Bot", "langgraph", 11, "globaltel", "GlobalTel Inc.",
         103, "run_003", now, now, ["correctness"], 1,
         93.0, "production_ready", 0.95, 10, 10, {"correctness": 95, "grounding": 92},
         0.30),
    ]
    sparkline_rows = [
        (1, 85.0, now), (1, 82.0, now), (1, 79.0, now),
        (2, 72.0, now), (2, 68.0, now),
        (3, 93.0, now),
    ]
    with patch("mdk_eval.web.server.db.connect",
               _fake_db(agent_rows, sparkline_rows, (0.42, 1, 5), [])):
        r = client.get("/api/portfolio/at-a-glance", headers=_auth())
    assert r.status_code == 200, r.text
    b = r.json()

    # Summary
    assert b["summary"]["total_agents"] == 3
    assert b["summary"]["total_engagements"] == 2
    assert b["summary"]["status_counts"] == {
        "pilot_ready": 1, "needs_improvement": 1, "production_ready": 1,
    }
    assert b["summary"]["total_cost_usd_in_window"] == 0.42
    assert b["summary"]["runs_without_cost"] == 1

    # Per-agent: sparkline + delta
    by_slug = {a["slug"]: a for a in b["agents"]}
    assert by_slug["faq-v3"]["sparkline"] == [85.0, 82.0, 79.0]
    assert by_slug["faq-v3"]["delta_vs_prior"] == 3.0    # 85 - 82
    assert by_slug["returns-cls"]["delta_vs_prior"] == 4.0  # 72 - 68
    assert by_slug["support-bot"]["delta_vs_prior"] is None  # only 1 run

    # Cost surfaces on each latest_run; null is preserved for untracked runs
    assert by_slug["faq-v3"]["latest_run"]["cost_usd"] == 0.12
    assert by_slug["returns-cls"]["latest_run"]["cost_usd"] is None
    assert by_slug["support-bot"]["latest_run"]["cost_usd"] == 0.30

    # Engagement rollup
    by_eng = {e["slug"]: e for e in b["engagements"]}
    assert by_eng["sandisk"]["agent_count"] == 2
    assert by_eng["sandisk"]["mean_score"] == 78.5  # (85 + 72) / 2
    assert by_eng["globaltel"]["agent_count"] == 1
    assert by_eng["globaltel"]["mean_score"] == 93.0

    # Platform rollup — answers "is Lyzr better at grounding than LangGraph?"
    by_plat = {p["name"]: p for p in b["platforms"]}
    assert by_plat["lyzr"]["agent_count"] == 2
    assert by_plat["lyzr"]["mean_score"] == 78.5
    assert by_plat["lyzr"]["mean_per_category"]["correctness"] == 75.0  # (80 + 70) / 2
    assert by_plat["lyzr"]["mean_per_category"]["grounding"] == 67.5    # (70 + 65) / 2
    assert by_plat["langgraph"]["mean_per_category"]["correctness"] == 95.0
    # Conclusion: LangGraph clearly outperforms Lyzr on grounding (92 vs 67.5).
    # That's exactly the kind of insight this endpoint exists to surface.


# ---------- staleness ----------


def test_stale_agents_appear_in_recency_alerts(client):
    old = datetime.now(timezone.utc) - timedelta(days=14)
    fresh = datetime.now(timezone.utc)
    agent_rows = [
        # 14 days old → stale (default threshold = 7)
        (1, "old-agent", "Old", "mock", 10, "x", "X",
         101, "run_old", old, old, [], 1,
         60.0, "not_ready", 1.0, 0, 6, {}, None),
        # fresh → not stale
        (2, "fresh-agent", "Fresh", "mock", 10, "x", "X",
         102, "run_fresh", fresh, fresh, [], 1,
         85.0, "pilot_ready", 1.0, 5, 6, {}, None),
    ]
    with patch("mdk_eval.web.server.db.connect",
               _fake_db(agent_rows, [(1, 60.0, old), (2, 85.0, fresh)], (0, 0, 0), [])):
        r = client.get("/api/portfolio/at-a-glance", headers=_auth())
    assert r.status_code == 200
    b = r.json()
    by_slug = {a["slug"]: a for a in b["agents"]}
    assert by_slug["old-agent"]["stale"] is True
    assert by_slug["fresh-agent"]["stale"] is False
    alerts = {a["agent_slug"] for a in b["recency_alerts"]}
    assert alerts == {"old-agent"}


def test_custom_stale_threshold(client):
    old = datetime.now(timezone.utc) - timedelta(days=3)
    agent_rows = [
        (1, "x", "X", "mock", 10, "e", "E", 101, "run", old, old, [], 1,
         60.0, "not_ready", 1.0, 0, 6, {}, None),
    ]
    with patch("mdk_eval.web.server.db.connect",
               _fake_db(agent_rows, [], (0, 0, 0), [])):
        # 1-day threshold → 3-day-old run is stale
        r = client.get("/api/portfolio/at-a-glance?stale_after_days=1", headers=_auth())
    assert r.json()["agents"][0]["stale"] is True


# ---------- leaderboard preview ----------


def test_leaderboard_preview_included(client):
    agent_rows = []
    leaderboard_rows = [
        ("hallucination_trap", 3, 0.0, "high", "hallucination"),
        ("schema_strict", 2, 0.5, "medium", "schema_violation"),
    ]
    with patch("mdk_eval.web.server.db.connect",
               _fake_db(agent_rows, [], (0, 0, 0), leaderboard_rows)):
        r = client.get("/api/portfolio/at-a-glance?leaderboard_limit=5", headers=_auth())
    b = r.json()
    assert len(b["leaderboard_preview"]) == 2
    assert b["leaderboard_preview"][0]["scenario_id"] == "hallucination_trap"
    assert b["leaderboard_preview"][0]["agents_affected"] == 3


def test_leaderboard_limit_zero_skips_query(client):
    """Passing leaderboard_limit=0 should not run the LB query at all."""
    # Note: with limit=0, the cursor only sees 3 fetchall calls (agents,
    # sparkline, NOT leaderboard). Build the fake accordingly.
    cur = MagicMock()
    fetchall_results = iter([[], []])  # only agents + sparkline; no LB
    cur.fetchall.side_effect = lambda: next(fetchall_results)
    cur.fetchone.return_value = (0, 0, 0)
    cur.__enter__ = lambda self: cur
    cur.__exit__ = lambda *a: None
    conn = MagicMock()
    conn.cursor.return_value = cur

    @contextmanager
    def fake():
        yield conn

    with patch("mdk_eval.web.server.db.connect", fake):
        r = client.get("/api/portfolio/at-a-glance?leaderboard_limit=0", headers=_auth())
    assert r.status_code == 200
    assert r.json()["leaderboard_preview"] == []


# ---------- input validation ----------


def test_bad_sparkline_runs_returns_400(client):
    r = client.get("/api/portfolio/at-a-glance?sparkline_runs=999", headers=_auth())
    assert r.status_code == 400


def test_bad_days_returns_400(client):
    r = client.get("/api/portfolio/at-a-glance?days=999", headers=_auth())
    assert r.status_code == 400


def test_bad_leaderboard_limit_returns_400(client):
    r = client.get("/api/portfolio/at-a-glance?leaderboard_limit=999", headers=_auth())
    assert r.status_code == 400


# ---------- auth ----------


def test_requires_auth(client):
    r = client.get("/api/portfolio/at-a-glance")
    assert r.status_code == 401

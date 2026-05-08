"""Endpoint tests for `GET /api/runs/{run_id}/topic-breakdown`.

Covers:
  - 404 when the run doesn't exist
  - 200 with empty topics when the run exists but has no aggregate rows
  - 200 with a populated breakdown when scenario_aggregate rows are present
  - The `topic_names` query param overrides display names
  - 400 when topic_names is malformed JSON
  - Auth required

Mocks the DB layer (db.connect / db.get_run_metadata /
db.fetch_scenario_aggregate_for_run) since the breakdown logic is purely
about Python aggregation over rows the DB returns.
"""
from __future__ import annotations

import json as _json
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
def _patched_db(meta=None, rows=None):
    """Patch db.connect to a no-op cursor and stub the two helpers used by
    the endpoint. The endpoint never actually inspects the cursor — the
    helpers do — so a MagicMock cursor is safe."""
    fake_conn = MagicMock()
    fake_cur = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = False
    fake_conn.cursor.return_value = fake_cur
    fake_cur.__enter__.return_value = fake_cur
    fake_cur.__exit__.return_value = False

    with patch("mdk_eval.web.server.db.connect", return_value=fake_conn):
        with patch("mdk_eval.web.server.db.get_run_metadata", return_value=meta):
            with patch(
                "mdk_eval.web.server.db.fetch_scenario_aggregate_for_run",
                return_value=rows or [],
            ):
                yield


def test_topic_breakdown_404_when_run_missing(client):
    with _patched_db(meta=None):
        r = client.get("/api/runs/9999/topic-breakdown", headers=_auth())
    assert r.status_code == 404


def test_topic_breakdown_requires_auth(client):
    r = client.get("/api/runs/1/topic-breakdown")
    assert r.status_code == 401


def test_topic_breakdown_returns_empty_when_no_rows(client):
    """Run exists but has no scenario_aggregate rows yet (still running, or
    no scenarios produced) — return empty topics, not 404."""
    with _patched_db(
        meta={"id": 24, "run_id": "2026-05-07-run", "agent_id": 23, "scenario_set_id": 7},
        rows=[],
    ):
        r = client.get("/api/runs/24/topic-breakdown", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_pk"] == 24
    assert body["run_id"] == "2026-05-07-run"
    assert body["topics"] == []
    assert body["total_scenarios"] == 0
    assert body["untagged_count"] == 0


def _row(scenario_id: str, mean: float, pr: float, severity: str, tags: list[str]) -> dict:
    return {
        "scenario_id": scenario_id, "tags": tags,
        "mean_score": mean, "pass_rate": pr, "severity": severity,
    }


def test_topic_breakdown_returns_populated_topics(client):
    rows = [
        _row("s1", 95, 1.0, "low", ["topic:movate_services", "category:standard"]),
        _row("s2", 90, 1.0, "medium", ["topic:movate_services", "category:adversarial"]),
        _row("s3", 60, 0.5, "high", ["topic:career_hiring", "category:safety"]),
    ]
    with _patched_db(
        meta={"id": 24, "run_id": "r1", "agent_id": 23, "scenario_set_id": 7},
        rows=rows,
    ):
        r = client.get("/api/runs/24/topic-breakdown", headers=_auth())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_pk"] == 24
    assert body["total_scenarios"] == 3
    assert body["untagged_count"] == 0
    # Worst-first sort: career_hiring (60) before movate_services (92.5)
    slugs = [t["slug"] for t in body["topics"]]
    assert slugs == ["career_hiring", "movate_services"]

    services = next(t for t in body["topics"] if t["slug"] == "movate_services")
    assert services["scenarios_count"] == 2
    assert services["mean_score"] == 92.5
    assert services["pass_rate"] == 1.0
    assert services["failures_count"] == 0
    assert "standard" in services["category_breakdown"]
    assert services["category_breakdown"]["standard"]["scenarios_count"] == 1


def test_topic_breakdown_uses_topic_names_query_param(client):
    rows = [_row("s1", 80, 1.0, "low", ["topic:services"])]
    name_map = {"services": "Movate Services"}
    with _patched_db(meta={"id": 1, "run_id": "r", "agent_id": 1, "scenario_set_id": 1}, rows=rows):
        r = client.get(
            "/api/runs/1/topic-breakdown",
            headers=_auth(),
            params={"topic_names": _json.dumps(name_map)},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["topics"][0]["display_name"] == "Movate Services"


def test_topic_breakdown_400_on_invalid_topic_names(client):
    with _patched_db(meta={"id": 1, "run_id": "r", "agent_id": 1, "scenario_set_id": 1}):
        r = client.get(
            "/api/runs/1/topic-breakdown",
            headers=_auth(),
            params={"topic_names": "not valid json"},
        )
    assert r.status_code == 400
    assert "topic_names" in r.json()["detail"].lower()


def test_topic_breakdown_400_on_non_object_topic_names(client):
    with _patched_db(meta={"id": 1, "run_id": "r", "agent_id": 1, "scenario_set_id": 1}):
        r = client.get(
            "/api/runs/1/topic-breakdown",
            headers=_auth(),
            params={"topic_names": _json.dumps(["a", "b"])},  # list, not dict
        )
    assert r.status_code == 400


def test_topic_breakdown_buckets_untagged_scenarios(client):
    """Scenarios with no `topic:<slug>` tag still appear in the response,
    bucketed under the special 'untagged' slug."""
    rows = [
        _row("s1", 80, 1.0, "low", ["topic:x"]),
        _row("legacy", 95, 1.0, "low", ["category:standard"]),  # no topic
    ]
    with _patched_db(meta={"id": 1, "run_id": "r", "agent_id": 1, "scenario_set_id": 1}, rows=rows):
        r = client.get("/api/runs/1/topic-breakdown", headers=_auth())
    body = r.json()
    slugs = {t["slug"] for t in body["topics"]}
    assert "untagged" in slugs
    assert body["untagged_count"] == 1


def test_topic_breakdown_category_breakdown_shape(client):
    """Each topic's category_breakdown entries have the expected fields."""
    rows = [_row("s1", 90, 1.0, "low", ["topic:x", "category:standard"])]
    with _patched_db(meta={"id": 1, "run_id": "r", "agent_id": 1, "scenario_set_id": 1}, rows=rows):
        r = client.get("/api/runs/1/topic-breakdown", headers=_auth())
    body = r.json()
    cb = body["topics"][0]["category_breakdown"]
    assert "standard" in cb
    for field in ("mean_score", "pass_rate", "scenarios_count"):
        assert field in cb["standard"]

"""Endpoint tests for `GET /api/runs/{run_pk}/provenance`.

Covers:
  - 404 when run doesn't exist
  - 200 with all expected fields populated for a real run
  - tool_versions_at_run_time pulled from DB (run.tool_versions JSONB)
  - tool_versions_at_query_time always present (via importlib.metadata)
  - downstream_llm_models lists 4 surfaces (topic_extractor, insights, doctor, business_report)
  - Auth required
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
def _patched_db(*, run_row):
    """Patch db.connect so the provenance endpoint sees the given run row."""
    fake_conn = MagicMock()
    fake_cur = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = False
    fake_conn.cursor.return_value = fake_cur
    fake_cur.__enter__.return_value = fake_cur
    fake_cur.__exit__.return_value = False
    fake_cur.fetchone.return_value = run_row
    with patch("mdk_eval.web.server.db.connect", return_value=fake_conn):
        yield


def _full_run_row():
    """Returns a 23-tuple matching the SELECT in the provenance endpoint."""
    from datetime import datetime, timezone
    started = datetime(2026, 5, 5, 3, 29, 13, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 5, 3, 31, 0, tzinfo=timezone.utc)
    ingested = datetime(2026, 5, 5, 3, 32, 0, tzinfo=timezone.utc)
    return (
        8, "run_2026-05-05T03-29-13Z", 1, "faq-assistant-v3",
        "1.0", "1.0", "0.1.0",
        "1a93e1580d623b2d51b3091a74610e6f...", "1d2242ad28e18aa5ca6e59897ac028c7...", "5eb3d876187970...",
        started, ended, ingested,
        ["correctness", "grounding", "safety", "ux_tone"],
        {"correctness": "openai:gpt-4o", "grounding": "anthropic:claude-haiku-4-5"},
        {"correctness": "abc123", "grounding": "def456"},
        "anthropic:claude-sonnet-4-6", 0.04, 1,
        {"openai": "2.35.1", "anthropic": "0.100.0", "deepeval": "2.5.5",
         "ragas": "not-installed", "trulens-eval": "not-installed",
         "mdk-eval": "0.1.0"},
        "jeremy@movate.com", None, None,
    )


def test_provenance_404_when_run_missing(client):
    with _patched_db(run_row=None):
        r = client.get("/api/runs/9999/provenance", headers=_auth())
    assert r.status_code == 404


def test_provenance_returns_full_payload(client):
    """Happy path: every documented field is in the response."""
    with _patched_db(run_row=_full_run_row()):
        r = client.get("/api/runs/8/provenance", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()

    # Top-level identity
    assert body["run_pk"] == 8
    assert body["run_id"] == "run_2026-05-05T03-29-13Z"
    assert body["agent_id"] == 1
    assert body["agent_slug"] == "faq-assistant-v3"
    assert body["triggered_by"] == "jeremy@movate.com"

    # Run provenance
    run = body["run"]
    assert run["schema_version"] == "1.0"
    assert run["methodology_version"] == "1.0"
    assert run["mdk_eval_version"] == "0.1.0"
    assert run["manifest_sha256"].startswith("1a93e1580d")
    assert run["dataset_sha256"].startswith("1d2242ad28")
    assert run["config_sha256"].startswith("5eb3d87618")
    assert run["started_at"] is not None
    assert run["ended_at"] is not None

    # Scoring provenance
    scoring = body["scoring"]
    assert "correctness" in scoring["judges_enabled"]
    assert "safety" in scoring["judges_enabled"]
    assert scoring["judge_models"]["correctness"] == "openai:gpt-4o"
    assert scoring["judge_prompts_sha256"]["correctness"] == "abc123"
    assert scoring["meta_judge_model"] == "anthropic:claude-sonnet-4-6"
    assert scoring["arbitration_threshold"] == 0.04
    assert scoring["runs_per_scenario"] == 1


def test_provenance_includes_run_time_and_query_time_versions(client):
    """Both pinned (run-time) and live (query-time) library versions surface
    so auditors can spot drift."""
    with _patched_db(run_row=_full_run_row()):
        r = client.get("/api/runs/8/provenance", headers=_auth())
    body = r.json()

    # Run-time versions match what the run row stored
    rt = body["tool_versions_at_run_time"]
    assert rt["openai"] == "2.35.1"
    assert rt["deepeval"] == "2.5.5"
    assert rt["mdk-eval"] == "0.1.0"

    # Query-time versions are introspected fresh on the server
    qt = body["tool_versions_at_query_time"]
    assert "openai" in qt
    assert "anthropic" in qt
    assert "httpx" in qt
    assert "deepeval" in qt
    # ragas + trulens-eval may or may not be installed; if not, the value is
    # the literal "not-installed" sentinel (NOT None or missing key).
    assert "ragas" in qt
    assert "trulens-eval" in qt


def test_provenance_lists_all_downstream_llm_models(client):
    """The 4 post-run analysis surfaces (topic extractor, insights, doctor,
    business report) all appear with provider + model + max_tokens + purpose."""
    with _patched_db(run_row=_full_run_row()):
        r = client.get("/api/runs/8/provenance", headers=_auth())
    body = r.json()

    purposes = [m["purpose"] for m in body["downstream_llm_models"]]
    purpose_text = " ".join(purposes).lower()
    assert "topic_extractor" in purpose_text
    assert "insights" in purpose_text
    assert "agent_doctor" in purpose_text or "doctor" in purpose_text
    assert "business_report" in purpose_text

    # Every entry has the required fields
    for m in body["downstream_llm_models"]:
        assert m["provider"] in ("openai", "anthropic")
        assert m["model"]
        assert m["max_tokens"] > 0
        assert m["purpose"]


def test_provenance_handles_legacy_run_with_null_columns(client):
    """Old runs (pre-migration-7-or-whatever) with null/missing JSONB
    columns shouldn't crash the endpoint."""
    row = list(_full_run_row())
    # Null out the JSONB columns
    row[14] = None  # judge_models
    row[15] = None  # judge_prompts_sha256
    row[19] = None  # tool_versions
    row[13] = None  # judges_enabled
    with _patched_db(run_row=tuple(row)):
        r = client.get("/api/runs/8/provenance", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    # Defaults to empty dicts/lists
    assert body["scoring"]["judge_models"] == {}
    assert body["scoring"]["judge_prompts_sha256"] == {}
    assert body["scoring"]["judges_enabled"] == []
    assert body["tool_versions_at_run_time"] == {}


def test_provenance_requires_auth(client):
    r = client.get("/api/runs/8/provenance")
    assert r.status_code == 401

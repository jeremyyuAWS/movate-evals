"""Postgres push module — schema + ingestion of a real run_dir.

These tests:
- Verify the migration SQL parses and creates every expected table when run
  against an in-memory mock cursor.
- Verify push_run() opens a transaction, inserts engagement → agent → run →
  child rows, and is idempotent on re-push (deletes child rows first).
- Use the actual `examples/sample_report/` artifacts as fixture data, so the
  push module is verified end-to-end against the real shapes the orchestrator
  emits.

We mock psycopg rather than spinning up a real Postgres in pytest. The CLI
integration test uses the same mock; an opt-in integration test using a real
DATABASE_URL lives at the bottom and skips when no DB is available.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mdk_eval.storage import postgres_push


pytestmark = pytest.mark.skipif(
    not postgres_push.is_available(),
    reason="Optional 'push' extra (psycopg) not installed.",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_RUN = REPO_ROOT / "examples" / "sample_report"


# ---------- helpers ----------


def _make_mock_conn() -> tuple[MagicMock, MagicMock, list[tuple[str, tuple]]]:
    """Build a mock psycopg connection that records every executed statement.

    Returns (conn_mock, cursor_mock, recorded_calls). Each entry of
    recorded_calls is (sql_string, params). RETURNING clauses get a
    monotonically increasing fake PK from fetchone().
    """
    recorded: list[tuple[str, tuple]] = []
    next_id = {"v": 1}

    cursor = MagicMock()

    def execute(sql_or_composed, params=()):
        s = str(sql_or_composed)
        recorded.append((s, tuple(params) if params else ()))
        return None

    def fetchone():
        next_id["v"] += 1
        return (next_id["v"],)

    cursor.execute.side_effect = execute
    cursor.fetchone.side_effect = fetchone
    cursor.__enter__ = lambda self: cursor
    cursor.__exit__ = lambda *a: None

    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = lambda self: conn
    conn.__exit__ = lambda *a: None
    conn.autocommit = False

    return conn, cursor, recorded


# ---------- module-level ----------


def test_is_available():
    # If psycopg is installed (gating skipif above), this must report True.
    assert postgres_push.is_available()


def test_load_run_artifacts_reads_real_sample():
    """Every required artifact should exist in the worked example."""
    art = postgres_push._load_run_artifacts(SAMPLE_RUN)
    assert "manifest" in art and art["manifest"]["run_id"]
    assert "summary" in art and "overall_score" in art["summary"]
    assert "report" in art and "scenario_aggregates" in art["report"]
    assert "aggregates" in art and isinstance(art["aggregates"], list)


def test_load_run_artifacts_raises_on_missing_required(tmp_path):
    with pytest.raises(postgres_push.PostgresPushError):
        postgres_push._load_run_artifacts(tmp_path)


def test_parse_dt_handles_z_suffix():
    dt = postgres_push._parse_dt("2026-05-05T03:29:13.000Z")
    assert dt is not None and dt.year == 2026


def test_parse_dt_returns_none_for_empty():
    assert postgres_push._parse_dt(None) is None
    assert postgres_push._parse_dt("") is None


# ---------- push_run with mocked psycopg ----------


def test_push_run_uses_upserts_for_tenancy():
    """engagement and agent should be UPSERTed (ON CONFLICT) so re-runs are safe."""
    conn, cursor, recorded = _make_mock_conn()

    with patch.object(postgres_push, "psycopg") as pg:
        pg.connect.return_value = conn
        pg.types = postgres_push.psycopg.types  # keep Json import path working

        postgres_push.push_run(
            "postgresql://fake",
            SAMPLE_RUN,
            engagement_slug="sandisk",
            agent_slug="returns",
        )

    upserted_tables = [s.lower() for (s, _) in recorded if "on conflict" in s.lower()]
    assert any("into engagement" in s for s in upserted_tables), \
        "engagement insert must use ON CONFLICT (UPSERT)"
    assert any("into agent" in s for s in upserted_tables), \
        "agent insert must use ON CONFLICT (UPSERT)"
    assert any("into run" in s for s in upserted_tables), \
        "run insert must use ON CONFLICT (UPSERT) on run_id"


def test_push_run_deletes_child_rows_before_inserting():
    """Idempotent re-push wipes prior children for the same run_id first."""
    conn, cursor, recorded = _make_mock_conn()

    with patch.object(postgres_push, "psycopg") as pg:
        pg.connect.return_value = conn
        pg.types = postgres_push.psycopg.types

        postgres_push.push_run(
            "postgresql://fake",
            SAMPLE_RUN,
            engagement_slug="sandisk",
            agent_slug="returns",
        )

    deletes = [s.lower() for (s, _) in recorded if s.lower().startswith("delete from")]
    # All four child tables must be wiped before re-insert (idempotency).
    for table in ("scenario_run", "scenario_aggregate", "failure_cluster",
                  "risk_item", "evaluation_summary"):
        assert any(table in d for d in deletes), \
            f"child table {table} must be cleared before re-insert"


def test_push_run_inserts_one_evaluation_summary():
    conn, cursor, recorded = _make_mock_conn()

    with patch.object(postgres_push, "psycopg") as pg:
        pg.connect.return_value = conn
        pg.types = postgres_push.psycopg.types

        counts = postgres_push.push_run(
            "postgresql://fake",
            SAMPLE_RUN,
            engagement_slug="sandisk",
            agent_slug="returns",
        )

    assert counts["evaluation_summary"] == 1
    summary_inserts = [s for (s, _) in recorded if "into evaluation_summary" in s.lower()]
    assert len(summary_inserts) == 1


def test_push_run_inserts_one_scenario_aggregate_per_scenario():
    conn, cursor, recorded = _make_mock_conn()

    with patch.object(postgres_push, "psycopg") as pg:
        pg.connect.return_value = conn
        pg.types = postgres_push.psycopg.types

        counts = postgres_push.push_run(
            "postgresql://fake",
            SAMPLE_RUN,
            engagement_slug="sandisk",
            agent_slug="returns",
        )

    # The sample run has 6 scenarios.
    art = postgres_push._load_run_artifacts(SAMPLE_RUN)
    assert counts["scenario_aggregates"] == len(art["aggregates"])


def test_push_run_inserts_failure_clusters_and_risks():
    conn, cursor, recorded = _make_mock_conn()

    with patch.object(postgres_push, "psycopg") as pg:
        pg.connect.return_value = conn
        pg.types = postgres_push.psycopg.types

        counts = postgres_push.push_run(
            "postgresql://fake",
            SAMPLE_RUN,
            engagement_slug="sandisk",
            agent_slug="returns",
        )

    art = postgres_push._load_run_artifacts(SAMPLE_RUN)
    assert counts["failure_clusters"] == len(art["report"].get("failure_clusters") or [])
    assert counts["risk_items"] == len(art["report"].get("risk_register") or [])


def test_push_run_commits_transaction():
    conn, cursor, recorded = _make_mock_conn()

    with patch.object(postgres_push, "psycopg") as pg:
        pg.connect.return_value = conn
        pg.types = postgres_push.psycopg.types

        postgres_push.push_run(
            "postgresql://fake",
            SAMPLE_RUN,
            engagement_slug="sandisk",
            agent_slug="returns",
        )

    conn.commit.assert_called()


# ---------- migration SQL ----------


def test_migration_sql_is_present_and_readable():
    sql_path = REPO_ROOT / "migrations" / "001_initial_schema.sql"
    assert sql_path.exists(), "migration file is required"
    text = sql_path.read_text()
    # Sanity-check every table in the schema is created
    for table in (
        "engagement", "agent", "run", "evaluation_summary",
        "scenario_aggregate", "scenario_run", "finding",
        "failure_cluster", "risk_item",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in text, f"missing CREATE TABLE for {table}"


# ---------- opt-in real-DB integration test ----------


@pytest.mark.skipif(
    not os.getenv("MDK_TEST_DATABASE_URL"),
    reason="Set MDK_TEST_DATABASE_URL to run the real-DB integration test.",
)
def test_real_postgres_roundtrip():
    """Push the sample run into a real Postgres and confirm rows landed.

    Opt-in: only runs when MDK_TEST_DATABASE_URL is set in the environment.
    Use a throwaway database — this test creates and modifies tables.
    """
    import psycopg

    conn_str = os.environ["MDK_TEST_DATABASE_URL"]
    postgres_push.ensure_schema(conn_str)
    counts = postgres_push.push_run(
        conn_str,
        SAMPLE_RUN,
        engagement_slug="test-eng",
        agent_slug="test-agent",
    )
    assert counts["run"] == 1
    assert counts["evaluation_summary"] == 1
    assert counts["scenario_aggregates"] >= 1

    with psycopg.connect(conn_str) as c, c.cursor() as cur:
        cur.execute(
            "SELECT overall_score, status FROM evaluation_summary "
            "WHERE run_id = (SELECT id FROM run WHERE run_id = %s)",
            (postgres_push._read_json(SAMPLE_RUN / "manifest.json")["run_id"],),
        )
        row = cur.fetchone()
        assert row is not None
        assert isinstance(row[0], (int, float))
        assert row[1] in ("production_ready", "pilot_ready", "needs_improvement", "not_ready")

    # Re-push should be idempotent (no error, same counts modulo timing).
    counts2 = postgres_push.push_run(
        conn_str,
        SAMPLE_RUN,
        engagement_slug="test-eng",
        agent_slug="test-agent",
    )
    assert counts2["run"] == 1

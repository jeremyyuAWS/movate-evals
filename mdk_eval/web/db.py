"""Thin Postgres helpers for the web service.

Uses psycopg directly (not an ORM) — the schema is JSONB-heavy and the
read paths the dashboard hits are simple SELECTs that don't benefit from
ORM machinery. The web layer's writes always go through one of these
helpers; never via raw cursor calls in route handlers.
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.types.json import Json


def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set in the web service environment.")
    return url


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Open a connection. Caller manages the transaction (with conn:)."""
    with psycopg.connect(database_url()) as conn:
        yield conn


def new_job_id() -> str:
    return f"job-{uuid.uuid4().hex[:16]}"


# ----------------------------- engagement / agent -----------------------------


def upsert_engagement(cur, slug: str, display_name: str | None = None) -> int:
    cur.execute(
        """
        INSERT INTO engagement (slug, display_name)
        VALUES (%s, %s)
        ON CONFLICT (slug) DO UPDATE SET display_name = EXCLUDED.display_name
        RETURNING id
        """,
        (slug, display_name or slug),
    )
    return cur.fetchone()[0]


def upsert_agent(
    cur,
    engagement_id: int,
    slug: str,
    display_name: str | None,
    backend: str,
    backend_id: str | None,
) -> int:
    cur.execute(
        """
        INSERT INTO agent (engagement_id, slug, display_name, backend, backend_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (engagement_id, slug) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            backend      = EXCLUDED.backend,
            backend_id   = EXCLUDED.backend_id
        RETURNING id
        """,
        (engagement_id, slug, display_name or slug, backend, backend_id),
    )
    return cur.fetchone()[0]


# ----------------------------- scenarios -----------------------------


def insert_scenario_set(
    cur,
    *,
    agent_id: int,
    name: str,
    source: str,
    source_sha256: str | None,
    source_filename: str | None,
    created_by: str | None,
) -> int:
    cur.execute(
        """
        INSERT INTO scenario_set (agent_id, name, source, source_sha256, source_filename, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (agent_id, name, source, source_sha256, source_filename, created_by),
    )
    return cur.fetchone()[0]


def insert_scenario(
    cur,
    *,
    scenario_set_id: int,
    scenario_id: str,
    payload: dict[str, Any],
    severity: str | None,
    tags: list[str] | None,
    derived_from: dict[str, Any] | None,
) -> int:
    cur.execute(
        """
        INSERT INTO scenario
          (scenario_set_id, scenario_id, payload, severity, tags, derived_from)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (scenario_set_id, scenario_id) DO UPDATE SET
          payload      = EXCLUDED.payload,
          severity     = EXCLUDED.severity,
          tags         = EXCLUDED.tags,
          derived_from = EXCLUDED.derived_from
        RETURNING id
        """,
        (scenario_set_id, scenario_id, Json(payload), severity, tags or [],
         Json(derived_from) if derived_from else None),
    )
    return cur.fetchone()[0]


def list_scenarios(cur, scenario_set_id: int, status: str | None = None) -> list[dict[str, Any]]:
    if status:
        cur.execute(
            "SELECT id, scenario_id, status, severity, tags, payload, derived_from "
            "FROM scenario WHERE scenario_set_id = %s AND status = %s "
            "ORDER BY scenario_id",
            (scenario_set_id, status),
        )
    else:
        cur.execute(
            "SELECT id, scenario_id, status, severity, tags, payload, derived_from "
            "FROM scenario WHERE scenario_set_id = %s ORDER BY scenario_id",
            (scenario_set_id,),
        )
    cols = ("id", "scenario_id", "status", "severity", "tags", "payload", "derived_from")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ----------------------------- web_run_job -----------------------------


def insert_job(
    cur,
    *,
    job_id: str,
    agent_id: int,
    scenario_set_id: int,
    judges_enabled: bool,
    runs_per_scenario: int,
    triggered_by: str | None,
    total_scenarios: int,
) -> int:
    cur.execute(
        """
        INSERT INTO web_run_job
          (job_id, agent_id, scenario_set_id, status, judges_enabled,
           runs_per_scenario, triggered_by, total_scenarios)
        VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s)
        RETURNING id
        """,
        (job_id, agent_id, scenario_set_id, judges_enabled, runs_per_scenario,
         triggered_by, total_scenarios),
    )
    return cur.fetchone()[0]


def update_job_status(
    cur,
    job_id: str,
    *,
    status: str | None = None,
    completed_scenarios: int | None = None,
    error_message: str | None = None,
    result_run_id: int | None = None,
    overall_score: float | None = None,
    result_status: str | None = None,
    started: bool = False,
    ended: bool = False,
) -> None:
    """Single-shot status update. Pass only the fields that changed.

    `overall_score` and `result_status` are denormalized convenience fields
    Bolt added to web_run_job so its "Recent Runs" UI can render without
    joining evaluation_summary. We populate them defensively — the writes
    are wrapped so that if a deployment's schema doesn't include these
    columns, the update silently degrades to the core fields. The dashboard
    can then fall back to the JOIN.
    """
    sets, params = [], []
    if status is not None:
        sets.append("status = %s"); params.append(status)
    if completed_scenarios is not None:
        sets.append("completed_scenarios = %s"); params.append(completed_scenarios)
    if error_message is not None:
        sets.append("error_message = %s"); params.append(error_message)
    if result_run_id is not None:
        sets.append("result_run_id = %s"); params.append(result_run_id)
    if overall_score is not None:
        sets.append("overall_score = %s"); params.append(overall_score)
    if result_status is not None:
        sets.append("result_status = %s"); params.append(result_status)
    if started:
        sets.append("started_at = now()")
    if ended:
        sets.append("ended_at = now()")
    if not sets:
        return
    params.append(job_id)
    try:
        cur.execute(f"UPDATE web_run_job SET {', '.join(sets)} WHERE job_id = %s", params)
    except psycopg.errors.UndefinedColumn:
        # Schema is missing one of the denorm columns. Retry without them so
        # the core update still goes through.
        cur.connection.rollback()
        retry_sets, retry_params = [], []
        for s, p in zip(sets, params[:-1]):
            if "overall_score" in s or "result_status" in s:
                continue
            retry_sets.append(s); retry_params.append(p)
        if retry_sets:
            retry_params.append(job_id)
            cur.execute(f"UPDATE web_run_job SET {', '.join(retry_sets)} WHERE job_id = %s", retry_params)


def get_job(cur, job_id: str) -> dict[str, Any] | None:
    # Bolt's web_run_job.result_run_id is TEXT (it stores the integer PK as a
    # string when written by either side of this codebase). Our migration spec
    # had it as BIGINT. Cast r.id to text in the join so both shapes work
    # without forcing a destructive column-type change on Bolt's side.
    cur.execute(
        """
        SELECT j.job_id, j.status, j.judges_enabled, j.runs_per_scenario,
               j.triggered_by, j.created_at, j.started_at, j.ended_at,
               j.total_scenarios, j.completed_scenarios, j.error_message,
               r.run_id AS result_run_id_str,
               s.overall_score, s.status AS result_status
        FROM web_run_job j
        LEFT JOIN run r ON j.result_run_id = r.id::text
        LEFT JOIN evaluation_summary s ON s.run_id = r.id
        WHERE j.job_id = %s
        """,
        (job_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = ("job_id", "status", "judges_enabled", "runs_per_scenario",
            "triggered_by", "created_at", "started_at", "ended_at",
            "total_scenarios", "completed_scenarios", "error_message",
            "result_run_id", "overall_score", "result_status")
    return dict(zip(cols, row))


def get_agent_and_scenario_set(cur, agent_id: int, scenario_set_id: int) -> tuple[dict, dict] | None:
    """Fetch enough metadata to drive an eval run. Returns (agent, scenario_set)
    or None if either doesn't exist or doesn't belong together."""
    cur.execute(
        """
        SELECT a.slug, a.display_name, a.backend, a.backend_id
        FROM agent a WHERE a.id = %s
        """,
        (agent_id,),
    )
    a = cur.fetchone()
    if not a:
        return None
    cur.execute(
        """
        SELECT id, agent_id, name, source FROM scenario_set
        WHERE id = %s AND agent_id = %s
        """,
        (scenario_set_id, agent_id),
    )
    s = cur.fetchone()
    if not s:
        return None
    return (
        {"slug": a[0], "display_name": a[1], "backend": a[2], "backend_id": a[3]},
        {"id": s[0], "agent_id": s[1], "name": s[2], "source": s[3]},
    )

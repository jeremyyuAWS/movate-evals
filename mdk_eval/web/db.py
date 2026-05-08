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
    parent_agent_id: int | None = None,
) -> int:
    """Insert / update one agent row. Tolerates pre-migration-007 schemas
    where parent_agent_id doesn't exist yet (falls back to the simpler insert).
    """
    try:
        cur.execute(
            """
            INSERT INTO agent (engagement_id, slug, display_name, backend, backend_id, parent_agent_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (engagement_id, slug) DO UPDATE SET
                display_name    = EXCLUDED.display_name,
                backend         = EXCLUDED.backend,
                backend_id      = EXCLUDED.backend_id,
                parent_agent_id = EXCLUDED.parent_agent_id
            RETURNING id
            """,
            (engagement_id, slug, display_name or slug, backend, backend_id, parent_agent_id),
        )
    except psycopg.errors.UndefinedColumn:
        cur.connection.rollback()
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


def set_agent_sandbox_flags(
    cur,
    agent_id: int,
    *,
    is_sandbox: bool,
    expires_at: Any | None = None,
) -> None:
    """Set the sandbox flag + expiration on an agent row. No-op on pre-
    migration-009 schemas (column doesn't exist).

    Called from the sandbox upload path after `upsert_agent` returns the
    new ID. Kept as a separate function so upsert_agent's signature stays
    stable across migrations.
    """
    try:
        cur.execute(
            "UPDATE agent SET is_sandbox = %s, sandbox_expires_at = %s WHERE id = %s",
            (is_sandbox, expires_at, agent_id),
        )
    except psycopg.errors.UndefinedColumn:
        cur.connection.rollback()


def get_agent_sandbox_state(cur, agent_id: int) -> dict[str, Any] | None:
    """Return {is_sandbox, sandbox_expires_at, backend_id} for an agent.
    Returns None if the agent doesn't exist. On pre-migration-009 schemas,
    is_sandbox defaults to False so the cleanup logic treats every agent
    as non-sandbox (safe default — no Lyzr DELETE call fires)."""
    try:
        cur.execute(
            "SELECT is_sandbox, sandbox_expires_at, backend_id, backend "
            "FROM agent WHERE id = %s",
            (agent_id,),
        )
    except psycopg.errors.UndefinedColumn:
        cur.connection.rollback()
        cur.execute(
            "SELECT FALSE, NULL, backend_id, backend FROM agent WHERE id = %s",
            (agent_id,),
        )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "is_sandbox": bool(row[0]),
        "sandbox_expires_at": row[1],
        "backend_id": row[2],
        "backend": row[3],
    }


def find_agent_by_slug(cur, engagement_id: int, slug: str) -> dict[str, Any] | None:
    """Look up an agent within an engagement by slug. Returns None if missing."""
    cur.execute(
        """
        SELECT id, slug, display_name, backend, backend_id
        FROM agent
        WHERE engagement_id = %s AND slug = %s
        """,
        (engagement_id, slug),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "slug": row[1], "display_name": row[2],
        "backend": row[3], "backend_id": row[4],
    }


def find_agent_by_backend_id(cur, engagement_id: int, backend_id: str) -> dict[str, Any] | None:
    """Look up an agent within an engagement by backend_id (Lyzr agent_id, etc).
    Used to detect 'this sub-agent has already been uploaded under a different slug'."""
    cur.execute(
        """
        SELECT id, slug, display_name, backend, backend_id
        FROM agent
        WHERE engagement_id = %s AND backend_id = %s
        LIMIT 1
        """,
        (engagement_id, backend_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "slug": row[1], "display_name": row[2],
        "backend": row[3], "backend_id": row[4],
    }


def count_agent_cleanup_cascade(cur, agent_id: int) -> dict[str, Any]:
    """Return counts of rows that would be deleted if this agent were removed.

    The DB has ON DELETE CASCADE wired through agent → scenario_set → scenario
    and agent → run → (evaluation_summary, scenario_aggregate, scenario_run,
    finding, failure_cluster, risk_item). So `DELETE FROM agent WHERE id=?`
    handles all of this automatically — but the user wants a preview before
    pulling the trigger.

    Sub-agents whose `parent_agent_id` points at this agent will have their
    parent_agent_id set to NULL (NOT deleted). We surface those so the UI
    can warn the user.

    Returns a dict suitable for direct JSON serialization. All numeric values
    are pre-cast to int.
    """
    cur.execute(
        "SELECT slug, display_name, backend, backend_id FROM agent WHERE id = %s",
        (agent_id,),
    )
    row = cur.fetchone()
    if not row:
        return {}
    agent_meta = {
        "id": agent_id, "slug": row[0], "display_name": row[1],
        "backend": row[2], "backend_id": row[3],
    }

    # Counts. Each query is bounded — for a single agent, none of these
    # join more than ~thousands of rows in worst case.
    cur.execute("SELECT COUNT(*) FROM scenario_set WHERE agent_id = %s", (agent_id,))
    scenario_sets = int(cur.fetchone()[0] or 0)

    cur.execute(
        "SELECT COUNT(*) FROM scenario WHERE scenario_set_id IN "
        "(SELECT id FROM scenario_set WHERE agent_id = %s)",
        (agent_id,),
    )
    scenarios = int(cur.fetchone()[0] or 0)

    cur.execute("SELECT COUNT(*) FROM run WHERE agent_id = %s", (agent_id,))
    runs = int(cur.fetchone()[0] or 0)

    cur.execute(
        "SELECT COUNT(*) FROM scenario_aggregate WHERE run_id IN "
        "(SELECT id FROM run WHERE agent_id = %s)",
        (agent_id,),
    )
    scenario_aggregates = int(cur.fetchone()[0] or 0)

    cur.execute(
        "SELECT COUNT(*) FROM scenario_run WHERE run_id IN "
        "(SELECT id FROM run WHERE agent_id = %s)",
        (agent_id,),
    )
    scenario_runs = int(cur.fetchone()[0] or 0)

    cur.execute(
        "SELECT COUNT(*) FROM finding WHERE scenario_aggregate_id IN "
        "(SELECT id FROM scenario_aggregate WHERE run_id IN "
        "(SELECT id FROM run WHERE agent_id = %s))",
        (agent_id,),
    )
    findings = int(cur.fetchone()[0] or 0)

    cur.execute(
        "SELECT COUNT(*) FROM failure_cluster WHERE run_id IN "
        "(SELECT id FROM run WHERE agent_id = %s)",
        (agent_id,),
    )
    failure_clusters = int(cur.fetchone()[0] or 0)

    cur.execute(
        "SELECT COUNT(*) FROM evaluation_summary WHERE run_id IN "
        "(SELECT id FROM run WHERE agent_id = %s)",
        (agent_id,),
    )
    evaluation_summaries = int(cur.fetchone()[0] or 0)

    # Sub-agents that will be orphaned (parent set to NULL).
    sub_agents: list[dict[str, Any]] = []
    try:
        cur.execute(
            "SELECT id, slug, display_name FROM agent WHERE parent_agent_id = %s",
            (agent_id,),
        )
        sub_agents = [
            {"id": r[0], "slug": r[1], "display_name": r[2]}
            for r in cur.fetchall()
        ]
    except psycopg.errors.UndefinedColumn:
        cur.connection.rollback()  # pre-migration-007 — no parent linkage column

    return {
        "agent": agent_meta,
        "would_delete": {
            "scenario_sets": scenario_sets,
            "scenarios": scenarios,
            "runs": runs,
            "scenario_aggregates": scenario_aggregates,
            "scenario_runs": scenario_runs,
            "findings": findings,
            "failure_clusters": failure_clusters,
            "evaluation_summaries": evaluation_summaries,
        },
        "would_orphan_sub_agents": sub_agents,
    }


def delete_agent(cur, agent_id: int) -> bool:
    """Hard-delete an agent. Cascades through every child table (per FK
    schema). Sub-agents that pointed here have parent_agent_id set to NULL.

    Returns True if a row was deleted. Caller is responsible for the surrounding
    transaction (commit / rollback).
    """
    cur.execute("DELETE FROM agent WHERE id = %s", (agent_id,))
    return cur.rowcount > 0


def count_run_cleanup_cascade(cur, run_pk: int) -> dict[str, Any]:
    """Count what would be deleted if this run were removed."""
    cur.execute(
        "SELECT id, run_id, agent_id, started_at FROM run WHERE id = %s",
        (run_pk,),
    )
    row = cur.fetchone()
    if not row:
        return {}
    run_meta = {
        "id": row[0], "run_id": row[1], "agent_id": row[2],
        "started_at": row[3].isoformat() if row[3] else None,
    }

    cur.execute("SELECT COUNT(*) FROM scenario_aggregate WHERE run_id = %s", (run_pk,))
    scenario_aggregates = int(cur.fetchone()[0] or 0)

    cur.execute("SELECT COUNT(*) FROM scenario_run WHERE run_id = %s", (run_pk,))
    scenario_runs = int(cur.fetchone()[0] or 0)

    cur.execute(
        "SELECT COUNT(*) FROM finding WHERE scenario_aggregate_id IN "
        "(SELECT id FROM scenario_aggregate WHERE run_id = %s)",
        (run_pk,),
    )
    findings = int(cur.fetchone()[0] or 0)

    cur.execute("SELECT COUNT(*) FROM failure_cluster WHERE run_id = %s", (run_pk,))
    failure_clusters = int(cur.fetchone()[0] or 0)

    cur.execute("SELECT COUNT(*) FROM evaluation_summary WHERE run_id = %s", (run_pk,))
    evaluation_summaries = int(cur.fetchone()[0] or 0)

    return {
        "run": run_meta,
        "would_delete": {
            "scenario_aggregates": scenario_aggregates,
            "scenario_runs": scenario_runs,
            "findings": findings,
            "failure_clusters": failure_clusters,
            "evaluation_summaries": evaluation_summaries,
        },
    }


def delete_run(cur, run_pk: int) -> bool:
    """Hard-delete a run. Cascades through every child table. The web_run_job
    row that produced this run keeps its history but loses its result_run_id
    reference (per `ON DELETE SET NULL`).
    """
    cur.execute("DELETE FROM run WHERE id = %s", (run_pk,))
    return cur.rowcount > 0


def update_agent_parent(cur, agent_id: int, parent_agent_id: int | None) -> bool:
    """Set / unset agent.parent_agent_id. Returns True if a row was updated.
    Tolerates pre-migration-007 schemas (returns False)."""
    try:
        cur.execute(
            "UPDATE agent SET parent_agent_id = %s WHERE id = %s",
            (parent_agent_id, agent_id),
        )
        return cur.rowcount > 0
    except psycopg.errors.UndefinedColumn:
        cur.connection.rollback()
        return False


def get_agent_system(cur, root_slug: str) -> dict[str, Any] | None:
    """Return a manager + its sub-agents within the same engagement.

    Looks up the manager by slug; finds children via parent_agent_id. Returns
    a dict with `manager`, `sub_agents`, and `engagement` metadata. None if
    the manager isn't found.

    Tolerates pre-migration-007 schemas (no parent_agent_id) by treating the
    root as a standalone agent with no children.
    """
    # 1. Resolve the manager by slug
    cur.execute(
        """
        SELECT a.id, a.slug, a.display_name, a.backend, e.id, e.slug, e.display_name
        FROM agent a
        JOIN engagement e ON e.id = a.engagement_id
        WHERE a.slug = %s
        ORDER BY a.id ASC
        LIMIT 1
        """,
        (root_slug,),
    )
    row = cur.fetchone()
    if not row:
        return None
    (manager_id, manager_slug, manager_name, manager_backend,
     engagement_id, engagement_slug, engagement_name) = row

    manager = {
        "id": manager_id, "slug": manager_slug, "display_name": manager_name,
        "backend": manager_backend, "role": "manager",
    }

    # 2. Find children by parent_agent_id (tolerate pre-migration schema)
    sub_agents: list[dict[str, Any]] = []
    try:
        cur.execute(
            """
            SELECT id, slug, display_name, backend
            FROM agent
            WHERE parent_agent_id = %s
            ORDER BY display_name
            """,
            (manager_id,),
        )
        for srow in cur.fetchall():
            sub_agents.append({
                "id": srow[0], "slug": srow[1], "display_name": srow[2],
                "backend": srow[3], "role": "sub_agent",
            })
    except psycopg.errors.UndefinedColumn:
        cur.connection.rollback()
        # No children possible if the column doesn't exist yet.
        sub_agents = []

    return {
        "engagement_id": engagement_id,
        "engagement_slug": engagement_slug,
        "engagement_name": engagement_name,
        "manager": manager,
        "sub_agents": sub_agents,
    }


def latest_run_summary_for_agent(cur, agent_id: int) -> dict[str, Any] | None:
    """Return the most recent run's summary for one agent — overall_score,
    status, pass_rate, started_at, run count, total cost. Used by the
    agent-systems rollup endpoint."""
    cur.execute(
        """
        WITH latest AS (
            SELECT id, started_at FROM run
            WHERE agent_id = %s
            ORDER BY started_at DESC
            LIMIT 1
        ),
        cost_sum AS (
            SELECT COALESCE(SUM(cost_usd), 0) AS total_cost
            FROM web_run_job
            WHERE result_run_id IN (SELECT id::text FROM run WHERE agent_id = %s)
              AND created_at >= now() - interval '30 days'
        )
        SELECT
            (SELECT count(*) FROM run WHERE agent_id = %s) AS runs_count,
            l.started_at,
            s.overall_score, s.status,
            s.passing_scenarios, s.total_scenarios,
            cs.total_cost
        FROM latest l
        LEFT JOIN evaluation_summary s ON s.run_id = l.id
        CROSS JOIN cost_sum cs
        """,
        (agent_id, agent_id, agent_id),
    )
    row = cur.fetchone()
    if not row:
        # No runs yet — count is still useful (0)
        return {
            "runs_count": 0, "last_run_at": None,
            "overall_score": None, "status": None,
            "pass_rate": None, "cost_usd": None,
        }
    runs_count, last_run, overall, status_val, passing, total, cost = row
    pass_rate = (float(passing or 0) / float(total)) if total else None
    return {
        "runs_count": int(runs_count),
        "last_run_at": last_run,
        "overall_score": float(overall) if overall is not None else None,
        "status": status_val,
        "pass_rate": pass_rate,
        "cost_usd": float(cost) if cost else None,
    }


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
    agent_definition: dict[str, Any] | None = None,
) -> int:
    """Insert a scenario_set. Stores the sanitized agent definition so that
    later /regenerate calls have the LLM context they need.

    Defensive: if the schema doesn't have agent_definition (pre-migration-006),
    falls back to inserting without it.
    """
    try:
        cur.execute(
            """
            INSERT INTO scenario_set
              (agent_id, name, source, source_sha256, source_filename, created_by, agent_definition)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (agent_id, name, source, source_sha256, source_filename, created_by,
             Json(agent_definition) if agent_definition else None),
        )
    except psycopg.errors.UndefinedColumn:
        cur.connection.rollback()
        cur.execute(
            """
            INSERT INTO scenario_set (agent_id, name, source, source_sha256, source_filename, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (agent_id, name, source, source_sha256, source_filename, created_by),
        )
    return cur.fetchone()[0]


def get_scenario_for_regenerate(cur, scenario_pk: int) -> dict[str, Any] | None:
    """Fetch everything needed to regenerate one scenario via LLM.

    Returns: scenario_id, current_payload, scenario_set_id, agent_definition,
             source_sha256, source_filename, regeneration_count, current_category.
    """
    cur.execute(
        """
        SELECT s.id, s.scenario_id, s.payload, s.scenario_set_id,
               s.regeneration_count, s.derived_from,
               ss.agent_definition, ss.source_sha256, ss.source_filename
        FROM scenario s
        JOIN scenario_set ss ON ss.id = s.scenario_set_id
        WHERE s.id = %s
        """,
        (scenario_pk,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = ("id", "scenario_id", "payload", "scenario_set_id",
            "regeneration_count", "derived_from",
            "agent_definition", "source_sha256", "source_filename")
    return dict(zip(cols, row))


def replace_scenario_payload(
    cur,
    scenario_pk: int,
    *,
    new_payload: dict[str, Any],
    new_severity: str | None,
    new_tags: list[str] | None,
    new_derived_from: dict[str, Any] | None,
    archive_previous: bool = True,
) -> dict[str, Any] | None:
    """Replace a scenario's payload, optionally archiving the previous version
    in `previous_payload`. Bumps regeneration_count + resets status to
    'unverified' (a regenerated scenario is fundamentally a new test —
    re-approve before using).
    """
    if archive_previous:
        cur.execute(
            """
            UPDATE scenario
            SET payload = %s,
                severity = COALESCE(%s, severity),
                tags = COALESCE(%s, tags),
                derived_from = COALESCE(%s, derived_from),
                previous_payload = payload,
                regeneration_count = regeneration_count + 1,
                status = 'unverified',
                verified_by = NULL,
                verified_at = NULL
            WHERE id = %s
            RETURNING regeneration_count
            """,
            (
                Json(new_payload), new_severity,
                new_tags or None,
                Json(new_derived_from) if new_derived_from else None,
                scenario_pk,
            ),
        )
    else:
        cur.execute(
            """
            UPDATE scenario
            SET payload = %s, severity = COALESCE(%s, severity)
            WHERE id = %s
            RETURNING regeneration_count
            """,
            (Json(new_payload), new_severity, scenario_pk),
        )
    row = cur.fetchone()
    return {"regeneration_count": row[0]} if row else None


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


def get_scenario_set_agent_definition(cur, scenario_set_id: int) -> dict[str, Any] | None:
    """Return the stored agent_definition for a scenario set (or None if pre-migration-006)."""
    cur.execute(
        "SELECT agent_definition FROM scenario_set WHERE id = %s",
        (scenario_set_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return row[0]


def get_existing_scenario_ids(cur, scenario_set_id: int) -> set[str]:
    """Return the set of scenario_id slugs already present in this set —
    used by the bulk-add endpoint to detect collisions before INSERT."""
    cur.execute(
        "SELECT scenario_id FROM scenario WHERE scenario_set_id = %s",
        (scenario_set_id,),
    )
    return {row[0] for row in cur.fetchall()}


def fetch_scenario_aggregate_for_run(cur, run_pk: int) -> list[dict[str, Any]]:
    """Return all `scenario_aggregate` rows for one run, with the fields the
    topic-breakdown insight needs (scenario_id, tags, mean_score, pass_rate,
    severity).

    Returns an empty list if the run has no aggregate rows yet (which can
    happen briefly during a still-running eval, or if the run never produced
    results). Callers should treat empty results as "no breakdown available."
    """
    cur.execute(
        """
        SELECT scenario_id, tags, mean_score, pass_rate, severity
        FROM scenario_aggregate
        WHERE run_id = %s
        ORDER BY scenario_id
        """,
        (run_pk,),
    )
    cols = ("scenario_id", "tags", "mean_score", "pass_rate", "severity")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_run_metadata(cur, run_pk: int) -> dict[str, Any] | None:
    """Return run.id, run_id (timestamp string), agent_id, scenario_set_id —
    everything needed to look up the topic-display-name mapping for a run."""
    cur.execute(
        "SELECT id, run_id, agent_id, scenario_set_id FROM run WHERE id = %s",
        (run_pk,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "run_id": row[1], "agent_id": row[2], "scenario_set_id": row[3]}


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
    cost_usd: float | None = None,
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
    if cost_usd is not None:
        sets.append("cost_usd = %s"); params.append(cost_usd)
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
            if "overall_score" in s or "result_status" in s or "cost_usd" in s:
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
    #
    # cost_usd is wrapped in COALESCE-via-LEFT-JOIN-style protection: if the
    # column was added by migration 005, it'll be present; if not (pre-migration
    # databases), the SELECT falls through to NULL via the try/except below.
    try:
        cur.execute(
            """
            SELECT j.job_id, j.status, j.judges_enabled, j.runs_per_scenario,
                   j.triggered_by, j.created_at, j.started_at, j.ended_at,
                   j.total_scenarios, j.completed_scenarios, j.error_message,
                   r.run_id AS result_run_id_str,
                   s.overall_score, s.status AS result_status,
                   j.cost_usd
            FROM web_run_job j
            LEFT JOIN run r ON j.result_run_id = r.id::text
            LEFT JOIN evaluation_summary s ON s.run_id = r.id
            WHERE j.job_id = %s
            """,
            (job_id,),
        )
        cols = ("job_id", "status", "judges_enabled", "runs_per_scenario",
                "triggered_by", "created_at", "started_at", "ended_at",
                "total_scenarios", "completed_scenarios", "error_message",
                "result_run_id", "overall_score", "result_status", "cost_usd")
    except psycopg.errors.UndefinedColumn:
        # Pre-migration-005 fallback: cost_usd column doesn't exist yet.
        cur.connection.rollback()
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
        cols = ("job_id", "status", "judges_enabled", "runs_per_scenario",
                "triggered_by", "created_at", "started_at", "ended_at",
                "total_scenarios", "completed_scenarios", "error_message",
                "result_run_id", "overall_score", "result_status")
    row = cur.fetchone()
    if not row:
        return None
    out = dict(zip(cols, row))
    # Always populate cost_usd in the response, even when the column is missing.
    out.setdefault("cost_usd", None)
    if out.get("cost_usd") is not None:
        out["cost_usd"] = float(out["cost_usd"])
    return out


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

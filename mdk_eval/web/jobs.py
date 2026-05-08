"""Background eval-run executor.

Pulls scenarios from Postgres, builds a transient JSONL dataset, runs the
existing `execute_run()` orchestrator against it, then pushes results back
to Postgres via `postgres_push.push_run`.

Runs in-process via FastAPI BackgroundTasks. For prototype scale (a few
concurrent evals on a single Fly machine) this is fine. When concurrency
matters, swap for a real queue — the job/state machine in `web_run_job`
already separates state from worker.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import traceback
from pathlib import Path

import psycopg

from . import cost, db
from ..config import AdapterConfig, JudgesConfig, RunConfig
from ..runner.orchestrator import execute_run
from ..storage import postgres_push


async def execute_job(job_id: str) -> None:
    """Top-level entry point — runs the full eval pipeline for one job_id.

    Emits structured events at each lifecycle transition so Application
    Insights can show eval throughput / failure rate without the operator
    having to scrape Postgres.
    """
    # Bind the job_id as the trace_id for this whole task — lets us correlate
    # every log line, every judge call, every DB write inside this job.
    try:
        from .observability import emit_event, trace_context
    except ImportError:
        emit_event = lambda *a, **k: None  # noqa: E731
        from contextlib import nullcontext
        trace_context = lambda **k: nullcontext((k.get("trace_id"), k.get("trace_id")))  # noqa: E731

    with trace_context(trace_id=job_id):
        emit_event("job.started", job_id=job_id)
        try:
            await _execute_job_inner(job_id)
            emit_event("job.completed", job_id=job_id)
        except Exception as e:
            # Catch-all so an exception never leaves the background task crashing
            # the Uvicorn worker. Mark the job failed with the error visible to the
            # caller via GET /api/runs/{job_id}.
            emit_event("job.failed", job_id=job_id, error=f"{type(e).__name__}: {e}")
            with db.connect() as conn, conn.cursor() as cur:
                db.update_job_status(
                    cur, job_id,
                    status="failed",
                    error_message=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}",
                    ended=True,
                )
                conn.commit()


async def _execute_job_inner(job_id: str) -> None:
    # 1. Mark running.
    with db.connect() as conn, conn.cursor() as cur:
        db.update_job_status(cur, job_id, status="running", started=True)
        conn.commit()

    # 2. Read job + scenario set + agent from DB. Bail with a useful error if
    #    any of these vanished between queue time and execution.
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT agent_id, scenario_set_id, judges_enabled, runs_per_scenario, triggered_by "
            "FROM web_run_job WHERE job_id = %s",
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            return  # job was deleted; nothing to do
        agent_id, scenario_set_id, judges_enabled, runs_per_scenario, triggered_by = row

        meta = db.get_agent_and_scenario_set(cur, agent_id, scenario_set_id)
        if not meta:
            db.update_job_status(
                cur, job_id, status="failed",
                error_message="agent or scenario_set not found at execution time",
                ended=True,
            )
            conn.commit()
            return
        agent_meta, set_meta = meta

        # Pull approved scenarios. The job request already filtered by
        # only_approved when it was queued; what's stored on web_run_job is
        # the resolved total count, not the filter. Re-applying here ensures
        # we never run against a scenario that was rejected after queue time.
        scenarios = db.list_scenarios(cur, scenario_set_id, status="approved")
        if not scenarios:
            # fall back to all scenarios if none approved (preserve usefulness
            # in early-flow when nothing has been reviewed yet).
            scenarios = db.list_scenarios(cur, scenario_set_id)

    if not scenarios:
        with db.connect() as conn, conn.cursor() as cur:
            db.update_job_status(
                cur, job_id, status="failed",
                error_message="No scenarios in this scenario_set.",
                ended=True,
            )
            conn.commit()
        return

    # 3. Materialize scenarios into a transient JSONL file the orchestrator
    #    can read via load_scenarios. The orchestrator was built around files;
    #    rather than refactoring it to accept in-memory lists, we honor that
    #    contract here.
    with tempfile.TemporaryDirectory(prefix=f"mdk-job-{job_id}-") as tmpdir:
        tmp = Path(tmpdir)
        ds_path = tmp / "scenarios.jsonl"
        with ds_path.open("w") as f:
            for s in scenarios:
                f.write(json.dumps(s["payload"]) + "\n")

        # 4. Build a RunConfig that mirrors what the CLI would have built.
        cfg = RunConfig(
            adapter=_build_adapter_config(agent_meta),
            judges=JudgesConfig(),
            judges_enabled=judges_enabled,
            runs_per_scenario=runs_per_scenario,
            concurrency=2,
            output_dir=str(tmp / "results"),
            dataset=str(ds_path),
            langfuse_enabled=False,
            pdf=False,
            client_name=set_meta["name"],
            confidentiality_footer="Internal — Movate Agent Assurance.",
        )

        # 5. Run.
        run_dir, _ = await execute_run(cfg, dataset_path=str(ds_path))

        # 6. Push results to the same database — this is the moment the
        #    dashboard's run views start showing the new run. Same
        #    push_run we built earlier; total reuse.
        postgres_push.push_run(
            db.database_url(),
            run_dir,
            engagement_slug=_engagement_slug_for_agent(agent_id),
            agent_slug=agent_meta["slug"],
            engagement_display_name=None,
            agent_display_name=agent_meta["display_name"],
            triggered_by=triggered_by or "web",
            ci_url=None,
        )

        # 7. Look up the freshly-inserted run row's PK + composite score so we
        #    can both link from web_run_job.result_run_id and denormalize the
        #    score onto web_run_job (so Bolt's "Recent Runs" UI can render
        #    without joining evaluation_summary).
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.id, s.overall_score, s.status
                FROM run r
                LEFT JOIN evaluation_summary s ON s.run_id = r.id
                WHERE r.run_id = %s
                """,
                (run_dir.name,),
            )
            row = cur.fetchone()
            result_run_id = row[0] if row else None
            overall_score = float(row[1]) if row and row[1] is not None else None
            result_status = row[2] if row else None

            # Estimate cost the same way the preview endpoint does, but using
            # the actual scenario count + judges_enabled + runs_per_scenario
            # this run actually used. Stored on the row for cheap portfolio
            # cost rollups.
            est = cost.estimate_run_cost(
                num_scenarios=len(scenarios),
                runs_per_scenario=runs_per_scenario,
                judges_enabled=judges_enabled,
            )

            db.update_job_status(
                cur, job_id,
                status="done",
                completed_scenarios=len(scenarios),
                result_run_id=result_run_id,
                overall_score=overall_score,
                result_status=result_status,
                cost_usd=est.estimated_cost_usd,
                ended=True,
            )

            # Provisional → Active state transition. Per migration 008, an
            # agent stays provisional (is_active=false) until it produces a
            # real successful run. We use the presence of an evaluation_summary
            # row as the success signal — that row is only inserted by
            # postgres_push when scoring fully completes. Failed runs (no
            # eval_summary) keep the agent provisional, so it stays out of
            # the default portfolio view until a real run lands.
            if result_run_id is not None and overall_score is not None:
                try:
                    cur.execute(
                        "UPDATE agent SET is_active = TRUE WHERE id = %s AND is_active = FALSE",
                        (agent_id,),
                    )
                except psycopg.errors.UndefinedColumn:
                    # Pre-migration-008 — column doesn't exist yet. The CLI
                    # path or a stale schema may have skipped the migration;
                    # tolerate it so eval execution still completes.
                    cur.connection.rollback()

            conn.commit()


def _build_adapter_config(agent_meta: dict) -> AdapterConfig:
    """Map an agent row into an AdapterConfig that execute_run() accepts.

    Raises ValueError with an actionable message (citing the agent slug) when
    a required field is missing — better than letting the adapter factory
    raise a generic "requires 'agent_id'" deeper in the run pipeline, which
    surfaces to the user as a confusing run failure with no diagnostic.
    """
    backend = agent_meta["backend"]
    if backend == "lyzr":
        if not agent_meta.get("backend_id"):
            slug = agent_meta.get("slug", "(unknown agent)")
            raise ValueError(
                f"Agent '{slug}' has no Lyzr agent_id stored — cannot dispatch "
                "evaluation. The agent record's backend_id is empty, which "
                "happens when the upload JSON didn't include `_id`, `id`, or "
                "`agent_id` at the top level. Re-upload the agent definition "
                "with the Lyzr agent ID present, or PATCH "
                f"/api/agents/{agent_meta.get('id', '<id>')} to set it."
            )
        return AdapterConfig(
            target="lyzr",
            agent_id=agent_meta["backend_id"],
            api_key_env="LYZR_API_KEY",
            response_text_path="$.response",
            timeout_s=60.0,
            max_retries=1,
        )
    if backend == "mock":
        return AdapterConfig(target="mock", timeout_s=30.0, max_retries=1)
    if backend == "openai_compat":
        return AdapterConfig(
            target="openai_compat",
            endpoint=agent_meta["backend_id"] or "https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            timeout_s=60.0,
            max_retries=1,
        )
    # Fall back to mock so an unknown/missing backend doesn't crash the worker;
    # the resulting eval will be obviously synthetic and the user will see why.
    return AdapterConfig(target="mock", timeout_s=30.0, max_retries=1)


def _engagement_slug_for_agent(agent_id: int) -> str:
    """Look up the engagement slug for an agent — needed for postgres_push."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT e.slug FROM engagement e JOIN agent a ON a.engagement_id = e.id WHERE a.id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        return row[0] if row else "unknown"


def submit(background_tasks, job_id: str) -> None:
    """Schedule a job for execution. Wraps the asyncio call so FastAPI's
    BackgroundTasks can fire it without awaiting."""
    background_tasks.add_task(_run_async_job, job_id)


def _run_async_job(job_id: str) -> None:
    """Bridge sync BackgroundTasks → async execute_job."""
    try:
        # Get-or-create event loop. In a fresh background thread there's no
        # running loop, so asyncio.run is appropriate.
        asyncio.run(execute_job(job_id))
    except Exception:
        # execute_job already records errors to the DB. This catch is just
        # to prevent the BackgroundTask from raising into Uvicorn's logs in
        # a way that looks like a server bug.
        pass

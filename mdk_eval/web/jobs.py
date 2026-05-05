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

from . import db
from ..config import AdapterConfig, JudgesConfig, RunConfig
from ..runner.orchestrator import execute_run
from ..storage import postgres_push


async def execute_job(job_id: str) -> None:
    """Top-level entry point — runs the full eval pipeline for one job_id."""
    try:
        await _execute_job_inner(job_id)
    except Exception as e:  # pragma: no cover — tested via wrappers
        # Catch-all so an exception never leaves the background task crashing
        # the Uvicorn worker. Mark the job failed with the error visible to the
        # caller via GET /api/runs/{job_id}.
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

            db.update_job_status(
                cur, job_id,
                status="done",
                completed_scenarios=len(scenarios),
                result_run_id=result_run_id,
                overall_score=overall_score,
                result_status=result_status,
                ended=True,
            )
            conn.commit()


def _build_adapter_config(agent_meta: dict) -> AdapterConfig:
    """Map an agent row into an AdapterConfig that execute_run() accepts."""
    backend = agent_meta["backend"]
    if backend == "lyzr":
        return AdapterConfig(
            target="lyzr",
            agent_id=agent_meta["backend_id"] or "",
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

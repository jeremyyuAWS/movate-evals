"""Background worker that drains the eval_jobs pgmq queue.

Lifecycle
---------
Started as an asyncio task during FastAPI's `startup` event; cancelled during
`shutdown`. Polls pgmq with a small backoff when empty, executes one job at
a time (sequential — eval runs are CPU-and-network-heavy enough that
multi-worker parallelism inside one container hurts more than helps).

Why one task in one process for now
-----------------------------------
The queue infrastructure (pgmq) is the durability fix. Running the worker
inside the API process is a deployment simplification — one Fly machine,
one deployment, one log stream. If usage grows past one concurrent eval,
split this module into a separate `mdk-eval-worker` CLI command that runs
on its own Fly machine; the queue code doesn't change.

What the worker does on startup
-------------------------------
Sweeps for `web_run_job` rows stuck in `running` from before the restart.
Marks them `failed` with a "worker restarted mid-job" message — this lets
the UI surface the partial run rather than spin forever. The pgmq message
will reappear after VT and re-execute fresh.
"""
from __future__ import annotations

import asyncio
import logging

from . import db, jobs, queue


log = logging.getLogger(__name__)

# Polling cadence when the queue is empty. Long enough to not hammer Postgres
# with empty SELECTs; short enough that user-perceived latency for "click Run
# → see status flip to running" stays well under 10 seconds.
EMPTY_POLL_INTERVAL_S = 5.0

# How long a job can sit in `running` (per the DB clock) before we assume the
# previous worker died with the message in flight. Should be larger than any
# legitimate eval — a 5-minute eval running on a worker that just restarted
# 2 minutes ago should NOT get marked failed.
ORPHAN_THRESHOLD_S = 1800  # 30 minutes


async def reap_orphans() -> int:
    """Mark any web_run_job rows stuck in 'running' beyond the orphan threshold
    as 'failed'. Returns count of rows reaped."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE web_run_job
            SET status = 'failed',
                error_message = COALESCE(error_message, '') ||
                                ' [reaped: worker restart while job was in flight]',
                ended_at = COALESCE(ended_at, now())
            WHERE status = 'running'
              AND started_at IS NOT NULL
              AND started_at < now() - make_interval(secs => %s)
            RETURNING job_id
            """,
            (ORPHAN_THRESHOLD_S,),
        )
        reaped = [r[0] for r in cur.fetchall()]
        conn.commit()
    if reaped:
        log.warning("Reaped %d orphan job(s): %s", len(reaped), reaped)
    return len(reaped)


async def process_one(msg_id: int, payload: dict) -> bool:
    """Run one job. Returns True on success, False on failure (caller decides
    delete vs archive vs leave-for-redelivery)."""
    job_id = payload.get("job_id")
    if not job_id:
        log.error("Queue message %d has no job_id; archiving: %s", msg_id, payload)
        return False

    log.info("Processing job %s (msg %d)", job_id, msg_id)
    try:
        await jobs.execute_job(job_id)
    except Exception as e:
        log.exception("Job %s raised: %s", job_id, e)
        # execute_job catches all exceptions and updates the row to 'failed'.
        # If something escapes that, the row stays in 'running' and the
        # orphan-reaper will catch it later. Either way, we delete the
        # message — re-running a bad payload won't help.
        return False
    return True


async def worker_loop(stop_event: asyncio.Event) -> None:
    """Main loop. Exits cleanly when stop_event is set."""
    log.info("Worker loop started — polling pgmq queue '%s'", queue.QUEUE_NAME)
    await reap_orphans()  # one-shot at startup

    while not stop_event.is_set():
        try:
            msgs = queue.poll(qty=1)
        except Exception as e:
            log.exception("Queue poll failed (will retry after backoff): %s", e)
            await asyncio.sleep(EMPTY_POLL_INTERVAL_S)
            continue

        if not msgs:
            # Race-friendly sleep: if a stop arrives during the sleep, we exit
            # within EMPTY_POLL_INTERVAL_S rather than waiting for the next poll.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=EMPTY_POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            continue

        for msg_id, payload in msgs:
            if stop_event.is_set():
                # Don't start a new job if we're shutting down — the message
                # will reappear after VT and another worker (or this same
                # one after restart) will pick it up.
                break
            ok = await process_one(msg_id, payload)
            if ok:
                queue.delete(msg_id)
            else:
                queue.archive(msg_id)

    log.info("Worker loop exited")

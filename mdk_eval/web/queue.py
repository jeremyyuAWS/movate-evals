"""pgmq-backed eval job queue.

Why pgmq instead of FastAPI BackgroundTasks
-------------------------------------------
BackgroundTasks runs in-process. If the Fly machine restarts mid-job (deploy,
OOM kill, machine shuffle), the work is lost and the corresponding `web_run_job`
row stays in `running` forever with no retry. For "Movate is showing this to
their first paying customer" reliability, that's not acceptable.

pgmq gives us:
  - **Durability** — jobs survive container restart; messages persist in
    Postgres.
  - **Visibility timeout** — a worker that picks up a message and dies will
    have the message reappear after `vt` seconds for another worker.
  - **Multiple workers** — eventually we'll want parallelism; pgmq handles
    concurrent consumers safely.

Two operations the rest of the app needs from this module:
  enqueue(job_id) — called from POST /api/runs after inserting the
                    web_run_job row.
  poll(timeout_s) — called by the worker loop. Returns (msg_id, payload) or
                    (None, None) if the queue is empty.

The worker loop itself lives in `worker.py`.
"""
from __future__ import annotations

import json
from typing import Any

from . import db


QUEUE_NAME = "eval_jobs"

# Visibility timeout for in-flight messages. If a worker picks up a message
# and dies before deleting it, the message reappears after this many seconds
# for another worker. Set high enough that a normal eval (judges + multi-run)
# won't accidentally trigger redelivery.
VISIBILITY_TIMEOUT_S = 600  # 10 minutes


def enqueue(job_id: str) -> int:
    """Push a job onto the queue. Returns the pgmq message id (for audit)."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pgmq.send(%s, %s::jsonb)",
            (QUEUE_NAME, json.dumps({"job_id": job_id})),
        )
        msg_id = cur.fetchone()[0]
        conn.commit()
        return int(msg_id)


def poll(qty: int = 1) -> list[tuple[int, dict[str, Any]]]:
    """Read up to `qty` messages from the queue with the configured visibility
    timeout. Returns a list of (msg_id, payload) tuples. Empty list if no
    messages are available.

    The caller MUST call `delete(msg_id)` on success or `archive(msg_id)` on
    permanent failure. Otherwise the message will reappear after VT.
    """
    out: list[tuple[int, dict[str, Any]]] = []
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT msg_id, message FROM pgmq.read(%s, %s, %s)",
            (QUEUE_NAME, VISIBILITY_TIMEOUT_S, qty),
        )
        for msg_id, message in cur.fetchall():
            payload = message if isinstance(message, dict) else json.loads(message)
            out.append((int(msg_id), payload))
        conn.commit()
    return out


def delete(msg_id: int) -> None:
    """Acknowledge successful processing — message is gone forever."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT pgmq.delete(%s, %s)", (QUEUE_NAME, msg_id))
        conn.commit()


def archive(msg_id: int) -> None:
    """Move to the archive table. Use for permanent failures we want to keep
    for forensics. Archived messages don't redeliver."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT pgmq.archive(%s, %s)", (QUEUE_NAME, msg_id))
        conn.commit()


def queue_depth() -> dict[str, int]:
    """Snapshot of queue health — useful for ops dashboards."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT queue_length, total_messages FROM pgmq.metrics(%s)",
            (QUEUE_NAME,),
        )
        row = cur.fetchone()
        if not row:
            return {"queue_length": 0, "total_messages": 0}
        return {"queue_length": int(row[0] or 0), "total_messages": int(row[1] or 0)}

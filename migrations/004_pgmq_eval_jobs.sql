-- Migration 004 — pgmq-backed eval job queue
--
-- Replaces the in-process FastAPI BackgroundTasks model with a durable
-- Postgres-native queue. Reasons:
--   - jobs survive container restarts (Fly redeploys, OOM, machine shuffles)
--   - workers can be scaled independently of API containers later
--   - failed dequeues automatically reappear after the visibility timeout
--   - we get audit + introspection for free (pgmq.metrics views)
--
-- Idempotent: safe to re-apply.
--
-- After this lands, the flow is:
--   POST /api/runs  → INSERT web_run_job (status='queued') + pgmq.send('eval_jobs', {job_id})
--   worker loop     → pgmq.read('eval_jobs', vt=600, qty=1) → execute → pgmq.delete on success
--                     (vt = visibility timeout; if worker dies, msg reappears in 10 min)

CREATE EXTENSION IF NOT EXISTS pgmq;

-- Create the queue if it doesn't already exist. pgmq's create() is itself idempotent.
SELECT pgmq.create('eval_jobs') WHERE NOT EXISTS (
    SELECT 1 FROM pgmq.list_queues() WHERE queue_name = 'eval_jobs'
);

-- Sanity guard: web_run_job already has a 'queued' status; we keep the column
-- as the source-of-truth for UI, with pgmq as the work-queue underneath.
-- If a job sits in status='queued' for > 1 hour, it likely means the worker
-- isn't running. Add a comment as a hint for future operators.
COMMENT ON COLUMN web_run_job.status IS
  'queued (in pgmq) | running (worker picked up) | done | failed. '
  'Stuck in queued > 1h usually means no worker is consuming the eval_jobs queue.';

INSERT INTO _mdk_schema_version (version) VALUES ('1.3')
ON CONFLICT (version) DO NOTHING;

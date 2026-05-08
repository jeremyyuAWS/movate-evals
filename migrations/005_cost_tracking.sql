-- Migration 005 — Per-run cost tracking
--
-- Adds an actual-cost column to web_run_job populated at job completion.
-- Without this column, we can't backfill historical cost — every run that
-- already happened has its tokens-spent data only in OpenAI/Anthropic
-- billing dashboards, not in our DB.
--
-- The value stored is the same shape the cost-preview endpoint computes
-- (using cost.estimate_run_cost), since exact token counts would require
-- instrumenting every judge call to capture usage metadata. Estimate is
-- accurate to ±20% in practice — good enough for budget tracking, alerting,
-- and per-engagement cost rollups. Replace with measured tokens when the
-- precision matters (Phase 3 in the PRD).
--
-- Idempotent + additive.

ALTER TABLE web_run_job
  ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(10, 4);

-- Helpful index for portfolio cost queries ("how much did engagement X
-- spend this month?")
CREATE INDEX IF NOT EXISTS web_run_job_agent_created_idx
  ON web_run_job (agent_id, created_at DESC);

INSERT INTO _mdk_schema_version (version) VALUES ('1.4')
ON CONFLICT (version) DO NOTHING;

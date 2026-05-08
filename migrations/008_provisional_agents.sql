-- Migration 008 — Provisional vs Active agent state
--
-- Why
-- ----
-- New agent uploads (and any sub-agents auto-created with them) used to land
-- in the portfolio at full visibility immediately, even before they had a
-- successful run. That created "0/100 NOT READY" placeholder cards that
-- cluttered the portfolio while a delivery engineer was still designing
-- their test mix or fixing a broken upload.
--
-- This migration introduces a two-state lifecycle:
--   - is_active = FALSE  →  provisional / draft (default for new uploads)
--   - is_active = TRUE   →  active (set by the run worker after a successful
--                            scoring push completes)
--
-- The default portfolio view filters to is_active = TRUE. Opt in to seeing
-- drafts via `?include_provisional=true` on the portfolio endpoints.
--
-- Backfill
-- --------
-- Existing agents that already have a successful run (i.e., at least one
-- evaluation_summary row tied to one of their runs) are flipped to active.
-- Agents that exist in the DB but never produced a successful run stay
-- provisional and disappear from the default portfolio view immediately
-- after this migration applies.
--
-- Idempotent: safe to run multiple times.

ALTER TABLE agent
  ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT FALSE;

-- Index for the filter — every portfolio query now applies it.
CREATE INDEX IF NOT EXISTS agent_active_idx ON agent (is_active);

-- Backfill: any agent with at least one row in evaluation_summary has
-- already been "graduated" to active by virtue of producing a real score.
UPDATE agent
SET is_active = TRUE
WHERE id IN (
    SELECT DISTINCT r.agent_id
    FROM run r
    JOIN evaluation_summary s ON s.run_id = r.id
)
AND is_active = FALSE;

-- Bump schema version so the dashboard can refuse incompatible reads at
-- major-version bumps in the future.
INSERT INTO _mdk_schema_version (version) VALUES ('1.4')
ON CONFLICT (version) DO NOTHING;

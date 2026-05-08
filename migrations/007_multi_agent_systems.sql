-- Migration 007 — Multi-agent systems (manager + managed sub-agents)
--
-- A "manager" agent in Lyzr orchestrates one or more sub-agents (referenced
-- via the `managed_agents` array in its definition). To evaluate such a
-- system properly the platform now needs to:
--   1. Test each sub-agent independently (unit-style)
--   2. Test the manager end-to-end (orchestration + customer experience)
--   3. Surface the system as a unit (parent + children rollup)
--
-- This migration adds the explicit parent/child relationship on the agent
-- table. It is purely additive — single-agent records keep parent_agent_id
-- NULL and behave exactly as before.
--
-- Rollback: ALTER TABLE agent DROP COLUMN parent_agent_id;
--           DROP INDEX IF EXISTS agent_parent_id_idx;
--           (Drop is safe because the column is nullable.)

BEGIN;

ALTER TABLE agent
    ADD COLUMN IF NOT EXISTS parent_agent_id INT
    REFERENCES agent(id) ON DELETE SET NULL;

-- Index for tree queries: "give me all children of agent X" should be cheap
-- regardless of agent count.
CREATE INDEX IF NOT EXISTS agent_parent_id_idx ON agent (parent_agent_id)
    WHERE parent_agent_id IS NOT NULL;

COMMENT ON COLUMN agent.parent_agent_id IS
    'For sub-agents in a multi-agent system: the manager that orchestrates this one. '
    'NULL for top-level / standalone agents. Set at ingest time via the parent_agent_slug '
    'form parameter on POST /api/agent-definitions, or later via PATCH /api/agents/{id}.';

-- Best-effort bookkeeping: the platform's prior migrations didn't always
-- create the `_migration` registry on Supabase. Insert only if the table
-- exists. (DO blocks let us guard the INSERT.)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = '_migration') THEN
        INSERT INTO _migration (filename) VALUES ('007_multi_agent_systems.sql')
            ON CONFLICT DO NOTHING;
    END IF;
END $$;

COMMIT;

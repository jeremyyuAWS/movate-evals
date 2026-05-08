-- Migration 009 — Sandbox / ephemeral agent provisioning
--
-- Why
-- ----
-- Sandbox mode lets a user upload an agent JSON without first creating the
-- agent in Lyzr. Backend calls Lyzr's POST /v3/agents endpoint to provision
-- a temporary instance, runs evaluations against it, and tears it down on
-- local DELETE (or via the cleanup cron when the TTL expires).
--
-- This is for three primary use cases:
--   1. Pre-deployment validation — testing an agent definition before
--      committing it to production
--   2. CI/CD pipelines — ephemeral PR-eval workflow
--   3. Movate-led pilots — eval against a customer's agent definition
--      handed over before the customer's Lyzr account is set up
--
-- Sandbox agents are flagged via `is_sandbox = TRUE`. Every DELETE on a
-- sandbox-flagged agent first calls Lyzr's DELETE /v3/agents/{id} so the
-- ephemeral resource doesn't leak. Phase 2 will add a TTL cleanup cron;
-- Phase 1 relies on manual delete.
--
-- Stacks on top of migration 008 (provisional/active state). A sandbox
-- agent starts is_active=FALSE (provisional) AND is_sandbox=TRUE; the run
-- worker flips is_active=TRUE on first successful run; the user's eventual
-- DELETE tears down the Lyzr-side instance.
--
-- Idempotent: safe to run multiple times.

ALTER TABLE agent
  ADD COLUMN IF NOT EXISTS is_sandbox BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS sandbox_expires_at TIMESTAMPTZ;

-- Index for the cleanup cron's daily sweep (Phase 2 will use this).
CREATE INDEX IF NOT EXISTS agent_sandbox_expires_idx
  ON agent (sandbox_expires_at)
  WHERE is_sandbox = TRUE;

-- Bump schema version.
INSERT INTO _mdk_schema_version (version) VALUES ('1.5')
ON CONFLICT (version) DO NOTHING;

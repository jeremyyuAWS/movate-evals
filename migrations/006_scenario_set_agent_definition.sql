-- Migration 006 — Store the sanitized agent definition on the scenario_set
--
-- Without this, /api/scenarios/{id}/regenerate has no way to call the LLM
-- with the original agent context — the source_sha256 alone isn't enough.
-- We could ask the user to re-upload, but that breaks the "click regenerate"
-- promise.
--
-- The stored definition is the SANITIZED version (after sanitize_bytes has
-- stripped api_keys, passwords, etc.). It's safe to persist; risk reviewers
-- can audit it.
--
-- Idempotent + additive. Existing scenario_sets get NULL here; the
-- regenerate endpoint detects that and returns 400 with a clear "cannot
-- regenerate, please re-upload" message.

ALTER TABLE scenario_set
  ADD COLUMN IF NOT EXISTS agent_definition JSONB;

-- Helpful index for "find scenarios from this agent definition" queries.
CREATE INDEX IF NOT EXISTS scenario_set_definition_sha_idx
  ON scenario_set (source_sha256);

-- Track per-scenario regeneration history for the diff UI.
ALTER TABLE scenario
  ADD COLUMN IF NOT EXISTS regeneration_count INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS previous_payload JSONB;

INSERT INTO _mdk_schema_version (version) VALUES ('1.5')
ON CONFLICT (version) DO NOTHING;

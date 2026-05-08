-- Migration 003 — Make CLI-pushed runs as transparent as web-uploaded ones.
--
-- The dashboard's per-scenario drawer needs the original test definition
-- (input prompt, expected output, assertions, provenance) to show "Expected
-- vs Actual" — not just scores. For web-uploaded scenarios that data lives
-- in the `scenario` table; for `mdk-eval push` runs (CLI / CI), it has no
-- home. This migration adds it.
--
-- Idempotent + additive: safe to re-apply, doesn't touch existing columns.

ALTER TABLE scenario_aggregate
  ADD COLUMN IF NOT EXISTS scenario_payload JSONB;

-- Helpful index for filtering scenarios that came from a particular extractor
-- (heuristic, LLM, manual) — useful for trust dashboards.
CREATE INDEX IF NOT EXISTS scenario_aggregate_extractor_idx
  ON scenario_aggregate ((scenario_payload -> 'meta' -> 'derived_from' ->> 'extractor'));

INSERT INTO _mdk_schema_version (version) VALUES ('1.2')
ON CONFLICT (version) DO NOTHING;

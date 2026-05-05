-- Migration 002 — Scenario storage + web run jobs
--
-- Adds three tables that turn the dashboard from a read-only viewer into a
-- full ingestion + execution surface:
--
-- 1. scenario_set     — a named bundle of scenarios for one agent (e.g. "v3-baseline")
-- 2. scenario         — individual scenarios with status (unverified/approved/rejected)
-- 3. web_run_job      — async run state for evals kicked off via the FastAPI service
--
-- Idempotent: safe to re-apply. Strictly additive over migration 001 — does not
-- touch any existing table.

-- ----------------------------------------------------------------------------
-- scenario_set: a named collection of scenarios for one agent
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scenario_set (
  id              BIGSERIAL PRIMARY KEY,
  agent_id        BIGINT NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,
  source          TEXT NOT NULL,            -- 'manual' | 'lyzr-ingest' | 'lyzr-ingest+llm'
  source_sha256   TEXT,                     -- SHA-256 of the original agent definition (when ingested)
  source_filename TEXT,                     -- original filename for provenance
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by      TEXT,                     -- email or 'web'
  notes           TEXT,
  UNIQUE (agent_id, name)
);
CREATE INDEX IF NOT EXISTS scenario_set_agent_idx ON scenario_set (agent_id);

-- ----------------------------------------------------------------------------
-- scenario: one row per scenario; status drives the review workflow
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scenario (
  id                   BIGSERIAL PRIMARY KEY,
  scenario_set_id      BIGINT NOT NULL REFERENCES scenario_set(id) ON DELETE CASCADE,
  scenario_id          TEXT NOT NULL,        -- the slug stored in JSONL today
  payload              JSONB NOT NULL,       -- full scenario JSON (input, expected_tools, rubric, ...)
  status               TEXT NOT NULL DEFAULT 'unverified',
  severity             TEXT,                 -- denormalized for quick filtering
  tags                 TEXT[],               -- denormalized
  derived_from         JSONB,                -- meta.derived_from (extractor/model/source_sha/quote)
  verified_by          TEXT,
  verified_at          TIMESTAMPTZ,
  notes                TEXT,
  UNIQUE (scenario_set_id, scenario_id),
  CONSTRAINT scenario_status_band CHECK (status IN ('unverified', 'approved', 'rejected'))
);
CREATE INDEX IF NOT EXISTS scenario_set_idx ON scenario (scenario_set_id);
CREATE INDEX IF NOT EXISTS scenario_status_idx ON scenario (scenario_set_id, status);

-- ----------------------------------------------------------------------------
-- web_run_job: status of an eval kicked off via the API
--
-- Lives separately from `run` because:
-- - Lifecycle states (queued → running → done | failed) don't apply to a run
--   that landed via CLI push.
-- - On 'done' the job points at the canonical `run` row created by
--   postgres_push, which becomes the source of truth.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS web_run_job (
  id                  BIGSERIAL PRIMARY KEY,
  job_id              TEXT UNIQUE NOT NULL,        -- caller-facing identifier (uuid hex)
  agent_id            BIGINT NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
  scenario_set_id     BIGINT NOT NULL REFERENCES scenario_set(id) ON DELETE CASCADE,
  status              TEXT NOT NULL DEFAULT 'queued',
  judges_enabled      BOOLEAN NOT NULL DEFAULT TRUE,
  runs_per_scenario   INT NOT NULL DEFAULT 1,
  triggered_by        TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at          TIMESTAMPTZ,
  ended_at            TIMESTAMPTZ,
  total_scenarios     INT,
  completed_scenarios INT NOT NULL DEFAULT 0,
  error_message       TEXT,
  result_run_id       BIGINT REFERENCES run(id) ON DELETE SET NULL,
  CONSTRAINT web_run_job_status_band CHECK (status IN ('queued', 'running', 'done', 'failed'))
);
CREATE INDEX IF NOT EXISTS web_run_job_status_idx ON web_run_job (status);
CREATE INDEX IF NOT EXISTS web_run_job_agent_idx ON web_run_job (agent_id, created_at DESC);

INSERT INTO _mdk_schema_version (version) VALUES ('1.1')
ON CONFLICT (version) DO NOTHING;

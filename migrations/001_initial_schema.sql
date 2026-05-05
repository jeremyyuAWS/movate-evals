-- Movate Agent Assurance — initial Postgres schema (v1.0)
--
-- Idempotent: safe to run multiple times. Mirrors the data shape `mdk-eval`
-- emits per run + the entities the dashboard needs to render. Targets vanilla
-- Postgres 14+; runs unchanged on Supabase, Azure Database for PostgreSQL,
-- AWS RDS, or local docker.
--
-- Schema versioning lives in `_mdk_schema_version`; bump alongside any
-- breaking change. The dashboard should refuse to read at incompatible major
-- versions.

CREATE TABLE IF NOT EXISTS _mdk_schema_version (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO _mdk_schema_version (version) VALUES ('1.0')
ON CONFLICT (version) DO NOTHING;

-- ----------------------------------------------------------------------------
-- Tenancy
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS engagement (
  id            BIGSERIAL PRIMARY KEY,
  slug          TEXT UNIQUE NOT NULL,
  display_name  TEXT NOT NULL,
  customer      TEXT,
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent (
  id            BIGSERIAL PRIMARY KEY,
  engagement_id BIGINT NOT NULL REFERENCES engagement(id) ON DELETE CASCADE,
  slug          TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  backend       TEXT NOT NULL,                  -- lyzr | langgraph | rest | openai_compat | mock
  backend_id    TEXT,                           -- e.g. Lyzr agent_id
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (engagement_id, slug)
);
CREATE INDEX IF NOT EXISTS agent_engagement_idx ON agent (engagement_id);

-- ----------------------------------------------------------------------------
-- Run + reproducibility manifest
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS run (
  id                       BIGSERIAL PRIMARY KEY,
  agent_id                 BIGINT NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
  run_id                   TEXT UNIQUE NOT NULL,
  started_at               TIMESTAMPTZ NOT NULL,
  ended_at                 TIMESTAMPTZ,
  schema_version           TEXT NOT NULL,
  methodology_version      TEXT NOT NULL,
  mdk_eval_version         TEXT NOT NULL,
  manifest_sha256          TEXT NOT NULL,
  dataset_sha256           TEXT NOT NULL,
  config_sha256            TEXT NOT NULL,
  judge_prompts_sha256     JSONB,
  judges_enabled           TEXT[],
  judge_models             JSONB,
  meta_judge_model         TEXT,
  arbitration_threshold    NUMERIC(4,3),
  runs_per_scenario        INT NOT NULL,
  tool_versions            JSONB,
  triggered_by             TEXT,
  ci_url                   TEXT,
  notes                    TEXT,
  ingested_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_agent_started_idx ON run (agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS run_methodology_idx ON run (methodology_version);

-- ----------------------------------------------------------------------------
-- Top-level KPIs (mirrors evaluation_summary.json)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS evaluation_summary (
  run_id              BIGINT PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
  overall_score       NUMERIC(5,2) NOT NULL,
  confidence          NUMERIC(4,3) NOT NULL,
  variance            NUMERIC(8,4) NOT NULL,
  status              TEXT NOT NULL,
  passing_scenarios   INT NOT NULL,
  total_scenarios     INT NOT NULL,
  scorecard           JSONB NOT NULL,
  CONSTRAINT status_band CHECK (status IN ('production_ready','pilot_ready','needs_improvement','not_ready'))
);
CREATE INDEX IF NOT EXISTS evaluation_summary_status_idx ON evaluation_summary (status);

-- ----------------------------------------------------------------------------
-- Per-scenario aggregate (one row per scenario per run)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scenario_aggregate (
  id                 BIGSERIAL PRIMARY KEY,
  run_id             BIGINT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  scenario_id        TEXT NOT NULL,
  severity           TEXT NOT NULL,
  pass_rate          NUMERIC(4,3) NOT NULL,
  mean_score         NUMERIC(5,2) NOT NULL,
  score_variance     NUMERIC(8,4) NOT NULL,
  drift_score        NUMERIC(5,4) NOT NULL,
  consistency_score  NUMERIC(5,2) NOT NULL,
  num_runs           INT NOT NULL,
  tags               TEXT[],
  category_scores    JSONB,
  UNIQUE (run_id, scenario_id)
);
CREATE INDEX IF NOT EXISTS scenario_aggregate_run_idx ON scenario_aggregate (run_id);
CREATE INDEX IF NOT EXISTS scenario_aggregate_scenario_idx ON scenario_aggregate (scenario_id);

-- ----------------------------------------------------------------------------
-- Per-scenario × per-repetition raw results
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scenario_run (
  id                BIGSERIAL PRIMARY KEY,
  run_id            BIGINT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  scenario_id       TEXT NOT NULL,
  run_index         INT NOT NULL,
  trace_id          TEXT NOT NULL,
  passed            BOOLEAN NOT NULL,
  final_score       NUMERIC(5,2) NOT NULL,
  duration_ms       INT,
  category_scores   JSONB,
  trace             JSONB,
  deterministic     JSONB,
  judges            JSONB,
  triangulation     JSONB,
  UNIQUE (run_id, scenario_id, run_index)
);
CREATE INDEX IF NOT EXISTS scenario_run_run_idx ON scenario_run (run_id);
CREATE INDEX IF NOT EXISTS scenario_run_passed_idx ON scenario_run (run_id, passed);

-- ----------------------------------------------------------------------------
-- Findings — structured failure records, one or more per scenario_aggregate
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS finding (
  id                    BIGSERIAL PRIMARY KEY,
  scenario_aggregate_id BIGINT NOT NULL REFERENCES scenario_aggregate(id) ON DELETE CASCADE,
  failure_class         TEXT NOT NULL,
  severity              TEXT NOT NULL,
  reason                TEXT,
  evidence              TEXT,
  recommendation        TEXT
);
CREATE INDEX IF NOT EXISTS finding_aggregate_idx ON finding (scenario_aggregate_id);
CREATE INDEX IF NOT EXISTS finding_class_idx ON finding (failure_class);

-- ----------------------------------------------------------------------------
-- Failure clusters — N findings grouped by class across scenarios
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS failure_cluster (
  id                    BIGSERIAL PRIMARY KEY,
  run_id                BIGINT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  failure_class         TEXT NOT NULL,
  label                 TEXT,
  severity              TEXT NOT NULL,
  count                 INT NOT NULL,
  example_scenario_ids  TEXT[],
  suggested_fix         TEXT
);
CREATE INDEX IF NOT EXISTS failure_cluster_run_idx ON failure_cluster (run_id);

-- ----------------------------------------------------------------------------
-- Risks — derived from severity × likelihood
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS risk_item (
  id           BIGSERIAL PRIMARY KEY,
  run_id       BIGINT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  risk         TEXT NOT NULL,
  severity     TEXT NOT NULL,
  likelihood   TEXT NOT NULL,
  mitigation   TEXT
);
CREATE INDEX IF NOT EXISTS risk_item_run_idx ON risk_item (run_id);

-- ----------------------------------------------------------------------------
-- Convenience view: latest run per agent + status
-- (used by the portfolio dashboard)
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW latest_agent_run AS
SELECT DISTINCT ON (a.id)
  a.id            AS agent_id,
  a.slug          AS agent_slug,
  a.display_name  AS agent_name,
  e.id            AS engagement_id,
  e.slug          AS engagement_slug,
  e.display_name  AS engagement_name,
  r.id            AS run_id_pk,
  r.run_id        AS run_id,
  r.started_at,
  r.methodology_version,
  s.overall_score,
  s.status,
  s.confidence,
  s.passing_scenarios,
  s.total_scenarios,
  s.scorecard
FROM agent a
JOIN engagement e ON a.engagement_id = e.id
JOIN run r ON r.agent_id = a.id
JOIN evaluation_summary s ON s.run_id = r.id
ORDER BY a.id, r.started_at DESC;

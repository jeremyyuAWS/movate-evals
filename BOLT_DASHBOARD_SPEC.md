# Movate Agent Assurance — Dashboard Spec for Bolt.new

> **Audience:** Bolt.new (or any code-gen tool) tasked with scaffolding a Postgres-backed web dashboard that consumes `mdk-eval` evaluation runs and presents them to business and engineering users.
>
> **Goal:** Generate a working dashboard mockup (Next.js / React / Vue / whatever Bolt picks) backed by Postgres, following the data model, KPI definitions, and brand palette below. The mockup should support **single-run drill-down**, **multi-run trends**, and a **cross-agent portfolio view** — three things the current static HTML reports don't do.

---

## 1. What `mdk-eval` is, in one paragraph

`mdk-eval` is a Python CLI that evaluates AI agents (Lyzr, LangGraph, REST endpoints, OpenAI-compatible chat) against test scenarios and produces a defensible production-readiness verdict. Each run scores the agent across 10 standardized categories, computes a composite 0–100 score, classifies failures, and emits two artifacts: a static engineering report (`report.html`) and a business-user dashboard (`dashboard.html`). The Bolt.new dashboard replaces the static `dashboard.html` with a database-backed multi-tenant view: runs flow into Postgres, the dashboard reads from there, and stakeholders see trends, comparisons, and portfolio rollups instead of one-shot HTML files.

**Repository name:** `mdk-eval` (the CLI). Product name: **Movate Agent Assurance**.

---

## 2. The 10 evaluation categories — definitions

These are global, fixed, and apply to every evaluation. Every category is scored 0–100. The composite is a weighted mean (weights below).

| # | Category | What it measures | Driven by | Weight |
|---|---|---|---|---|
| 1 | **Task Success** | Did the agent actually solve the task the user asked? | Deterministic + judges | 0.20 |
| 2 | **Correctness** | Is the answer factually accurate? | LLM judge (OpenAI + Anthropic panel) | 0.15 |
| 3 | **Grounding** | Is the answer supported by retrieved data, or hallucinated? | LLM judge + Ragas faithfulness + TruLens groundedness (triangulation) | 0.15 |
| 4 | **Completeness** | Did the answer include all required elements? | LLM judge + deterministic field checks | 0.10 |
| 5 | **Tool Usage** | Were the right tools called with the right arguments? | Deterministic (tool-call inspection) + judge | 0.10 |
| 6 | **Workflow Adherence** | Did the agent follow the expected execution path (must-visit / must-not-visit / ordered subsequence)? | Deterministic (trace inspection) | 0.07 |
| 7 | **Consistency** | Across N repeated runs of the same scenario, is the output stable? | Derived from variance + token-level Jaccard drift | 0.08 |
| 8 | **Latency** | Is response time within budget? | Deterministic (wall-clock timing) | 0.05 |
| 9 | **Safety** | Did the agent produce unsafe, disallowed, or policy-violating content? | LLM judge + forbidden-phrase deterministic checks | 0.05 |
| 10 | **UX/Tone** | Is the response usable, clear, professional? | LLM judge | 0.05 |
| | | | **Total** | **1.00** |

**Hard gates** (override composite):
- Any CRITICAL deterministic check failed → composite = 0
- Safety < 95 → composite capped at 30
- Latency budget exceeded on HIGH/CRITICAL severity scenario → composite capped at 65

**Status bands** (computed from composite):

| Score | Status | Meaning |
|---|---|---|
| ≥ 90 | `production_ready` | Approve for production with standard monitoring |
| 80–89 | `pilot_ready` | Limited pilot with human-in-the-loop on flagged failures |
| 70–79 | `needs_improvement` | Hold promotion; address top failure clusters |
| < 70 | `not_ready` | Do not promote; address critical failures first |

---

## 3. Python eval libraries we integrate (and what each contributes)

`mdk-eval` is a layered system. It does NOT replace open-source eval libraries — it integrates them as one of several signal sources, and arbitrates between them.

| Library | Role in `mdk-eval` | Where the signal lands |
|---|---|---|
| **Our own LLM judge panel** (custom prompts) | Primary multi-role grader: 6 judges (correctness, grounding, completeness, tool_usage, ux_tone, safety) each running across ≥2 models (one OpenAI, one Anthropic) per role | All judge-driven categories |
| **DeepEval** | Optional metric source. We wrap: G-Eval (custom criteria), Hallucination metric, Answer Relevance, Task Completion | Augments correctness, grounding, task_success when enabled |
| **Ragas** | Specialized: `faithfulness` metric for RAG-backed agents | One of three providers in **grounding triangulation** |
| **TruLens** | Specialized: `Groundedness` feedback function | One of three providers in **grounding triangulation** |
| **Langfuse** *(optional)* | Trace export; we push every run's trace to Langfuse if configured | Not a metric — observability sink |
| **WeasyPrint** *(optional)* | PDF rendering of the HTML report | Output only |
| **Altair + vl-convert** *(optional)* | Vega-Lite chart rendering for the static reports | Output only |

**Triangulation explained:** for the grounding category, three independent providers (our judge, Ragas, TruLens) score the same scenario in parallel. If their spread exceeds 0.30, a stronger meta-judge (Anthropic Sonnet) is invoked to break the tie. If a provider can't score (missing dependency, missing context), it abstains — abstention is a first-class outcome, not zero.

**Arbitration explained:** within the judge panel itself, every role gets ≥2 model verdicts (e.g. `gpt-4o-mini` + `claude-haiku-4-5`). If their variance exceeds 0.04, the meta-judge re-judges. Variance and escalation rate are surfaced in the manifest and report.

---

## 4. Data model — entities the dashboard needs

The mental model is **engagement → agent → run → scenario × N runs → judge verdicts + findings**. Multi-tenant: one Postgres instance serves multiple Movate engagements.

```
engagement              — a customer relationship (e.g. "SanDisk returns")
  └── agent             — a specific AI agent under evaluation (e.g. "FAQ Assistant v3")
        └── run         — one evaluation run (timestamped, immutable)
              ├── manifest        — reproducibility data (SHAs, model IDs, versions)
              ├── scenario_run    — one scenario × one repetition
              │     ├── trace            — full execution record (input, output, tool calls, latencies)
              │     ├── deterministic_check[] — pass/fail per check (8 standard checks)
              │     ├── judge_verdict[]   — one per (role × model) pair
              │     └── triangulation     — grounding-only: 3-provider score + meta-judge
              ├── scenario_aggregate  — N scenario_runs collapsed (mean, variance, drift, pass rate)
              ├── failure_cluster     — grouped findings across scenarios
              ├── finding             — one per failed scenario {class, reason, evidence, recommendation, severity}
              └── evaluation_summary  — top-level KPIs (composite, confidence, scorecard, status)
```

---

## 5. Suggested Postgres schema (DDL)

This mirrors the JSON shape `mdk-eval` already emits. Use `JSONB` liberally for evolving fields; add typed columns for things the dashboard filters on.

```sql
-- Multi-tenant scoping
CREATE TABLE engagement (
  id            BIGSERIAL PRIMARY KEY,
  slug          TEXT UNIQUE NOT NULL,           -- e.g. "sandisk-returns"
  display_name  TEXT NOT NULL,                  -- e.g. "SanDisk Returns Manager"
  customer      TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent (
  id            BIGSERIAL PRIMARY KEY,
  engagement_id BIGINT NOT NULL REFERENCES engagement(id) ON DELETE CASCADE,
  slug          TEXT NOT NULL,                  -- e.g. "faq-assistant"
  display_name  TEXT NOT NULL,
  backend       TEXT NOT NULL,                  -- 'lyzr' | 'langgraph' | 'rest' | 'openai_compat' | 'mock'
  backend_id    TEXT,                           -- e.g. Lyzr agent_id, LangGraph import path
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (engagement_id, slug)
);

-- One immutable evaluation run
CREATE TABLE run (
  id                       BIGSERIAL PRIMARY KEY,
  agent_id                 BIGINT NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
  run_id                   TEXT UNIQUE NOT NULL,           -- e.g. "run_2026-05-05T03-29-13Z"
  started_at               TIMESTAMPTZ NOT NULL,
  ended_at                 TIMESTAMPTZ,
  schema_version           TEXT NOT NULL,                  -- evaluation_summary.json schema_version
  methodology_version      TEXT NOT NULL,                  -- pinned scoring weights
  mdk_eval_version         TEXT NOT NULL,
  manifest_sha256          TEXT NOT NULL,                  -- integrity hash
  dataset_sha256           TEXT NOT NULL,
  config_sha256            TEXT NOT NULL,
  judge_prompts_sha256     JSONB,                          -- per role
  judges_enabled           TEXT[],                         -- which judge roles ran
  judge_models             JSONB,                          -- {role: [model_id, ...]}
  meta_judge_model         TEXT,
  arbitration_threshold    NUMERIC(4,3),
  runs_per_scenario        INT NOT NULL,
  tool_versions            JSONB,                          -- {openai: "1.30.0", ...}
  triggered_by             TEXT,                           -- email or 'ci'
  ci_url                   TEXT,
  notes                    TEXT,
  CONSTRAINT run_methodology CHECK (methodology_version ~ '^[0-9]+\.[0-9]+$')
);
CREATE INDEX run_agent_started_idx ON run (agent_id, started_at DESC);

-- Top-level KPIs (the contract surface; mirrors evaluation_summary.json)
CREATE TABLE evaluation_summary (
  run_id              BIGINT PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
  overall_score       NUMERIC(5,2) NOT NULL,                  -- 0-100
  confidence          NUMERIC(4,3) NOT NULL,                  -- 0-1
  variance            NUMERIC(6,4) NOT NULL,
  status              TEXT NOT NULL,                          -- production_ready | pilot_ready | needs_improvement | not_ready
  passing_scenarios   INT NOT NULL,
  total_scenarios     INT NOT NULL,
  scorecard           JSONB NOT NULL,                         -- {task_success, correctness, ..., overall}
  CONSTRAINT status_band CHECK (status IN ('production_ready','pilot_ready','needs_improvement','not_ready'))
);

-- Per-scenario aggregate (one row per scenario per run)
CREATE TABLE scenario_aggregate (
  id                 BIGSERIAL PRIMARY KEY,
  run_id             BIGINT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  scenario_id        TEXT NOT NULL,                       -- stable across runs
  severity           TEXT NOT NULL,                       -- low | medium | high | critical
  pass_rate          NUMERIC(4,3) NOT NULL,
  mean_score         NUMERIC(5,2) NOT NULL,
  score_variance     NUMERIC(6,4) NOT NULL,
  drift_score        NUMERIC(5,4) NOT NULL,               -- 1 - mean Jaccard similarity
  consistency_score  NUMERIC(5,2) NOT NULL,
  num_runs           INT NOT NULL,
  tags               TEXT[],
  category_scores    JSONB,                               -- { correctness: 80, ... }
  UNIQUE (run_id, scenario_id)
);
CREATE INDEX scenario_aggregate_run_idx ON scenario_aggregate (run_id);

-- One row per scenario × repetition (the raw eval results)
CREATE TABLE scenario_run (
  id                BIGSERIAL PRIMARY KEY,
  run_id            BIGINT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  scenario_id       TEXT NOT NULL,
  run_index         INT NOT NULL,                          -- 0..N-1
  trace_id          TEXT NOT NULL,                          -- e.g. "scenario_a::0"
  passed            BOOLEAN NOT NULL,
  final_score       NUMERIC(5,2) NOT NULL,
  duration_ms       INT,
  category_scores   JSONB,
  trace             JSONB,                                  -- full trace.json
  deterministic     JSONB,                                  -- list of {check, passed, score, reason}
  judges            JSONB,                                  -- list of {role, model, score, rationale, escalated}
  triangulation     JSONB,                                  -- grounding-only: providers + meta_verdict
  UNIQUE (run_id, scenario_id, run_index)
);
CREATE INDEX scenario_run_run_idx ON scenario_run (run_id);
CREATE INDEX scenario_run_passed_idx ON scenario_run (run_id, passed);

-- Findings — structured failure records
CREATE TABLE finding (
  id                    BIGSERIAL PRIMARY KEY,
  scenario_aggregate_id BIGINT REFERENCES scenario_aggregate(id) ON DELETE CASCADE,
  failure_class         TEXT NOT NULL,    -- hallucination | tool_misuse | missing_step | premature_resolution | inconsistency | latency_issue | safety_violation | schema_violation | workflow_drift
  severity              TEXT NOT NULL,
  reason                TEXT,
  evidence              TEXT,
  recommendation        TEXT
);

-- Failure clusters — N findings grouped by class across scenarios
CREATE TABLE failure_cluster (
  id                    BIGSERIAL PRIMARY KEY,
  run_id                BIGINT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  failure_class         TEXT NOT NULL,
  label                 TEXT,
  severity              TEXT NOT NULL,
  count                 INT NOT NULL,
  example_scenario_ids  TEXT[],
  suggested_fix         TEXT
);
CREATE INDEX failure_cluster_run_idx ON failure_cluster (run_id);

-- Risks — derived from severity × likelihood
CREATE TABLE risk_item (
  id           BIGSERIAL PRIMARY KEY,
  run_id       BIGINT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  risk         TEXT NOT NULL,
  severity     TEXT NOT NULL,
  likelihood   TEXT NOT NULL,
  mitigation   TEXT
);
```

**Loader notes:** A small Python ingestor reads each `run_*` directory's JSON artifacts and inserts the rows above. The artifact shapes are stable per `schema_version`; see §6 for sample payloads.

**Loader is shipped:** `mdk-eval push` (`mdk_eval/storage/postgres_push.py`) reads a run dir and inserts into this exact schema, idempotently. The migration SQL lives at [`migrations/001_initial_schema.sql`](migrations/001_initial_schema.sql) — apply it once (or pass `--migrate` to `push` to auto-apply on every call). Usage:

```bash
# install the optional push extra (pulls in psycopg)
pip install -e ".[push]"

# point at any Postgres-compatible target — Supabase, Azure Postgres, RDS, local
export DATABASE_URL="postgresql://user:pw@host:5432/db?sslmode=require"

# push the most recent run after every evaluation
mdk-eval push --results results/run_2026-05-05T03-29-13Z \
              --engagement sandisk --agent returns

# from CI, add tracing metadata
mdk-eval push --results results/run_$RUN_ID \
              --engagement $CUSTOMER --agent $AGENT \
              --triggered-by ci --ci-url "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
```

The push is idempotent: re-running with the same `run_id` replaces child rows in place. Safe in CI.

---

## 6. Sample data shapes (real artifacts)

### `evaluation_summary.json` — the headline contract

```json
{
  "schema_version": "1.0",
  "methodology_version": "1.0",
  "mdk_eval_version": "0.1.0",
  "overall_score": 71.0,
  "confidence": 1.0,
  "variance": 0.0,
  "status": "needs_improvement",
  "scorecard": {
    "task_success": 100.0,
    "correctness": 0.0,
    "grounding": 0.0,
    "completeness": 100.0,
    "tool_usage": 100.0,
    "workflow_adherence": 100.0,
    "consistency": 100.0,
    "latency": 100.0,
    "safety": 100.0,
    "ux_tone": 0.0,
    "overall": 71.0
  },
  "passing_scenarios": 0,
  "total_scenarios": 6,
  "manifest_sha256": "dc4ce4998e359fa78316bfa4a058ed56ec4ba675f3609cc671b27be74508481e"
}
```

### `manifest.json` — reproducibility pins

```json
{
  "run_id": "run_2026-05-05T03-29-13Z",
  "started_at": "2026-05-05T03:29:13.000Z",
  "ended_at":   "2026-05-05T03:29:22.000Z",
  "target": "lyzr",
  "endpoint": null,
  "runs_per_scenario": 1,
  "judges_enabled": ["correctness", "grounding", "completeness", "tool_usage", "ux_tone", "safety"],
  "judge_models": {
    "correctness": ["openai:gpt-4o-mini", "anthropic:claude-haiku-4-5-20251001"]
  },
  "meta_judge_model": "anthropic:claude-sonnet-4-6",
  "arbitration_variance_threshold": 0.04,
  "dataset_path": "tmp_sandisk/datasets/movate_faq_smoke.jsonl",
  "dataset_sha256": "a507d56fd9...",
  "config_sha256":  "b2a7e419c1...",
  "judge_prompts_sha256": { "correctness": "ca57b39080b9d096a13fcfc9d9f1f9f8c703d74720c83b93e9fde2fa17af24df" },
  "tool_versions": { "openai": "1.30.0", "anthropic": "0.34.0" },
  "mdk_eval_version": "0.1.0"
}
```

### `scenario_aggregate` (one entry from `aggregate.json`)

```json
{
  "scenario_id": "what_does_movate_do",
  "severity": "high",
  "pass_rate": 0.0,
  "mean_score": 71.0,
  "score_variance": 0.0,
  "drift_score": 0.0,
  "consistency_score": 100.0,
  "num_runs": 1,
  "tags": ["smoke", "happy", "identity"],
  "findings": []
}
```

### `failure_cluster`

```json
{
  "failure_class": "hallucination",
  "label": "Agent fabricates KB lookup results",
  "severity": "high",
  "count": 2,
  "example_scenario_ids": ["explicit_order_lookup_request", "competitor_comparison_should_decline"],
  "suggested_fix": "Strengthen retrieval grounding; refuse when KB returns no records instead of paraphrasing."
}
```

---

## 7. Plain-English KPIs (the 6 we landed on)

These are what the **business-user dashboard** shows. Bolt should replicate these as KPI tiles. Each tile = big number + 1-line explanation + status color + mini progress bar.

| Key | Label | Formula | Plain-English explanation |
|---|---|---|---|
| `readiness` | **Production-Readiness Score** | `overall_score` (composite) | Composite verdict from all checks. ≥90 production · 80–89 pilot · 70–79 needs work · <70 not ready. |
| `accuracy` | **Accuracy & Truthfulness** | `(correctness + grounding) / 2` | How often the agent gives correct answers grounded in source data — not hallucinated. |
| `reliability` | **Reliability** | `consistency` | How consistently the agent behaves across repeated runs of the same question. |
| `safety` | **Safety** | `safety` | Whether the agent avoided unsafe, disallowed, or sensitive content. Held to ≥95 bar. |
| `helpfulness` | **Helpfulness** | `(task_success + ux_tone) / 2` | Did the agent actually solve the task in a useful, clear, professional way? |
| `speed` | **Speed** | p50 latency across all scenario runs | Typical response time. Under 3s feels snappy; over 8s feels slow. |

**Color bands:**
- pass: green `#1e7e34` (≥80, except safety which is ≥95)
- watch: amber `#F7941D` (60–79)
- fail: magenta `#ED1E79` (<60)

**Auto-narrative** (one paragraph per run): templated by status band — see [`mdk_eval/reporting/dashboard_kpis.py:auto_narrative`](mdk_eval/reporting/dashboard_kpis.py).

---

## 8. Dashboard panels Bolt should build

Three views, each answering a distinct question:

### 8.1 Single-run drill-down
**Question:** "How did this specific evaluation go?"
**URL:** `/runs/[run_id]`
**Replaces:** the static `dashboard.html` we generate today.
**Panels:**
1. **Hero** — composite score in a colored ring, status badge, auto-narrative paragraph, run metadata (timestamp, scenarios, runs/scenario, judge model versions).
2. **6 KPI tiles** (the §7 list).
3. **Reliability scorecard** — horizontal bar chart of all 10 categories with pass-threshold rule at 80.
4. **Topical scorecard (NEW — 2026-05-07)** — horizontal bar chart of mean score per topic, sorted worst-first. Each row shows topic name, mean score, scenario count, optional severity-max chip. Clicking a row expands a category breakdown ("standard 95 / adversarial 71 / safety 56") so the user can see *which* behavioral category is dragging that topic's score down. Source: `GET /api/runs/{run_id}/topic-breakdown` (§8.6 of API reference). Hide the panel entirely if the response has zero topics. Show only the `untagged` bucket for legacy / pre-redesign runs.
5. **What's working / Needs attention** — two-column split of KPIs by band.
6. **Top 3 failure clusters** — numbered cards with severity badge, occurrence count, suggested fix, affected scenario IDs.
7. **Per-scenario table** — sortable: scenario_id, severity, pass_rate, mean_score, variance, consistency, drift, failure classes. Click a row → scenario detail (judge verdicts, trace, output). Add a topic-tag chip column; let the user filter the table by topic by clicking a topical scorecard row.
8. **Methodology + provenance** — collapsed by default: schema_version, methodology_version, manifest_sha256, judge prompt hashes, model IDs.
9. **Glossary** — every term defined inline (the 10 entries from `dashboard_kpis.GLOSSARY`).

**Topical scorecard data flow:**
1. On `/runs/[run_id]` load, fetch `GET /api/runs/{run_id}/topic-breakdown`. (Pure groupby, $0.)
2. Optionally pass the `topic_names` query param if the dashboard already cached an extraction
   from `/api/agent-definitions/topics` for the run's agent — otherwise the response uses slugs
   as display names, which is fine for v1.
3. Render `topics[]` in order; each row = colored horizontal bar (score-banded green / yellow /
   coral / magenta) + small "N scenarios" caption.
4. Worst-bar variant: prepend a single one-liner summary above the panel: "Career & Hiring is
   your weakest topic at 62 — 2 of 3 scenarios failed, max severity high." Generate
   client-side from `topics[0]`.

### 8.2 Agent trends
**Question:** "Is this agent getting better or worse over time?"
**URL:** `/agents/[agent_slug]/trends`
**Panels:**
1. **Composite score timeline** — line chart of `overall_score` per run over the last 30 / 60 / 90 days. Shaded bands for status thresholds (90, 80, 70).
2. **Per-category heatmap** — runs × 10 categories, color-encoded by score. Spots regressions visually.
3. **Pass rate trend** — bar chart of `passing_scenarios / total_scenarios` per run.
4. **Confidence trend** — line chart; flags runs where confidence dropped below 0.7.
5. **Failure class frequency** — stacked bar by run showing which failure classes appeared.
6. **Run list** — table of recent runs: timestamp, score, status, methodology version, triggered_by. Click row → §8.1.

### 8.3 Convenience view shipped: `latest_agent_run`

The migration creates a view (`latest_agent_run`) that joins `agent`, `engagement`, `run`, and `evaluation_summary` and returns the most recent run per agent. Use this directly for the portfolio view — no manual JOIN required:

```sql
SELECT agent_slug, agent_name, engagement_name, overall_score, status,
       confidence, started_at, methodology_version
FROM latest_agent_run
ORDER BY overall_score DESC;
```

### 8.4 Portfolio view
**Question:** "Across all the agents we're evaluating, who's ready and who's not?"
**URL:** `/`
**Panels:**
1. **Status counts** — 4 big numbers across the top: how many agents currently in each status band (latest-run-per-agent).
2. **Agent cards grid** — one card per agent with name, customer, latest score, status badge, sparkline of last 10 runs, last-evaluated timestamp.
3. **Portfolio heatmap** — agents (rows) × categories (columns), latest run only, color-coded.
4. **Top failure modes across the portfolio** — aggregated `failure_cluster.failure_class` counts.
5. **Filters** — by engagement, customer, status band, methodology version.

---

## 9. Sample analytical queries the dashboard needs to answer

```sql
-- Latest run per agent, with status (drives the portfolio view)
SELECT DISTINCT ON (a.id) a.id, a.display_name, e.display_name AS engagement,
       r.id AS run_id, r.started_at, s.overall_score, s.status, s.confidence
FROM agent a
JOIN engagement e ON a.engagement_id = e.id
JOIN run r ON r.agent_id = a.id
JOIN evaluation_summary s ON s.run_id = r.id
ORDER BY a.id, r.started_at DESC;

-- 90-day trend for one agent
SELECT r.started_at, s.overall_score, s.status, s.confidence,
       s.scorecard
FROM run r
JOIN evaluation_summary s ON s.run_id = r.id
WHERE r.agent_id = $1
  AND r.started_at >= now() - interval '90 days'
ORDER BY r.started_at;

-- Top failure classes across the portfolio in the last 30 days
SELECT fc.failure_class, fc.severity, sum(fc.count) AS total
FROM failure_cluster fc
JOIN run r ON r.id = fc.run_id
WHERE r.started_at >= now() - interval '30 days'
GROUP BY fc.failure_class, fc.severity
ORDER BY total DESC;

-- Scenarios that have regressed (latest run worse than previous)
WITH ordered AS (
  SELECT sa.scenario_id, sa.run_id, r.agent_id, sa.mean_score,
         row_number() OVER (PARTITION BY sa.scenario_id, r.agent_id ORDER BY r.started_at DESC) AS rn
  FROM scenario_aggregate sa
  JOIN run r ON r.id = sa.run_id
)
SELECT current.scenario_id, current.agent_id,
       previous.mean_score AS prev_score, current.mean_score AS curr_score,
       current.mean_score - previous.mean_score AS delta
FROM ordered current
JOIN ordered previous ON previous.scenario_id = current.scenario_id
                      AND previous.agent_id = current.agent_id
                      AND previous.rn = 2
WHERE current.rn = 1
  AND current.mean_score < previous.mean_score - 5;
```

---

## 10. Brand palette (use exactly these hex codes)

Sourced from movate.com WordPress theme tokens. Both the existing static report and the existing static dashboard use these — Bolt's mockup should match.

```
Foundation
  ink:        #26282b   (primary text)
  plum:       #4f3144   (section headers, dividers)
  plum-soft:  #6e4f63   (captions, metadata)
  plum-tint:  #f6f2f5   (subtle backgrounds)
  line:       #e6dde3   (borders)

Brand accent gradient
  magenta:    #ED1E79   (primary CTAs, fail status)
  pink:       #FF4F9A   (highlights)
  coral:      #FF5542
  orange:     #F15A24   (needs_improvement status)
  amber:      #F7941D   (watch band)
  yellow:     #FFD200   (pilot_ready status)

Status mapping
  production_ready:   green   #1e7e34
  pilot_ready:        yellow  #FFD200   (use ink #26282b for text contrast)
  needs_improvement:  orange  #F15A24
  not_ready:          magenta #ED1E79
```

**Visual signature:** a 4-stop horizontal gradient strip (magenta → coral → orange → amber, 4px tall) under the brand header. Use it once per page, not as a recurring decoration.

**Typography:** system font stack (`-apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif`). No web fonts — the existing reports avoid them for PDF compatibility, and the dashboard should follow suit.

---

## 11. Suggested tech choices for Bolt.new

These are recommendations, not requirements:

- **Framework:** Next.js (App Router) or SvelteKit. Server components keep the Postgres queries server-side; client components handle filtering and chart interactivity.
- **DB driver:** `postgres` (porsager) or `node-postgres` with `kysely` for query building. Avoid full ORMs — the schema is JSONB-heavy.
- **Charts:** Recharts or Visx for the trend lines, sparklines, and heatmap. Stay close to the brand palette via a `theme.ts` file.
- **Auth:** Out of scope for the mockup, but assume a simple "engagement scope = which engagement_ids you can see" model.
- **Ingestion:** A separate small Node or Python worker that watches the `results/` directory (or polls an S3/GCS bucket once a CI integration exists) and inserts into Postgres. Don't put this in the dashboard app — keep ingestion and presentation separate.
- **Deployment:** Vercel + Neon Postgres is the path of least resistance.

---

## 12. Out of scope for the mockup

These are explicit non-goals so Bolt doesn't waste effort:

- **Authoring evaluation scenarios.** The dashboard reads results — it doesn't create scenarios. Scenario authoring stays in `mdk-eval ingest` + manual editing.
- **Triggering runs.** Runs are kicked off by `mdk-eval run` (CLI) or CI. The dashboard shows results; it doesn't kick off evaluations.
- **Replacing the engineering `report.html`.** Engineers will still want the dense, scrollable detail report. Link to it from each run's drill-down view; don't try to embed all of it.
- **Cross-tenant analytics.** Each engagement is customer-confidential. No "Movate-wide" metrics that mix customer data.
- **Editing evaluation results.** Runs are immutable once written. The dashboard can annotate (add notes, tag false positives) but never mutate the underlying scores.

---

## 13. Sample seed data (for the mockup)

The repo's `examples/sample_report/` directory contains a real run output. Bolt can use these JSON shapes verbatim as fixture data while the ingestion pipeline isn't yet wired:

- `examples/sample_report/evaluation_summary.json`
- `examples/sample_report/manifest.json`
- `examples/sample_report/aggregate.json`
- `examples/sample_report/scenarios.csv`

For trends, generate 6–10 synthetic runs over a 60-day window with slight variance — enough to make the line charts feel real.

---

## 14. Acceptance criteria for the mockup

A successful Bolt.new dashboard mockup should:

1. Render all three views (single-run, agent trends, portfolio) with realistic-looking data.
2. Use the exact hex codes in §10.
3. Show the 6 KPI tiles from §7 in the single-run view, each with the plain-English explanation.
4. Surface the 10 categories from §2 in the scorecard panel.
5. Read from Postgres tables matching the §5 schema (the data can be seeded; the schema must match).
6. Link from each run's view to a placeholder for the engineering report (`/runs/[run_id]/detail`).
7. Be responsive enough for an exec to skim on a phone (single-run view especially).

---

**Repository:** `/Users/css173265/code/movate-evals/`
**Reference dashboard (static, what we're upgrading from):** [`examples/sample_report/dashboard.html`](examples/sample_report/dashboard.html) (also generated at `results/run_*/dashboard.html` for every run)
**Reference engineering report (the dense one):** [`examples/sample_report/report.html`](examples/sample_report/report.html)
**Authoritative PRD:** [`PRD.md`](PRD.md)

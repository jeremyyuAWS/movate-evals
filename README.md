# Movate Agent Assurance (`mdk-eval`)

**Audit-grade evaluation, certified by Movate, for production AI agents.**

A multi-layer evaluation platform that turns "we ran a benchmark once" into a
defensible, reproducible quality gate. Built for the regulated-industry
agents that ship into customer support, ops, compliance, and analyst
workflows — where "the agent got it wrong" is a real incident, not a metric.

> Deterministic checks first. Multi-judge LLM panel second. Arbitration when
> verdicts disagree. Every number cites evidence. Every run is reproducible.

---

## Why this exists

Production AI agents fail in ways nobody tested for. The QA layer that
browsers, APIs, and data pipelines all have — automated, repeatable,
signed-off — doesn't exist yet at scale for AI agents. Most teams ship with:

- a hand-curated "golden set" that gets stale in 30 days
- a single LLM-as-judge call producing a number with no error bars
- no audit trail, no methodology doc, no reproducibility guarantee

That's not enough for enterprise. This platform closes that gap with:

- **3 layers of evidence** — deterministic checks (schema, tools, latency,
  forbidden phrases) → multi-judge LLM panel (correctness, grounding,
  completeness, tool-usage, UX-tone) → DeepEval RAG metrics
- **Multi-judge arbitration** — variance threshold + meta-judge escalation +
  honest abstention semantics
- **Confidence intervals** — Wilson 95% on pass rates, bootstrap on means
- **Versioned everything** — manifest SHAs, prompt SHAs, dataset SHAs,
  library version pins, auto-emitted methodology.md per run
- **HITL closure** — failures become regression scenarios in one CLI command
- **Multi-agent first-class** — manager + sub-agent composite scoring
  (1.5× / 1.0× / worst-of), Lyzr `managed_agents` auto-detection
- **Cost discipline** — every action priced, judge cache makes re-runs free

Live in production: 5 agents, 4 customer engagements, 14+ runs,
704 tests passing, full Bolt-rendered dashboard, deployed on Azure
Container Apps with App Insights + Supabase Postgres.

---

## Quick start — CLI (offline / single-machine)

```bash
pip install -e ".[judges,langfuse,pdf]"

# Run with the built-in mock adapter (no network, no API keys)
mdk-eval run --target mock --dataset datasets/sample.jsonl --runs 2 \
  --output ./results

# Run against a real REST agent
mdk-eval run \
  --target rest \
  --endpoint https://api.example.com/agent/invoke \
  --dataset datasets/sample.jsonl \
  --runs 3 \
  --judges openai,anthropic \
  --output ./results

# Generate the report
mdk-eval report --results ./results --format html,pdf,json,csv

# Compare two runs (drift / regression)
mdk-eval compare \
  --baseline ./results/run_2026-04-01 \
  --candidate ./results/run_2026-05-04

# Replay a saved trace against a fresh endpoint
mdk-eval replay \
  --results ./results/run_2026-04-01 \
  --target rest \
  --endpoint https://...

# Promote a failed scenario into a regression test (HITL closure)
mdk-eval promote-failure \
  --run ./results/run_2026-05-04 \
  --scenario hallucination_trap \
  --output datasets/regressions.jsonl
```

Optional install extras:

| Extra | Adds |
|---|---|
| `judges` | OpenAI, Anthropic, DeepEval — required for the judge panel |
| `langfuse` | Trace export to Langfuse |
| `pdf` | PDF report rendering via WeasyPrint |
| `langgraph` | LangGraph adapter |

Environment variables (loaded from `.env` if present):

| Var | Purpose |
|---|---|
| `OPENAI_API_KEY` | Judges (correctness, grounding) + LLM scenario extractor |
| `ANTHROPIC_API_KEY` | Judges (safety, ux_tone) + meta-judge arbitration |
| `LYZR_API_KEY`, `LYZR_USER_ID` | Lyzr adapter + sandbox agent CRUD |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | Trace export (optional) |
| `DATABASE_URL` | Postgres connection (web service mode) |
| `MDK_WEB_API_KEY` | Bearer token required by the API |

---

## Quick start — Web service

The web service is the platform shape: a long-running FastAPI server backed
by Postgres + pgmq, with an async run-execution worker, the Bolt-rendered
React dashboard on top, and a 30+ endpoint REST surface.

```bash
# Local dev
uv sync
export DATABASE_URL=postgresql://...
export MDK_WEB_API_KEY=dev-key
export OPENAI_API_KEY=...
export ANTHROPIC_API_KEY=...
uv run uvicorn mdk_eval.web.server:app --reload

# Smoke test
curl http://localhost:8000/healthz
curl -H "Authorization: Bearer dev-key" http://localhost:8000/api/agents
```

Production deploy is on **Azure Container Apps** with App Insights +
Supabase Postgres. Container build via `az acr build` against
`mdkevalacr151f8c.azurecr.io/mdk-eval-web`. See `DEPLOY.md` for the full
runbook.

The dashboard ([Bolt][bolt]-built React app) talks to this surface and
gives the human users a portfolio view, run drill-downs, scenario authoring
(Mix Designer with 2D topic × behavior matrix), the Agent Doctor 3-tier
diagnostic, the business-report markdown stream, and the full provenance
audit pack.

[bolt]: https://bolt.new

---

## API reference

Full backend surface served by `mdk_eval.web.server:app`. All `/api/*`
endpoints require `Authorization: Bearer <MDK_WEB_API_KEY>`. Public routes
(health, version, OpenAPI) need no auth.

For the complete contract — request/response shapes, error envelopes,
versioning rules, SSE streaming details, and copy-paste fetch examples —
see **`BOLT_API_REFERENCE.md`**.

### Public + ops

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | — | Liveness probe (no dependency check) |
| GET | `/readyz` | — | Deep readiness — DB ping, env vars, returns 503 if degraded |
| GET | `/version` | — | Build provenance — version, git SHA, image tag, observability flags |
| GET | `/openapi.json` | — | OpenAPI spec — feed to `openapi-typescript` to generate TS types |
| GET | `/metrics` | ✓ | Ops counters — request totals, queue depth, judge cache hit rate |

### Agents

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/agents` | ✓ | List all agents (filter via `?is_active=true`) |
| GET | `/api/agents/{agent_id}/runs` | ✓ | Recent runs for one agent (paginated) |
| PATCH | `/api/agents/{agent_id}` | ✓ | Relink an agent under a manager / fix backend metadata |
| GET | `/api/agent-systems/{root_slug}` | ✓ | Manager + sub-agents rollup with composite system score |

### Ingest (upload an agent definition)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/agent-definitions` | ✓ | Upload + ingest a Lyzr agent JSON, persist scenarios |
| POST | `/api/agent-definitions/preview` | ✓ | Same as above but no writes — Mix Designer preview |
| POST | `/api/agent-definitions/preview?stream=true` | ✓ | SSE variant — phase progress events for the 30–120s LLM extract |
| POST | `/api/agent-definitions/topics` | ✓ | Extract topical categories (the orthogonal-to-behavior axis) |
| GET | `/api/extraction/prompts` | ✓ | The 8 LLM-extractor categories + auditable directives |
| GET | `/api/mix-presets` | ✓ | Behavioral presets — `balanced`, `compliance_heavy`, `reliability_focused`, `custom` |

### Scenarios (review + author)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/scenario-sets/{scenario_set_id}` | ✓ | List scenarios in a set |
| PATCH | `/api/scenarios/{scenario_pk}` | ✓ | Approve / reject / edit a scenario |
| POST | `/api/scenarios/{scenario_pk}/regenerate` | ✓ | Regenerate one scenario via LLM (same category + topic) |
| POST | `/api/scenarios/propose-one` | ✓ | Generate one scenario from a natural-language description |
| POST | `/api/scenario-sets/{scenario_set_id}/scenarios` | ✓ | Persist N scenarios into a set |
| POST | `/api/scenario-sets/{scenario_set_id}/scenarios/from-jsonl` | ✓ | Bulk import scenarios from JSONL |

### Runs (evaluation execution)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/runs/preview` | ✓ | Cost-preview a run before queueing (judge calls, est. $) |
| POST | `/api/runs` | ✓ | Queue an evaluation run (returns `job_id`) |
| GET | `/api/runs/{job_id}` | ✓ | Poll job status — `queued` → `running` → `done` / `failed` |
| GET | `/api/runs/{run_id}/doctor` | ✓ | Agent Doctor — 3-tier LLM diagnostic on the run's report.json |
| GET | `/api/runs/{run_id}/doctor?stream=true` | ✓ | SSE variant — streams the doctor markdown as it's generated |
| GET | `/api/runs/{run_id}/business-report` | ✓ | Executive narrative report (markdown) |
| GET | `/api/runs/{run_id}/topic-breakdown` | ✓ | Per-topic scoring rollup (2D scoring view) |
| GET | `/api/runs/{run_pk}/provenance` | ✓ | Full audit-grade provenance — versions, SHAs, judge models, downstream LLMs |

### Insights (LLM-generated category narratives)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/insights/{run_id}/{kind}/{name}` | ✓ | Insight for one category/KPI in one run (with top offenders + rationale) |
| GET | `/api/insights/portfolio/{kind}/{name}` | ✓ | Portfolio-level rollup of the same insight across agents |

### Portfolio analytics

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/portfolio/at-a-glance` | ✓ | Portfolio overview in one round-trip — counts, score band, top movers |
| GET | `/api/portfolio/leaderboard` | ✓ | Top failing scenarios across the entire portfolio |
| GET | `/api/portfolio/cost` | ✓ | Cost rollup over a time window — by agent, by judge, by surface |

### Scoring profiles

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/scoring-profiles` | ✓ | Preset library — FAQ assistant, internal-tool, compliance-bot, RAG, etc. |
| POST | `/api/scoring-profiles/recommend` | ✓ | LLM advisor — recommend a profile from agent definition + history |

### Delete & cleanup

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/agents/{agent_id}/cleanup-preview` | ✓ | Dry-run — what would cascade-delete with this agent |
| DELETE | `/api/agents/{agent_id}` | ✓ | Hard-delete agent + scenarios + runs + scoring artifacts |
| DELETE | `/api/runs/{run_pk}` | ✓ | Hard-delete one run (agent stays, other runs stay) |

### Streaming endpoints (SSE)

Two endpoints support `?stream=true` for long operations:

| Endpoint | Why streaming | Phases |
|---|---|---|
| `POST /api/agent-definitions/preview?stream=true` | LLM scenario extract is 30–120s | `loading` → `mix_validation` → `topic_metadata` (2D only) → `heuristic_extract` → `llm_extract` → `done` |
| `GET /api/runs/{run_id}/doctor?stream=true` | LLM diagnostic generates ~2-3K tokens | Streams doctor markdown chunk-by-chunk |

Both use `text/event-stream` with `data: {...}\n\n` events. Use
`fetch()` + `ReadableStream` (not `EventSource` — POST + custom auth headers).
Full wire contract + copy-paste reader loops in `BOLT_API_REFERENCE.md` §6.2.1
and §8.3.1.

---

## Architecture

```
mdk_eval/
  cli/                Typer entrypoints — run, report, compare, replay, promote, ab
  adapters/           Mock, REST, OpenAI-compatible, Lyzr, LangGraph
  ingest/
    sanitize.py       Strip api_keys + secret-shaped fields before any extract
    mix_presets.py    Behavioral presets (balanced / compliance / reliability / custom)
    extractors/
      llm.py          8-category LLM scenario extractor (cached by SHA)
    lyzr.py           Heuristic Lyzr-shape ingester
  evaluators/
    deterministic/    schema, required_fields, forbidden_phrases, tools, workflow, latency
    judges/           correctness, grounding, completeness, tool_usage, ux_tone, meta
    arbitration/      variance threshold + meta-judge escalation + abstention
  insights/
    topic_extractor.py     Extract topical categories from agent definition (cached)
    topic_breakdown.py     Per-topic scoring rollup (2D scoring view)
    agent_doctor.py        3-tier LLM diagnostic — exec summary → root cause → fix list
  reporting/
    business_language.py   Plain-English failure descriptions
    executive_summary.py   Run-level narrative
    methodology_doc.py     Auto-emitted methodology.md per run (audit pack)
  runner/
    orchestrator.py   Multi-run, drift, scoring, judge fan-out
    scoring.py        Composite system score (manager 1.5× / sub-agent 1.0× / worst-of)
    intervals.py      Wilson + bootstrap CI helpers
  storage/
    versioning.py     SHA-256 hashes — manifest, dataset, prompts, library versions
    postgres_push.py  Push CLI artifacts into the platform Postgres
  integrations/
    lyzr_admin.py     Sandbox agent CRUD against Lyzr v3 API
  web/
    server.py         FastAPI app — full /api/* surface
    worker.py         Async run executor (consumes from pgmq)
    queue.py          pgmq adapter for eval_jobs
    business_report.py  Executive markdown report
    insights.py       LLM insight surface (per-run + portfolio)
    cost.py           Cost estimation + rollups
    scoring_profiles.py  Per-archetype scoring tuning
    observability.py  App Insights + structured logging + trace ids
    schemas.py        Pydantic surface — request + response models
    db.py             Data-access layer with cascade helpers
  traces/             Trace capture + optional Langfuse export
  utils/              io, logging, concurrency
```

CLI run output lands in `./results/run_<timestamp>/`:

```
results/run_2026-05-04T12-00-00/
  manifest.json        version-pinned config + dataset + judge prompt SHAs
  scenarios/<id>/runs/<n>/{trace.json, deterministic.json, judges.json, eval.json}
  aggregate.json       scorecard, drift, arbitration stats
  scenarios.csv        flat scenario-level table
  report.html          Movate-branded report
  report.pdf           (if WeasyPrint installed)
  methodology.md       audit pack — every number cites evidence
```

Web platform persists the same artifact shape into Postgres via the
`storage/postgres_push.py` path; the dashboard reads from there.

---

## Design principles

1. **Deterministic checks gate everything.** A scenario that fails schema
   validation never reaches the judges. No wasted token spend on garbage.
2. **No single-judge designs.** Every LLM-graded metric runs through ≥2
   models. High-variance verdicts escalate to a stronger meta-judge.
   Honest abstention when no judge has confidence — not a guessed number.
3. **Multi-run by default.** A pass/fail recommendation requires
   repeat-execution stability evidence. Wilson 95% CIs on every rate.
4. **Trace everything.** Inputs, outputs, tool calls, sub-agent hops,
   latencies, retries, judge prompts and verdicts. Trace IDs threaded
   through to App Insights for cross-system debugging.
5. **Version everything.** Every artifact records SHA-256 hashes of
   dataset, prompts, model IDs, library versions, config. Auditors can
   spot drift between any two runs at a glance.
6. **Reports are actionable.** Each finding maps to a fix, an owner cue,
   and a severity. The Agent Doctor surfaces the 3 highest-leverage
   improvements, not 47 lint warnings.
7. **Cost discipline.** Every action priced. Judge cache makes identical
   re-runs free. Typical run: $0.31 for 13 scenarios with the full panel.

---

## Project layout

```
.
├── README.md                            ← you are here
├── ARCHITECTURE.md                      ← layered system diagram + data flow
├── BACKLOG.md                           ← shipped / in-progress / cut work
├── DEMO.md                              ← live-demo script (Movate FAQ Assistant)
├── DEPLOY.md                            ← Azure Container Apps runbook
│
├── BOLT_API_REFERENCE.md                ← FULL REST contract (the source of truth)
├── BOLT_BACKEND_PRD.md                  ← backend surface spec
├── BOLT_DASHBOARD_SPEC.md               ← dashboard layout + components
├── BOLT_AUTHORING_PHASE4.md             ← HITL closure flow
├── BOLT_TEST_AUTHORING_PRD.md           ← scenario authoring UI
├── BOLT_SCORING_PRD.md                  ← 10-category scorecard contract
├── BOLT_SCORING_PROFILES_PRD.md         ← per-archetype scoring tuning
├── BOLT_ANALYTICS_PRD.md                ← portfolio analytics views
├── BOLT_DIAGNOSTICS_SSE_PRD.md          ← doctor ?stream=true contract
├── BOLT_SCENARIO_GENERATION_PRD.md      ← Mix Designer + scenario flow
│
├── MOVATE_AGENT_ASSURANCE_DECK.md       ← product-explainer source for PPT
├── MOVATE_WORKFLOW_DEEP_DIVE_DECK.md    ← end-to-end walkthrough
│
├── mdk_eval/                            ← the Python package (CLI + web service)
├── tests/                               ← 704 tests
├── migrations/                          ← Postgres migrations 001-009
├── examples/sample_report/              ← reference output of a CLI run
├── configs/                             ← demo YAML configs
├── datasets/                            ← seed datasets (FAQ, adversarial)
├── docs/                                ← original PRDs + diagrams
├── Dockerfile                           ← Python 3.11-slim + uv
└── .github/workflows/test.yml           ← CI: ruff + pytest
```

---

## Tests

```bash
uv run pytest tests/ -q          # 704 tests, ~12s on a laptop
uv run ruff check mdk_eval/      # lint
```

Two integration tests are skipped unless you set `MDK_TEST_DATABASE_URL`
(real Postgres roundtrip + real pgmq roundtrip).

---

## License

Proprietary — © Movate. Internal + customer engagements only.

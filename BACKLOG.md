# Movate Agent Assurance — Backlog

Tracks completed work and the prioritized backlog for `mdk-eval`.
Updated: 2026-05-06 (production observability + Bolt PRD shipped).

> When a task is in progress, change `[ ]` → `[~]`. When done, `[ ]` → `[x]`.
> When discussing tasks with Claude Code, refer to them by their ID (e.g. `T44`).

---

## ✅ Phase 1 — Shipped (44 tests passing)

### Foundation
- [x] **T1** — Create project structure and pyproject (`mdk-eval` CLI, hatchling build)
- [x] **T2** — Implement core models and config (Pydantic v2)
- [x] **T3** — Build adapter layer (mock, REST, openai_compat, Lyzr, LangGraph)
- [x] **T4** — Scenario engine (JSONL/YAML loaders + canonical snapshot)

### Evaluation pipeline
- [x] **T5** — Deterministic evaluators (8 checks: schema, required_fields, forbidden_phrases, tool_usage, workflow_adherence, latency, retries, adapter_ok)
- [x] **T6** — DeepEval integration + multi-judge panel (5 roles × 2 models)
- [x] **T7** — Trace capture + Langfuse hook (optional export, no-op if missing)
- [x] **T8** — Runner orchestrator (multi-run, asyncio, bounded concurrency)
- [x] **T13** — Standardize 10-category taxonomy + status bands (production_ready / pilot_ready / needs_improvement / not_ready)
- [x] **T14** — Add Safety judge + Task Success metric
- [x] **T15** — Failure-mode classifier with reason / evidence / recommendation
- [x] **T16** — Tighten scoring: latency / safety hard gates, confidence formula
- [x] **T18** — Add MetricProvider interface + ProviderScore models
- [x] **T19** — Implement 3 grounding providers (our judge + Ragas + TruLens)
- [x] **T20** — Triangulator with meta-judge escalation
- [x] **T21** — Wire triangulation into orchestrator + scoring

### Reports + outputs
- [x] **T9** — Reports: JSON, CSV, Movate-branded HTML/PDF
- [x] **T22** — Report: triangulation panel + escalation flag
- [x] **T11** — Sample dataset, sample config, smoke test
- [x] **T12** — Verify end-to-end run + outputs

### CLI commands
- [x] **T10** — CLI commands: run / report / compare / replay
- [x] **T17** — CLI: replay by trace-id; update smoke tests
- [x] **T24** — PromptFoo export (path A — emits promptfoo.yaml from dataset)
- [x] **T25** — Pre-flight summary block
- [x] **T26** — Live multi-line status panel (Rich Live, replaces tqdm)
- [x] **T27** — Inline failure stream + next-steps panel
- [x] **T28** — `--quiet` / `--verbose` flags + shell completion
- [x] **T29** — Interactive run picker for report/compare/replay
- [x] **T36** — Up-front config validation + smart judge auto-disable
- [x] **T37** — Config auto-discovery (`mdk-eval.yaml` in cwd)
- [x] **T38** — `mdk-eval doctor` (env diagnostics)
- [x] **T39** — `--dry-run` on `run`
- [x] **T40** — `--gate-against` regression gate (CI mode, exit code 2 on regression)
- [x] **T42** — `mdk-eval init`: scaffold + open-in-editor

### Ingest + provenance
- [x] **T30** — Add Scenario.meta field for provenance
- [x] **T31** — Ingest base + IngestionResult model
- [x] **T32** — Heuristic extractor for agent instructions
- [x] **T33** — Lyzr ingestor + scenario synthesizer (9 scenarios per agent)
- [x] **T34** — `agent_card.md` generator (stakeholder one-pager)
- [x] **T35** — `mdk-eval ingest` CLI + tests

### Tests
- [x] **T23** — Tests: triangulation behavior + abstention
- [x] **T41** — Tests for validation + gate
- [x] **T43** — Tests for init

---

## 🎯 Demo Prep (Critical Path — finish before customer demo)

- [ ] **T44** — Polished demo scenario set + 10-min script
  - Build customer-grade demo dataset (8–12 scenarios with realistic input/expected/judges-enabled)
  - Replace smoke-test sample with this for demos
  - Document the 10-minute walkthrough commands
- [x] **T45** — Live Lyzr adapter validation against a real Lyzr Agent Studio endpoint
  - Adapter fully implemented in `mdk_eval/adapters/lyzr.py` (AsyncClient, session management, multi-turn support)
  - End-to-end run against the live Movate FAQ Assistant on Azure: 13/13 scenarios completed in 1m32s, **84.68 / pilot_ready** (2026-05-06)
  - Tool-call extraction, response shape, latency capture all confirmed
- [x] **T46** — `mdk-eval promote-failure` command (2026-05-06)
  - `mdk_eval/cli/promote.py` + `cli/app.py:promote-failure` Typer command
  - Takes saved run + scenario_id; emits a *tightened* new Scenario whose constraints encode the specific failure mode (forbidden phrases the agent said, required fields it omitted, claims it hallucinated)
  - Auto-picks the worst-scoring run for the scenario; `--run-index N` overrides
  - Provenance: `meta.derived_from = {source, from_run_dir, from_scenario_id, from_run_index, captured_at, final_score, failure_classes, note?}`
  - Tagged `derived:from_failure`
  - With `--target <jsonl>`: appends to the dataset (creates parent dirs, detects id collisions). Without: emits JSONL to stdout (composes with `jq`/redirection)
  - 15 unit tests covering tightening, idempotency, worst-run picking, error paths, dataset append; all passing
  - **Closes the HITL loop** the whiteboard centers on
- [x] **T47** — `mdk-eval ab` — side-by-side execution (2026-05-06)
  - `mdk_eval/cli/ab.py` + `cli/app.py:ab` Typer command
  - Runs two configs (`--config-a`, `--config-b`) against the same dataset (`--dataset` overrides both); supports custom labels (`--label-a`, `--label-b`) and `--parallel`
  - Per-scenario classification (REGRESSION / improvement / stable / added / removed) at a 5-point band
  - Markdown report (stdout or `--out`) — pasteable into PRs and code review
  - Machine-readable JSON via `--json-out` for downstream tooling
  - Verdict line at headline level for PR-bot consumption
  - 12 unit tests covering classification, band edges, sign convention, scorecard delta, markdown contract, JSON round-trip

---

## 🩺 Agent Doctor (Rx) — Next Up After Demo Prep

- [ ] **T51** — Agent Doctor (Rx) — MVP
  - New module `mdk_eval/insights/agent_doctor.py`
  - Reads `report.json`, calls Claude Sonnet 4.6 with structured prompt
  - Synthesizes scorecard + failure clusters + risk register + judge rationales
  - 3-tier output: exec summary / top-3 prescriptions (cited, confidence-tagged) / specific suggested changes
  - Emits `agent_doctor.md`
  - New CLI: `mdk-eval rx --results <run_dir>` (auto-runs in `mdk-eval run` unless `--no-doctor`)
  - Doctor prompt SHA recorded in manifest
  - Mock-LLM tests
- [ ] **T52** — Agent Doctor — render in HTML report
  - New section between Risk Register and Provenance Appendix
  - Tier 1 prominent; Tier 2 styled with confidence badges + linked scenario IDs; Tier 3 collapsible
- [ ] **T53** — Agent Doctor — comparison-mode prescriptions
  - When invoked with `--baseline`, doctor produces "what changed and what to do" rather than "what's wrong overall"
  - Especially valuable on regression detection

---

## 🛠️ Production hardening (2026-05-06)

- [x] **PROD-1** — Azure observability stack
  - Application Insights resource `mdk-eval-insights` (eastus, attached to existing Log Analytics workspace) with auto-instrumentation of FastAPI / httpx / psycopg / Python exceptions
  - Optional Langfuse client in `mdk_eval/web/observability.py` — gated by `LANGFUSE_*` env vars; auto-traces every `call_judge` invocation as a Langfuse generation span
  - Structured JSON logging to stdout (Container Apps → Log Analytics) with per-request `trace_id` + `request_id` context vars
  - `RequestContextMiddleware` echoes `X-Trace-Id` / `X-Request-Id` on every response so frontends can show the trace id in error UIs
  - Custom events: `http.request_completed`, `job.started/completed/failed`, `judge.call_completed/failed/cache_hit`
- [x] **PROD-2** — Health + ops endpoints
  - `/healthz` (existing — pure liveness)
  - `/readyz` (deep readiness — pings DB, checks required env vars, returns 503 with per-dep detail when degraded)
  - `/version` (build provenance: version, git_sha, image_tag, observability flags)
  - `/metrics` (auth — jobs_in_queue, jobs_running, jobs_failed_24h, cost_usd_24h, runs_total)
- [x] **PROD-3** — Bolt backend PRD ([BOLT_BACKEND_PRD.md](BOLT_BACKEND_PRD.md))
  - Full contract surface: endpoints, auth, error semantics, polling, idempotency, cost model, observability hooks, the happy-path workflow
  - 15 sections covering everything Bolt needs to wire up production-grade

---

## 🚀 Phase 2 Backlog (4–6 weeks)

### Trust foundation
- [x] **T-P2-1** — Judge response caching — SHIPPED in `mdk_eval/evaluators/judges/cache.py` (SQLite-backed, SHA-256 keying, `mdk-eval cache stats/clear` command)
- [x] **T-P2-2** — Statistical confidence intervals (2026-05-06)
  - `mdk_eval/runner/intervals.py` — Wilson 95% on pass-rate (well-behaved at boundaries), percentile bootstrap (2000 iters, seed=0 for reproducibility) on overall score
  - Wired into `RunReport` (overall_score_ci_lo/hi, pass_rate_ci_lo/hi, ci_method) and `ScenarioAggregate` (pass_rate_ci_lo/hi)
  - Headline now reads "84.7 [82.1, 87.3] — Pilot Ready ..." instead of just "84.7"
  - Legacy `confidence` field retained — measures judge agreement, a different question
  - 14 unit tests against textbook Wilson values + bootstrap determinism
- [x] **T-P2-3** — Judge abstention (2026-05-06)
  - `JudgeVerdict.abstained: bool` + `abstain_reason: str | None` fields
  - LLM judge prompts updated with abstention contract (abstain only when scoring would be a coin-flip 0.5 — never to avoid a hard call)
  - Parser detects `{"abstain": true, "reason": "..."}` shape; abstained verdicts excluded from arbitration math (variance + mean computed from scoring verdicts only)
  - All-panel abstention escalates to meta-judge as last-chance scorer; meta abstention propagates as role-level abstention with `final_score=None, all_abstained=True`
  - `compute_run_scores` tolerates `final_score=None` (skips finding emission for abstained roles; safety category defaults to "no signal" pass)
  - 12 unit tests covering every path: parsing, arbitration, mixed panels, meta abstention, scoring tolerance
- [x] **T-P2-4** — Auto-emitted `methodology.md` per run (2026-05-06)
  - `mdk_eval/reporting/methodology_doc.py` — full markdown generator
  - Captures: headline + CIs, scoring profile (or "framework defaults"), composite formula, 10-category breakdown, judge panel models, abstention summary (per-role + role-level), provenance (dataset/config SHAs, mdk-eval version), reproducibility command
  - Emitted alongside `report.json` by orchestrator (non-fatal — failures log a warning, run continues)
  - 17 unit tests locking section structure + abstention aggregation + profile rendering + file-IO

### Production data + synthesis
- [ ] **T48** — Langfuse trace ingestion (`mdk-eval ingest --from-langfuse`) — Langfuse *export* exists in `mdk_eval/traces/langfuse_export.py`, but ingest direction (traces → Scenario records) is still TODO
- [x] **T49** — LLM-assisted scenario synthesis — SHIPPED in `mdk_eval/ingest/extractors/llm.py` with 8 categories (standard, edge, adversarial, safety, honesty, multi_turn, performance, custom), focus + custom_directive knobs, full provenance
- [x] **T50** — Reference adversarial pack (2026-05-06)
  - `datasets/adversarial.jsonl` — 30 curated scenarios across 14 attack families (prompt injection, indirect injection, PII exfil, jailbreak, tool-call hijacking, off-topic forcing, role confusion, refusal bypass, multi-turn priming, output manipulation, etc.)
  - 6 critical-severity, 16 high-severity, 8 medium; no low-severity by design
  - Every scenario carries `forbidden_phrases` and/or `forbidden_claims` encoding the attack signature
  - All ids prefixed `adv_` for grep/filter; all tagged `adversarial` + `derived:reference_pack`
  - `tests/test_adversarial_pack.py` locks invariants (loader compat, family coverage, severity floor, attack-signature presence, naming convention)

### Ingest enhancements
- [ ] **T-P2-5** — `mdk-eval ingest --diff` — detect drift between agent definition and existing scenarios
- [ ] **T-P2-6** — `mdk-eval ingest --approve` — bulk-promote derived scenarios after review
- [ ] **T-P2-7** — More ingest sources: Markdown system prompts, OpenAI Assistants exports

### Reporting / dashboard
- [ ] **T-P2-8** — `results/index.html` — static cross-run trend page (sparklines + status history)
- [ ] **T-P2-9** — `mdk-eval compare` HTML — PromptFoo-style side-by-side grid for regression view

---

## 📅 Phase 3 Strategic Horizon (2 quarters)

- [ ] **T-P3-1** — Calibration tooling — `mdk-eval calibrate` computes Cohen's κ between LLM judges and human labels
- [ ] **T-P3-2** — Adversarial / red-team suite — garak + PromptBench integration as `mdk-eval redteam`
- [ ] **T-P3-3** — Reference adversarial pack expansion (multi-turn attacks, indirect injection)
- [ ] **T-P3-4** — Synthesized scenarios behind `--synthesize` flag (LLM extracts edge cases from natural-language constraints)
- [ ] **T-P3-5** — LangGraph deep introspection — node/edge enumeration, state-shape extraction
- [ ] **T-P3-6** — `mdk-eval list runs/datasets/configs` + `mdk-eval clean --keep-last N`
- [ ] **T-P3-7** — `mdk-eval verify <run_dir>` — re-validate SHAs of a saved run (audit feature)
- [ ] **T-P3-8** — CI examples — GitHub Actions, GitLab CI, Jenkins reference pipelines
- [ ] **T-P3-9** — Public methodology page — covers scoring formulas, judge prompts, calibration data

---

## 🌍 Phase 4 — Strategic (only if customer-driven)

- [ ] Multi-tenant SaaS dashboard (only if ≥3 customers explicitly request)
- [ ] BI-tool connectors (Tableau, Looker, Metabase) reading from JSON exports
- [ ] Agent certification scoring (Movate-stamped readiness rating)
- [ ] Continuous monitoring agent (cron-driven re-runs with drift alerting)

---

## Open questions (decisions owed)

- [ ] **Q1** — Should P3.1 calibration be required for every customer engagement, or opt-in?
- [ ] **Q2** — Is `garak` the right red-team primary, or is `PyRIT` worth the orchestration weight?
- [ ] **Q3** — Do we publish the standard judge prompts in our public methodology, or treat them as Movate IP?
- [ ] **Q4** — Movate brand kit (logo, color palette, typography) — current report uses placeholders
- [ ] **Q5** — Pricing / packaging — open-source core + paid Movate-stamped certification, or pure-services?
- [ ] **Q6** — When to stand up the public docs site?

---

## How to use this file with Claude Code

In any Claude Code session inside this repo:

> "Continue with T44. Show me current state of the sample dataset first."

Claude Code will read this file (along with `CLAUDE.md` if you've run `/init`), pick up T44, and proceed. Update the checkbox to `[~]` when starting and `[x]` when done.

For cross-session continuity, prefer this file over Claude Code's session-scoped TodoList when the work spans multiple days or hand-offs.

# Movate Agent Assurance — Product Requirements Document

**Product**: `mdk-eval` (Movate Development Kit — Evaluations)
**Version**: 1.0 (drafted post-Phase-1)
**Status**: Active development
**Owner**: Movate Agent Assurance team
**Audience**: Product leadership, engineering, customer-facing delivery, AI risk reviewers

---

## 1. Executive summary

`mdk-eval` is a Python-first, CLI-driven evaluation system for AI agents. It scores agent reliability, classifies failures, and produces a Movate-branded report that gives a defensible answer to a single question:

> **Is this AI agent ready for production?**

It is not a benchmarking tool, a leaderboard, or a prompt-engineering playground. It is a reliability, QA, and production-readiness system for enterprise AI agents — the equivalent of unit tests, code review, and a security audit, applied to non-deterministic LLM-driven systems.

The product is designed to be **trustworthy under scrutiny**. Every score is reproducible, every judgment is traceable, every recommendation has evidence, and every artifact is version-pinned. Customers can hand the output to an AI risk committee.

---

## 2. Problem statement

Enterprises adopting AI agents (Lyzr, LangGraph, LangChain, custom REST agents) face three failure modes:

1. **Silent unreliability.** An agent that scores 90% on a curated test passes review and ships, then drifts in production. Average-only scoring hides variance.
2. **Untrusted evaluation.** Single-LLM-as-judge systems produce numbers no one defends in a stakeholder review. "GPT-4 said our agent is 8.5/10" is not a basis for a go-live decision.
3. **Unactionable output.** Existing eval tools dump metrics. None answer "what should we fix" or "is this safe to ship." Customers want a recommendation, not a leaderboard.

The result: AI deployments stall in pilot, get shipped on vibes, or fail in production. Movate needs a system that lets delivery teams produce defensible production-readiness evidence in hours, not weeks.

---

## 3. Goals and non-goals

### 3.1 Goals

| # | Goal | How it's measured |
|---|------|-------------------|
| G1 | Defensible production-readiness verdict | Every report ends with one of four status bands and the evidence trail to defend it |
| G2 | Reproducibility across runs and environments | SHA-256 pinning of dataset, prompts, configs, models. `replay` reproduces results within score variance bounds |
| G3 | Multi-layer evaluation (deterministic > probabilistic) | Deterministic checks gate everything; LLM judges are signal, not authority |
| G4 | Multi-judge arbitration with disagreement detection | Variance + spread tracked per role; meta-judge escalation on disagreement |
| G5 | Failure analysis, not just scores | Every failure carries class + reason + evidence + recommendation |
| G6 | Five-minute first-run experience | `mdk-eval init` → `mdk-eval doctor` → `mdk-eval run --dry-run` → `mdk-eval run` |
| G7 | CI-grade integration | `--gate-against` exits non-zero on regression. `--quiet` mode pipes parseable summary to stdout |
| G8 | Vendor-neutral architecture | Adapters for Lyzr, LangGraph, REST, OpenAI-compatible, mock. New backends added without touching evaluation code |
| G9 | Trust through transparency | Judge prompts, model versions, thresholds, weights all visible and hashed in every report |

### 3.2 Non-goals

- **Not a prompt-engineering playground.** PromptFoo, OpenAI Evals, and similar exist for that. We export to PromptFoo (Path A) for interop; we don't replicate it.
- **Not a leaderboard.** No public model rankings, no marketing-friendly scores. Each evaluation is project-specific and customer-confidential.
- **Not an inference platform.** We evaluate agents others build. We do not host, route, or proxy production traffic.
- **Not a generic LLM eval framework.** Scope is enterprise agent reliability. Pure-text RAG, pure-classification, and pure-completion benchmarks are out of primary scope (though deterministic + judge layers are reusable for them).
- **Not a server.** No auth, no multi-tenant SaaS, no admin UI. CLI + static HTML reports + JSON artifacts. If a customer wants a dashboard, the path is "embed our JSON in your BI tool," not "use our hosted dashboard."

---

## 4. Target users and personas

### 4.1 Primary personas

**P1 — Movate Delivery Engineer**
*Goal*: ship an AI agent for a customer with defensible reliability evidence.
*Constraints*: tight timelines, customer scrutiny, multiple agent stacks (Lyzr today, LangGraph tomorrow).
*What they need*: one CLI that handles every backend; produces a client-ready report; runs in CI.

**P2 — Customer AI Risk Reviewer**
*Goal*: decide whether to approve an AI agent for production traffic.
*Constraints*: must defend the decision to internal compliance, legal, security.
*What they need*: a report showing methodology, judge models, prompt hashes, scenario coverage, failure analysis, regression history.

**P3 — Customer Engineering Lead**
*Goal*: know what to fix when an evaluation fails.
*Constraints*: limited bandwidth, wants prioritized fixes not raw metrics.
*What they need*: failure clusters, suggested fixes, regression diffs against prior runs.

### 4.2 Secondary personas

**S1 — Customer Prompt Engineer** — uses our PromptFoo export to A/B test prompts in their existing workflow.
**S2 — Customer SRE / On-call** — uses Langfuse trace export to debug production incidents.
**S3 — Movate Sales Engineer** — runs the tool against a prospect's agent during a POV; produces a report as a deliverable.

---

## 5. Product principles

These are non-negotiable. They drive every design decision below.

1. **Deterministic before probabilistic.** A scenario that fails schema validation never reaches the LLM judges. Cheap, fast, hard checks gate expensive, slow, soft ones.
2. **Multi-judge by default.** No single LLM is authoritative. Every LLM-graded metric runs through ≥2 models; high-variance verdicts escalate to a stronger meta-judge.
3. **Reliability ≠ quality.** A high mean score with high variance is not production-ready. Multi-run is the default, not an option.
4. **Trace everything.** Inputs, outputs, tool calls, sub-agent hops, latencies, retries, judge prompts and verdicts. Auditability is a feature, not overhead.
5. **Version everything.** Every artifact records SHA-256 of dataset, judge prompts, model IDs, and config. Without this, no regression testing.
6. **Reports are actionable.** Every finding maps to a class, a reason, evidence, and a recommended fix. "Score = 0.62" is not actionable; "grounding judge flagged unsupported claim — strengthen retrieval" is.
7. **Provenance on derived artifacts.** Anything generated automatically (ingest scenarios, synthesized tests) carries source path, source SHA, extractor name, and the constraint quote it came from.
8. **Fail loudly, fail early.** Config errors raise before any work happens. Failed adapter calls don't waste judge tokens. Clear error messages with concrete fixes.
9. **CI-friendly stdout discipline.** Live UI to stderr; machine-parseable summary to stdout. Pipe the result of a run into a build gate without parsing screen scrapes.
10. **No magic.** Every judge prompt is visible. Every weight is in config. Every scoring formula is documented. Customers can defend the numbers without trusting us.

---

## 6. Functional requirements

### 6.1 CLI commands (Phase 1, shipped)

| Command | Purpose | Notes |
|---------|---------|-------|
| `mdk-eval init [path]` | Scaffold a new evaluation project | Interactive Rich prompts; `--vscode` adds debug configs; `--cd-script` for shell-eval auto-nav |
| `mdk-eval doctor` | Diagnose environment | Checks Python, env vars, optional extras, dataset validity, endpoint reachability |
| `mdk-eval run` | Execute an evaluation run | Auto-discovers `mdk-eval.yaml`; `--dry-run`, `--gate-against`, `--no-judges`, multi-run |
| `mdk-eval report` | Re-generate report from saved run dir | Interactive run picker if `--results` omitted |
| `mdk-eval compare` | Diff two run dirs | Picks twice (candidate + baseline); surfaces score deltas, regressions, status changes |
| `mdk-eval replay` | Re-run saved scenarios against (possibly different) endpoint | `--trace-id` narrows to one scenario; useful for regression isolation |
| `mdk-eval ingest` | Derive scenarios from agent definition file | Lyzr today; heuristic extractor; emits config + dataset + agent_card |
| `mdk-eval export` | Export dataset to other tools' formats | `--format promptfoo` emits a valid `promptfoo.yaml` |
| `mdk-eval version` | Print version | |

### 6.2 Adapter layer (Phase 1, shipped)

Single `AgentAdapter.run(input) -> AdapterResult` contract. Backends:

- **mock** — deterministic-with-jitter, no network; used for self-tests and CI
- **REST** — generic POST/GET against arbitrary endpoint; configurable request/response paths
- **openai_compat** — OpenAI-compatible chat completions (works with vLLM, Together, etc.)
- **Lyzr** — Lyzr Agent Studio (`agent_id`, `LYZR_API_KEY`)
- **LangGraph** — wraps a compiled graph via dotted import path

New adapter = one file + one factory entry. No changes to evaluators, runner, or report.

### 6.3 Evaluation layers (Phase 1, shipped)

**Layer 1 — Deterministic validators** (always run first; gate downstream layers):
- Adapter call success
- Schema validation (Draft 2020-12 JSON Schema)
- Required fields (dotted-path JSONPath)
- Forbidden phrases (case-insensitive substring)
- Tool usage (required tools called with optional arg matching)
- Workflow path adherence (must-visit, must-not-visit, ordered subsequence)
- Latency budget
- Retry cap

**Layer 2 — DeepEval bridge** (optional metric source):
- G-Eval (custom criteria)
- Hallucination metric
- Answer relevance
- Task completion

**Layer 3 — Multi-role judge panel** (parallel; one role per concern):
- Correctness judge
- Grounding/faithfulness judge
- Completeness judge
- Tool usage judge
- UX/tone judge
- Safety/policy judge

Each role runs across ≥2 models (default: OpenAI + Anthropic). Verdicts include score, pass/fail, rationale.

**Layer 4 — Arbitration**:
- Variance computed across panel verdicts per role
- Variance > threshold (default 0.04) escalates to meta-judge (default Anthropic Sonnet)
- Meta-judge re-judges; does not average
- Confidence score derived from variance + escalation rate

**Layer 5 — Grounding triangulation** (specialized; opt-out):
- Three providers run in parallel: our judges (one per panel model), Ragas faithfulness, TruLens groundedness
- Spread = max − min on [0,1]
- Spread > threshold (default 0.30) → meta-judge tie-break
- Status: `agreed` | `escalated` | `insufficient_signal`
- Abstentions are first-class outcomes (provider lacks input or library not installed)

### 6.4 Standardized scoring taxonomy

All scores 0–100. **Ten categories, fixed and global** (do not vary per project):

| # | Category | What it measures |
|---|----------|------------------|
| 1 | Task Success | Did the agent actually solve the task? |
| 2 | Correctness | Is the answer factually accurate? |
| 3 | Grounding | Is it supported by data/context vs hallucinated? |
| 4 | Completeness | Did it include all required elements? |
| 5 | Tool Usage | Were the right tools called correctly? |
| 6 | Workflow Adherence | Did it follow the expected agent path? |
| 7 | Consistency | Does it behave the same across repeated runs? |
| 8 | Latency | Is it fast and responsive? |
| 9 | Safety | Any unsafe or disallowed output? |
| 10 | UX/Tone | Is it usable, clear, professional? |

**Composite formula**: weighted mean across the ten categories. Weights documented in `runner/scoring.py:compute_overall` and visible in every report.

**Hard gates** (force-fail):
- Any CRITICAL deterministic check failed → score = 0
- Safety judge < 0.95 → max score = 30
- Latency budget exceeded on HIGH/CRITICAL severity scenario → max score = 65

### 6.5 Status bands

| Score range | Status | Meaning |
|-------------|--------|---------|
| ≥ 90 | `production_ready` | Approve for production with standard monitoring |
| 80 – 89 | `pilot_ready` | Limited pilot with human-in-the-loop on flagged failures |
| 70 – 79 | `needs_improvement` | Hold promotion; address top failure clusters |
| < 70 | `not_ready` | Do not promote; address critical failures first |

Status overrides: any CRITICAL-severity scenario regression OR safety < 80 forces `not_ready` regardless of composite.

### 6.6 Top-level contract (`evaluation_summary.json`)

Stable shape consumed by CI gates and downstream tools:

```json
{
  "overall_score": 84.2,
  "confidence": 0.76,
  "variance": 0.9,
  "status": "pilot_ready",
  "scorecard": { "task_success": 87, "correctness": 89, "...": "..." },
  "passing_scenarios": 14,
  "total_scenarios": 18
}
```

### 6.7 Failure-mode taxonomy

Standardized classes; every failed scenario produces ≥1 finding with `{class, reason, evidence, recommendation, severity}`:

- `hallucination`
- `tool_misuse`
- `missing_step`
- `premature_resolution`
- `inconsistency`
- `latency_issue`
- `safety_violation`
- `schema_violation`
- `workflow_drift`

### 6.8 Multi-run and reliability metrics

Every scenario runs N times (configurable; default 3). Reported:

- Per-run final score
- Mean score
- Score variance
- Drift score (1 − mean Jaccard similarity of output tokens across runs)
- Consistency score (0–100, derived from variance + drift)
- Pass rate

Inconsistency itself becomes a finding when 0 < pass_rate < 1.

### 6.9 Versioning and reproducibility

Every run dir contains a `manifest.json` pinning:

- `dataset_sha256` (canonical JSONL snapshot)
- `config_sha256`
- `judge_prompts_sha256` (per role)
- `tool_versions` (openai, anthropic, deepeval, langfuse, httpx, pydantic, jinja2)
- `mdk_eval_version`
- `meta_judge_model`, `arbitration_variance_threshold`, `judges_enabled`, `runs_per_scenario`
- Wall-clock start/end

`mdk-eval replay --results <dir>` re-executes against the snapshotted dataset.

### 6.10 Ingest (Phase 1, shipped)

`mdk-eval ingest -i agent.json` reads an agent definition and emits:
- A wired `configs/<name>.yaml`
- A draft `datasets/<name>.jsonl` (heuristically derived scenarios)
- An `agent_cards/<name>.md` (stakeholder one-pager)

**Trust principles enforced:**
- Every derived scenario tagged `unverified`
- Each carries `meta.derived_from = {source_path, source_sha256, extractor, constraint_quote}`
- Scenarios needing real data tagged `requires_fixture`
- Heuristic extraction only (no LLM fabrication)

Currently supports Lyzr JSON. Categories derived (when present in declaration):
`happy`, `schema`, `tool_sequence`, `edge_threshold`, `edge_threshold_above`, `default_rule`, `forbidden`, `failure`, `slo`.

### 6.11 Reports (Phase 1, shipped)

Every run produces:

| Format | Purpose |
|--------|---------|
| `report.html` | Movate-branded primary deliverable; opens in any browser; no JS required |
| `report.pdf` | Same content; client-archival format (WeasyPrint) |
| `report.json` | Full machine-readable structure |
| `evaluation_summary.json` | Stable contract for CI consumers |
| `scenarios.csv` | Flat scenario-level table for spreadsheets |
| `aggregate.json` | Per-scenario aggregates only |
| `manifest.json` | Provenance pinning |
| `dataset.snapshot.jsonl` | Canonical dataset snapshot for replay |
| `scenarios/<id>/runs/<n>/{trace,deterministic,judges,triangulation,eval}.json` | Per-run-per-scenario artifacts |

Report sections (HTML/PDF):
1. Executive Summary (overall score, status badge, recommendation, key findings)
2. Reliability Scorecard (10-axis grid, color-coded)
3. Methodology (judges, models, thresholds, dataset stats, hashes)
4. Deterministic Validation Results (per-check pass rate)
5. Judge Panel & Arbitration (escalation rate, agreement per role, triangulation summary)
6. Scenario-Level Results (pass rate, mean, variance, consistency, drift, failure classes)
7. Failure Mode Analysis (clusters, severity, suggested fixes)
8. Risk Register (severity × likelihood × mitigation)
9. Scenario Detail (per-scenario expandable sections with judge verdicts and triangulation breakdown)
10. Provenance Appendix (tool versions, judge prompt hashes)

### 6.12 Observability

Optional Langfuse export gated by `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`. When present, every run pushes traces (input, output, tool calls, latencies, scores) to Langfuse. No-op when keys absent. Never fails the run.

---

## 7. Non-functional requirements

### 7.1 Performance

- **Mock-adapter run** (6 scenarios × 3 runs, no judges): < 5 seconds wall clock
- **Live-judge run** (6 scenarios × 3 runs, full panel + triangulation): < 90 seconds wall clock at concurrency 4
- **Report generation**: < 1 second after run completes
- **Memory**: < 500 MB peak during a 100-scenario × 5-run evaluation

### 7.2 Reliability

- Zero crashes during evaluation. A failed adapter call, judge timeout, or library error becomes a recorded failure, never a process exit.
- Optional integrations (Langfuse, Ragas, TruLens, WeasyPrint) gracefully no-op when missing.
- Network errors in judges fall back to mean rather than failing the run.

### 7.3 Security

- No secrets in logs, reports, or persisted artifacts.
- API keys read from env (`.env` autoload via python-dotenv); never written back.
- Forbidden-phrase + Safety judge run on all outputs by default.

### 7.4 Compatibility

- Python 3.10+
- macOS, Linux primary; Windows via WSL (untested but no platform-specific code)
- Works in CI (GitHub Actions, GitLab CI, Jenkins) without modification

### 7.5 Usability

- First successful run from `init` to opened report: < 5 minutes for a new user.
- Every error message includes the concrete fix.
- `--quiet` mode emits one parseable line to stdout; everything else to stderr.
- Live multi-line status panel during runs (Rich-based, replaces plain progress bar).
- Inline failure stream surfaces problems while the run continues.
- Interactive run picker for `report` / `compare` / `replay`.

### 7.6 Maintainability

- Pydantic v2 models everywhere; no untyped dicts in interfaces.
- Async throughout the orchestrator; bounded concurrency.
- Tests cover adapters, deterministic checks, judges (with mocked LLM calls), triangulation, scoring, ingest, validation, gating, init.
- 44 tests pass on each commit; CI gating planned.

---

## 8. Architecture

```
mdk_eval/
  cli/                      Typer CLI entry points
    app.py                  run, report, compare, replay, ingest, export, init, doctor, version
    init.py                 scaffolding + open-in-editor
    doctor.py               environment diagnostics
    validate.py             config validation, judge auto-disable, config auto-discovery
  adapters/                 unified AgentAdapter contract
    base.py                 abstract class
    factory.py              build_adapter(cfg)
    mock.py                 deterministic-with-jitter
    rest.py                 generic REST
    openai_compat.py        chat-completions
    lyzr.py                 Lyzr Agent Studio
    langgraph.py            in-process LangGraph
  evaluators/
    deterministic/checks.py 8 deterministic checks
    judges/
      panel.py              multi-role panel + per-role arbitration
      prompts.py            judge prompts (versioned via SHA-256)
      llm_clients.py        thin async OpenAI / Anthropic wrappers
      deepeval_bridge.py    optional DeepEval integration
    providers/              MetricProvider Protocol
      base.py               ProviderScore, TriangulationResult
      our_judge.py          our grounding judge as a provider
      ragas.py              Ragas faithfulness provider
      trulens.py            TruLens groundedness provider
    triangulation.py        N-provider triangulator with meta-judge escalation
  runner/
    orchestrator.py         end-to-end execute_run; multi-run, async, bounded concurrency
    scoring.py              run-level + aggregate scoring; status decision; risk register
  reporting/
    templates/report.html.j2  Movate-branded Jinja2 template
    generators/
      html_gen.py
      pdf_gen.py            WeasyPrint (best-effort)
      csv_gen.py            scenario-level CSV
  ingest/
    base.py                 IngestSource Protocol; auto-detect
    lyzr.py                 LyzrIngestor
    extractors/heuristic.py regex/parsing extractor
    agent_card.py           stakeholder markdown card
  exporters/
    promptfoo.py            promptfoo.yaml emitter
  scenarios.py              JSONL/YAML loader; canonical snapshot
  storage/
    run_dir.py              filesystem layout
    versioning.py           SHA-256 helpers; tool_versions
  traces/langfuse_export.py  optional Langfuse exporter
  utils/
    logging.py              Rich logger; verbosity flag
    ui.py                   preflight, live status panel, failure stream, next-steps, run picker
    jsonpath.py             tiny JSONPath accessor
  models.py                 Pydantic v2 schemas (Scenario, Trace, ScenarioRunResult, RunReport, ...)
  config.py                 RunConfig, AdapterConfig, JudgesConfig
```

---

## 9. Key user workflows

### 9.1 First-run (new user)

```bash
mdk-eval init my_project --vscode
cd my_project
mdk-eval doctor
# edit datasets/agent.jsonl with real scenarios
mdk-eval run --dry-run
mdk-eval run
open results/run_*/report.html
```

### 9.2 Ingest from agent definition

```bash
mdk-eval ingest -i exports/lyzr_brief_agent.json
# review datasets/brief_ingestion.jsonl, fill 'requires_fixture' scenarios
# remove 'unverified' tags after review
mdk-eval run -c configs/brief_ingestion.yaml
```

### 9.3 Regression gate in CI

```yaml
# .github/workflows/eval.yml
- name: Agent regression gate
  run: |
    mdk-eval -q run \
      --target lyzr --agent-id "$LYZR_AGENT_ID" \
      --dataset datasets/agent.jsonl \
      --runs 3 \
      --gate-against ./baseline_run \
      --gate-threshold 5
  # exits 2 on regression > 5 points
```

### 9.4 Interactive comparison

```bash
mdk-eval compare
# numbered table of recent runs; pick candidate then baseline
# prints score deltas + status changes
```

### 9.5 Single-scenario debugging

```bash
mdk-eval replay --trace-id hallucination_trap --target mock --runs 5
# isolates one scenario; useful when tracking down flake
```

### 9.6 Cross-tool interop

```bash
mdk-eval export --format promptfoo --out promptfoo.yaml
promptfoo eval -c promptfoo.yaml
# prompt engineers keep their existing UI; same scenarios
```

---

## 10. Trust and reproducibility guarantees

These are commitments customers can depend on; they justify the cost premium over open-source alternatives.

| Guarantee | Mechanism |
|-----------|-----------|
| Same dataset + config produces same evaluation structure | Canonical JSONL snapshot + SHA-256 in manifest |
| Same judge prompts can be re-applied to a saved run | Judge prompts versioned via SHA-256; cached judge responses planned (Phase 2) |
| Disagreement between LLM judges is surfaced, not hidden | Per-role variance computed; escalation rate reported; meta-judge invoked on threshold breach |
| Failed scenarios trace back to root cause | Every finding has `class + reason + evidence + recommendation` |
| Derived scenarios trace back to source spec | `meta.derived_from` with source path + SHA + constraint quote |
| Status decision is defensible | Score formula documented; weights in config; gating rules explicit |
| Audit trail | Full traces (inputs, outputs, tool calls, latencies, judge verdicts) persisted per run, optionally exported to Langfuse |

---

## 11. Phased roadmap

### Phase 1 — shipped (current state)

- All ten CLI commands
- Five adapters (mock, REST, OpenAI-compat, Lyzr, LangGraph)
- Five evaluation layers (deterministic, DeepEval, judge panel, arbitration, triangulation)
- Ten-category scoring taxonomy + status bands
- Failure-mode taxonomy with reason/evidence/recommendation
- HTML + PDF + JSON + CSV reports
- Multi-run + drift + consistency
- Versioning + reproducibility manifest
- Lyzr ingest (heuristic)
- PromptFoo export (path A)
- Langfuse trace export
- Rich CLI UX (preflight, live panel, inline failures, next-steps panel, picker, doctor, dry-run, gate)
- 44 passing tests

### Phase 2 — next 4–6 weeks

| # | Item | Why |
|---|------|-----|
| P2.1 | **Judge-response caching** keyed by `(prompt_sha, model, input_sha)` | Prove score reproducibility without re-spending tokens; auditor-grade replay |
| P2.2 | **Statistical confidence intervals** (Wilson on pass-rate, bootstrap on overall) | Replace point estimates; flag low-N runs |
| P2.3 | **Judge abstention** — `{"abstain": true, "reason": "..."}` allowed in verdicts | Honest "I can't tell" beats noisy 0.5 |
| P2.4 | **Auto-emitted `methodology.md` per run** | Standalone artifact for AI risk committees |
| P2.5 | **`mdk-eval ingest --diff`** | Detect drift between agent definition and existing scenarios |
| P2.6 | **`mdk-eval ingest --approve`** | Bulk-promote derived scenarios after review |
| P2.7 | **More ingest sources**: Markdown system prompts, OpenAI Assistants exports | Coverage |
| P2.8 | **`results/index.html`** — static cross-run trend page (sparklines + status history) | Eval-ops audience without a server |
| P2.9 | **`mdk-eval compare` HTML** — PromptFoo-style side-by-side grid | The one place where their UX is the right pattern |

### Phase 3 — next 2 quarters

| # | Item | Why |
|---|------|-----|
| P3.1 | **Calibration tooling** — `mdk-eval calibrate` computes Cohen's κ between LLM judges and human labels | Trust justification for the judge panel |
| P3.2 | **Adversarial / red-team suite** — garak + PromptBench integration as `mdk-eval redteam` | Safety story; enterprise risk requirement |
| P3.3 | **Reference adversarial pack** — versioned `datasets/adversarial.jsonl` (PII exfil, prompt injection, jailbreak attempts) | Day-one safety baseline |
| P3.4 | **Synthesized scenarios** behind `--synthesize` flag (LLM extracts edge cases from natural-language constraints) | Higher coverage from same agent definition |
| P3.5 | **LangGraph deep introspection** — node/edge enumeration, state-shape extraction | Better LangGraph ingest |
| P3.6 | **`mdk-eval list runs/datasets/configs`** + **`mdk-eval clean --keep-last N`** | Tidy local artifacts |
| P3.7 | **`mdk-eval verify <run_dir>`** — re-validate SHAs of a saved run | Audit feature |
| P3.8 | **CI examples** — GitHub Actions, GitLab CI, Jenkins reference pipelines | Adoption |
| P3.9 | **Public methodology page** | Customer-facing docs covering scoring formulas, judge prompts, calibration data |

### Phase 4 — strategic horizon

- Multi-tenant SaaS dashboard (only if ≥3 customers explicitly request — see non-goals)
- BI-tool connectors (Tableau, Looker, Metabase) reading from JSON exports
- Agent certification scoring (Movate-stamped readiness rating)
- Continuous monitoring agent (cron-driven re-runs with drift alerting)

---

## 12. Success metrics

### 12.1 Product

- **Time-to-first-report**: < 5 minutes from `mdk-eval init` to opened HTML report (target: 90% of new users)
- **Report regeneration time**: < 1 second
- **Test coverage**: ≥ 80% line coverage on `mdk_eval/` (currently 44 tests, exact % TBD)

### 12.2 Adoption (Movate internal)

- **Delivery teams using `mdk-eval`** in agent engagements: 100% within 6 months of GA
- **Time saved per engagement**: ≥ 2 days vs. hand-built evaluation
- **Customer-facing report consistency**: 100% use the standard 10-category taxonomy

### 12.3 Customer outcomes

- **Production-readiness decisions backed by `mdk-eval` evidence**: track per engagement
- **Post-launch incidents traceable to a missed eval scenario**: target 0; each one becomes a regression scenario in the next dataset

---

## 13. Risks and mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Judge model drift between runs (vendor silently changes weights) | Medium | Pin model version strings in manifest; surface drift via cross-run comparison |
| LLM judges agree confidently on the wrong answer | High | Multi-judge arbitration; meta-judge escalation; planned calibration set with human labels (P3.1) |
| Ragas / TruLens / DeepEval API churn breaks our integrations | Medium | All optional; abstention-first design; pin minor versions; integration tests |
| Customers find scoring weights opaque | Medium | Weights documented in `runner/scoring.py` and visible in report methodology section |
| `mdk-eval ingest` derives bad scenarios that get auto-trusted | High | Review-required gating: every derived scenario starts `unverified`; explicit promotion required |
| PII or secrets leaked via Langfuse / report artifacts | High | No raw env vars in artifacts; Safety judge runs on every output; planned report scrubber (P3) |
| CLI UX regressions during refactors | Medium | Smoke test scripts (shipped); 44 unit/integration tests; CI gating planned |

---

## 14. Out of scope (explicit)

- A web UI / SaaS dashboard
- Real-time agent monitoring (Langfuse handles this; we batch-evaluate)
- Hosting or proxying customer agent traffic
- Generic ML benchmark leaderboards
- Auto-generated synthetic test data without human review (intentional friction)
- Replacing Langfuse, PromptFoo, or DeepEval — we integrate with them

---

## 15. Open questions

| # | Question | Decision needed by |
|---|----------|--------------------|
| Q1 | Should P3.1 calibration tooling be required for every customer engagement, or opt-in? | Phase 3 kickoff |
| Q2 | Is `garak` the right red-team primary, or is `PyRIT` worth the orchestration weight for multi-turn attacks? | Phase 3 kickoff |
| Q3 | Do we publish the standard judge prompts in our public methodology, or treat them as Movate IP? | Pre-GA |
| Q4 | What is the Movate brand kit (logo, color palette, typography) — current report uses placeholder Movate orange + ink colors | Pre-GA |
| Q5 | Pricing / packaging model — open-source core + paid Movate-stamped certification, or pure-services? | Product leadership |
| Q6 | When to stand up the public docs site? Phase 3 plans `methodology.md` per-run, but a docs portal is a separate decision | Marketing kickoff |

---

## 16. Glossary

| Term | Definition |
|------|------------|
| **Adapter** | Module that translates between `mdk-eval`'s contract and a specific agent backend |
| **Arbitration** | Process of resolving disagreement between multiple judges' verdicts |
| **Confidence** | Derived from variance + judge disagreement; `1 − (norm_var + disagreement_penalty)` clamped [0,1] |
| **Drift score** | 1 − mean Jaccard similarity of output tokens across multi-run executions |
| **Finding** | Structured failure record `{class, reason, evidence, recommendation, severity}` |
| **Gate** | Hard threshold beyond which a scenario fails regardless of LLM-graded scores |
| **Manifest** | Per-run JSON pinning all reproducibility data (hashes, model IDs, versions) |
| **Meta-judge** | Stronger LLM invoked when panel judges disagree beyond threshold; re-judges, doesn't average |
| **Provider** (`MetricProvider`) | Tool-agnostic scoring source (our judges, Ragas, TruLens, DeepEval, garak, …) |
| **Scenario** | One test case: input + expectations + rubric + provenance |
| **Status band** | One of `production_ready` / `pilot_ready` / `needs_improvement` / `not_ready` |
| **Trace** | Full execution record: inputs, outputs, tool calls, sub-agent hops, latencies, retries |
| **Triangulation** | N-provider parallel scoring on the same role; spread + meta-judge escalation |
| **Unverified scenario** | Auto-derived scenario awaiting human review before contributing to readiness decision |

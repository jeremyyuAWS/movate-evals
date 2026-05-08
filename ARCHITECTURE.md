# Movate Agent Assurance — Technical Architecture (for diagramming)

This document is structured to be **drawn**. Each section maps to a diagram type with the boxes, arrows, and labels you'd put on it. Use it as the script for an architecture deck or a system diagram.

> Note on libraries: we use **Rich** (Live + Progress) — not tqdm. Rich is strictly more capable: multi-line panels, inline failure streams, color-coded tables. The user-facing "progress bar" is a `rich.live.Live` region containing a `Group(Panel(stats), Progress)`.

---

## 1. System overview — one-paragraph anchor

`mdk-eval` is a CLI-driven Python evaluation system. A user invokes a subcommand (`run`, `ingest`, `report`, etc.). The CLI loads + validates a config, builds an **adapter** for the target agent backend, loads **scenarios** from JSONL/YAML, and the **orchestrator** runs each scenario × N times through five evaluation **layers** in order: deterministic checks → DeepEval metrics → multi-role judge panel → arbitration → grounding triangulation. **Scoring** maps the layered signals onto a fixed 10-category scorecard, applies hard gates, and decides a status band. **Reporting** writes JSON/CSV/HTML/PDF artifacts (and optionally pushes traces to Langfuse).

---

## 2. High-level diagram (suggested: layered block diagram)

ASCII first; redraw in your tool of choice.

```
┌──────────────────────────────────────────────────────────────────────┐
│                            CLI (Typer)                               │
│   init  doctor  run  report  compare  replay  ingest  export         │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ (validates config, autodetects file,
                                 │  auto-disables judges if no API keys)
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  Orchestrator  (asyncio + bounded concurrency)       │
│        loads scenarios → snapshots dataset → fans out runs           │
└──┬──────────────────────────────────────────────────────────────┬────┘
   │                                                              │
   ▼                                                              ▼
┌──────────────────────┐    ┌────────────────────────────────────────┐
│  Adapter (per call)  │    │  Evaluation Layers (run for each run)  │
│  - mock              │    │                                        │
│  - REST              │    │  ┌──────────────────────────────────┐  │
│  - openai_compat     │    │  │ L1 Deterministic (gate)          │  │
│  - Lyzr              │    │  ├──────────────────────────────────┤  │
│  - LangGraph         │    │  │ L2 DeepEval bridge (optional)    │  │
└──────────────────────┘    │  ├──────────────────────────────────┤  │
   ▲                        │  │ L3 Multi-role Judge Panel        │  │
   │ executes agent;        │  │   correctness, completeness,     │  │
   │ returns AdapterResult  │  │   tool_usage, ux_tone, safety    │  │
   │ (text + JSON + Trace)  │  ├──────────────────────────────────┤  │
   │                        │  │ L4 Arbitration                   │  │
   │                        │  │   variance > thresh → meta-judge │  │
   │                        │  ├──────────────────────────────────┤  │
   │                        │  │ L5 Grounding triangulation       │  │
   │                        │  │   our judge + Ragas + TruLens    │  │
   │                        │  └──────────────────────────────────┘  │
   │                        └────────────────────────────────────────┘
   │                                            │
   │                                            ▼
   │                    ┌──────────────────────────────────────────┐
   │                    │   Scoring                                │
   │                    │   - per-run: 10 categories → composite   │
   │                    │   - hard gates (safety, latency, crit)   │
   │                    │   - aggregate across runs (variance,     │
   │                    │     consistency, drift)                  │
   │                    │   - status band decision                 │
   │                    │   - confidence = 1 − (var + disagreement)│
   │                    └──────────────────────────────────────────┘
   │                                            │
   │                                            ▼
   │                  ┌──────────────────────────────────────────────┐
   │                  │    Reporting (Jinja2 → Movate-branded)       │
   │                  │    report.html  report.pdf  report.json      │
   │                  │    scenarios.csv  evaluation_summary.json    │
   │                  │    manifest.json  per-scenario JSONs         │
   │                  └──────────────────────────────────────────────┘
   │                                            │
   └────────────────► [optional] Langfuse ◄─────┘   (trace export)
```

**Diagram tip**: the five evaluation layers are the heart of the diagram; show them as horizontal bands, with the adapter feeding all five. The orchestrator wraps everything. CLI sits on top. Reports + Langfuse hang off the bottom-right.

---

## 3. Data flow for one scenario (suggested: sequence diagram)

This is the lifecycle of **one scenario through one run**. Multi-run is just a loop around this.

```
User                  CLI                   Orchestrator          Adapter           Evaluators           Scoring            Reports
 │                     │                          │                  │                  │                   │                   │
 │  mdk-eval run       │                          │                  │                  │                   │                   │
 │────────────────────▶│                          │                  │                  │                   │                   │
 │                     │ validate config          │                  │                  │                   │                   │
 │                     │ auto-disable judges      │                  │                  │                   │                   │
 │                     │ if no API keys           │                  │                  │                   │                   │
 │                     │ load scenarios           │                  │                  │                   │                   │
 │                     │ snapshot dataset (sha256)│                  │                  │                   │                   │
 │                     │ print preflight panel    │                  │                  │                   │                   │
 │                     │─────────────────────────▶│                  │                  │                   │                   │
 │                     │                          │ build adapter    │                  │                   │                   │
 │                     │                          │─────────────────▶│                  │                   │                   │
 │                     │                          │ for each (s,run):│                  │                   │                   │
 │                     │                          │   adapter.run()  │                  │                   │                   │
 │                     │                          │─────────────────▶│                  │                   │                   │
 │                     │                          │                  │ call agent       │                   │                   │
 │                     │                          │                  │ (HTTP or local)  │                   │                   │
 │                     │                          │                  │ capture trace    │                   │                   │
 │                     │                          │◀─AdapterResult───│                  │                   │                   │
 │                     │                          │   det.run_all()  │                  │                   │                   │
 │                     │                          │─────────────────────────────────────▶│                   │                   │
 │                     │                          │   IF crit gate   │                  │                   │                   │
 │                     │                          │   FAIL: skip L2-5│                  │                   │                   │
 │                     │                          │                  │                  │  L2 deepeval      │                   │
 │                     │                          │                  │                  │  L3 panel (||)    │                   │
 │                     │                          │                  │                  │  L4 arbitrate     │                   │
 │                     │                          │                  │                  │  L5 triangulate   │                   │
 │                     │                          │◀─ArbitratedScores│                  │                   │                   │
 │                     │                          │   compute_run_   │                  │                   │                   │
 │                     │                          │   scores()       │                  │                   │                   │
 │                     │                          │──────────────────────────────────────────────────────────▶│                   │
 │                     │                          │◀─cat_scores, final, passed, findings──────────────────────│                   │
 │                     │                          │   write JSONs to scenario run dir                        │                   │
 │                     │                          │   update LiveStats                                       │                   │
 │                     │ inline failure line ◀────│   (if !passed)                                           │                   │
 │  ✗ <id> ...     ◀───│                          │                                                          │                   │
 │                     │                          │ ───────────[after all scenarios]─────────────────────────────────────────────▶│
 │                     │                          │                  │                  │                   │   build_scorecard │
 │                     │                          │                  │                  │                   │   cluster_failures│
 │                     │                          │                  │                  │                   │   build_risk_reg  │
 │                     │                          │                  │                  │                   │   decide_readiness│
 │                     │                          │                  │                  │                   │   compute_run_var │
 │                     │                          │                  │                  │                   │   _and_confidence │
 │                     │                          │                  │                  │                   │──────────────────▶│
 │                     │                          │                                                                            html_gen
 │                     │                          │                                                                            pdf_gen
 │                     │                          │                                                                            csv_gen
 │ Next steps panel ◀──│◀─ run_dir, report ──────│                                                                              │
 │ open report.html    │                          │                                                                              │
```

**Diagram tip**: render this as a sequence diagram in Mermaid or PlantUML. Vertical lifelines for User / CLI / Orchestrator / Adapter / Evaluators / Scoring / Reports.

---

## 4. The five evaluation layers (suggested: stacked horizontal bands)

```
┌──────────────────────────────────────────────────────────────────────┐
│                         AdapterResult                                │
│           (text + JSON output + Trace + tool calls)                  │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ L1  DETERMINISTIC VALIDATORS                          (always first) │
│     adapter_ok • schema (jsonschema) • required_fields               │
│     forbidden_phrases • tool_usage • workflow_adherence              │
│     latency • retries                                                │
│  ▶ if any CRITICAL fails → skip L2-L5; record gate failure           │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ L2  DEEPEVAL BRIDGE                                       (optional) │
│     g_eval • hallucination • answer_relevance • task_completion      │
│  ▶ no-op if deepeval not installed; never raises                     │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ L3  MULTI-ROLE JUDGE PANEL                          (parallel async) │
│     ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐      │
│     │correctness │ │completeness│ │ tool_usage │ │  ux_tone   │      │
│     └────────────┘ └────────────┘ └────────────┘ └────────────┘      │
│     ┌────────────┐                                                   │
│     │   safety   │   (grounding handled by L5 triangulation)         │
│     └────────────┘                                                   │
│  Each role × every panel model (default: openai+anthropic)           │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ L4  ARBITRATION                                                      │
│     variance(scores) > 0.04?                                         │
│        no  → final = mean,        confidence = 1 − norm_var          │
│        yes → meta-judge (Anthropic Sonnet) re-judges → final         │
│              flag escalated=true                                     │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ L5  GROUNDING TRIANGULATION                       (specialized role) │
│   ┌──────────────┐ ┌──────────────┐ ┌────────────────────┐           │
│   │ our judge    │ │ Ragas        │ │ TruLens            │           │
│   │ (per panel   │ │ faithfulness │ │ groundedness       │           │
│   │  model)      │ │              │ │                    │           │
│   └──────────────┘ └──────────────┘ └────────────────────┘           │
│   abstain if missing context / library / API key                     │
│   spread = max - min on [0,1]                                        │
│      ≤ 0.30 → agreed (mean)                                          │
│      > 0.30 → escalated (meta-judge tie-break)                       │
│      < 2 active providers → insufficient_signal                      │
└──────────────────────────────────────────────────────────────────────┘
```

**Diagram tip**: stack as five horizontal bands with the AdapterResult feeding the top. Use color: L1 = grey/deterministic, L2 = blue/optional, L3 = orange/parallel, L4 = red/escalation, L5 = purple/specialized.

---

## 5. Library inventory — what is used where (suggested: dependency-graph diagram)

Group as **Required** vs **Optional**. Optional libraries always degrade gracefully.

### 5.1 Core / required

| Library | Where | Purpose |
|---------|-------|---------|
| **Typer** | `cli/app.py` | CLI framework (built on Click). Subcommands, flags, completion. |
| **Rich** | `utils/logging.py`, `utils/ui.py`, `cli/*.py` | Terminal UI: live multi-line panels, color tables, progress bars, prompts, log handler. **Replaces tqdm** with strictly more capable `rich.live.Live` + `rich.progress.Progress`. |
| **Pydantic v2** | `models.py`, `config.py`, throughout | Data models. Every interface boundary uses Pydantic. |
| **PyYAML** | `config.py`, `scenarios.py`, `cli/init.py`, exporters | YAML config + dataset parsing; PromptFoo export. |
| **jsonschema** | `evaluators/deterministic/checks.py` | Draft 2020-12 schema validation. |
| **httpx** | `adapters/rest.py`, `openai_compat.py`, `lyzr.py`, `cli/doctor.py` | Async HTTP client for adapters and reachability checks. |
| **tenacity** | `adapters/rest.py` | Retry-with-backoff for REST adapter. |
| **anyio** | (transitive) | Async runtime helpers. |
| **Jinja2** | `reporting/generators/html_gen.py` + `templates/report.html.j2` | Movate-branded HTML report rendering. |
| **python-dotenv** | `cli/app.py` | `.env` autoload at command entry. |

### 5.2 Optional — extras

| Library | Extra | Where | Purpose | Behavior if missing |
|---------|-------|-------|---------|----------------------|
| **openai** | `[judges]` | `evaluators/judges/llm_clients.py` | OpenAI-provider judge calls + DeepEval LLM client | judge call returns `LLMClientError`, panel records as abstention |
| **anthropic** | `[judges]` | `evaluators/judges/llm_clients.py` | Anthropic-provider judge calls + meta-judge | same as above |
| **DeepEval** | `[judges]` | `evaluators/judges/deepeval_bridge.py` | G-Eval, hallucination, answer_relevance, task_completion | bridge returns `{}`, scoring proceeds without those signals |
| **Ragas** | (separate `pip install ragas`) | `evaluators/providers/ragas.py` | Faithfulness metric for grounding triangulation | provider abstains, triangulation continues with remaining sources |
| **TruLens** | (separate `pip install trulens`) | `evaluators/providers/trulens.py` | Groundedness w/ chain-of-thought reasons | provider abstains |
| **Langfuse** | `[langfuse]` | `traces/langfuse_export.py` | Trace export to Langfuse observability | exporter no-ops, run unaffected |
| **WeasyPrint** | `[pdf]` | `reporting/generators/pdf_gen.py` | HTML → PDF rendering | PDF skipped, HTML still produced |
| **LangGraph** | `[langgraph]` | `adapters/langgraph.py` | Adapter wraps in-process compiled graph | only matters if `target=langgraph` |

### 5.3 Dev / test

| Library | Purpose |
|---------|---------|
| **pytest** + **pytest-asyncio** | 44 tests covering adapters, deterministic, judges (mocked), triangulation, scoring, ingest, validation, gating, init |
| **ruff** | Linting (config in `pyproject.toml`) |
| **mypy** | Type checking (planned) |

### 5.4 Notable absences (intentional)

| Library | Why not |
|---------|---------|
| **tqdm** | Replaced by `rich.live.Live` + `rich.progress.Progress`. Rich gives multi-line panels, color, inline failure stream — tqdm is just a bar. |
| **click** (directly) | Used transitively via Typer; we don't write Click code directly. |
| **pandas** | Avoided — adds 30MB. CSV export uses stdlib `csv`. |
| **requests** | Replaced by `httpx` (async-native). |
| **logging.basicConfig** | Replaced by Rich's `RichHandler`. |

**Diagram tip**: a dependency map with concentric rings — `mdk-eval` core in the center, required libs in the inner ring, optional libs in the outer ring with dotted lines (the dotted lines mean "graceful no-op if missing"). Color the optional ring by extra-group (`[judges]`, `[langfuse]`, `[pdf]`, `[langgraph]`).

---

## 6. Adapter abstraction (suggested: ports-and-adapters / hexagonal diagram)

```
                    ┌─────────────────────────────────┐
                    │   AgentAdapter (ABC)            │
                    │   async run(input) → Result     │
                    └─────────────────────────────────┘
                                 △
              ┌──────────┬───────┴────────┬──────────────┐
              │          │                │              │
       ┌──────────┐ ┌────────┐ ┌──────────────────┐ ┌──────────┐ ┌────────────┐
       │  Mock    │ │  REST  │ │  OpenAICompat    │ │  Lyzr    │ │ LangGraph  │
       │ (CI)     │ │ (any)  │ │ (vLLM,Together)  │ │ (Studio) │ │ (in-proc)  │
       └──────────┘ └────────┘ └──────────────────┘ └──────────┘ └────────────┘
                                 │
                                 │ all return:
                                 ▼
                    ┌─────────────────────────────────┐
                    │   AdapterResult                 │
                    │   - ok: bool                    │
                    │   - output_text: str            │
                    │   - output_json: dict | None    │
                    │   - trace: Trace                │
                    │   - error: str | None           │
                    └─────────────────────────────────┘
```

`Trace` includes: `started_at`, `ended_at`, `latency_ms`, `retries`, `tool_calls[]`, `sub_agents[]`, `workflow_path[]`, `raw_request`, `raw_response`.

**Diagram tip**: classic hexagonal architecture — core (orchestrator + evaluators) in the center, adapter port on one side, all five concrete adapters plugging into it.

---

## 7. Provider abstraction (for triangulation) (suggested: same hexagonal pattern, smaller)

```
                  ┌────────────────────────────────────┐
                  │   MetricProvider (Protocol)        │
                  │   role: str                        │
                  │   async score(scenario, result)    │
                  │     → ProviderScore                │
                  └────────────────────────────────────┘
                                  △
              ┌───────────────────┼───────────────────┐
              │                   │                   │
   ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
   │ OurGrounding     │ │ Ragas            │ │ TruLens          │
   │ (per panel model)│ │ Faithfulness     │ │ Groundedness     │
   └──────────────────┘ └──────────────────┘ └──────────────────┘
                                  │
                                  ▼
                  ┌────────────────────────────────────┐
                  │   ProviderScore                    │
                  │   - score: float (0..1)            │
                  │   - abstained: bool                │
                  │   - reason: str | None             │
                  │   - raw: dict | None               │
                  └────────────────────────────────────┘
```

**Diagram tip**: place this beneath / beside the L5 triangulation band in the layer diagram.

---

## 8. Filesystem layout per run (suggested: tree diagram)

```
results/run_2026-05-05T12-34-56Z/
├── manifest.json                    ← provenance: hashes, model IDs, versions, timing
├── config.json                      ← effective RunConfig at run-time
├── dataset.snapshot.jsonl           ← canonical dataset snapshot (for replay)
├── report.json                      ← full RunReport (machine)
├── report.html                      ← Movate-branded report (primary deliverable)
├── report.pdf                       ← (if WeasyPrint installed)
├── evaluation_summary.json          ← stable contract for CI consumers
├── aggregate.json                   ← per-scenario aggregates only
├── scenarios.csv                    ← flat scenario-level table
└── scenarios/
    └── happy_path_qna/
        └── runs/
            ├── 0/
            │   ├── trace.json              ← full execution trace
            │   ├── deterministic.json      ← L1 check results
            │   ├── judges.json             ← L3 + L4 verdicts
            │   ├── triangulation.json      ← L5 (if grounding ran)
            │   └── eval.json               ← composite ScenarioRunResult
            ├── 1/  …
            └── 2/  …
```

**Diagram tip**: tree diagram with annotations on the side explaining what each file is consumed by (manifest by auditors, evaluation_summary by CI, report.html by stakeholders, etc.).

---

## 9. Trust mechanisms (suggested: callout overlays on the main diagram)

These aren't components — they're properties enforced across components. On a diagram, draw them as colored callouts pointing at the relevant area.

| Mechanism | Where it lives | What it proves |
|-----------|----------------|----------------|
| **SHA-256 dataset snapshot** | `storage/versioning.py`, `manifest.json` | Same dataset → same hash. Replay reproducible. |
| **Judge prompt SHA-256** | `manifest.judge_prompts_sha256` | Prompt versions are pinned per run. |
| **Tool versions captured** | `manifest.tool_versions` | Detect "DeepEval 1.0 vs 1.1 changed scoring." |
| **Multi-judge arbitration** | L4 / panel.py | No single LLM is authoritative. |
| **Triangulation spread** | L5 / triangulation.py | Surface disagreement between independent grounding sources. |
| **Confidence formula** | `scoring.compute_run_variance_and_confidence` | `1 − (norm_var + disagreement_penalty)` — variance hurts confidence even when mean is high. |
| **Hard gates** | `scoring.compute_run_scores` | Safety / critical fails override LLM-graded scores. |
| **Provenance on derived scenarios** | `Scenario.meta.derived_from` | `ingest`-generated scenarios trace back to source spec + constraint quote. |
| **Review gating** | `unverified` tag on derived scenarios | Auto-derived tests don't influence readiness until human approves. |

**Diagram tip**: use a callout pattern like "🔒 SHA-256 pin" pointing at the data flow from snapshot → manifest → replay; "⚖️ multi-judge" pointing at L3+L4; "🚥 hard gate" pointing at scoring.

---

## 10. CLI command map (suggested: command-tree / radial diagram)

```
                              mdk-eval
                                  │
        ┌──────┬───────┬──────────┼──────────┬───────┬──────┬───────┬─────────┐
        │      │       │          │          │       │      │       │         │
      init   doctor   run       report    compare  replay  ingest  export   version
        │      │       │          │          │       │      │       │
        │      │       ├ --dry-run│          │       ├ --trace-id   │
        │      │       ├ --gate-against      │       │      │       │
        │      │       ├ --no-judges         │       │      │       │
        │      │       ├ --runs N            │       │      │       │
        │      │       └ --target            │       │      │       │
        │      │                                              │
        │      ├ --dataset (validate loadable)                │
        │      └ --endpoint (probe)                           │
        │                                                     │
        ├ --target (mock|rest|openai_compat|lyzr|langgraph)   ├ --format promptfoo
        ├ --vscode (scaffold .vscode/)                        │
        ├ --cd-script (emit `cd <path>` for shell-eval)       │
        └ --no-prompt                                         │

        Top-level options (apply to every subcommand):
            --quiet (-q)   stdout one-liner only
            --verbose (-v) inline pass/fail + judge rationales
            --install-completion
```

**Diagram tip**: a radial tree (Mermaid `mindmap`) or vertical command tree. Useful for the "what can the user do" slide.

---

## 11. CLI usability features (suggested: callout list on the CLI box)

- Pre-flight summary panel before any work (target / dataset / plan / output / wall-clock estimate)
- Live multi-line status panel during run (counters + progress bar + ETA)
- Inline failure stream (`✗ <id> <reason>` printed *during* the run, above the live region)
- Verbose mode streams pass lines + judge rationales; quiet mode is CI-friendly stdout-only
- Next-steps panel with `file://` links + copy-pasteable commands
- Interactive run picker for `report` / `compare` / `replay` (numbered table; uses `rich.prompt.IntPrompt`)
- Shell completion via Typer's built-in (`mdk-eval --install-completion`)
- Config auto-discovery: looks for `mdk-eval.yaml` / `.mdk-eval.yaml` in cwd if `-c` omitted
- Up-front config validation: lyzr requires `agent_id`, REST requires `endpoint`, etc., with concrete fix hints
- Smart judge auto-disable: if API keys missing, drops affected providers (or fully disables) at preflight with a warning instead of failing mid-run
- `--dry-run` validates config + first scenario through deterministic checks only — no tokens spent
- `--gate-against BASELINE` exits code 2 on overall_score regression > threshold (CI-grade gate)
- `mdk-eval doctor` diagnoses environment (env vars + extras + dataset + endpoint reachability)
- `mdk-eval init my_proj --vscode` scaffolds a project with VS Code launch.json + settings.json prewired

---

## 12. Standardized scoring taxonomy (suggested: scorecard / 10-cell grid)

All scores 0–100. Render as a single 2×5 or 5×2 grid:

| Category | Source signal |
|----------|---------------|
| **Task Success** | weighted blend of correctness + completeness + tool_usage + workflow; gated by deterministic schema + crit failures |
| **Correctness** | L3 correctness judge + L2 deepeval g_eval (when present) |
| **Grounding** | L5 triangulation final score (if enabled) OR L3 grounding judge fallback |
| **Completeness** | L3 completeness judge + L2 deepeval task_completion + L1 required_fields |
| **Tool Usage** | L1 deterministic tool_usage check (weighted 2×) + L3 tool_usage judge |
| **Workflow Adherence** | L1 workflow_adherence check (authoritative) |
| **Consistency** | aggregate-time: 100 − weighted(variance, drift) across runs |
| **Latency** | L1 latency check |
| **Safety** | L3 safety judge (zero-tolerance: 0.95 threshold) |
| **UX/Tone** | L3 ux_tone judge |

**Composite weights** (in `scoring.compute_overall`):
```
task_success: 2.0   correctness: 1.5   grounding: 1.5   completeness: 1.2
tool_usage:   1.2   workflow:    1.0   consistency: 1.0  latency:    0.8
safety:       1.5   ux_tone:     0.6
```

**Status bands** (overall_score): `≥90 production_ready` · `80–89 pilot_ready` · `70–79 needs_improvement` · `<70 not_ready`. Hard overrides: any CRITICAL deterministic fail OR safety < 80 → forced `not_ready`.

**Diagram tip**: a 2×5 colored grid where each cell shows category name + the libraries/layers contributing to it. Above the grid: composite formula. Below: status-band swimlane.

---

## 13. Failure-mode taxonomy (suggested: classification chart)

Every failed scenario produces ≥1 `FailureFinding{class, reason, evidence, recommendation, severity}`.

```
        ┌─────────── deterministic-derived ───────────┐
        │                                             │
   schema_violation     workflow_drift     latency_issue   missing_step
   tool_misuse          safety_violation
        │                                             │
        └────────────── judge-derived ────────────────┘
                                │
        hallucination     premature_resolution     inconsistency
              ▲                                         ▲
              │                                         │
       L3 correctness/                            L5 triangulation
       grounding judges                           OR multi-run pass-rate
                                                   between 0 and 1
```

Each finding answers: **what** (class), **why** (reason), **how do we know** (evidence), **what to fix** (recommendation).

**Diagram tip**: a hierarchical chart — failure → 9 classes, each with its source layer annotated.

---

## 14. Ingest pipeline (suggested: pipeline / linear flow)

```
agent.json (Lyzr)
      │
      ▼
┌─────────────────────────────────────┐
│ auto_detect (signature match)       │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│ LyzrIngestor.ingest()               │
│   ├─ extract instructions           │
│   ├─ heuristic.extract():           │
│   │    - inputs                     │
│   │    - tools (bulleted parser)    │
│   │    - tool_sequence (numbered)   │
│   │    - output_schema (relaxed     │
│   │      JSON parser, angle-bracket │
│   │      placeholder handling)      │
│   │    - forbidden_phrases          │
│   │    - forbidden_fields (paren-   │
│   │      list near "do NOT emit")   │
│   │    - thresholds (€/$/value+kw)  │
│   │    - default_rules ("X required │
│   │      ... if missing, set to Y") │
│   │    - failure_behaviors          │
│   │    - SLO latency (regex p\d+)   │
│   └─ build_scenarios() →            │
│        9 derived scenarios:         │
│          happy / schema /           │
│          tool_sequence /            │
│          edge_threshold (at/above)/ │
│          default_rule / forbidden / │
│          failure / slo              │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│ EMITS:                              │
│  - configs/<name>.yaml              │
│      adapter pre-wired with         │
│      agent_id from JSON,            │
│      LYZR_API_KEY env reference     │
│  - datasets/<name>.jsonl            │
│      every scenario tagged          │
│      'unverified' + meta.derived_   │
│      from{source_path, sha256,      │
│      extractor, constraint_quote}   │
│  - agent_cards/<name>.md            │
│      stakeholder one-pager          │
└─────────────────────────────────────┘
               │
               ▼
        review by human
               │
               ▼
        remove 'unverified' tag → ready for run
```

**Diagram tip**: pipeline diagram with the heuristic extractor as the central transform. Show "every scenario tagged unverified" prominently as the trust gate.

---

## 15. External integration touch-points (suggested: side-channel call-outs)

```
            Optional, all degrade gracefully
                        ▲
                        │
                        │
   ┌────────┐    ┌──────┴──────┐    ┌────────┐
   │ Lyzr   │    │  mdk-eval   │    │ Lang-  │
   │ Studio │◀──▶│             │───▶│ fuse   │
   │ (REST) │    │             │    │        │
   └────────┘    │             │    └────────┘
                 │             │
   ┌────────┐    │             │    ┌────────┐
   │ OpenAI │◀──▶│             │───▶│Prompt- │
   │ API    │    │             │    │Foo (via│
   └────────┘    │             │    │export) │
                 │             │    └────────┘
   ┌────────┐    │             │
   │Anthropic│◀──▶            │    ┌────────┐
   │ API    │    │             │───▶│ VS     │
   └────────┘    │             │    │ Code   │
                 │             │    │(launch)│
   ┌────────┐    │             │    └────────┘
   │ vLLM / │◀──▶│             │
   │OpenAI- │    └─────────────┘
   │compat  │
   └────────┘
```

- **Lyzr**: agent execution (via Lyzr adapter); `LYZR_API_KEY` + `LYZR_USER_ID`
- **OpenAI / Anthropic**: judge calls + meta-judge; `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`
- **vLLM / Together / OpenAI-compat**: agent execution via OpenAICompatAdapter
- **Langfuse**: trace export (optional); `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
- **PromptFoo**: outbound only (via `mdk-eval export`); writes a `promptfoo.yaml` they can run independently
- **VS Code**: outbound launch via `code <project>` from `mdk-eval init --open`

---

## 16. Suggested diagram set (one per audience)

| Audience | Diagram | Sections to source |
|----------|---------|---------------------|
| **Exec / sales** | High-level layered block (5 layers + adapter + reports) | §2 |
| **Engineering** | Sequence diagram for one scenario | §3 |
| **Architecture review** | Hexagonal adapter + provider diagrams | §6, §7 |
| **AI risk reviewer** | Trust mechanism overlay | §9 |
| **Library inventory** (security review) | Dependency graph with optional/required rings | §5 |
| **Customer onboarding** | CLI command tree + ingest pipeline | §10, §14 |
| **Internal Movate brand deck** | 10-cell scorecard + status bands + composite formula | §12 |

---

## 17. Tool recommendations for actually drawing

| Tool | When to use |
|------|-------------|
| **Mermaid** (text in markdown) | Sequence diagrams, command trees. Renders inline in GitHub/Notion. Quickest path. |
| **Excalidraw** | Sketchy hand-drawn aesthetic; fast for whiteboard-style architecture diagrams. Good for §2 and §6. |
| **draw.io** (diagrams.net) | Free, structured, exports to PNG/SVG. Good for the dependency graph (§5). |
| **Lucidchart / Miro** | Corporate-acceptable; use if presenting to a customer. |
| **PlantUML** | If your team already maintains text-based diagrams. Good sequence diagrams. |
| **Figma** | If you want pixel-perfect for the brand deck. Heavyweight. |

For a single deck I'd pair: **Excalidraw for the high-level system view** + **Mermaid sequence diagrams in the appendix** + **Mermaid command-tree** for "what can users do."

---

## 18. Quick-reference: numbers to put in any diagram

- **5** evaluation layers (deterministic → DeepEval → judges → arbitration → triangulation)
- **5** adapter targets (mock / REST / openai_compat / Lyzr / LangGraph)
- **6** judge roles (correctness, completeness, tool_usage, ux_tone, safety, grounding-via-triangulation)
- **9** failure-mode classes
- **9** CLI commands (init, doctor, run, report, compare, replay, ingest, export, version)
- **10** scorecard categories (Task Success, Correctness, Grounding, Completeness, Tool Usage, Workflow Adherence, Consistency, Latency, Safety, UX/Tone)
- **4** status bands (production_ready / pilot_ready / needs_improvement / not_ready)
- **44** passing tests (Phase 1)
- **3** triangulation providers (our judge, Ragas, TruLens)
- **2** judge providers in default panel (OpenAI, Anthropic)
- **1** meta-judge (Anthropic Sonnet by default)

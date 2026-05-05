# Movate Agent Assurance (`mdk-eval`)

Multi-layer evaluation system for AI agent reliability and production readiness.

> Deterministic checks first. LLM judges second. Multi-judge arbitration when it matters. Versioned, reproducible, audit-ready.

## Install

```bash
pip install -e ".[judges,langfuse,pdf]"
```

Optional extras:
- `judges` — OpenAI, Anthropic, DeepEval (required for judge panel)
- `langfuse` — trace export to Langfuse
- `pdf` — PDF report rendering via WeasyPrint
- `langgraph` — LangGraph adapter

## CLI

```bash
mdk-eval run \
  --target rest \
  --endpoint https://api.example.com/agent/invoke \
  --dataset datasets/sample.jsonl \
  --runs 3 \
  --judges openai,anthropic \
  --output ./results

mdk-eval report --results ./results --format html,pdf,json,csv
mdk-eval compare --baseline ./results/run_2026-04-01 --candidate ./results/run_2026-05-04
mdk-eval replay --results ./results/run_2026-04-01 --target rest --endpoint https://...
```

Run with the built-in mock adapter (no network, no API keys):

```bash
mdk-eval run --target mock --dataset datasets/sample.jsonl --runs 2 --output ./results
```

## Configuration

Either CLI flags or a YAML config (`configs/sample.yaml`):

```bash
mdk-eval run --config configs/sample.yaml
```

Environment variables (loaded from `.env` if present):
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` — judges
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` — observability (optional)
- `LYZR_API_KEY`, `LYZR_USER_ID` — Lyzr adapter

## Architecture

```
mdk_eval/
  cli/                Typer entrypoints (run, report, compare, replay)
  adapters/           Mock, REST, OpenAI-compatible, Lyzr, LangGraph
  evaluators/
    deterministic/    schema, required fields, forbidden, tools, workflow, latency
    judges/           correctness, grounding, completeness, tool_usage, ux_tone, meta
    arbitration/      variance + meta-judge escalation
  runner/             multi-run orchestration, drift, scoring
  reporting/          generators (json, csv, html, pdf) + Movate-branded template
  storage/            run directories, versioning, hashing
  traces/             trace capture + optional Langfuse export
  utils/              io, logging, concurrency
```

Outputs land in `./results/run_<timestamp>/`:

```
results/run_2026-05-04T12-00-00/
  manifest.json        version-pinned config + dataset + judge prompt hashes
  scenarios/<id>/runs/<n>/{trace.json, deterministic.json, judges.json, eval.json}
  aggregate.json       scorecard, drift, arbitration stats
  scenarios.csv        flat scenario-level table
  report.html          Movate-branded report
  report.pdf           (if WeasyPrint installed)
```

## Design principles

1. Deterministic checks gate everything. A scenario that fails schema validation never reaches the judges.
2. No single-judge designs. Every LLM-graded metric runs through ≥2 models; high-variance verdicts escalate to a stronger meta-judge.
3. Multi-run by default. A pass/fail recommendation requires repeat-execution stability evidence.
4. Trace everything. Inputs, outputs, tool calls, sub-agent hops, latencies, retries, judge prompts and verdicts.
5. Version everything. Every artifact records SHA-256 hashes of dataset, prompts, model IDs, and config.
6. Reports are actionable. Each finding maps to a fix, an owner cue, and a severity.

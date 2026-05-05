# Sample report — `mdk-eval` worked example

This directory is a real, unmodified `mdk-eval` run output. It exists so reviewers, prospects, and auditors can see the actual artifact shape without having to install anything or spend judge tokens.

## What was run

```
mdk-eval run -c configs/sample-mock.yaml
```

- **Target:** `mock` adapter (deterministic-with-jitter, no network)
- **Dataset:** `datasets/sample.jsonl` — 6 reference scenarios covering happy path, schema strictness, tool requirements, hallucination traps, multi-hop completeness, and a strict latency budget
- **Runs per scenario:** 1
- **Judges enabled:** none — see "What's missing" below
- **Wall clock:** ~4 seconds

## What's in here

| File | Purpose |
|---|---|
| `report.html` | Movate-branded primary deliverable. Open in any browser. |
| `report.json` | Full machine-readable structure backing `report.html`. |
| `evaluation_summary.json` | Stable top-level contract for CI consumers (see PRD §6.6). Pinned by `schema_version` + `methodology_version`. |
| `manifest.json` | Reproducibility pinning: dataset SHA, prompt SHAs, tool versions, model IDs, thresholds (see PRD §6.9 / §11). |
| `aggregate.json` | Per-scenario aggregates only — useful for spreadsheet pivots. |
| `scenarios.csv` | Flat per-scenario table for non-engineers. |
| `dataset.snapshot.jsonl` | Canonical dataset snapshot. `mdk-eval replay --results .` re-executes against this snapshot. |
| `config.json` | Resolved config used by this run (after defaults + CLI flags). |
| `scenarios/<id>/runs/<n>/` | Per-run per-scenario raw artifacts: `trace.json`, `deterministic.json`, `judges.json`, `eval.json`. |

Open `report.html` in a browser to see the assembled output a customer would receive.

## What `evaluation_summary.json` looks like in this run

```json
{
  "schema_version": "1.0",
  "methodology_version": "1.0",
  "mdk_eval_version": "0.1.0",
  "overall_score": 60.98,
  "confidence": 1.0,
  "variance": 0.0,
  "status": "not_ready",
  "scorecard": { "task_success": 71.67, "correctness": 0.0, "...": "..." },
  "passing_scenarios": 0,
  "total_scenarios": 6,
  "manifest_sha256": "dc4ce4998e359fa78316bfa4a058ed56ec4ba675f3609cc671b27be74508481e"
}
```

The `manifest_sha256` field lets a downstream consumer verify that the manifest backing this summary hasn't been tampered with — re-run `sha256sum manifest.json` and compare.

## What's missing from this example (and why)

This example was generated **without** the LLM judge panel because no API keys were configured at run time. Concretely:

- `correctness`, `grounding`, and `ux_tone` categories show 0 — these are judge-only categories. With a real run, they'd be populated by the multi-role panel.
- `judge_panel` is empty in every scenario's `eval.json`.
- `judges_enabled: []` in `manifest.json`.
- The "Judge Panel & Arbitration" report section is absent.

To produce a complete report (all 10 categories scored, judge verdicts visible), set `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` in `.env` and re-run with `configs/sample.yaml` instead of `configs/sample-mock.yaml`. Expect ~$0.05–$0.20 in judge tokens for this 6-scenario × 1-run dataset (see PRD §7.7 for the cost model).

## What this example is good for

- **Auditors** — verify the manifest, dataset snapshot, and per-scenario artifact shape match what the PRD §6.9, §6.11, and §11 describe.
- **Engineers** — copy the JSON shapes when wiring downstream tooling against `evaluation_summary.json` or `report.json`.
- **Sales / delivery** — open `report.html` to demo the Movate-branded deliverable a customer receives at the end of an engagement.

## What this example is NOT

- A passing evaluation. The mock adapter is deliberately imperfect (it produces schema violations, missing tools, latency overruns). The point is to show what failure analysis looks like, not to claim the agent is production-ready.
- A representative score. With judges off, three categories collapse to 0, dragging the composite into `not_ready`. A judge-enabled run on a real well-tuned agent typically scores in `pilot_ready` or `production_ready` range.

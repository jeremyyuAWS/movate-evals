# Agent Card — brief-ingestion-agent [symrise-brief-to-submission] v1.00

_Generated 2026-05-05 02:48 UTC by mdk-eval ingest._

**Role**: Brief Ingestion
**Version**: 3
**Model**: OpenAI / `gpt-4o-mini` (temp 0)

**Description**: Parses SAP CX brief, validates completeness, applies €50K budget gate, classifies brief type.

## Mandate

Read a SAP CX brief object by ID, extract structured attributes, validate
completeness against the mandatory field checklist, apply the €50K budget
gate, and emit a Brief conforming to brief.schema.json.

## Declared interface

- **Inputs**: workflow_id, sap_brief_id
- **Tools** (4): fetch_sap_brief, assess_brief_completeness, apply_budget_gate, escalate
- **Tool sequence**: fetch_sap_brief → assess_brief_completeness → apply_budget_gate
- **Output keys** (14): brief_id, customer_id, customer_name, category, region, format, budget, timeline, brief_type, olfactive_keywords, completeness, gate_decision, received_at, source
- **SLO latency**: 2000 ms (p95)
- **Forbidden phrases**: ```, ```json
- **Forbidden fields**: customer_segment, duplicate_check, economic_feasibility, olfactive_keywords_raw, sla_tier

## Derived test suite

**9 scenarios** generated heuristically. All marked `unverified` until you review.

| Category | Count |
|---|---:|
| happy | 1 |
| schema | 1 |
| tool_sequence | 1 |
| edge_threshold | 1 |
| edge_threshold_above | 1 |
| default_rule | 1 |
| forbidden | 1 |
| failure | 1 |
| slo | 1 |

⚠ **6 scenarios** are tagged `requires_fixture` — they need realistic input data before they exercise the live agent meaningfully.

## Review checklist

Before promoting derived scenarios to verified status:

1. Open `datasets/<name>.jsonl` and skim each scenario.
2. For scenarios tagged `requires_fixture`, fill in real input payloads.
3. Confirm `expected_schema` and `forbidden_phrases` match agent intent (the heuristic extractor is conservative but not infallible).
4. Remove the `unverified` tag (or use `mdk-eval ingest --approve <dataset>`).
5. Run `mdk-eval run --config configs/<name>.yaml --runs 3`.

## Provenance

- Source: `examples/lyzr_brief_ingestion_agent.json`
- SHA-256: `06b6d12fcc918601…`
- Extractor: `heuristic` (no LLM)

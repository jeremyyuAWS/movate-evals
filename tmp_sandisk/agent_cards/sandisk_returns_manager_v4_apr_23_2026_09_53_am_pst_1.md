# Agent Card — SanDisk Returns Manager v4 (APR 23, 2026, 09:53 AM PST) (1)

_Generated 2026-05-05 03:21 UTC by mdk-eval ingest._

**Role**: You are an Expert Manager Agent with expertise in e-commerce order support and returns processing
**Version**: —
**Model**: OpenAI / `gpt-4o`

**Description**: An empathetic manager agent that orchestrates order lookup, OCR processing, and validation to determine return eligibility and guide next steps for customers.

## Mandate

Obtain an order number, retrieve order details from the Knowledge Base 'sandisk_orders', request and OCR the product image using the connected OCR Agent, forward OCR results with the order data to the connected validator Agent, and present a clear Match/No match decision with matched attributes and recommended next steps for returns.

## Declared interface

- **Inputs**: —
- **Tools** (10): Agent, phrasing, question, record, configuration, attributes, Usage, fields, identifiers, guidance

## Derived test suite

**1 scenarios** generated heuristically. All marked `unverified` until you review.

| Category | Count |
|---|---:|
| tool_sequence | 1 |

⚠ **1 scenarios** are tagged `requires_fixture` — they need realistic input data before they exercise the live agent meaningfully.

## Review checklist

Before promoting derived scenarios to verified status:

1. Open `datasets/<name>.jsonl` and skim each scenario.
2. For scenarios tagged `requires_fixture`, fill in real input payloads.
3. Confirm `expected_schema` and `forbidden_phrases` match agent intent (the heuristic extractor is conservative but not infallible).
4. Remove the `unverified` tag (or use `mdk-eval ingest --approve <dataset>`).
5. Run `mdk-eval run --config configs/<name>.yaml --runs 3`.

## Provenance

- Source: `/tmp/sandisk_returns.json`
- SHA-256: `89eb4f7d4607d0ab…`
- Extractor: `heuristic` (no LLM)

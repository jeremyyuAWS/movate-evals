# Scenario Synthesizer + Agent Card — detailed spec

How `mdk-eval ingest` turns one agent definition into 9 scenarios + a stakeholder card.

Examples throughout reference the Symrise `brief-ingestion-agent` Lyzr JSON.

---

## Part 1 — Scenario Synthesizer

The synthesizer is **conditional**: each of the 9 scenarios is only emitted when the heuristic extractor found the trigger condition in the agent's instructions. If your agent doesn't declare an SLO, no SLO scenario is emitted.

For every scenario:
- **Tagged** `unverified` (gating tag — won't influence readiness until human removes it)
- **Tagged** `derived:<category>` (so reviewers can filter)
- **Tagged** `requires_fixture` if it needs realistic input data (currently 6 of 9)
- **Carries provenance** in `meta.derived_from` — `{source_path, source_sha256, extractor: "heuristic", constraint_quote, category}`

---

### Scenario 1 — `<name>__happy_path`

**Tag set**: `unverified` · `derived:happy` · `requires_fixture`
**Severity**: HIGH

**What it tests**
The agent's normal path. Given valid, complete input, does it succeed end-to-end — call the right tools in the right order, produce a schema-valid output, stay under SLO?

**Trigger condition**
Always emitted if the heuristic extracted at least one input from the `# Inputs` section.

**Scenario shape**
```json
{
  "id": "brief_ingestion_agent__happy_path",
  "tags": ["unverified", "derived:happy", "requires_fixture"],
  "severity": "high",
  "description": "Valid input, all required fields present. Expect normal completion.",
  "input": {"workflow_id": "<fill in workflow_id>", "sap_brief_id": "<fill in sap_brief_id>"},
  "context": [],
  "expected_schema": { /* the extracted JSON schema */ },
  "required_fields": ["brief_id", "customer_id", "category", "region", ...],
  "expected_tools": [
    {"name": "fetch_sap_brief", "required": true},
    {"name": "assess_brief_completeness", "required": true},
    {"name": "apply_budget_gate", "required": true},
    {"name": "escalate", "required": true}
  ],
  "workflow": {
    "must_visit": [],
    "ordered_subsequence": ["fetch_sap_brief", "assess_brief_completeness", "apply_budget_gate", "escalate"]
  },
  "latency_budget_ms": 2000,
  "rubric": {"pass_threshold": 0.75},
  "meta": {
    "derived_from": {
      "source_path": "examples/lyzr_brief_ingestion_agent.json",
      "source_sha256": "06b6d12fcc918601…",
      "extractor": "heuristic",
      "constraint_quote": "agent should succeed on a complete input",
      "category": "happy"
    },
    "requires_fixture": true
  }
}
```

**For the Symrise example, a reviewer should fill in**
- A real `sap_brief_id` that exists in the SAP CX system
- A `workflow_id` (UUID or trace key)
- The `context` array with any retrieved-doc snippets the agent will see (if applicable)
- Optionally narrow `expected_tools` (e.g., `escalate` is only required on certain branches; the heuristic can't tell — mark it `required: false` or remove)

**What "passing" looks like**
- All four tools called in declared order
- Output validates against `expected_schema`
- All `required_fields` present and non-empty
- Latency ≤ 2000 ms

---

### Scenario 2 — `<name>__schema_conformance`

**Tag set**: `unverified` · `derived:schema`
**Severity**: HIGH
**No fixture required** — runs as-is.

**What it tests**
The agent must return JSON conforming to the declared schema, with all required keys present and **no markdown fences or commentary** wrapping it.

**Trigger condition**
Emitted when extractor found either an `expected_schema` (parsed JSON-ish block) OR `required_fields` (top-level keys) OR `forbidden_phrases` ("no markdown fences").

**Scenario shape**
```json
{
  "id": "brief_ingestion_agent__schema_conformance",
  "tags": ["unverified", "derived:schema"],
  "severity": "high",
  "description": "Output must conform to the declared JSON schema and contain no markdown fences.",
  "input": {"workflow_id": "<fill in>", "sap_brief_id": "<fill in>"},
  "expected_schema": { /* full extracted schema */ },
  "required_fields": ["brief_id", "customer_id", ...],
  "forbidden_phrases": ["```", "```json"]
}
```

**Why this scenario is special**
This is one of the most reliable tests. It's deterministic (L1 schema check + L1 forbidden phrases) — no LLM judges involved. If the agent ever emits "Here's the brief:\n```json\n{...}\n```" instead of raw JSON, this catches it.

**For the Symrise example**
Schema test will flag if:
- Agent forgets `received_at`, `source`, `gate_decision`, etc.
- Agent wraps output in ` ```json ... ``` `
- Agent emits prose like "I've parsed the brief. Here it is:" before the JSON

**Reviewer should**
- Confirm the extracted schema is correct (the heuristic types everything as `string` for placeholders like `<raw.id>` — you may want to relax types for fields like `volume_kg`)

---

### Scenario 3 — `<name>__tool_sequence`

**Tag set**: `unverified` · `derived:tool_sequence` · `requires_fixture`
**Severity**: HIGH

**What it tests**
Tools must be called **in the declared order**, with every required tool called at least once.

**Trigger condition**
Emitted when extractor found a numbered tool sequence under `# Required tool sequence`.

**Scenario shape**
```json
{
  "id": "brief_ingestion_agent__tool_sequence",
  "tags": ["unverified", "derived:tool_sequence", "requires_fixture"],
  "severity": "high",
  "description": "Required tool sequence: fetch_sap_brief → assess_brief_completeness → apply_budget_gate → escalate.",
  "input": {"workflow_id": "<fill in>", "sap_brief_id": "<fill in>"},
  "expected_tools": [
    {"name": "fetch_sap_brief", "required": true},
    {"name": "assess_brief_completeness", "required": true},
    {"name": "apply_budget_gate", "required": true},
    {"name": "escalate", "required": true}
  ],
  "workflow": {
    "must_visit": [...],
    "ordered_subsequence": ["fetch_sap_brief", "assess_brief_completeness", "apply_budget_gate", "escalate"]
  },
  "latency_budget_ms": 2000
}
```

**How L1 evaluates this**
- `tool_usage` check: every `required: true` tool must appear in `trace.tool_calls`
- `workflow_adherence` check: `ordered_subsequence` must be a subsequence (not necessarily contiguous) of `trace.workflow_path`

**The Symrise nuance the heuristic CAN'T see**
Steps 4 and 5 of the original instructions are conditional ("If gate_decision == 'route_manual_above_threshold': escalate(...)"). The heuristic emits `escalate` as required, but it should only be required on those branches. **Reviewer must fix this**: either mark `escalate` as `required: false` and add separate scenarios per branch, OR split this scenario into "happy_path_proceed" / "escalation_above_threshold" / "escalation_incomplete".

This is exactly why HITL exists.

---

### Scenario 4 — `<name>__edge_threshold_at_<amount>`

**Tag set**: `unverified` · `derived:edge_threshold` · `requires_fixture`
**Severity**: HIGH

**What it tests**
Boundary case: a value exactly at the declared threshold should be treated as the **inclusive** side (per agent's declaration).

**Trigger condition**
Emitted when extractor found a threshold via the regex `(€|$|EUR|USD)?\s?\d+\s?[KkMm]?` near keywords like "threshold", "budget", "gate", "above", "below", "exactly".

For Symrise, the extractor caught `€50K` and `50,000`, plus the explicit rule "Amounts at exactly 50,000 are below threshold (proceed)."

**Scenario shape**
```json
{
  "id": "brief_ingestion_agent__edge_threshold_at_50000",
  "tags": ["unverified", "derived:edge_threshold", "requires_fixture"],
  "severity": "high",
  "description": "Boundary case: value at EUR50000 should be treated as below threshold.",
  "input": {"workflow_id": "<fill in>", "sap_brief_id": "<fill in>"},
  "meta": {
    "derived_from": {
      "constraint_quote": "Amounts at exactly 50,000 are below threshold (proceed).",
      ...
    }
  }
}
```

**For the Symrise example**
The heuristic CANNOT inject a synthetic SAP brief with `budget.amount_eur = 50000`. The agent fetches the brief from SAP, so the test data lives in SAP. Reviewer must:
- Stage a real SAP brief with `budget.amount_eur = 50000`
- Use that brief's ID in `input.sap_brief_id`
- Add an `expected_output` or extra deterministic check asserting `gate_decision == "proceed"`

OR — write a sandbox/mock SAP that the agent calls during evaluation, with the threshold-edge brief already loaded.

**Why this matters**
This is the most error-prone class of bugs in business-rule agents. "Above 50K → manual route" is easy; "exactly at 50K → which side?" is where shipping bugs come from.

---

### Scenario 5 — `<name>__edge_threshold_above_<amount>`

**Tag set**: `unverified` · `derived:edge_threshold_above` · `requires_fixture`
**Severity**: HIGH

**What it tests**
The opposite of #4: a value just above the threshold must trigger the manual-route path.

**Trigger condition**
Same as #4 — emitted as a pair so both sides of the boundary are tested.

**Scenario shape**
```json
{
  "id": "brief_ingestion_agent__edge_threshold_above_50000",
  "tags": ["unverified", "derived:edge_threshold_above", "requires_fixture"],
  "severity": "high",
  "description": "Above threshold: value > EUR50000 must trigger the manual-route path.",
  "input": {"workflow_id": "<fill in>", "sap_brief_id": "<fill in>"}
}
```

**For the Symrise example, a reviewer should add**
- A staged SAP brief with `budget.amount_eur = 50001` (or 75000)
- An assertion that `gate_decision == "route_manual_above_threshold"`
- An assertion that `escalate()` was called with `reason_code = "policy_exception"` and `priority = "high"`

The reviewer can encode the assertions either as:
- Adding `forbidden_phrases: ["proceed", "reject_incomplete"]` to catch wrong gate decisions
- Adding `expected_tools: [{"name": "escalate", "required": true, "args_contains": {"reason_code": "policy_exception"}}]`

---

### Scenario 6 — `<name>__default_rule_<field>`

**Tag set**: `unverified` · `derived:default_rule` · `requires_fixture`
**Severity**: HIGH

**What it tests**
Compound behavior: missing field is defaulted to a declared value AND simultaneously listed in a "missing fields" tracker. (Two things must happen, not one.)

**Trigger condition**
Emitted when extractor matched either pattern:
- `if X (is) missing, set [it] to Y` (Pattern A)
- `X is required ... if missing, set [it] to Y` (Pattern B — the Symrise case)

For Symrise, the constraint quote is: `"Region is required for downstream agents; if missing, set it to 'global'"`.

**Scenario shape**
```json
{
  "id": "brief_ingestion_agent__default_rule_region",
  "tags": ["unverified", "derived:default_rule", "requires_fixture"],
  "severity": "high",
  "description": "When 'region' is missing, agent must default to 'global' AND list it under missing_fields.",
  "input": {"workflow_id": "<fill in>", "sap_brief_id": "<fill in>"},
  "required_fields": ["brief_id", "customer_id", ...],
  "meta": {
    "derived_from": {
      "constraint_quote": "Region is required for downstream agents; if missing, set it to 'global'",
      ...
    }
  }
}
```

**For the Symrise example**
The reviewer should:
- Stage a SAP brief WITHOUT a `region` field
- Add explicit assertions:
  - Output `region == "global"` (the default)
  - Output `completeness.missing_fields` contains `"region"`
- These can be encoded as `forbidden_phrases: []` plus a custom JSONPath in `required_fields` (e.g., `completeness.missing_fields.0`) — this is one of the cases where a reviewer might add a small custom assertion that the heuristic can't synthesize

**Why this scenario is uniquely tricky**
The agent has to do TWO things: substitute the default AND log the missingness. Most LLMs naturally do one or the other, not both. This is the kind of compound rule that quietly breaks.

---

### Scenario 7 — `<name>__forbidden_fields`

**Tag set**: `unverified` · `derived:forbidden`
**Severity**: HIGH
**No fixture required.**

**What it tests**
Output must not contain enrichment-field names that downstream stages will add (or any other fields the agent was told not to emit).

**Trigger condition**
Emitted when extractor matched either:
- Inline form: `do NOT emit X, Y, Z`
- Antecedent form: `enrichment fields (X, Y, Z) ... do NOT emit them`

For Symrise, the antecedent form caught: `customer_segment, sla_tier, economic_feasibility, duplicate_check, olfactive_keywords_raw`.

**Scenario shape**
```json
{
  "id": "brief_ingestion_agent__forbidden_fields",
  "tags": ["unverified", "derived:forbidden"],
  "severity": "high",
  "description": "Agent output must not contain any forbidden / enrichment field names.",
  "input": {"workflow_id": "<fill in>", "sap_brief_id": "<fill in>"},
  "forbidden_phrases": [
    "customer_segment", "duplicate_check", "economic_feasibility",
    "olfactive_keywords_raw", "sla_tier"
  ]
}
```

**How L1 evaluates this**
The `forbidden_phrases` check does a case-insensitive substring match on the output text. If any phrase appears, the check fails (HIGH severity).

**Reviewer caveats**
- The check is substring-based. If a forbidden field happens to also be a substring of a legitimate value (unlikely here, but possible), you'll get false positives. Reviewer can drop those entries.
- Doesn't catch the field if the agent wraps it: `"sla_tier_v2"` would be flagged as containing `sla_tier`. Usually fine; occasionally over-strict.

---

### Scenario 8 — `<name>__failure_handling`

**Tag set**: `unverified` · `derived:failure` · `requires_fixture`
**Severity**: **CRITICAL** (only scenario at this level)

**What it tests**
When a declared failure trigger fires (here: SAP fetch raises), the agent must produce the declared fallback behavior (here: empty Brief with `gate_decision: reject_incomplete` + escalate with `policy_exception`).

**Trigger condition**
Emitted when extractor found a `## Failure behavior` section.

**Scenario shape**
```json
{
  "id": "brief_ingestion_agent__failure_handling",
  "tags": ["unverified", "derived:failure", "requires_fixture"],
  "severity": "critical",
  "description": "Failure path: declared failure trigger must produce the declared fallback output.",
  "input": {"workflow_id": "<fill in>", "sap_brief_id": "<fill in>"},
  "meta": {
    "derived_from": {
      "constraint_quote": "If `fetch_sap_brief` raises, emit a Brief with empty fields and `gate_decision: reject_incomplete`. Escalate with reason `policy_exception`.",
      ...
    }
  }
}
```

**Why CRITICAL**
Failure-handling bugs are silent. An agent that "kind of works" when SAP is up but does the wrong thing when SAP is down can sit in production for weeks before someone notices. The `CRITICAL` severity means any failure on this scenario forces status = `not_ready`, regardless of how well other scenarios pass.

**For the Symrise example, a reviewer needs to set up the failure**
Three options:
1. **Sandbox SAP** — point the agent at a SAP test endpoint that returns 500 for a specific brief_id
2. **Use the mock adapter** — switch this one scenario's adapter to `mock` with the `_trigger: "failure"` flag (the mock adapter supports failure injection — see `adapters/mock.py`)
3. **Network layer mock** — use `httpx.MockTransport` in a test fixture

Option 2 is what we use in the smoke tests for our own sample dataset.

---

### Scenario 9 — `<name>__slo_latency`

**Tag set**: `unverified` · `derived:slo`
**Severity**: MEDIUM
**No fixture required** — uses a minimal "ping" input.

**What it tests**
The agent stays within its declared SLO across runs. This is a multi-run test by default — the orchestrator runs every scenario N times, so latency variance is captured automatically.

**Trigger condition**
Emitted when extractor found an SLO declaration matching `p\d+\s+latency.{0,8}\d+\s*(ms|s|sec|min)`.

For Symrise: `p95 latency ≤ 2s` → `latency_budget_ms = 2000`.

**Scenario shape**
```json
{
  "id": "brief_ingestion_agent__slo_latency",
  "tags": ["unverified", "derived:slo"],
  "severity": "medium",
  "description": "P95 latency must stay within 2000 ms.",
  "input": {"workflow_id": "<fill in>", "sap_brief_id": "<fill in>"},
  "latency_budget_ms": 2000
}
```

**How it interacts with hard gates**
Latency is severity MEDIUM by default, so a single breach doesn't force `not_ready`. BUT the scoring layer's hard gate kicks in if a scenario tagged HIGH or CRITICAL severity also breaches its budget — that caps the score at 65. So if you want SLO failures to be more punishing, manually bump this scenario's severity.

**Reviewer should consider**
- Whether 2 seconds is the right budget for the test — if the agent calls SAP, network latency can spike. Maybe set 2500 ms in the scenario but keep p95=2s as the production target.
- Bumping severity to HIGH if SLO breach is a release-blocker for this customer.

---

### What the synthesizer DOESN'T emit (gaps to know about)

| Concern | Why not emitted |
|---------|-----------------|
| **Adversarial / prompt-injection scenarios** | Heuristic doesn't infer attack vectors. Use the (planned) reference adversarial pack instead. |
| **Schema-violation traps** (e.g. inject malformed input) | Same — adversarial in nature. |
| **Real-data fixtures** | We refuse to fabricate test data without human review (intentional). |
| **Multi-turn conversational scenarios** | Out of scope for Phase 1. Each scenario is single-turn. |
| **Scenarios per workflow branch** | Heuristic emits one `tool_sequence` scenario; conditional branches require human-authored scenarios. |
| **Cross-agent integration scenarios** | This agent is one of N in a workflow. Testing the whole workflow needs a separate dataset. |

The HITL gate exists for exactly these gaps — derived scenarios are a starting point, not a complete suite.

---

## Part 2 — Agent Card

The agent card is a stakeholder-friendly one-pager auto-generated alongside the scenarios. It lives at `agent_cards/<name>.md`.

**Audience**: anyone who needs to understand an agent without reading its instructions:
- Customer AI risk reviewer deciding whether to approve the agent
- Movate delivery PM tracking which agents have eval coverage
- Engineer reviewing the derived scenarios before promoting them
- Customer eng lead during a handoff

### What it contains, section by section

The card is generated by `mdk_eval/ingest/agent_card.py:build_agent_card()`. Every section is data-driven from the agent JSON + extracted spec.

#### Section 1 — Header

```
# Agent Card — brief-ingestion-agent [symrise-brief-to-submission] v1.00

_Generated 2026-05-05 01:49 UTC by mdk-eval ingest._

**Role**: Brief Ingestion
**Version**: 3
**Model**: OpenAI / `gpt-4o-mini` (temp 0)

**Description**: Parses SAP CX brief, validates completeness, applies €50K budget gate, classifies brief type.
```

| Field | Source |
|-------|--------|
| Title | `agent.name` |
| Generated timestamp | runtime UTC |
| Role | `agent.agent_role` |
| Version | `agent.version` |
| Model | `agent.provider_id` + `agent.model` + `agent.temperature` |
| Description | `agent.description` |

**Why temp matters**: temperature > 0 means consistency tests will fail more often. Card surfaces this so reviewers don't waste time debugging "non-deterministic" output that's actually configured non-determinism.

---

#### Section 2 — Mandate

```
## Mandate

Read a SAP CX brief object by ID, extract structured attributes, validate
completeness against the mandatory field checklist, apply the €50K budget
gate, and emit a Brief conforming to brief.schema.json.
```

**Source**: `agent.agent_goal` field (verbatim).

**Why it exists**: stakeholders skim this to understand "what does this agent actually do" without reading the prompt. If `agent_goal` is empty in the source JSON, this section is omitted.

---

#### Section 3 — Declared interface

```
## Declared interface

- **Inputs**: workflow_id, sap_brief_id
- **Tools** (4): fetch_sap_brief, assess_brief_completeness, apply_budget_gate, escalate
- **Tool sequence**: fetch_sap_brief → assess_brief_completeness → apply_budget_gate
- **Output keys** (14): brief_id, customer_id, customer_name, category, region, format, budget, timeline, brief_type, olfactive_keywords, completeness, gate_decision, received_at, source
- **SLO latency**: 2000 ms (p95)
- **Forbidden phrases**: ```, ```json
- **Forbidden fields**: customer_segment, duplicate_check, economic_feasibility, olfactive_keywords_raw, sla_tier
```

| Bullet | Source from extractor |
|--------|------------------------|
| Inputs | `spec.inputs` |
| Tools | `spec.tools` (count + list) |
| Tool sequence | `spec.tool_sequence` (joined with `→`; only shown if non-empty) |
| Output keys | `spec.output_keys` (count + list) |
| SLO latency | `spec.slo_latency_ms` (with `(p95)` annotation) |
| Forbidden phrases | `spec.forbidden_phrases` |
| Forbidden fields | `spec.forbidden_fields` |

**Stakeholder use**: this is the contract. If a customer's eng lead disagrees with anything here ("we don't actually require `escalate` on the happy path"), the agent's instructions need to be tightened — that's the point.

---

#### Section 4 — Derived test suite

```
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

⚠ **6 scenarios** are tagged `requires_fixture` — they need realistic input data
before they exercise the live agent meaningfully.
```

| Element | Source |
|---------|--------|
| Total count | `len(scenarios)` |
| Per-category table | derived from `derived:<category>` tags on each scenario |
| Fixture warning | count of scenarios where `meta.requires_fixture is True` |

**Why the table format**: stakeholders can see at a glance "we have 6 categories of test, but 6 of 9 need real data." That sets expectations: this is a starting point, not a finished suite.

---

#### Section 5 — Review checklist

```
## Review checklist

Before promoting derived scenarios to verified status:

1. Open `datasets/<name>.jsonl` and skim each scenario.
2. For scenarios tagged `requires_fixture`, fill in real input payloads.
3. Confirm `expected_schema` and `forbidden_phrases` match agent intent
   (the heuristic extractor is conservative but not infallible).
4. Remove the `unverified` tag (or use `mdk-eval ingest --approve <dataset>`).
5. Run `mdk-eval run --config configs/<name>.yaml --runs 3`.
```

**Source**: hardcoded — these are the project's review conventions.

**Why hardcoded**: every customer engagement follows the same review flow. The card serves as the runbook.

**Note**: step 4 references `mdk-eval ingest --approve` which is on the Phase 2 roadmap (P2.6). Today, reviewers manually edit the dataset to remove the `unverified` tag.

---

#### Section 6 — Provenance

```
## Provenance

- Source: `examples/lyzr_brief_ingestion_agent.json`
- SHA-256: `06b6d12fcc918601…`
- Extractor: `heuristic` (no LLM)
```

| Element | Source |
|---------|--------|
| Source | the input file path passed to `mdk-eval ingest` |
| SHA-256 | first 16 chars of the file's full SHA-256 |
| Extractor | always "heuristic (no LLM)" today; will be `heuristic + llm` when `--synthesize` ships in Phase 2 |

**Why the explicit "no LLM" note**: AI risk reviewers care about this. The card is making a positive claim: nothing in this card or these scenarios was hallucinated by an LLM.

---

### What the agent card does NOT contain (and why)

| Section | Why not |
|---------|---------|
| Full agent_instructions | Too long; would defeat the "one-pager" purpose. The provenance link points to the source file. |
| Tool implementations | Out of scope — we don't have access to tool source. Customer maintains tool docs separately. |
| Past evaluation results | Decoupled. Past results live in `results/`. The card is about the agent definition, not its history. |
| Performance budget vs actual | This is what the report shows after a run. The card is pre-eval. |
| Risk register | Same — emerges from running the agent, not from reading its definition. |

---

### Where the agent card sits in the workflow

```
[Lyzr JSON]
    │
    ▼
[mdk-eval ingest]
    │
    ├─► configs/<name>.yaml         ← engineer uses this with `mdk-eval run`
    │
    ├─► datasets/<name>.jsonl       ← reviewer edits this (HITL gate)
    │
    └─► agent_cards/<name>.md       ← stakeholder reads this (review + handoff)
                                      ▲
                                      │ also useful in the customer's
                                      │ AI risk submission package
```

**Tip for diagrams**: draw the agent card as a small icon (📄) hanging off the right side of the Lyzr Ingestor box, not part of the main eval flow. It's a *deliverable*, not a runtime artifact.

---

## How the synthesizer + card work together

The card promises something specific (X scenarios across Y categories with Z requiring fixtures). The dataset delivers exactly that. A reviewer reading the card knows:

1. **What was extracted** (declared interface)
2. **What was derived** (test suite breakdown)
3. **What needs work** (the fixture warning + review checklist)
4. **Where it came from** (provenance)

That's a complete handoff package for a delivery PM dropping this on an engineer's desk: "Review this card, fill the fixtures, run the suite."

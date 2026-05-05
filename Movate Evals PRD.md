Here’s a **clean, high-level build brief** you can hand to Claude Code. It’s structured to drive implementation without over-specifying.

---

# 🧠 Movate Agent Assurance — Build Brief

## 🎯 Objective

Build a **Python CLI + reporting system** that evaluates live AI agent endpoints (e.g., Lyzr, LangGraph) and produces a **branded, enterprise-grade evaluation report**.

This is not just an eval tool — it is a **reliability and production-readiness system for AI agents**.

---

# 🧱 Core System Components

## 1. CLI Interface

Build a Python CLI using:

* Typer

### Required Commands

```bash
mdk-eval run
mdk-eval report
mdk-eval compare
mdk-eval replay
```

### Example

```bash
mdk-eval run \
  --target lyzr \
  --agent-id AGENT_ID \
  --endpoint URL \
  --dataset evals.jsonl \
  --runs 3 \
  --judges openai,anthropic \
  --output ./results
```

---

## 2. Adapter Layer (Critical)

Create a unified interface to evaluate **any agent system**.

### Base Interface

```python
class AgentAdapter:
    async def run(self, input: dict) -> dict:
        pass
```

### Implementations

* LyzrAdapter (REST endpoint)
* LangGraphAdapter (local graph execution)
* Generic REST Adapter

---

## 3. Dataset / Scenario Engine

Support:

* JSONL / YAML scenario files

Each scenario includes:

* input prompt
* expected outcomes
* required tools
* forbidden outputs
* scoring rubric
* tags / severity

---

## 4. Evaluation Engine

### A. Deterministic Validators (priority)

Implement:

* schema validation
* required fields check
* forbidden claims
* tool usage validation
* workflow/path validation
* latency / retry checks

These are **non-LLM checks and must always run first**.

---

### B. LLM Judge System (multi-role)

Use:

* DeepEval for metric scaffolding
* OpenAI + Anthropic for judge models

Define separate judges:

* Correctness Judge
* Grounding / Hallucination Judge
* Completeness Judge
* Tool Usage Judge
* UX / Tone Judge

Do NOT use a single judge prompt.

---

### C. Multi-LLM Arbitration

If judges disagree:

* calculate variance
* escalate to a stronger “meta-judge”
* produce final score + explanation

---

### D. Multi-Run Stability

Support:

```bash
--runs N
```

Track:

* score variance
* output drift
* consistency

---

## 5. Trace & Observability

Capture:

* full request/response
* tool calls
* sub-agent calls
* workflow path
* latency + retries

Integrate with:

* Langfuse (preferred)

---

## 6. Scoring System

Compute:

* per-metric scores
* weighted final score
* confidence score (based on variance + disagreement)
* pass / fail status

---

# 📊 Report Generator (Key Product Feature)

Generate a **Movate-branded report**.

## Output Formats

* HTML (primary)
* PDF (client-ready)
* JSON (machine-readable)
* CSV (scenario-level results)
* Optional: PPTX summary

---

## Report Sections (Required)

### 1. Executive Summary

* overall score
* risk level
* readiness (Not Ready / Pilot / Production)
* key findings
* recommendation

---

### 2. Agent / Workflow Overview

* architecture
* tools / subagents
* models used

---

### 3. Scorecard

* category-level scores:

  * correctness
  * grounding
  * tool usage
  * workflow adherence
  * latency
  * etc.

---

### 4. Methodology

* dataset size
* runs
* judge models
* eval framework
* thresholds

---

### 5. Deterministic Validation Results

* pass/fail checks
* schema, tools, constraints

---

### 6. Judge Panel Results

* scores per judge
* agreement %
* arbitration results

---

### 7. Scenario-Level Results

* each test case
* expected vs actual
* failure reason
* severity

---

### 8. Failure Mode Analysis

Group failures:

* hallucination
* tool misuse
* workflow errors
* latency issues

---

### 9. Regression Analysis

Compare baseline vs candidate:

* score deltas
* improvements / regressions

---

### 10. Risk Register

* identified risks
* severity
* mitigation

---

### 11. Production Readiness Recommendation

Clear decision:

* Not Ready / Pilot / Production

---

### 12. Appendix

* dataset
* configs
* judge prompts
* trace references

---

# 🎨 Branding Requirements

* Movate logo + client logo
* clean enterprise design (minimal, no fluff)
* strong typography
* risk color coding:

  * green = pass
  * yellow = watch
  * red = fail
* confidentiality footer

---

# 🧩 File / Repo Structure

```bash
mdk-eval/
  cli/
  adapters/
  datasets/
  evaluators/
    deterministic/
    judges/
    arbitration/
  runner/
  reporting/
    templates/
    generators/
  storage/
  utils/
```

---

# 🚀 Implementation Phases

## Phase 1 (MVP)

* CLI: run + report
* Lyzr + REST adapter
* JSONL datasets
* deterministic validators
* 2–3 LLM judges
* HTML + JSON report

---

## Phase 2

* multi-run variance
* arbitration layer
* Langfuse integration
* regression comparison

---

## Phase 3

* PPT/PDF export
* scenario generation
* CI/CD integration
* agent certification scoring

---

# ⚠️ Key Design Principles

1. Deterministic checks > LLM judges
2. Separate evaluation layers (don’t mix concerns)
3. Multi-LLM only when needed (avoid cost explosion)
4. Trace everything (auditability)
5. Version everything (datasets, prompts, models)
6. Reports must be actionable, not just metrics

---

# 🧠 One-Line Summary

> Build a CLI-driven, multi-layer evaluation system that scores agent reliability, analyzes failures, and produces a branded Movate report that clearly answers whether an AI agent is production-ready.

---

If you want next step, I can generate:

* exact repo scaffold with starter code
* or a Claude Code prompt that will build 80% of this automatically

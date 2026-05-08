# Movate Agent Assurance — 10-Minute Demo

**Audience:** Customers, executives, internal stakeholders. Anyone seeing the platform for the first time.

**Goal:** In 10 minutes, walk through the full evaluation arc — from a fresh agent definition to a regression-proof test case — using a real customer scenario.

**The story:** We evaluate the **Movate FAQ Assistant** (a real Lyzr agent that sits on the Movate website) against a 10-scenario test pack. The agent does well on standard FAQs, slips on adversarial probes, and ends with a customer-grade scorecard + a one-click HITL fix.

---

## Two flavors

| Flavor | Cost | Reproducibility | When to use |
|---|---|---|---|
| **Mock** (deterministic) | $0 | Identical every time | Training videos, customer-on-laptop demos, offline |
| **Live** (real Lyzr agent) | ~$0.30 per run | Real numbers, may vary slightly run-to-run | Authentic story for serious customers |

Recommend showing **mock first** (clean walkthrough, no surprises), then **live** (real numbers) if the audience wants depth.

---

## Pre-flight (before the audience arrives)

```bash
# 1. Verify environment is healthy
mdk-eval doctor

# 2. Confirm the demo dataset loads cleanly
.venv/bin/python -c "from mdk_eval.scenarios import load_scenarios; \
  print(len(load_scenarios('datasets/demo_movate_faq.jsonl')), 'scenarios loaded')"

# 3. (Live flavor only) Verify Lyzr connectivity
echo "$LYZR_API_KEY" | head -c 12   # must print 'sk-default-' or your key prefix
echo "$ANTHROPIC_API_KEY" | head -c 12  # must print 'sk-ant-'
echo "$OPENAI_API_KEY" | head -c 8   # must print 'sk-'
```

If any check fails — fix before continuing. The demo is the moment audiences notice the seams.

---

## The 10-minute arc

### Minute 1 — "What this is" (the framing)

> "This is the platform we use at Movate to evaluate AI agents before they ship. We're going to take a real Movate FAQ agent — the one on movate.com — and put it through ten realistic test cases that mimic what real visitors would ask. Some are friendly questions; some are attempts to break the agent. The platform tells us whether to ship, where to fix first, and gives us a regression suite for the future."

### Minute 2 — Show the scenarios (transparency)

```bash
# Quick sanity-check the scenario types
.venv/bin/python -c "
from mdk_eval.scenarios import load_scenarios
ss = load_scenarios('datasets/demo_movate_faq.jsonl')
print(f'{len(ss)} test cases — story arc:')
for s in ss:
    cats = [t for t in s.tags if t in ('happy', 'edge', 'adversarial', 'safety', 'honesty')]
    print(f'  {s.severity.value:8} {s.id:42} {cats}')
"
```

Talking points:
- **4 happy-path:** standard customer FAQ questions (e.g., "What does Movate do?")
- **1 edge:** ambiguous phrasing the agent has to interpret
- **1 honesty:** out-of-scope finance question — agent must refuse to fabricate
- **3 adversarial / safety:** prompt injection, system-prompt extraction, medical advice trap
- **1 critical:** indirect injection via a typo-squat URL

> "Every scenario carries an explicit assertion — what the agent must NOT say, or what it must include. The platform scores against the assertion, then judges the response with two LLMs from different vendors for redundancy."

### Minutes 3–6 — Run the evaluation

```bash
# Mock version (no cost, ~5 seconds)
mdk-eval run --config configs/demo_mock.yaml

# OR live version (~$0.30, ~3-5 minutes against the real agent)
mdk-eval run --config configs/demo_lyzr.yaml
```

Talking points while it runs:
- **Pre-flight summary** — shows the dataset SHA, judges enabled, methodology version. Provenance is locked-in BEFORE we touch the agent.
- **Live status panel** — per-scenario pass/fail streaming. Failures are surfaced immediately with the failure class.
- **Headline** — when it finishes: `84.7 [82.1, 87.3] — Pilot Ready (8/10 passing) · confidence 0.93`. The bracketed range is a 95% bootstrap CI — honest sample-size uncertainty, not a single point estimate.

### Minutes 6–8 — The report (where the value lands)

```bash
# Open the customer-facing dashboard
open results/$(ls -t results | head -1)/dashboard.html

# Or the engineering-facing one
open results/$(ls -t results | head -1)/report.html
```

What to point at, in order:

1. **Headline + status pill** at the top — the one-line answer to "should we ship?"
2. **Scorecard radar / bars** — 10 categories. Show the pattern: high on grounding + safety, lower on tone where the agent slipped on the medical-advice scenario.
3. **Top failure clusters** — dashboard renders these in business language ("The agent says it's done before fully answering" — not "premature_resolution"). Show the affected scenarios. Show the recommended fix.
4. **Risk register** — exec-ready risk statements. "HIGH risk — likelihood medium — mitigation: …" Standard vocabulary.
5. **Per-scenario detail** (click one) — actual agent output, what the judges said, what the deterministic checks caught. This is the auditable layer.

> "Notice every number on this page can be traced back to an evidence trail. The agent's output is captured, the judges' rationales are captured, the deterministic checks cite their evidence. If an auditor asks 'how did you get this number?', we can show them — every piece."

### Minute 8 — HITL closure (turn a failure into a regression test)

> "When the platform finds a real failure, the team's job is to fix the agent. But we also want this exact failure to be a permanent test case — so the next version of the agent has to pass it before we ship. One command does that."

```bash
# Find the lowest-scoring scenario, then promote it
mdk-eval promote-failure \
  --results results/$(ls -t results | head -1) \
  --scenario demo_009_safety_medical_advice \
  --target datasets/regression.jsonl \
  --note "Demo run — refusal must hold across versions" \
  --show-tightening
```

Talking points:
- **Tightening:** the platform takes the actual failure mode (the forbidden phrase the agent said) and adds it to the new scenario's constraints. The next version of the agent must actively defend against the exact failure, not just the original test.
- **Provenance recorded:** the new scenario carries `derived_from.from_run_dir`, `from_scenario_id`, `from_run_index`, `final_score`, `failure_classes`, `note`. Full audit trail.
- **Append to dataset:** the new scenario lands in `datasets/regression.jsonl` and runs in every future eval. Failures become durable tests in one command.

### Minute 9 — A/B comparison (regression detection)

> "Now imagine the team makes a change to the agent's prompt. We want to know whether the change actually helped — without re-reading 100 scenarios manually."

```bash
mdk-eval ab \
  --config-a configs/demo_mock.yaml \
  --config-b configs/demo_mock.yaml \
  --label-a "v1 prompt" \
  --label-b "v2 prompt"
```

(For a customer-facing demo, the two configs would point at different prompt versions; here we use the same config twice to demonstrate the diff format.)

Talking points:
- **Per-scenario diff:** REGRESSION / improvement / stable for each test case
- **Composite delta:** the headline number — did this prompt change actually help?
- **Markdown output:** the entire diff is a markdown report you can paste into a PR or send to a stakeholder

### Minute 10 — Closing

> "We've gone from a fresh agent definition to a scored, fully-audited evaluation — including regression-proofing the failures we found — in ten minutes. This is the workflow the team uses every time we update an agent: ingest, run, review, promote-failure, A/B. The dashboard surfaces all of it for non-engineers; the CLI is there for engineers. The methodology is open and the math is reproducible."

If the audience asks **"how confident are you in the score?"** — open the methodology page (per [BOLT_SCORING_PRD.md](BOLT_SCORING_PRD.md)) and walk them through the weighted-mean formula. Always show the math; never hide it.

---

## What this demo proves (for the customer)

1. **Real evaluation against real agents** — not synthetic benchmarks. The Movate FAQ agent on the Movate website was scored end-to-end.
2. **Defensible scoring** — every number cites evidence; every assertion carries an audit trail.
3. **HITL closure** — failures become regression tests in one command. The platform learns from every issue it surfaces.
4. **Cost discipline** — judges cached, runs traceable to dollars, every action shows estimated cost before commit.
5. **Methodology transparency** — open formulas, open prompts, public failure-class taxonomy. No black box.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `LYZR_API_KEY` not found | `export LYZR_API_KEY="sk-default-..."` (rotate yours; never paste in chat) |
| `ANTHROPIC_API_KEY` invalid | Re-fetch from Azure: `az containerapp secret show -n mdk-eval-web -g mdk-eval-rg --secret-name anthropic-api-key --query value -o tsv` |
| Live run hangs | Check `fly logs` (legacy) or `az containerapp logs show --name mdk-eval-web --resource-group mdk-eval-rg --follow` |
| Mock run shows all-passing | Expected — the mock adapter answers based on `expected_output`. To see realistic failures, use the live config or the `examples/sample_report` dataset which has intentional triggers |
| Demo dataset has only 10 scenarios | Designed for 10-min walkthrough. For full coverage, use `datasets/adversarial.jsonl` (30 scenarios) or your own |

---

## Files referenced

- `datasets/demo_movate_faq.jsonl` — the 10-scenario customer demo dataset
- `configs/demo_lyzr.yaml` — config for live Lyzr agent (~$0.30/run)
- `configs/demo_mock.yaml` — mock-adapter config ($0, deterministic)
- `BOLT_SCORING_PRD.md` — methodology page reference
- `BOLT_BACKEND_PRD.md` — production backend contract
- `BOLT_ANALYTICS_PRD.md` — analytics surface for Bolt's dashboard
- `BOLT_SCORING_PROFILES_PRD.md` — agent-archetype scoring presets

---

## Recovery script (if a live demo goes sideways)

```bash
# 1. Check liveness
curl -s https://mdk-eval-web.whitefield-b83c207d.eastus.azurecontainerapps.io/healthz

# 2. Check readiness (deps healthy?)
curl -s https://mdk-eval-web.whitefield-b83c207d.eastus.azurecontainerapps.io/readyz | jq

# 3. Show the audience the most recent successful run instead
ls -t results/ | head -3 | xargs -I {} echo "results/{}"
open results/$(ls -t results | head -1)/dashboard.html
```

If all else fails, **show a saved run**. The `results/` directory has every run on the laptop. Pick a clean one, narrate it.

---

## After the demo

The audience will ask "where do I sign up." The current answer:

1. They send us their agent's JSON definition (Lyzr export, OpenAI Assistant export, or any equivalent).
2. We onboard them in the Bolt-hosted dashboard at `https://mdk-eval-web.whitefield-b83c207d.eastus.azurecontainerapps.io`.
3. They see their first eval scorecard within 10 minutes.

Provide the four Bolt PRDs ([BOLT_BACKEND_PRD](BOLT_BACKEND_PRD.md), [BOLT_ANALYTICS_PRD](BOLT_ANALYTICS_PRD.md), [BOLT_SCORING_PRD](BOLT_SCORING_PRD.md), [BOLT_SCORING_PROFILES_PRD](BOLT_SCORING_PROFILES_PRD.md)) for any questions about API integration.

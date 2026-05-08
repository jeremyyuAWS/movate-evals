# Movate Agent Assurance — Applied AI Internal Review

**Audience:** Movate Applied AI team
**Format:** Source markdown for a 17-slide + appendix presentation (~30 min talk)
**Author:** Jeremy Yu
**Date:** 2026-05-07

> This is the source-of-truth markdown for the deck. Each slide section has the on-slide content, speaker notes (what to say), and any visual / diagram callouts. Feed into your PPT generator of choice (Gamma, marp, pandoc) or hand to a designer.

---

## Slide 1 — Cover

### On slide

- **Movate Agent Assurance**
- Audit-grade evaluation. Certified by Movate. For production AI agents.
- Applied AI internal review · 2026-05-07
- Jeremy Yu

### Speaker notes

What I'm going to walk through in the next 30 minutes is the platform we've built — Movate Agent Assurance — and the service shape behind it. The audience for this is you, the Applied AI team. You're going to deliver this on customer engagements, so I'm going to give you the technical depth to evaluate it honestly, and the commercial framing so you can take it to your accounts. We'll close with a concrete ask: book a 30-minute walkthrough so you can see the live system end-to-end.

### Visuals

Movate logo · simple cover layout · the strap-line "Audit-grade evaluation. Certified by Movate." reads bold below the title.

---

## Slide 2 — The problem

### On slide

- Production AI agents are shipping faster than enterprises can verify them
- The failures that hurt are the ones nobody tested for: hallucinations on KB-backed answers, prompt-injection compliance, incomplete answers to multi-part questions, wrong-tool selection on edge cases
- "We ran a benchmark once" doesn't survive an audit, doesn't catch regressions, and doesn't give a CTO the language to defend the agent in a board review

### Speaker notes

Three patterns we keep seeing in customer conversations. One — agents launch with vibes-based QA: a few engineers ran a few prompts in a chat window and called it shipped. Two — when something breaks in production, there's no instrumentation to say *what* broke or whether it's been broken for a week. Three — the moment an enterprise customer asks "is this safe? prove it," the team has nothing to show. This isn't an "AI safety" abstraction. It's a delivery quality problem. Agents are software; software ships with quality processes; agents currently don't.

---

## Slide 3 — The audit gap

### On slide

- Browsers, APIs, data pipelines all have a defensible quality layer. AI agents don't.
- "Defensible AI quality" = three things working together:
  1. **A scorecard** that an exec can read in 60 seconds
  2. **Evidence** beneath every number — not opinions, sources
  3. **Reproducibility** — same inputs always produce the same scoring
- Today, customers either build this themselves (slow, expensive) or skip it (risky)

### Speaker notes

This is the gap we're closing. Note what *defensible* means here. Defensible doesn't mean "we got a good score." It means "if a regulator, an auditor, or a board member asks how we got this number, we can show them." That requires three things and most platforms have one or zero of them. Open-source eval frameworks like RAGAS or DeepEval give you metrics but not provenance. Commercial benchmarks give you a score but not an audit trail. We give you all three, and we package it as a Movate-delivered service. That's the differentiator.

---

## Slide 4 — Movate Agent Assurance

### On slide

- One sentence: **a multi-layer evaluation platform that produces a defensible, auditable score for any AI agent — delivered by Movate Applied AI on customer engagements**
- Five-stage workflow:
  - **Ingest** — read the agent definition (Lyzr, LangGraph, OpenAI, custom REST), extract test scenarios automatically + LLM-augmented
  - **Score** — multi-judge LLM panel + deterministic checks + DeepEval signals, blended via a weighted 10-category scorecard
  - **Run** — execute the test suite against the live agent, collect traces, compute Wilson + bootstrap CIs
  - **Diagnose** — Agent Doctor (Rx) generates a 3-tier prescription; Business Report turns the data into a customer narrative
  - **Close the loop** — promote-failure converts production failures into permanent regression tests in one command

### Speaker notes

Read the one-sentence definition aloud. The workflow diagram is the mental model — five stages, each independently auditable. Most evaluation platforms stop after stage two. We go all the way to stage five because that's what closes the gap between *we ran a benchmark* and *we have a defensible quality program*. The closed loop is what makes this an ongoing service rather than a one-time report. Each customer engagement walks through this loop the first time, then we run it on a recurring cadence.

### Visuals

Five-box horizontal flow diagram: `Ingest → Score → Run → Diagnose → Close-the-loop`. Each box has a one-line label underneath ("from agent definition to test scenarios", "10-category scorecard", "live execution + CIs", "executive narrative + Rx", "failures become regression tests"). A single arrow loops from "Close-the-loop" back to "Run" to emphasize the recurring cadence.

---

## Slide 5 — A real run in 90 seconds

### On slide

- **Customer agent:** Movate FAQ Assistant (Lyzr) — the live agent on movate.com
- **Score:** **87.19 / 100 — Pilot Ready** (passed 12 of 13 scenarios)
- **Cost:** $0.31 · 1m36s end-to-end · 167 judge calls
- Top wins: latency (100), consistency (100), safety (99.2)
- Top losses: ux_tone (69.4), completeness (71.4), wrong-tool selection on 4 scenarios
- Output artifacts: report.json, methodology.md, agent_doctor.md, executive business report — all auto-emitted

### Speaker notes

This is a real run from this week against the live Movate FAQ agent. Not a sandbox. Not a demo dataset. The real production agent. The numbers: 87 overall, pilot-ready band, 12 of 13 passing. The full run took 96 seconds and cost 31 cents. The agent did well on speed and safety; it slipped on tone and completeness. We can show you all of that in the dashboard. The point of this slide isn't the score — it's that this is a one-command operation now. An engagement starts here and we can produce this artifact for any customer agent within a day of getting their definition.

### Visuals

Screenshot of the dashboard's run-detail view OR a clean scorecard mock with these numbers. If using a screenshot, redact anything customer-confidential. The cost / runtime / scenarios numbers are the credibility moment — make them readable from the back of the room.

---

## Slide 6 — The 10-category scorecard

### On slide

| Category | What it measures | Default weight |
|---|---|---:|
| **task_success** | Did the agent actually solve the user's task? | 2.0 |
| **correctness** | Was the answer factually right? | 1.5 |
| **grounding** | Did the agent stay anchored to provided context (no hallucination)? | 1.5 |
| **safety** | Did the agent avoid PII leaks / disallowed content? | 1.5 |
| **completeness** | Did the agent address the entire request? | 1.2 |
| **tool_usage** | Right tool, right arguments? | 1.2 |
| **workflow_adherence** | Did the agent follow the expected path? | 1.0 |
| **consistency** | Stable across multiple runs of the same scenario? | 1.0 |
| **latency** | Fast enough vs SLO? | 0.8 |
| **ux_tone** | On-brand, appropriate format? | 0.6 |

- Composite = `Σ(weight × category) / Σ(weight)`
- Weights are configurable per agent archetype (FAQ vs internal tool vs compliance bot)

### Speaker notes

Ten categories, all on the same 0–100 scale. The composite is a weighted mean — math you can verify with a calculator. This is the scoreboard a non-engineer reads. The weights are global defaults, but they're configurable per agent archetype — a customer-facing FAQ agent weights safety and tone higher than an internal data extractor. Nothing about the math is hidden. Auditors love this slide. The methodology document we auto-generate per run reproduces this table with the actual scores plugged in, so a reviewer can verify the composite by hand.

### Visuals

The table at the top, then a horizontal stacked-bar chart underneath showing the per-category scores from the FAQ run on slide 5. Color-grade by band: red <60, yellow 60-79, green 80+. Pass-line at 80 marked with a dashed vertical.

---

## Slide 7 — Three layers of evidence

### On slide

Every score combines three independent signal sources:

1. **Deterministic checks** (8 of them, code-based, $0)
   - schema, required_fields, forbidden_phrases, tool_usage, workflow_adherence, latency, retries, adapter_ok
2. **Multi-judge LLM panel** (5 roles, 2 models per role, vendor-redundant)
   - correctness, grounding, completeness, tool_usage, safety, ux_tone
   - When judges disagree past a threshold → meta-judge arbitrates
3. **DeepEval third-party metrics**
   - g_eval, hallucination, task_completion, answer_relevance — feeds another vote into the meta-judge

### Speaker notes

Why three layers? Because no single layer is trustworthy on its own. Deterministic checks are reliable but blind to nuance — they can't tell you if the agent's tone is wrong. LLM judges are nuanced but unreliable solo — they hallucinate. Third-party metrics like DeepEval are useful but vendor-locked. The redundancy is what makes the score defensible. When all three layers agree, we trust it. When they disagree, the meta-judge resolves it on the record. Every layer's contribution is auditable: every deterministic check has structured evidence; every judge has a written rationale; every meta-judge call is logged with its own reasoning.

### Visuals

Three-column flow: Deterministic checks (left) · LLM panel with two models per role (middle) · DeepEval (right) — all flowing into a single "composite score" node at the bottom. Show the meta-judge as a dashed-line escalation path between the panel models when their variance exceeds the threshold.

---

## Slide 8 — Defensibility

### On slide

Every number cites evidence. The mechanisms that make the score audit-grade:

- **Wilson 95% CI** on aggregate pass-rate — well-behaved at boundaries (0/N, N/N, small N)
- **Bootstrap 95% CI** on the overall composite — 2000 resamples, seeded for reproducibility
- **Multi-judge arbitration** — variance threshold + meta-judge override, every judge rationale captured
- **Honest abstention** — judges may return `{"abstain": true, "reason": "..."}` when they genuinely can't tell. Beats noisy 0.5
- **Auto-emitted methodology.md** — per run, captures formula + judges + thresholds + abstentions + dataset/config SHAs
- **Prompt SHAs** — every judge prompt versioned, tied into the run manifest
- **Scenario provenance** — every test case carries `derived_from` showing extractor, source SHA, the exact agent-definition substring it tests

### Speaker notes

This is the slide that wins regulated-industry deals. Read each bullet aloud and don't apologize for the technical density — Applied AI engineers will reward depth, and CTO buyers want to see this list. Confidence intervals matter because a 3-scenario run shouldn't give you the same confidence as a 30-scenario run. Honest abstention matters because forcing a judge to score an ambiguous case at 0.5 corrupts the data. Methodology.md matters because an auditor doesn't trust a number; they trust a document. All of this is live in production today. We can hand a methodology.md to any auditor and they can reproduce the score from it.

### Visuals

Three callouts: a snippet of the bootstrap CI bracket on the headline ("Overall: 87.19 [85.4, 89.0]"), a snippet of methodology.md showing the formula + weights + judge models, and a small flowchart of the multi-judge arbitration path with the meta-judge fallback.

---

## Slide 9 — Closing the loop: promote-failure

### On slide

- Failures become regression tests in one command:
  ```
  mdk-eval promote-failure --results <run> --scenario <id> --target regression.jsonl
  ```
- The new test is **tightened** against the specific failure mode the agent exhibited:
  - Forbidden phrases the agent said → added to `forbidden_phrases`
  - Required fields it omitted → added to `required_fields`
  - Claims it hallucinated → captured as `forbidden_claims`
- Provenance recorded: `derived_from = {from_run, from_scenario, captured_at, failure_classes, note}`
- The agent must now actively defend against the exact mistake on every future run

### Speaker notes

This is the differentiator that nobody else has. Every other eval platform stops after the report. We close the loop. When you find a real failure on the FAQ agent — let's say it hallucinated when asked about pricing — one command turns that into a permanent test. The next version of the agent must pass it before we ship. Over a year of customer engagement, you accumulate a regression dataset that's genuinely customer-specific and gets better every run. This is the answer to "how do you know the agent isn't getting worse." We have a test suite that grows with every issue we find.

### Visuals

Before/after JSON snippet: original scenario on the left (with empty `forbidden_phrases`), tightened version on the right (with the agent's actual failure phrase added). Annotation arrow: "the agent said this; now the test forbids it."

---

## Slide 10 — Multi-agent systems

### On slide

- Real customer agents don't ship as singletons. They ship as **manager + sub-agents** (Lyzr `managed_agents`, LangGraph supervisors)
- We treat the system as a first-class entity:
  - **Auto-detection**: ingesting a manager surfaces its `managed_agents` so the user uploads each sub-agent next, pre-linked
  - **`parent_agent_id`** on every sub-agent — explicit relationship, persisted in Postgres
  - **Composite system score** — weighted (manager 1.5×, each sub-agent 1.0×) with **worst-of status** (a system can't be more production-ready than its weakest member)
  - Un-evaluated members cap status at `needs_improvement` — every component must clear the bar
- Endpoint: `GET /api/agent-systems/{root_slug}` returns the manager + all linked children + composite

### Speaker notes

This came from a real customer architecture — SanDisk's returns flow has a Returns Manager that delegates to an OCR agent and a Product Validator. Three independent agents, one customer outcome. If you only test the manager you can't isolate which sub-agent is failing. If you only test the sub-agents you miss orchestration bugs. We test all three independently, plus the composite. The "worst-of status" rule is on purpose — a system is only as production-ready as its weakest link, and customers tend to forget that until something breaks.

### Visuals

Tree diagram: Returns Manager at top, OCR Agent and Product Validator branches below. Each node shows its individual score; a "system composite" node above shows the weighted result with the worst-of status applied.

---

## Slide 11 — Cost discipline

### On slide

- Every action costs LLM tokens — we expose that to the user
- **Cost preview before commit**: every run modal shows `estimated_cost_usd` upper bound before the user clicks
- **Judge cache** — same `(prompt, model, input)` hash → response served from cache at $0
- **Real cost numbers** for a 13-scenario run with full judge panel: ~$0.31. At 50 agents × monthly re-evaluation: ~$15/month per customer
- Cost is visible everywhere: per-run card, sparkline tooltip, portfolio "spent this week" tile
- `cost_usd: null` (pre-instrumentation) renders as `—`; `0` is real (judges-off run)

### Speaker notes

This is the slide that closes commercial conversations. Customers ask "what does it cost to evaluate our agent" and we have a precise answer. Thirty-one cents per full run on a 13-scenario suite. Even at scale, the recurring cost is small relative to the value of finding a regression before customers do. The judge cache is what makes this economical — re-running the same scenario after a small prompt tweak is free for the cached portion. Every cost number on the dashboard cites the actual `web_run_job.cost_usd` field, populated post-completion. Numbers we show are what was actually spent.

### Visuals

Screenshot of the cost-preview modal showing "≤ $0.31" before run commit. Smaller callout: a portfolio tile showing "$1.85 spent this week."

---

## Slide 12 — Scoring profiles + Agent Doctor

### On slide

**Scoring profiles** — agent-archetype tuning

- Five curated presets: `faq_external`, `internal_tool`, `data_extractor`, `manager_orchestrator`, `compliance_bot`
- Each profile overrides weights, gates, thresholds for its archetype
- LLM advisor: reads the agent definition, recommends a preset with reasoning
- Endpoint: `POST /api/scoring-profiles/recommend`

**Agent Doctor (Rx)** — 3-tier LLM-narrated diagnostic per run

- **Tier 1**: Executive summary + headline action (single line, 'do this first')
- **Tier 2**: Top 3 prescriptions, each cited (which scenarios, which findings) and confidence-tagged
- **Tier 3**: Specific suggested changes — engineer-actionable edits (system prompt addition, tool description tweak, temperature adjustment)
- Endpoint: `GET /api/runs/{id}/doctor`

### Speaker notes

Two related capabilities. Scoring profiles let you tune *how* the agent is scored to match what the agent is *for* — a compliance bot's safety threshold is 0.99, a data extractor doesn't care about ux_tone. The LLM advisor reads the agent's role and instructions and picks the right preset, with reasoning you can show the customer. Agent Doctor goes the other direction — given a run, it produces three tiers of output: a paragraph for the delivery manager, three cited prescriptions for the engineer, and concrete edits for the next pull request. Both are LLM-backed with deterministic-template fallback so the system always returns useful output even when the LLM is unavailable.

### Visuals

Side-by-side: profile picker UI mock (radio buttons for the 5 presets, with "Recommended" pre-selected) on the left; agent_doctor.md sample on the right showing executive_summary + one prescription with cited_scenarios.

---

## Slide 13 — What's live in production

### On slide

- **5 customer agents** evaluated across 4 engagements (Movate FAQ, SanDisk Returns, Telco Support, HR Onboarding)
- **15 evaluation runs** completed; latest cost-tracked at $0.31
- **477 unit + integration tests passing** in CI; ruff-clean codebase
- **Backend:** Azure Container Apps · `git_sha: f74cc46-rx` · App Insights instrumented · Log Analytics workspace
- **Database:** Supabase Postgres · 7 migrations applied · pgmq durable job queue
- **Frontend:** Bolt-rendered dashboard wired to all endpoints (in active build-out)
- **Endpoint surface:** 30+ documented in `BOLT_API_REFERENCE.md` (1,180 lines)

### Speaker notes

Specifics. Five real customer agents, fifteen real runs, four hundred seventy-seven tests. This isn't a research project. Production deployment is on Azure with full observability — App Insights captures every HTTP request, every judge call, every cost event. The frontend is being built by Bolt against a stable contract documented in five PRDs that I'll show you. Everything you see today is reproducible: pull the run directory, you have config, dataset SHA, judge prompts SHAs, methodology.md, every artifact. If anyone in the audience wants to verify, I can hand you a run dir and you can reproduce the math.

### Visuals

A simple status-board layout: production state on the left (image tag, revision, test count, agent count, run count); architecture sketch on the right (Azure Container Apps box → Supabase box; App Insights side-channel; Bolt frontend separate).

---

## Slide 14 — The Bolt dashboard

### On slide

What end-users actually see:

- **Portfolio overview** — all agents, status pills, sparklines, cost rollup, recency alerts, leaderboard preview (one round-trip endpoint)
- **Agent detail** — score trend, per-category small-multiples, run history table with cost column
- **Run detail** — scorecard radar, failure clusters, business report (LLM-narrated executive summary), agent-doctor prescriptions, methodology.md inline
- **Test authoring** — Mix Designer (preset-tuned), scenario review with approve/reject/regenerate, bulk JSONL import
- **Multi-agent system view** — manager + sub-agents tree with composite score

Everything wired against documented endpoints. Five-PRD contract covers it.

### Speaker notes

The dashboard is where the customer experiences the platform. I want to show this live in the walkthrough rather than rely on screenshots, because it's been actively iterating. The structure is: portfolio at-a-glance is the landing page, click into an agent for trends, click into a run for the full report including the LLM-generated business narrative and the doctor's prescriptions. The test authoring flow uses our LLM extractor to propose scenarios from the agent definition; the reviewer approves, edits, or regenerates each one. The whole loop happens in the dashboard.

### Visuals

Four-up dashboard collage: Portfolio (KPI tiles + agent grid), Agent detail (sparkline + history table), Run detail (scorecard + business report excerpt), Scenario review card. If high-quality screenshots aren't ready, use clean wireframes with the same layout.

---

## Slide 15 — How Applied AI delivers this

### On slide

You don't sell the platform. You sell **Movate-certified audit grade**.

**Pilot engagement (30 days, fixed scope)**
- Customer ships us their agent definition (Lyzr / OpenAI / LangGraph / custom)
- We ingest, generate the evaluation pack, run it, deliver:
  - Executive scorecard (the business report PDF)
  - Engineering scorecard (per-scenario detail + agent doctor prescriptions)
  - Methodology document (auditable artifact)
  - Regression dataset (the customer's permanent test suite)

**Recurring engagement**
- Monthly re-evaluation
- Drift detection — alert when score moves > N points run-over-run
- New scenarios from real production failures (promote-failure as part of the engagement)
- Quarterly deep-review with prescription review and roadmap

### Speaker notes

This is your slide. The platform is the substrate; you deliver the service. The pilot is fixed-scope and sized so a customer can say yes — one agent, 30 days, four named artifacts. The recurring engagement is where the revenue lives — monthly re-evaluation, drift watch, regression suite that grows. The audit pack you deliver is something the customer can hand to their compliance team or their board. That's what justifies the price. We aren't competing on benchmark numbers. We're competing on "Movate certified this agent in production, here's the documented methodology."

### Visuals

Two columns. Left: "Pilot" — small box with four named deliverables. Right: "Recurring" — larger box with the four ongoing components. A bridge labeled "Pilot converts to recurring at month 2" between them.

---

## Slide 16 — Roadmap (next 3-6 months)

### On slide

Near-term, in priority order:

1. **Bolt UI completion** — finish wiring the four PRDs into the dashboard (in flight)
2. **Scoring-profile persistence** — chosen profile saved per scenario_set so re-runs auto-apply
3. **Methodology versioning + public methodology page** — public-facing reproducibility for buyers' audit teams
4. **Movate certification badge program** — agents that pass our criteria get a Movate-stamped certification mark to display
5. **Continuous monitoring** — cron-driven re-runs with drift alerting (replaces "scheduled monthly" with "runs whenever agent definition changes")
6. **Calibration suite** — Cohen's κ between LLM judges and human labels per engagement; published per-customer

What this unlocks: "Movate-certified" becomes a recognizable mark in customer conversations.

### Speaker notes

Six items, prioritized. The first three are reliability work — making what we have polished and shippable. Items four through six are where this becomes a real category. The certification badge is the long game — get to the point where customers say "we want the Movate mark on our customer-facing agents" and we have the platform to deliver it. Calibration is what separates us from open-source eval frameworks that nobody trusts. Three to six months is realistic for items one to three; items four to six are six-to-twelve-month bets that need GTM coordination.

### Visuals

Vertical timeline running left to right; each item plotted in its priority position. Color-code by category: blue for engineering, green for GTM. Don't draw firm dates — directional only.

---

## Slide 17 — Schedule a 30-minute walkthrough

### On slide

- **Book a 30-minute walkthrough.** I'll show you the live system end-to-end: ingest → score → run → diagnose → close-the-loop, on a real customer agent.
- **Bring an agent JSON if you have one.** We can score it in the walkthrough.
- **Contact:** Jeremy Yu · [Slack: @jeremy.yu] · [email: jeremy.yu@movate.com]
- The walkthrough is the fastest path from "interesting" to "I can pitch this on my next engagement."

### Speaker notes

Single ask. One sentence. If you take one thing from this deck, take this — book the walkthrough. I'll show you everything that's behind the slides; we'll run a live evaluation against an agent of your choice if you bring a definition. Thirty minutes. After that, you'll either think this is something you can sell to your accounts, or you'll have a sharper objection that I need to address. Either is a useful outcome. Walk out with my contact info and book the meeting.

### Visuals

Big, clean. The contact info readable from the back row. A small badge in the corner: "Movate Agent Assurance · live in production."

---

## Appendix A1 — Architecture deep-dive

### On slide

The platform decomposes into seven independently-testable layers:

1. **CLI / Web entry** — Typer CLI + FastAPI HTTP layer, both call into the same orchestrator
2. **Adapter layer** — uniform interface across `lyzr`, `langgraph`, `openai_compat`, `mock`, `rest`. Pluggable
3. **Scenario engine** — JSONL/YAML loaders, snapshot-with-SHA, schema validation
4. **Orchestrator** — async + bounded concurrency; runs scenarios × N reps through the evaluation layers
5. **Evaluation layers** — deterministic → DeepEval → judge panel → arbitration → triangulation
6. **Scoring** — 10-category composite + status bands + hard gates
7. **Reporting** — JSON / CSV / HTML / PDF / business report / methodology / agent doctor

Source of truth: `ARCHITECTURE.md` (619 lines, structured for diagramming).

### Speaker notes

For Q&A. The architecture is layered for a reason — each layer is independently testable and replaceable. Adapter layer means we can add a new agent backend (Anthropic Claude direct, Vertex AI, custom HTTP) without changing the scoring code. Orchestrator is async because evaluating 50 scenarios × 3 reps × 5 judges sequentially would take an hour; in parallel it's minutes. Reporting layer outputs to multiple formats because different audiences need different artifacts. Pull `ARCHITECTURE.md` if anyone wants to draw it in detail.

### Visuals

Layered block diagram from `ARCHITECTURE.md` §2. Seven horizontal bands stacked vertically; arrows show data flow downward through evaluation, then back up through reporting.

---

## Appendix A2 — Judge panel + arbitration math

### On slide

**Per role (e.g., correctness):**
- 2 panel judges — different vendors (OpenAI + Anthropic) → independent draws
- Each emits `{score, pass, rationale}` OR `{abstain: true, reason}`
- Variance > `arbitration_variance_threshold` (default 0.04) → escalate to **meta-judge**
- Meta-judge is a stronger model (Claude Sonnet) — re-judges from the original task, doesn't average
- All abstained → meta-judge gets last-chance scoring; if meta also abstains → role abstains entirely (`final_score: None`, `all_abstained: True`)

**Cache:**
- Every judge call keyed by `SHA-256(provider, model, system, user, temperature)`
- Re-runs of identical scenarios cost $0 in tokens
- Replay command exercises this — useful for auditor-grade reproduction without re-spending

### Speaker notes

For technical deep-dive. The reason we have two judges per role from different vendors is to break out of single-model-bias. If both judges agree, we trust the verdict. If they disagree past the threshold, the meta-judge breaks the tie on the record. Abstention is an honesty mechanism — judges are forbidden from forcing a 0.5 on cases they can't honestly score. The cache is what makes re-runs free. An auditor can replay any historical run and get bit-identical scores from cached judge responses without spending a token.

---

## Appendix A3 — API surface

### On slide

30+ endpoints, full schemas in `BOLT_API_REFERENCE.md` (1,180 lines).

| Group | Sample endpoints |
|---|---|
| **Public** | `/healthz`, `/readyz`, `/version`, `/openapi.json` |
| **Agents** | `GET /api/agents`, `GET /api/agents/{id}/runs`, `PATCH /api/agents/{id}` |
| **Multi-agent** | `GET /api/agent-systems/{slug}` |
| **Ingest** | `POST /api/agent-definitions`, `POST /api/agent-definitions/preview` |
| **Scenarios** | `GET /api/scenario-sets/{id}`, `POST /api/scenarios/propose-one`, `POST .../from-jsonl` |
| **Runs** | `POST /api/runs/preview`, `POST /api/runs`, `GET /api/runs/{job_id}` |
| **Per-run analytics** | `GET /api/runs/{id}/doctor`, `GET /api/runs/{id}/business-report`, `GET /api/insights/{id}/{kind}/{name}` |
| **Portfolio** | `GET /api/portfolio/at-a-glance`, `/leaderboard`, `/cost` |
| **Profiles** | `GET /api/scoring-profiles`, `POST /api/scoring-profiles/recommend` |

Auth: `Authorization: Bearer <key>`. CORS allowlist for `*.bolt.host`, `*.vercel.app`, `*.webcontainer-api.io`. Every response carries `X-Trace-Id` for debugging.

### Speaker notes

Reference for engineering integration questions. The full reference document is in the repo with every request shape, response shape, status codes, and a Bolt authoring-flow checklist. The OpenAPI spec is auto-generated and lives at `/openapi.json` — Bolt regenerates types from this on every frontend build. If a customer wants to integrate directly without using our dashboard, this is the surface they wire against. The PRD set documents how Bolt wires it; the API reference documents what they wire against.

---

## End of deck

**Next steps after the deck:**

1. Read through, flag any slides where the speaker notes don't match how you'd actually say it. Edit in this `.md`.
2. Convert to PPT — recommended path: paste sections into Gamma (gamma.app) for an automated layout, then refine the visuals manually. Alternative: marp / pandoc if you want a fully-text pipeline.
3. Add the diagrams. The `Visuals` blocks are concrete enough to hand to a designer.
4. Add Movate brand assets (logo, color palette) — coral `#FF5542`, plum `#4f3144`, ink `#26282b` per the existing brand kit work in the repo.
5. Run a dry-read with one Applied AI engineer before going wide.

This deck stands on its own as a written document — anyone in the Applied AI team can read it cold and walk away with the same picture you'd give them in a meeting. That's the test for a good source-of-truth markdown.

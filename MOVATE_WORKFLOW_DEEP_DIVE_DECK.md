# Movate Agent Assurance — Architecture Deep Dive

**Audience:** Movate Applied AI team (technical, internal)
**Length:** ~32 slides for a 60-minute walkthrough (≈1.5–2 min per slide + Q&A)
**Format:** Per-slide on-slide content + speaker notes detailed enough to read aloud
**Date:** 2026-05-07
**Speaker:** Jeremy Yu

---

## Slide 1 — Cover

### On slide

**Movate Agent Assurance**
Architecture deep dive — how a customer agent goes from upload to scorecard

Applied AI internal review · 2026-05-07 · Jeremy Yu

### Speaker notes

Hi everyone. Today I'm going to walk you through exactly how Movate Agent Assurance works end-to-end. Last time we did a stakes-and-pitch overview — this is the engineer-level deep dive. I want you to leave this room knowing what every LLM call does, where every cached value lives, and which mechanism produces every number on the final scorecard. The audience for this work is going to be your customers — so when they ask "wait, how exactly do you decide my agent is at 87.19?" you should have a precise answer ready.

I'll move through the system in workflow order: ingest → generation → run → scoring → diagnosis → closure. About 30 slides. Stop me whenever something needs more depth.

---

## Slide 2 — The gap we close

### On slide

- Production AI agents fail in ways nobody tested for
- "We ran a benchmark once" doesn't pass an enterprise audit
- Existing tools (DeepEval, Ragas, TruLens) test *prompts*, not *agents in their business context*
- The audit gap: no QA layer for agents the way browsers / APIs / data pipelines have one

### Speaker notes

The reason this product exists: AI agents in production fail in ways the team that built them never thought to test. They hallucinate confidently, they refuse legitimate requests, they comply with manipulation, they leak system prompts, they break their declared workflows when the input is slightly off-format. The existing eval libraries — DeepEval, Ragas, TruLens — are excellent at scoring individual prompts in isolation, but they don't know anything about the agent's *business purpose*. They can tell you "this answer scored 0.78 on faithfulness" but they can't tell you "your Career & Hiring topic is dragging the score down because two of three scenarios in that area failed adversarial probes."

Movate Agent Assurance fills the gap between "raw model evaluation" and "deployable agent QA." We build on top of those libraries — we don't replace them — but we add the agent context, the test-set governance, the audit trail, and the scoring that an enterprise customer can actually defend in front of a risk committee.

---

## Slide 3 — Mental model: agent-as-employee

### On slide

Treat the agent like a new hire being onboarded:

| Onboarding step | Agent equivalent |
|---|---|
| Read the job description | Parse the agent definition (role, instructions, goal, KBs, tools) |
| List areas of responsibility | Extract topical categories |
| Decide how to stress-test | Pick a behavioral mix preset |
| Run scenario-based competency tests | Execute the eval against the live agent |
| Score against rubric | Multi-layer scoring (deterministic + judge panel + DeepEval) |
| Report card with growth areas | Agent Doctor + per-topic breakdown |
| Add new failures to next test cycle | HITL promote-failure |

### Speaker notes

The mental model that drove the redesign: think of an AI agent like a new employee. When a manager onboards someone, they read the job description, list the areas the new hire is responsible for, design competency tests for each area, decide how to stress-test (basic competence, edge cases, what if a customer is rude), run the tests, score against a rubric, give a report card, and add failures to the next round. That's the entire shape of mdk-eval. Every component maps to a step in that loop.

The key insight: the agent's *own* definition is its job description. The role tells you what they are. The instructions tell you what they're supposed to do. The knowledge bases tell you what they know. The goal tells you what success looks like. We don't go to the web for any of this — we just structure what the customer already wrote.

The rest of this talk is the mechanics of each step. Hold onto the mental model — when something seems weirdly specific, ask "would a human onboarding officer do this step?" and the answer is almost always yes.

---

## Slide 4 — The seven-stage workflow

### On slide

```
1. INGEST                2. GENERATE                  3. RUN
   ┌──────────┐             ┌─────────────┐              ┌──────────┐
   │ Upload   │             │ Test Mix    │              │ Worker   │
   │ Sanitize │ ──────────▶ │ Designer    │ ───────────▶ │ executes │
   │ Heuristic│             │ (topics ×   │              │ live     │
   │ Topics   │             │  behaviors) │              │ agent    │
   └──────────┘             └─────────────┘              └──────────┘
                                  │                            │
                                  ▼                            ▼
                          ┌─────────────┐              ┌──────────────┐
                          │ HITL Review │              │ Three-layer  │
                          │ approve /   │              │ scoring +    │
                          │ edit /      │              │ arbitration  │
                          │ reject      │              └──────────────┘
                          └─────────────┘                     │
                                                              ▼
                                                       ┌────────────┐
                                                       │ 4. AGGREGATE│
                                                       │ Composite + │
                                                       │ status band │
                                                       │ Failures    │
                                                       │ Per-topic   │
                                                       └────────────┘
                                                              │
                                                              ▼
                                                       ┌────────────┐
                                                       │ 5. DIAGNOSE│
                                                       │ Doctor      │
                                                       │ Business    │
                                                       │ Report      │
                                                       │ Methodology │
                                                       └────────────┘
                                                              │
                                                              ▼
                                                       ┌────────────┐
                                                       │ 6. CLOSE    │
                                                       │ promote-    │
                                                       │ failure →   │
                                                       │ next set    │
                                                       └────────────┘
                                                              │
                                                              ▼
                                                       ┌────────────┐
                                                       │ 7. AUDIT    │
                                                       │ Methodology │
                                                       │ Provenance  │
                                                       │ Replay      │
                                                       └────────────┘
```

### Speaker notes

This is the system on one slide. Seven stages. Three of them are user-facing surfaces in Bolt (Ingest, Generate, Diagnose — the dashboard). Four of them are backend processes (Run, Aggregate, Close, Audit). Every stage has caching, every stage has provenance, every stage has a fallback for when an LLM is unavailable. We'll spend the rest of the hour walking left to right through this diagram. Each box gets one or more slides.

The thing I want you to notice now: we never *delete* anything. The full chain — agent definition → topics → mix → scenarios → run output → judge rationales → composite score → diagnosis — is recoverable end-to-end from any final score. If a customer asks "why is my agent at 87?" we can show them the exact 13 scenarios, the exact judge rationales for each, the exact arbitration decisions, the exact prompt SHAs we used. That's the audit story.

---

## Slide 5 — Stage 1a: Upload + sanitization

### On slide

- User uploads agent definition JSON (Lyzr export, OpenAI Assistant, custom shape)
- BEFORE anything else touches the bytes: sanitize
- Strip secret-shaped fields by regex: `api_key`, `password`, `token`, `secret`, `credential`, `auth_*`
- Replace with `<redacted>`, emit a warning, hash the SANITIZED bytes
- Why this matters: Lyzr exports often include the producer's actual API keys. We never persist, hash, or send them downstream.

### Speaker notes

Stage one starts with a multipart upload to `POST /api/agent-definitions`. The first thing that happens — before we parse the JSON, before we run any extractor, before we hash the file for the cache key — is sanitization. We have a small Python module that walks the JSON tree and looks for keys matching a regex of secret-shaped names. When it finds one, we replace the value with the literal string `<redacted>` and emit a warning the user sees in the UI: "Sanitized 1 secret-shaped field from upload before processing: api_key."

Why this is non-negotiable: agent definitions exported from Lyzr — and from most agent platforms, honestly — routinely include the producer's actual production API keys. If we hashed the raw bytes for our cache key, that hash would be a leakable identifier of the secret. If we sent the raw bytes to the LLM extractor, the secret would land in OpenAI's logs. So we strip first, hash second, and the SHA we use for caching the topic extraction and the per-cell scenario generation is the SHA of the sanitized bytes. The customer can verify this by re-running the same upload and getting the same SHA.

This stage is free. Sub-millisecond. Always runs.

---

## Slide 6 — Stage 1b: Heuristic extraction

### On slide

Deterministic Python — no LLM. Always runs. ~5-10 baseline scenarios.

Pulls structured declarations:
- **Tool definitions** → schema_conformance scenarios
- **`response_format`** → JSON validation scenarios
- **Forbidden phrases mentioned in instructions** → forbidden-phrase tests
- **SLO declarations** → latency tests
- **Tool sequence rules** → tool-ordering scenarios

Plus warnings the user sees:
- "No output schema declared — schema_conformance scenarios will be skipped"
- "Agent temperature=0.4 (>0) — multi-run consistency expected to be lower"
- "No SLO latency declared — latency thresholds left unset"

### Speaker notes

After sanitization, the heuristic extractor runs. This is the part I want you to internalize: nothing here calls an LLM. It's a few hundred lines of deterministic Python that walks the agent definition and looks for *declared* structure. If the agent has a tool definition with a JSON schema, we generate a schema-conformance scenario that calls that tool and asserts the response validates. If the instructions contain the literal phrase "never mention competitors by name," we extract "competitors by name" as a forbidden phrase. If there's a latency SLO declared, we generate a performance scenario.

The heuristic extractor is what I call the "free, always-on" baseline. Every agent that gets uploaded — even if our LLM keys are unavailable, even if the agent file is weird, even if everything else fails — gets at least these baseline scenarios. It's reproducible: the same agent definition produces the same scenarios byte-for-byte. It's auditable: a customer can read the Python code and say "yes, that's exactly the rule I'd want."

It also emits warnings. Those warnings — "no output schema, no SLO, temperature > 0" — show up as that yellow box in Bolt's scenario review screen that you've all seen. Those warnings are the system telling the user "your agent definition is missing things that would make our evaluation stronger; here's the list, here's what we'll skip." They're a nudge to write better agent definitions, not a blocker.

---

## Slide 7 — Stage 1c: Topic extraction (the new piece)

### On slide

**LLM call to Claude Haiku** — ~$0.005, 2-3s, **cached by agent SHA** (repeats free)

Reads four parts of the agent definition:
- `agent_role` ("Customer FAQ Assistant for Movate")
- `agent_instructions` (the system prompt)
- `agent_goal` ("Help visitors get accurate Movate info")
- `features.lyzr_rag.rag_name` (knowledge base names)

Returns 3-8 topical categories:

```json
{
  "topics": [
    { "name": "Movate Services",     "slug": "movate_services",
      "relevance": 0.95,
      "description": "Capabilities and engagement models",
      "example_queries": ["What does Movate do?", "How do I engage Movate?"] },
    { "name": "Career & Hiring",     "slug": "career_hiring", "relevance": 0.78, ... },
    ...
  ],
  "extracted_via": "llm",     // or "heuristic" if LLM unavailable
  "fallback_used": false
}
```

Heuristic fallback: pull KB names directly, strip Lyzr hash suffixes.

### Speaker notes

This slide is the heart of the new generation flow we shipped this week. Topic extraction.

We send Claude Haiku — the cheap fast model — four pieces of the agent definition: the role, the instructions, the goal, and the names of any RAG knowledge bases attached. We ask: "based on this job description, what 3-8 topics does this agent actually cover?" The system prompt tells the LLM in capital letters not to invent topics that aren't grounded in the agent definition; if it can't cite the part of the definition that supports a topic, it doesn't propose one.

The output is a list with name, slug, description, relevance score 0-1, and example queries. For the Movate FAQ agent, the result was Movate Services at 95%, Career & Hiring at 78%, Company Information at 72%, Engineering at 51%, Customer Service at 48%. These are the *business categories* the user will use to design their test plan.

Three properties matter:

One — it's cached by the SHA of the sanitized agent definition. The same agent uploaded twice does not pay for extraction twice. Same engagement, same agent, same week — free.

Two — there's a deterministic heuristic fallback. If our OpenAI key is rate-limited, if Anthropic is down, if the customer is in an air-gapped environment with no LLM access at all, we fall back to reading KB names directly and synthesizing topics from those. The names we pull are stripped of Lyzr's trailing 4-character hash (we have a regex for that) so `movate_website_knowledge_baseyg9f` becomes "Movate Website Knowledge Base." Lower quality than the LLM path, but we never return an empty list.

Three — it's surfaced as `POST /api/agent-definitions/topics`. Bolt calls this once per Mix Designer mount; we cache the response in client state for the page session. The response field `extracted_via` tells the UI whether the heuristic ran, and if so, Bolt renders an info chip "Topics derived from KB names — confirm or refine."

---

## Slide 8 — The two-axis mental model

### On slide

| Axis | Name | What it answers | Source | UI role |
|---|---|---|---|---|
| **Primary** | **Topical category** | WHAT the scenario is about | Per-agent — `/api/agent-definitions/topics` | User picks per-topic counts |
| **Secondary** | **Behavioral category** | HOW the scenario stresses the agent | Static — `/api/extraction/prompts` + `/api/mix-presets` | User picks one preset |

A scenario can be `adversarial × Career & Hiring` or `standard × Movate Services`.

Tags carried by every generated scenario:
- `topic:career_hiring`
- `category:adversarial`
- `unverified` (always — until human review)
- `derived:llm` (provenance)

### Speaker notes

Before this redesign, the user had to think in behavioral categories — "I want 4 standard tests, 3 edge, 3 adversarial, 2 safety." That's a quality-engineering question, not a business question. Most stakeholders couldn't answer it without guessing.

After the redesign, they think in topical categories — "I want 5 tests on Movate Services, 3 on Career & Hiring, 3 on Company Information." That's a business question they CAN answer.

The behavioral mix becomes a single dropdown: Balanced, Compliance-heavy, or Reliability-focused. Closer to a quality discipline governed by engagement type than a per-test choice. A regulated-industry customer picks Compliance-heavy once and never thinks about it again. A customer-support deployment picks Reliability-focused once. The behavioral mix is now a *property of the engagement*, not a *property of every test set*.

The scenarios themselves carry both labels. So when scoring comes back, the dashboard can slice the result two ways: by behavior ("how did adversarial scenarios do?") and by topic ("how did Career & Hiring score?"). Neither view is privileged. Same data, two projections.

---

## Slide 9 — Stage 2a: Test Mix Designer

### On slide

User-facing surface in Bolt at `/upload/{tempId}/mix`.

Inputs the user controls:
1. **Per-topic counts** (the topics list, one number per row, range 0-20)
2. **Behavioral preset** (radio: Balanced / Compliance-heavy / Reliability-focused / Custom)
3. **Optional focus textarea** ("particularly test non-English inputs")
4. **Optional custom directive** (only if Custom preset has `custom > 0`)

Live preview, debounced 500ms:
- Calls `POST /api/agent-definitions/preview` (no DB writes)
- Server caches by content hash — identical mixes after first preview cost $0
- Returns proposed scenarios + estimated cost + 2D `counts_by_topic_category` breakdown

### Speaker notes

The Mix Designer is the user-facing surface where they build a test plan. The page has the topics list on the left, the behavioral preset radio below it, the focus textarea, and a live preview of proposed scenarios on the right.

Every change the user makes — bumping a topic count, switching presets, typing in the focus — debounces 500 milliseconds and then fires a preview call. The preview endpoint is *no-persist*: nothing hits the database until the user clicks "Generate scenarios." That gives them an iterative loop. Type a number, see the cards refresh, see the cost update, decide if it's right.

The crucial design choice: the preview endpoint is cached server-side by the hash of the mix request. Identical mixes — same agent SHA, same topic counts, same preset, same focus — return the cached scenarios on the second call. So if the user clicks Preview, looks at the cards, types 6 instead of 5 in one row, and clicks Preview again, only the topics that changed cause new LLM calls. Topics that stayed the same hit the cache. The user can iterate freely without watching the cost meter spin.

---

## Slide 10 — Stage 2b: Behavioral presets

### On slide

Three named presets, exposed via `GET /api/mix-presets`:

| Preset | Distribution | Use case |
|---|---|---|
| **Balanced** | 40% standard · 20% edge · 15% adversarial · 10% safety · 5% honesty · 5% multi · 5% perf | Default — general-purpose agents |
| **Compliance-heavy** | 20% standard · 15% edge · 25% adversarial · 25% safety · 10% honesty · 5% multi | Regulated industries (finance, healthcare, legal) |
| **Reliability-focused** | 50% standard · 30% edge · 5% adversarial · 5% honesty · 5% multi · 5% perf | Customer-support, internal tooling |

Plus **Custom** — user-edited 8 sliders.

For each topic with N tests, the preset's ratios distribute N across behaviors using **Hamilton's largest-remainder method** — totals always sum exactly.

### Speaker notes

We ship three named presets. They map to the three engagement archetypes Movate sees most often. Balanced is the default — for general FAQ assistants, internal tools, anything where the risk profile is moderate. Compliance-heavy skews toward adversarial and safety — for customer agents in finance, healthcare, legal, where wrong answers carry policy or legal exposure. Reliability-focused skews toward happy-path correctness and edge cases — for customer-support agents where the main risk is being unhelpful, not being manipulated. And Custom is what it sounds like — eight sliders, the user dials whatever ratio they want.

The math under the hood: for each topic with N tests, we multiply N by each behavior's ratio. That gives fractional counts. We round using Hamilton's method, which is the same algorithm used to allocate seats in proportional-representation legislatures. Take the floor of each fraction, count the leftover, hand the leftover one at a time to the categories with the largest fractional remainder. Ties broken alphabetically. Result: totals always sum exactly to N. No off-by-one bugs where the user asked for 5 tests and got 4 or 6.

The frontend implements the same algorithm in TypeScript so the live mix preview matches what the backend will actually generate. Both sides use the same Hamilton algorithm so byte-for-byte agreement is automatic.

---

## Slide 11 — Stage 2c: Per-cell LLM scenario generation

### On slide

For each (topic, behavior) cell with `count > 0`:
- Compose **system prompt**: shared base prompt + behavioral category directive
- Compose **user prompt**: agent definition + topic context block + count
- One **LLM call** per cell — typically 8-15 calls per Mix Designer run
- Each cell **cached independently** — regenerating one cell doesn't invalidate others
- Per-scenario provenance:
  - `constraint_quote` — exact substring of the agent definition this scenario tests
  - `reasoning` — one sentence on why this test is valuable
  - `prompt_sha`, `model`, `provider`
- All scenarios start tagged `unverified` + `derived:llm` — cannot ship without HITL approval

### Speaker notes

Once the user has a mix, we generate scenarios. For each cell in the 2D mix — say "Movate Services × adversarial × 1 scenario" — we make one LLM call. The system prompt has two parts: the base prompt that's shared across every cell (this is in `_BASE_SYSTEM_PROMPT` in the code, ~50 lines, governs output JSON schema and universal rules), plus the category-specific directive (the adversarial directive says things like "propose prompt-injection attempts, system-prompt extraction, role-confusion attacks").

The user prompt has the agent definition (only the relevant parts: role, instructions, goal, KBs, examples — we strip everything else to keep the token cost down), plus a topic context block telling the LLM all scenarios in this cell must focus on Movate Services with the description "capabilities and engagement models," plus the count and the optional focus.

Two trust principles enforced by the prompt:

One — the LLM must *cite* the substring of the agent definition that motivates each scenario, in a field called `constraint_quote`. If it can't cite, it shouldn't propose. This is what powers the "why this test exists" panel in the UI later.

Two — every scenario is tagged `unverified` AND `derived:llm` from birth. The downstream pipeline will not auto-promote any LLM-proposed scenario into a production verdict. A human has to click approve. This is non-negotiable; it's how we keep the trust story clean.

Each cell is cached by the hash of (model, system prompt, user prompt). So if the user changes the count for one topic and re-previews, only that cell's call costs anything — the other cells are warm.

---

## Slide 12 — Stage 2d: Human-in-the-loop review

### On slide

After Generate, scenarios land in DB tagged `unverified`. User reviews each at `/scenario-sets/{id}`.

Per-card actions:
- **Approve** — status `unverified` → `approved`. Required before run.
- **Edit** — modal with three fields (input prompt, description, forbidden phrases). Adds `edited` tag.
- **Regenerate** — calls `POST /api/scenarios/{id}/regenerate` with optional new focus. One LLM call, cached. Old → new diff shown briefly.
- **Reject** — status → `rejected`. Excluded from runs.

Audit fields on every scenario:
- `verified_by` (email)
- `verified_at` (timestamp)
- `regeneration_count`
- `meta.derived_from` — full provenance: extractor, category, reasoning, model, prompt_sha, source_sha256

### Speaker notes

After the user clicks "Generate scenarios →" we persist them to the database. They land tagged `unverified`. Status is enforced at the run layer — the worker that executes the eval refuses to run any scenario whose status is not `approved`. This is the gate.

The user goes to the scenario review page and sees each scenario as a card. They have four buttons. Approve flips the status. Edit opens a small modal where they can change the prompt or the forbidden phrases. Regenerate makes a fresh LLM call with optional new focus and shows the old/new diff so the user can see what changed. Reject excludes the scenario from runs but keeps it in the audit trail.

Two audit trail bits I want you to notice. First, every scenario carries `verified_by` and `verified_at` — who approved it, when. If a customer audits the run, they can see "Jeremy approved scenario X on 2026-05-06 at 14:30." Second, the `meta.derived_from` field captures the full provenance: which extractor produced it (heuristic vs llm), what reasoning the LLM gave, what model and prompt SHA, what source agent SHA. We can re-run the same generation byte-for-byte from this metadata. That's audit-grade.

The point of HITL: the LLM is a fast first draft. A human is the editor of record. Customers in regulated industries will not accept "an LLM proposed these tests and we ran them" as a defensible test plan. They will accept "an LLM proposed these tests, my engineering team reviewed each one, here's the timestamp on each approval, here are the ones we rejected and why." The whole product flow is designed around making that second statement easy and auditable.

---

## Slide 13 — Stage 3a: Run preview + cost estimate

### On slide

Before the user clicks Run, show them what they're about to spend:

```ts
estimated_cost_usd =
    num_scenarios
  × runs_per_scenario        // default 1; bump to 3+ for consistency measurement
  × judges_per_scenario      // currently ~7 (correctness, grounding, safety, ...)
  × cost_per_judge_call      // $0.0015-0.005 depending on model
  + deterministic_cost       // ~$0
  + deepeval_cost            // ~$0.01 per RAG scenario
```

For the Movate FAQ run on 2026-05-07: **13 scenarios × 1 run × 7 judges = 91 judge calls = $0.31**

Cached re-runs: $0.

### Speaker notes

Before the user clicks Run, we show them the cost. The math is straightforward: number of scenarios times runs per scenario times judge panel size times cost per judge call, plus a small DeepEval surcharge for RAG-specific scenarios. Deterministic checks are free.

Default runs_per_scenario is 1. Customers who want to measure consistency — does the agent answer the same way to the same prompt over multiple invocations — bump that to 3 or 5. The cost scales linearly. Most pilots run at 1 the first time and only go to 3 once they have a baseline.

For the Movate FAQ run we just did, the math came out to 13 scenarios × 1 run × 7 judges = 91 judge calls, plus a few DeepEval calls = 31 cents. That's the whole evaluation. For comparison, a manual QA pass on 13 scenarios would take a Movate delivery engineer maybe two hours — call it $200 fully loaded. The cost discipline isn't really about saving 30 cents per run; it's about making it cheap enough to run *every day in CI*, not once a quarter.

Cache hit on a re-run: $0. Same scenarios, same agent, same judges → judge cache hits all the way. We re-render the dashboard from cached data; nothing burns. This is what makes daily CI runs viable.

---

## Slide 14 — Stage 3b: Run execution

### On slide

```
Bolt clicks "Run"
    │
    ▼
POST /api/runs    →    INSERT into web_run_job (status=pending)
                  →    POST to pgmq queue
    │
    ▼
Worker pops job  →   For each approved scenario:
                       1. Send input.prompt OR input.turns to live agent endpoint
                       2. Capture response, duration_ms, trace (tool calls, retrieved docs)
                       3. Run three-layer scoring (next slides)
                       4. INSERT into scenario_run (per-rep raw)
                       5. After all reps: aggregate into scenario_aggregate
    │
    ▼
On completion:   →   evaluation_summary, failure_cluster, finding rows
                 →   web_run_job.status = completed
```

### Speaker notes

Once the user clicks Run, we queue a job to pgmq — that's Postgres-backed message queue, an extension we use because the rest of our state is in Postgres anyway and it gives us free transactional durability. The web layer returns immediately with a job_id. Bolt polls `GET /api/runs/{job_id}` for status.

A worker process pops the job and iterates over approved scenarios. For each scenario, it sends the input prompt or input turns to the agent's live endpoint — Lyzr in our pilot case, but the adapter pattern means we can plug in OpenAI Assistants, LangChain agents, or raw HTTP endpoints. We capture the agent's response, the wall-clock duration, and a trace of any tool calls or document retrievals the agent did along the way.

That trace is critical for grounding judgement. When the LLM judge later evaluates whether an answer was grounded, it has the actual retrieved documents to compare against. This is how we get away from "vibes-based judgement" and into "did the answer cite the right source."

After scoring, results land in two tables. `scenario_run` is per-scenario per-repetition — the raw data. `scenario_aggregate` is per-scenario rolled up across repetitions — mean score, pass rate, variance, consistency score. The aggregate table is what powers most of the dashboard.

---

## Slide 15 — Stage 4: Three-layer scoring overview

### On slide

Every scenario gets scored by **three independent mechanisms in parallel**:

| Layer | What it does | Speed | Cost | Strength |
|---|---|---|---|---|
| **Deterministic** | Schema validation, forbidden phrases, regex, tool sequence, latency check | Microseconds | $0 | Always reproducible. Catches literal contract breaches. |
| **LLM judge panel** | 7 judges (correctness, grounding, safety, helpfulness, ux_tone, completeness, tool_usage) — each scores 0-100 with rationale | 5-15s | ~$0.02-0.05 per scenario | Catches semantic / nuanced failures a regex can't. |
| **DeepEval (RAG-specific)** | faithfulness, answer_relevance, contextual_relevance | 3-10s | ~$0.01 per scenario | Industry-standard RAG metrics. |

A single LLM judge is not enough. **All three layers contribute to the final per-scenario score.**

### Speaker notes

This is the slide where the credibility lives. Every scenario is scored by three independent mechanisms running in parallel.

Layer one is deterministic. If the scenario asserts the agent must never produce the literal substring "EvilBot," we substring-match. If it must conform to a JSON schema, we run jsonschema validation. If it must call tools in a specific order, we walk the trace and check. Microseconds. Free. Always reproducible. Anything that can be checked with a regex, a JSON validator, or a state-machine walker happens in this layer.

Layer two is the LLM judge panel. We currently have seven judges, each scoring a different dimension on a 0-to-100 scale plus a rationale paragraph. Correctness asks "is this answer factually correct given the agent's knowledge base?" Grounding asks "does the answer stick to retrieved context, or does it hallucinate beyond?" Safety, helpfulness, UX tone, completeness, tool usage — each is its own focused prompt. Total cost is 2-5 cents per scenario depending on the panel size.

Layer three is DeepEval. We use the open-source DeepEval library for RAG-specific metrics: faithfulness (does the answer stay faithful to the retrieved context), answer relevance (does the answer actually address the question), contextual relevance (was the right context retrieved in the first place). These metrics are industry-standard — the same ones every serious RAG team uses. We don't reinvent them; we run them and integrate their output.

Why three layers and not one? Single-layer eval is fragile. A regex misses semantic failures. A single LLM judge can be wrong, biased, or overly lenient. DeepEval's RAG metrics are great for grounding but don't capture refusal scenarios. Three independent mechanisms triangulating gives you a score where all three would have to be wrong simultaneously for the final number to be wrong. That's the defensibility argument.

---

## Slide 16 — Layer 1: Deterministic checks

### On slide

Pure Python, sub-millisecond per scenario. Always runs first.

| Check | Trigger | Failure mode |
|---|---|---|
| `forbidden_phrases` substring | Scenario declares literal forbidden strings | Hard fail. Agent emitted a banned token. |
| `forbidden_claims` semantic | Scenario declares semantic refusal | Defers to LLM judge (substring would false-positive on "I cannot give medical advice") |
| `expected_schema` JSON validation | Agent has `response_format` schema | Hard fail on validation error |
| `required_fields` presence | Scenario asserts response keys | Hard fail if missing |
| Regex matches | `expected_output` field with regex | Pass/fail per match |
| Tool sequence | `workflow.must_visit` / `must_not_visit` / `ordered_subsequence` | Walks trace, checks order |
| Latency budget | `latency_budget_ms` field | Soft warning if exceeded |

### Speaker notes

The deterministic layer is the simplest layer to explain and the most-trusted layer in a regulated audit. There's no LLM in this layer. There's no judgement. There are just rules we wrote in Python that walk the agent's output and the captured trace and check declared assertions.

Two distinctions worth calling out. First: forbidden_phrases vs forbidden_claims. Forbidden_phrases is a literal substring match — "the agent must not output the word EvilBot." Forbidden_claims is a semantic match handled by the LLM judge — "the agent must not give medical advice." We separate these because if you substring-match on "medical advice," you false-positive on the agent saying "I cannot give medical advice" — which is exactly the refusal we wanted. That kind of false positive destroys trust. So forbidden_phrases is for literal output patterns, forbidden_claims is for semantic refusals.

Second: tool sequence checking. If the agent's declared workflow says "ask for order number → look up order → ask for image → process," we walk the captured trace and verify the tools were invoked in the declared order. We support three constraints: must_visit (these tools must appear), must_not_visit (these must not appear), and ordered_subsequence (these must appear in this order, possibly with others between). This is how we catch agents that skip declared steps or shortcut workflow logic.

Sub-millisecond per scenario. Free. Always reproducible. The auditor's friend.

---

## Slide 17 — Layer 2: LLM judge panel

### On slide

Seven specialised judges run in parallel per scenario. Each cached by `(model, system_prompt, user_prompt, temperature)`.

| Judge | Scoring dimension | When it abstains |
|---|---|---|
| **correctness** | Factual accuracy | Question has no canonical answer |
| **grounding** | Adherence to retrieved context | No retrieval happened |
| **safety** | Refused harmful content / didn't leak PII | Scenario isn't safety-shaped |
| **helpfulness** | Answered the user's question | Answer was a refusal |
| **ux_tone** | Style + brand voice | Voice rubric not provided |
| **completeness** | Covered all required elements | No required_elements declared |
| **tool_usage** | Used the right tools right way | No tools available to use |

Each judge returns: `{score: 0-100, rationale: str, abstained: bool}`.

### Speaker notes

The judge panel is seven specialised LLM calls. Each judge has a focused system prompt — about 40 lines each — that turns Claude or GPT-4 into a specialist for that one dimension. Correctness is its own judge. Grounding is its own judge. Safety is its own judge. So on.

Why specialised vs one giant judge? Specialised judges produce more reliable scores. Asking a single LLM "score this answer 0-100 across correctness, grounding, safety, helpfulness, tone, completeness, and tool usage" produces vibes-based answers that average everything to about 75. Asking seven separate questions, each with one focused prompt, produces answers each tied to one dimension with a defensible rationale.

Every judge can abstain. If correctness can't be assessed because the question has no canonical answer — "what should I have for dinner?" — the correctness judge marks itself abstained instead of guessing. Abstention is treated honestly downstream: an abstained score doesn't count against the agent. This is "honest abstention" — refusing to score is more credible than fabricating a score. Customers in regulated industries care about this enormously; it's the difference between "this number means something" and "this is a magic number generated by an LLM."

Every judge call is cached by the hash of model, system prompt, user prompt, and temperature. So if we re-run the same scenario through the same agent producing the same output, the judge calls are free on the second pass. Re-running for cost-sensitivity testing or drift-checking is essentially free.

---

## Slide 18 — Layer 3: DeepEval (RAG-specific)

### On slide

Open-source library, integrated as a third independent signal:

- **`faithfulness`** — Does the answer stay faithful to the retrieved context, or does it invent details?
- **`answer_relevance`** — Does the answer address the question, or does it dodge?
- **`contextual_relevance`** — Was the right context retrieved in the first place?

Plus optional:
- `g_eval` (custom-rubric LLM grader)
- `bias`, `toxicity` (when scenario is safety-shaped)

Why integrate vs reimplement: DeepEval is industry-standard. RAG teams already trust the metrics. We add value by *combining* it with the judge panel + deterministic layer, not by replacing it.

### Speaker notes

Layer three is DeepEval. It's an open-source Python library focused on RAG-specific metrics, maintained by Confident AI. Most serious RAG teams already use it.

The three metrics we run by default: faithfulness, answer relevance, contextual relevance. Faithfulness asks "given the context the agent retrieved, did the agent's answer stick to that context?" If the agent retrieved a doc about Movate's cloud services and then answered with details about the cafeteria, faithfulness scores low. Answer relevance asks "did the answer actually address the question, or did it dodge?" Contextual relevance asks "was the *right* context retrieved in the first place?" — a separate failure mode from generation.

Why we integrate DeepEval rather than reimplement these metrics: DeepEval is industry-standard. The metrics are well-documented, well-understood, and customers' Applied AI teams probably already use them. If we wrote our own faithfulness scorer, every customer would ask "why don't you use the standard one?" By integrating DeepEval, we get free credibility from a tool the audience already trusts, and our value-add is *combining* it with the judge panel and the deterministic layer to give a triangulated score.

DeepEval calls are cached by the same content-hash mechanism as the judge panel. Three to ten seconds per scenario, about a cent. Optional metrics — bias, toxicity, custom G-Eval rubrics — fire only when the scenario is shaped to need them.

---

## Slide 19 — Multi-judge arbitration + honest abstention

### On slide

Within each scoring category (correctness, grounding, etc.), variance across judges triggers arbitration:

```
if variance(judge_scores) > arbitration_threshold:
    meta_judge = call_meta_judge(
        original_output, judge_rationales, agent_definition
    )
    if meta_judge.confident:
        final_score = meta_judge.score
    else:
        final_score = ABSTAINED      # not 0, not 50 — abstained
        confidence_band -= 0.05      # overall run confidence takes a hit
```

Why: judge disagreement is *signal*, not noise. Either we resolve it deliberately or we admit we couldn't.

### Speaker notes

When we run multiple judges on the same scenario, sometimes they disagree. The grounding judge says 85, the correctness judge says 60. Within a single category — say two grounding-style judges disagreeing — disagreement is information.

We compute the variance across judge scores. If variance exceeds a configured threshold, we escalate to a meta-judge. The meta-judge sees all the original judge rationales, the agent's output, and the agent definition. We ask it: "the judges disagreed. Resolve this. If you can't resolve it confidently, say so."

If the meta-judge resolves with confidence, we use its score. If it can't — if the disagreement is genuinely irreducible because the scenario is ambiguous — we mark the scenario abstained and the overall run's confidence band takes a 5-point hit.

This is honest abstention. We do not paper over judge disagreement by averaging. Averaging is a lie of consensus. We either resolve deliberately with citation (the meta-judge call is logged with its full reasoning) or we mark the score abstained. Abstention is what we tell the customer: "this scenario is ambiguous; we couldn't get a confident score; here's the reasoning trail."

Customers in regulated industries — finance, healthcare, legal — pay extra for this property. An eval framework that can say "I don't know" when it doesn't know is one they can defend. An eval framework that always returns a number is one they have to defend, often badly.

The arbitration threshold and meta-judge model are both auditable. Both end up in the methodology document for the run.

---

## Slide 20 — Statistical confidence

### On slide

Every aggregated number on the dashboard carries a confidence interval:

| Metric | Method | Why |
|---|---|---|
| **Pass rate** | Wilson 95% CI | Better than naïve binomial for small N — never produces 100% with N=2 |
| **Mean score** | Bootstrap 95% CI (1000 resamples) | Distribution-free; handles skewed score distributions |
| **Composite score** | Weighted bootstrap | Carries weights through the resampling |

Example in the dashboard:
**Composite: 87.19** &nbsp;&nbsp;&nbsp; (95% CI: 81.4 – 91.6)
**Pass rate: 92.3%** &nbsp;&nbsp;&nbsp; (95% CI: 64.0 – 99.0)

Wide CI is a *good* signal: it tells the user "I don't have enough data to be sure."

### Speaker notes

Every number on the dashboard carries a confidence interval. This is the audit-grade detail.

Pass rate uses Wilson 95% confidence interval. Wilson is what statisticians use for small-sample binomial proportions because it doesn't degenerate to "100% pass rate" when you ran 2 scenarios and both passed. Naïve binomial says "2 out of 2, that's 100%." Wilson says "2 out of 2 with 95% confidence we're somewhere between 16% and 100%." The wide bracket is the system telling you "you don't have enough data to be sure; run more scenarios."

Mean score uses bootstrap with 1000 resamples. Bootstrap is distribution-free — it doesn't assume scores are normally distributed, which they aren't (they tend to skew toward the high end). We resample the per-scenario scores 1000 times with replacement, compute the mean of each resample, and take the 2.5th and 97.5th percentile of those means as the CI bounds.

Composite score uses weighted bootstrap so the per-category weights (1.5× for the rubric_focus, 1.0× for others, 0.5× for tool_usage when it's not the focus) propagate through the uncertainty calculation.

Why this matters in customer engagements: if you tell a regulated-industry customer "your agent scored 87.19" with no uncertainty, they will ask you "out of how many trials, with what confidence?" An eval framework that answers "87.19 with 95% CI between 81.4 and 91.6 over 13 scenarios" is one they can take to a risk committee. Without the CI, every number on the dashboard is a vibes number.

We also auto-emit warnings: "13 scenarios is below the recommended 20 for tight CIs; consider running more before making production decisions." We are explicit about the limits of our own evidence.

---

## Slide 21 — Stage 5a: 10-category scorecard + status bands

### On slide

Per-scenario scores aggregate into a 10-category composite:

| Category | Weight | What it measures |
|---|---|---|
| `correctness` | 1.5× when rubric_focus | Factual accuracy |
| `grounding` | 1.5× when rubric_focus | Adherence to retrieved context |
| `completeness` | 1.5× when rubric_focus | Covered required elements |
| `tool_usage` | 1.5× / 0.5× | Right tools, right way |
| `ux_tone` | 1.5× when rubric_focus | Brand voice |
| `safety` | 1.0× | Refused harmful, didn't leak |
| `honesty` | 1.0× | Acknowledged uncertainty |
| `helpfulness` | 1.0× | Answered the question |
| `latency` | 1.0× | Within SLO |
| `consistency` | 1.0× | Same answer to same prompt across reps |

**Composite** = weighted mean per category, then averaged. **Status banding:**

| Score | Status |
|---|---|
| 90-100 | `production_ready` |
| 80-89 | `pilot_ready` |
| 70-79 | `needs_improvement` |
| <70 | `not_ready` |

### Speaker notes

The composite score is what shows up at the top of the dashboard. The math: every scenario produces a score in each of ten categories, weighted by its rubric_focus. We average per category, then average across categories with their weights, to get the final composite.

The ten categories are not arbitrary. They correspond to the ten failure modes we see most often in production AI agents. Correctness and grounding — the agent gets the facts wrong, or invents details beyond the source. Completeness and tool_usage — the agent skips required steps. UX tone — the brand voice fails. Safety and honesty — the agent fails refusal scenarios or fabricates uncertainty. Helpfulness — the agent dodges the question. Latency — too slow. Consistency — different answer to the same prompt twice.

Per-scenario weights come from the rubric_focus the LLM extractor picked when it generated the scenario. If it said "this scenario tests grounding," grounding gets a 1.5× weight in the composite for that scenario. Tool_usage when not the focus gets a 0.5× weight — because most scenarios don't exercise tools and we don't want to penalize an agent for not using tools when there were no tools to use.

Status banding is hard thresholds. 90 to 100 is production_ready — no major fixes needed. 80 to 89 is pilot_ready — controlled rollout while addressing identified issues. 70 to 79 is needs_improvement — specific issues identified, fix and re-run. Below 70 is not_ready — significant work needed. The Movate FAQ run we just did came out at 87.19, pilot_ready.

These thresholds are a default. Customers can configure their own bands per engagement — a healthcare customer might want production_ready to require 95+, with anything under 90 considered not_ready. The scoring profile system supports per-archetype thresholds.

---

## Slide 22 — Stage 5b: Failure clustering

### On slide

When scenarios fail, we cluster the failures by pattern:

```sql
-- failure_cluster table
SELECT failure_class, severity, count, example_scenario_ids, suggested_fix
FROM failure_cluster
WHERE run_id = ? ORDER BY count DESC LIMIT 5;
```

Common failure classes:
- `hallucination` — invented facts not in KB
- `refusal_failure` — agent answered when it should have refused
- `off_topic` — agent answered an off-topic prompt
- `tool_misuse` — wrong tool, wrong order, missing args
- `pii_leak` — agent emitted PII patterns
- `latency_breach` — exceeded SLO
- `inconsistency` — different answers across reps

Each cluster surfaces a `suggested_fix` (deterministic template; no LLM).

### Speaker notes

When scenarios fail, we don't just count them — we cluster them by failure mode. The failure_cluster table groups failures by what kind of failure they were. Hallucination is its own class. Refusal_failure is its own class. Tool_misuse, PII_leak, latency_breach — each is a class.

This is what powers the "Top 3 failure clusters" panel on the dashboard. Instead of showing the user 13 failed scenarios in a flat list, we show them three clusters: "5 scenarios failed with hallucination," "3 failed with refusal_failure," "2 failed with tool_misuse." The user immediately knows where to focus their fix effort.

Each cluster carries a suggested_fix string. These come from a deterministic template library — not from an LLM. The template for hallucination says "the agent is generating content beyond its knowledge base. Consider tightening the system prompt with explicit 'only answer from context' language, or adding a faithfulness check before the response is returned." The template for refusal_failure says "the agent is engaging when it should refuse. Add explicit refusal scenarios to the system prompt and confirm the safety category in the test mix is at or above 25%."

The customer's engineering team can act on these suggestions immediately. There's no LLM call needed to generate them, no waiting, no ambiguity. They're prescriptive. This is the first level of "what should I do about this."

---

## Slide 23 — Stage 5c: Per-topic breakdown (the 2D scoring view)

### On slide

The same scenario aggregate rows, projected onto the topical axis:

```
GET /api/runs/{run_id}/topic-breakdown

{
  "topics": [
    {
      "slug": "career_hiring",
      "display_name": "Career & Hiring",
      "mean_score": 62.0,           ← worst-first sort
      "pass_rate": 0.667,
      "failures_count": 2,
      "category_breakdown": {
        "standard":    { "mean_score": 70, "scenarios_count": 1 },
        "adversarial": { "mean_score": 60, "scenarios_count": 1 },
        "safety":      { "mean_score": 56, "scenarios_count": 1 }
      }
    },
    { "slug": "movate_services", "mean_score": 92.5, ... }
  ],
  "untagged_count": 1,
  "total_scenarios": 9
}
```

Same data the behavioral scorecard uses — different projection.

### Speaker notes

This is the new piece we just shipped. The per-topic breakdown.

The mechanism: when we ran the eval, every scenario was tagged with its `topic:<slug>`. After scoring lands in `scenario_aggregate`, this endpoint groups those rows by their topic tag and computes mean score, pass rate, failure count, and severity max per topic. Same data the behavioral scorecard uses. Different projection.

Why this matters: a customer reading the dashboard wants to know where in their *business* their agent is weak. "Adversarial is at 78" is not actionable. "Career & Hiring is at 62 — your weakest topic — and 2 of 3 scenarios in that area failed adversarial probes" is actionable. They know exactly which part of the business needs investment.

Inside each topic, we show the category breakdown — the same ten behavioral categories, but limited to scenarios in that topic. So Career & Hiring at 62 might break down as standard 70, adversarial 60, safety 56. The customer can see "OK so my hiring topic isn't weak across the board — it's specifically my safety responses that need work."

Cost: zero LLM calls. Pure SQL groupby + Python aggregation. Safe to call on every dashboard render. This is the kind of feature that's high-leverage because the data was already there — we just had to give it a second view.

---

## Slide 24 — Stage 6a: Agent Doctor (3-tier diagnostic)

### On slide

`GET /api/runs/{run_id}/doctor` — LLM-generated diagnostic, cached by run-content fingerprint.

| Tier | Audience | Output |
|---|---|---|
| **Tier 1** | Delivery managers | Executive summary (≤80 words) + headline action |
| **Tier 2** | Engineering team | Top 3 prescriptions, each with diagnosis + treatment + expected impact + cited findings + confidence-tagged |
| **Tier 3** | Engineer making the next edit | Specific suggested changes to the agent definition (target file, change, rationale) |

Falls back to deterministic template when LLM unavailable. Cost: ~$0.02-0.05 first call, $0 cached.

### Speaker notes

The Agent Doctor is the dashboard's "what should I do about this?" surface. Three tiers, one per audience.

Tier 1 is for the delivery manager who has fifteen seconds before her next meeting. Eighty words of executive summary plus one headline action. "Your agent is at 87, pilot-ready, but it's hallucinating about Movate cloud services. Tighten the grounding prompt before the customer demo on Thursday."

Tier 2 is for the engineering team. Top three prescriptions, each with a diagnosis paragraph, a treatment paragraph, an expected-impact paragraph, citations to specific findings and scenarios from the run, and a confidence label (high / medium / low) on the prescription itself. The team reads these prescriptions and decides which to action.

Tier 3 is for the engineer about to edit the agent definition. Specific suggested changes — "in agent_instructions, line 4, change 'answer accurately' to 'answer accurately AND only from the knowledge base; if the answer isn't in your KB, say I don't have that information'." Concrete diffs, not abstractions.

The whole thing is cached by a fingerprint of the run content. Same scores, same findings, same failure clusters → same Doctor output. Re-fetching costs nothing. The first call costs 2 to 5 cents — one LLM call to Claude Sonnet, the more capable model.

If the LLM is unavailable, the Doctor falls back to a deterministic template. The template version is less narratively rich but always cites real data from the run. So the endpoint always returns useful content; the difference between "LLM doctor" and "template doctor" is style, not substance.

---

## Slide 25 — Stage 6b: Business Report (executive narrative)

### On slide

`GET /api/runs/{run_id}/business-report` — for the Executive view tab.

Output structure:
- **Hero score + status band**
- **Headline** — one-sentence summary ("Movate FAQ scored 87 — Pilot Ready, with 5 fixable issues before full production launch")
- **Executive narrative** — 1-2 paragraphs of plain-English summary
- **Top wins** (3 bullet items by category)
- **Top losses** (3 by category, plus failure clusters with severity + scenarios_affected)
- **Production recommendation** — banded text ("Recommend a controlled rollout to internal users while...")

Same caching rules. Falls back to template.

### Speaker notes

The Business Report is the executive-view counterpart to Agent Doctor. Same input data, different audience. Where Doctor speaks to engineers, Business Report speaks to the buyer's executive sponsor.

The output is a polished narrative — hero score, headline sentence, two paragraphs of executive summary, top wins (the three categories the agent did best on), top losses (categories or failure clusters dragging the score down), and a production recommendation.

The tone matters. Doctor talks about "tighten the grounding prompt." Business Report talks about "the agent is reliable for general queries but needs targeted fixes in five areas before full deployment." Same finding, different framing.

The narrative paragraph is generated by an LLM (Claude Sonnet) with a focused prompt that tells it to be concrete, cite numbers, avoid marketing language. Cached by run-content fingerprint. Re-fetching is free.

This is what powers the Executive view tab in Bolt. A delivery manager pulls this up before a customer call, copies the headline into their notes, and walks into the meeting with one talking point: "the agent is pilot-ready with 5 fixable issues; here's the timeline." The customer hears a defensible position, not a vibe.

---

## Slide 26 — Stage 7a: Methodology auto-emission (the audit story)

### On slide

Every run produces a `methodology.md` artifact. Auto-generated. Pinned to the run.

Captured fingerprints:

```
schema_version:           1.4.0
methodology_version:      mdk-eval/2.3
mdk_eval_version:         git_sha: a1b2c3d-topics
manifest_sha256:          aa1246f9...
dataset_sha256:           bb...
config_sha256:            cc...
judge_prompts_sha256:     {correctness: dd..., grounding: ee..., ...}
judge_models:             {correctness: gpt-4o-mini, grounding: claude-haiku, ...}
meta_judge_model:         gpt-4o
arbitration_threshold:    15.0
runs_per_scenario:        1
triggered_by:             jeremy@movate.com
```

An auditor can replay this exact run from these fingerprints alone.

### Speaker notes

The audit story is the most underrated capability in this product. Every run automatically produces a methodology document. It's pinned to the run record, so every score has a methodology attached.

The methodology captures every fingerprint that affects the score: schema version, methodology version, the git SHA of mdk-eval at the time of the run, the SHA of the test manifest, the dataset, the config, the judge prompts (one SHA per judge), the judge models, the meta-judge model, the arbitration threshold, runs per scenario, and who triggered the run.

Why this matters: a regulator or auditor can read this document and replay the exact run. They check out our git SHA, build the methodology version, load the dataset by its SHA, run with the same judge models — and get the same scores. Reproducibility is the audit-grade property regulated industries pay for.

Without this, "your agent scored 87" is a vibes statement. With this, "your agent scored 87 under methodology mdk-eval/2.3 with these specific judge prompts hashed to these specific values, reproducible by anyone with access to the artifacts" is a defensible statement. The difference is the difference between getting onto a Fortune 500 customer's approved-vendor list and not.

We don't ship a separate compliance tool. The compliance story IS the methodology document, auto-emitted on every run.

---

## Slide 27 — Stage 7b: HITL closure — promote-failure

### On slide

Failed scenarios become permanent regression tests in one CLI command:

```bash
mdk-eval promote-failure \
  --run-pk 24 \
  --scenario-id career_hiring__adv_salary_disclosure \
  --add-to-set 7
```

What it does:
1. Reads the failed scenario + the agent's actual response
2. Tightens the scenario assertion based on the failure mode
   (hallucination → add forbidden_claim; refusal_failure → add forbidden_claim; latency_breach → tighten budget)
3. Adds the tightened scenario to the next test set
4. Tags it `promoted_from:<run_pk>` for provenance

Result: the agent never fails the same way twice without us seeing it.

### Speaker notes

This is the closure step. When the run finds failures, those failures become permanent regression tests. One CLI command.

The mechanism: we read the failed scenario from the database, look at the agent's actual response, and tighten the scenario's assertion based on the failure mode. If the scenario failed with hallucination — agent invented facts — we add a forbidden_claim that codifies the specific fabricated fact. If it failed with refusal_failure — agent engaged when it should have refused — we add a forbidden_claim around the specific behavior we wanted refused. If it was a latency_breach, we tighten the latency budget to a value tighter than what the agent did.

The tightened scenario goes into the agent's next test set, tagged `promoted_from:<run_pk>` so we have audit provenance. Next eval run — daily, weekly, whatever cadence — includes this scenario. The agent never fails the same way twice without us seeing it.

This is the self-improving regression corpus. Every customer engagement that runs evals has a test set that grows over time, weighted toward the failure modes specific to that customer. Their test set is unique to them.

It's a CLI today. The Bolt UI version — promote-failure as a button on the failed-scenario card — is the next phase. Already prototyped.

The bigger version of this same mechanism — pulling failures from production traces (Langfuse) and converting them into eval cases automatically — is on the roadmap. That closes the loop with no human in between.

---

## Slide 28 — System property: cost discipline via caching

### On slide

Every layer that costs money is cached by content hash:

| Layer | Cache key | Hit ratio in practice |
|---|---|---|
| Topic extraction | `sha256(sanitized_agent_def)` | ~99% on engagement |
| Per-cell scenario gen | `sha256(model + system_prompt + user_prompt)` | ~80% on iteration |
| Each judge call | `sha256(model + sys_prompt + user_prompt + temp)` | ~95% on re-run |
| DeepEval call | Same shape as judges | ~95% on re-run |
| Agent Doctor | `sha256(run-content fingerprint)` | 100% on re-fetch |
| Business Report | Same | 100% on re-fetch |

**Movate FAQ run, fresh: $0.31. Re-run on same artifacts: $0.00.**

This is what makes daily CI runs viable.

### Speaker notes

I want to drive this home because it's a load-bearing property of the architecture. Every layer that costs money is cached by content hash.

Topic extraction: cached by SHA of the sanitized agent definition. Same agent uploaded twice — free the second time. Hit rate is 99% within an engagement because the agent definition rarely changes between iterations.

Per-cell scenario generation: cached by hash of model plus system prompt plus user prompt. The user iterates on the Mix Designer — bumps a topic count from 5 to 6 — only the changed cell costs anything. Other cells warm. Hit rate is around 80% during iteration.

Each judge call: cached by hash of model plus prompts plus temperature. Re-running the same scenario through the same agent producing the same output triggers cache hits across all 7 judges. Hit rate is 95% on re-runs.

Agent Doctor and Business Report: cached by the run-content fingerprint. If the run data hasn't changed, re-fetching is 100% free.

The Movate FAQ run we did fresh cost 31 cents. Re-running it later — say a customer wants to look at the dashboard again, or we want to A/B compare against a new methodology version — costs zero. This is what makes daily CI evaluation viable. If a fresh run costs 31 cents and a CI run is essentially free, we can run evaluations every commit, every deploy, every day. The cost line for an engagement scales linearly with *agent count*, not with *run count*.

---

## Slide 29 — System property: multi-agent systems

### On slide

Manager + sub-agent first-class. Lyzr `managed_agents` field auto-detected.

```
Returns Manager (root)         ← composite weight 1.5×
├── OCR Agent                  ← 1.0×
├── Validator Agent            ← 1.0×
└── Knowledge Base Agent       ← 1.0×
```

System composite score:
- Each agent evaluated independently
- System score = weighted average (manager 1.5× because it orchestrates)
- System status = **worst-of** (one weak sub-agent makes the system not_ready)

API: `GET /api/agent-systems/{root_slug}` returns the rollup.

### Speaker notes

Real customer agent systems are rarely a single agent. SanDisk Returns has a manager agent that delegates to an OCR agent (reads return-form images), a validator agent (checks the OCR output against business rules), and a knowledge base agent (looks up policy). Movate FAQ has a manager + a sub-agent (Knowledge Base). The architecture treats this as first-class.

When we ingest a Lyzr export with `managed_agents` declared, we auto-detect the sub-agents and prompt the user to upload each one. The Bolt UI walks them through the chain.

Each agent gets evaluated independently — its own scenarios, its own scorecard, its own composite. Then the system gets a rollup score. The math: weighted average where the manager carries 1.5× weight (because it orchestrates) and each sub-agent carries 1.0×. Status is worst-of, not weighted — a system with a 95-scoring manager and a 65-scoring OCR agent is not_ready, because the OCR agent is the bottleneck.

Why worst-of for status: in production, the system fails as a chain. If the OCR agent hallucinates a serial number, the manager passes the wrong thing downstream, and the customer sees a wrong answer. Average-based status would mask this. Worst-of forces the team to fix the weak link before claiming production-ready.

The API returns the system rollup at `/api/agent-systems/{root_slug}` — one call gets the composite, status, per-agent scores, and the bottleneck identifier.

---

## Slide 30 — System property: trust principles

### On slide

Five principles enforced at every layer of the pipeline:

1. **Provenance** — every scenario carries `meta.derived_from` (extractor, model, prompt SHA, source SHA, reasoning, constraint quote)
2. **Honest abstention** — judges abstain rather than guess; abstained scores don't count
3. **Statistical confidence** — every aggregated number has a CI bracket
4. **Methodology auto-emission** — every run produces a reproducible methodology.md
5. **HITL gating** — no LLM-proposed scenario auto-promotes; human approval is required to ship

What this stops:
- "I can't tell where this number came from" → provenance fixes it
- "The judge made up a confident score" → abstention fixes it
- "We ran 4 scenarios and called it 100% pass" → CIs fix it
- "We can't reproduce last quarter's run" → methodology fixes it
- "The LLM proposed this and we ran it as-is" → HITL fixes it

### Speaker notes

These are the five trust principles enforced everywhere in the pipeline. I want you to be able to recite them because when a customer asks "why should we trust this?" your answer is one of these five plus an example.

Provenance — every scenario carries enough metadata to retrace exactly where it came from. The extractor that produced it, the model and prompt SHA, the source agent SHA, the LLM's stated reasoning, the cited constraint quote from the agent definition. If a customer asks "where did this scenario come from?" we have an answer. We don't say "the LLM made it up."

Honest abstention — when a judge can't score, it abstains. The abstained score doesn't count. The overall confidence band absorbs the uncertainty. We never pretend to know what we don't.

Statistical confidence — every aggregated number on the dashboard has a confidence interval. Wilson for proportions, bootstrap for means, weighted bootstrap for the composite. We're explicit about how much evidence we actually have.

Methodology auto-emission — every run produces a fingerprinted methodology document. Reproducible by an auditor with our artifacts.

HITL gating — every LLM-proposed scenario starts unverified. The pipeline refuses to run unverified scenarios. A human has to approve each one. Audit trail captures who and when.

Each one of these stops a specific failure mode of "vibes-based AI evaluation." We didn't invent these principles — they're standard in any safety-critical engineering discipline — we just enforced them everywhere in this pipeline. That's the audit-grade story in five bullets.

---

## Slide 31 — Recap: what makes this defensible

### On slide

Five mechanisms that, together, are very hard to copy quickly:

1. **Two-axis test design** (topic × behavior) — the test set is structured around the customer's business
2. **Three-layer scoring** (deterministic + judge panel + DeepEval) — no single mechanism can be wrong alone
3. **Multi-judge arbitration + abstention** — disagreement is signal, not averaged
4. **Statistical confidence on every number** — CIs on all aggregates, Wilson + bootstrap
5. **Methodology + provenance auto-emission** — every score is reproducible end-to-end

Plus the cost discipline that makes it viable: $0.31 per fresh run, $0 on re-runs, daily CI evaluation is affordable.

### Speaker notes

If you ask me what the moat is — the thing that makes this hard to copy quickly — it's not any one of these features. It's the combination of all five plus the cost discipline.

Two-axis test design. Most eval frameworks have a single axis (behavioral). The two-axis structure means the test set IS the customer's business taxonomy, not an abstract category list. You can't slap that on top of an existing eval framework — it touches generation, scoring, and reporting all at once.

Three-layer scoring. DeepEval alone is one layer. Judge panel alone is one layer. Either alone is a vibes machine. The combination triangulates. Building all three and integrating them coherently is a year of engineering.

Multi-judge arbitration. Most frameworks average judge scores. Averaging hides disagreement. The arbitration + abstention pattern is opinionated and audit-friendly; it's also painful to retrofit into an existing eval pipeline.

Statistical confidence. Most frameworks ship point estimates. CIs everywhere is a discipline that has to be enforced from the data layer up; bolt-on after the fact is structurally hard.

Methodology + provenance. End-to-end reproducibility — from agent SHA to final score — requires every layer to be hashed, cached, and auditable. Retrofitting reproducibility into a system not built for it doesn't work.

And cost discipline. The caching architecture means we can run daily CI for the price of a fresh run per agent per quarter. Customers who want continuous quality monitoring can have it without a budget conversation.

Any one of these is straightforward to add. All five together, with the cost discipline that makes them viable in production — that's the moat.

---

## Slide 32 — Where to dig deeper + Q&A

### On slide

For more depth on any layer:

| Layer | Doc |
|---|---|
| Architecture | `ARCHITECTURE.md` |
| Backend / API contract | `BOLT_BACKEND_PRD.md`, `BOLT_API_REFERENCE.md` |
| Scoring math | `BOLT_SCORING_PRD.md` |
| Per-archetype scoring tuning | `BOLT_SCORING_PROFILES_PRD.md` |
| Test authoring UX | `BOLT_TEST_AUTHORING_PRD.md`, `BOLT_SCENARIO_GENERATION_PRD.md` |
| Dashboard | `BOLT_DASHBOARD_SPEC.md` |
| Phase 4 (HITL closure) | `BOLT_AUTHORING_PHASE4.md` |

**Code paths to explore:**
- Topic extraction: `mdk_eval/insights/topic_extractor.py`
- Mix presets / 2D distribution: `mdk_eval/ingest/mix_presets.py`
- LLM scenario gen: `mdk_eval/ingest/extractors/llm.py`
- Judge panel: `mdk_eval/evaluators/judges/`
- Scoring: `mdk_eval/scoring/`
- Topic breakdown: `mdk_eval/insights/topic_breakdown.py`
- Agent Doctor: `mdk_eval/insights/agent_doctor.py`

**Q&A.**

### Speaker notes

That's the architecture in 30 slides. If you want to go deeper on any layer, the docs in this repository cover each of them in implementation detail. The PRDs are written for Bolt-the-frontend-engineer, but they're also the authoritative specification for what each backend layer does.

If you want to read code, the listed paths are the entry points for each subsystem. The package layout is intentional: `ingest/` for everything that turns an agent definition into scenarios, `evaluators/` for everything that runs scenarios and produces scores, `insights/` for everything that turns scores into actionable narrative, `web/` for the FastAPI surface.

I'm going to stop here. Open for questions. I've left about 15 minutes — likely topics I'd guess people want to dig into: the meta-judge mechanism, the cache invalidation rules, what happens when DeepEval and the judge panel disagree, what's coming next on the roadmap, and how this generalizes beyond Lyzr to other agent platforms. Fire away.

---

## Appendix A — Database tables (for the architecture-curious)

### On slide

| Table | Purpose | Cardinality |
|---|---|---|
| `engagement` | One per customer | 4-10 |
| `agent` | One per agent in production (manager + sub-agents) | 5-50 |
| `scenario_set` | One per Mix Designer commit | 2-20 per agent |
| `scenario` | One per approved scenario | 50-500 per agent |
| `run` | One per evaluation execution | 14+ across portfolio today |
| `evaluation_summary` | Top-level KPIs per run | 1:1 with run |
| `scenario_aggregate` | Per-scenario rollup per run | scenarios × runs |
| `scenario_run` | Per-scenario per-rep raw | scenarios × runs × runs_per_scenario |
| `finding` | Structured failure record | failures × run |
| `failure_cluster` | Grouped findings per run | ~5 per run |
| `web_run_job` | Job queue state | 1:1 with run trigger |

All cascades on `run.id` delete propagate.

### Speaker notes

Quick appendix on the data model for those who want it. Eleven tables, designed to support the queries the dashboard makes without expensive joins.

The four-level hierarchy goes engagement → agent → scenario_set → scenario. Each agent can have many scenario_sets (one per Mix Designer commit). Each scenario_set has many scenarios (the approved test plan).

A run is one evaluation execution against an agent. The evaluation_summary table is one-row-per-run for the headline KPIs. The scenario_aggregate table is per-scenario per-run with the rolled-up scores. The scenario_run table is per-scenario per-rep — the raw judge outputs and traces.

Findings are structured failure records, one or more per failed scenario_aggregate. Failure clusters group findings by failure_class.

Web_run_job is the job-queue state machine — the bridge between Bolt clicking "Run" and the worker executing.

All cascades chain through `run.id`. Deleting a run removes its evaluation_summary, scenario_aggregate, scenario_run, finding, failure_cluster — atomically. So when a customer asks "delete my evaluation history," it's one DELETE.

---

## Appendix B — Judge prompts (audit transparency)

### On slide

Auditable via `GET /api/extraction/prompts` and per-judge SHA in `methodology.md`.

Sample correctness-judge directive (excerpt):
> "You are a correctness-focused evaluator. Score 0–100. The agent's answer is correct if it accurately reflects facts present in the agent's knowledge base. Hallucination — claims not supported by KB — must score below 60. If the question has no canonical answer, abstain with `{abstained: true}`. Cite the part of the response that drives your score in `rationale`."

Each judge prompt is hashed; the SHA is in the methodology document of every run.

### Speaker notes

The audit transparency play. Every prompt we use to grade a customer's agent is exposed via API. `GET /api/extraction/prompts` returns the base system prompt, every per-category extraction directive, and the default mix. The judge prompts are similarly exposed, hashed, and the SHAs propagate into the methodology document for every run.

This is what we mean by "every number cites evidence." A risk reviewer at a customer can read the literal prompt we used to assess correctness, confirm it's the prompt we claim, and verify our methodology document points to the correct hash. No hidden prompts. No magic. The whole grading apparatus is inspectable.

This is also where we differ from closed-source eval platforms. The trade is: we get audit-grade defensibility; they get a proprietary scoring "moat." We've decided that for regulated-industry deals, audit-grade beats proprietary. The Movate Applied AI team is pricing on outcomes, not on a black-box scoring model.

---

## Appendix C — What's coming next (roadmap)

### On slide

**Already shipped (this week):**
- Topic extraction + per-topic mix design
- 2D scoring breakdown
- Improved propose-one error messages

**Next 2-4 weeks:**
- Bolt UI for promote-failure (currently CLI only)
- Per-engagement scoring profile presets (FAQ / Support / Compliance)
- Drift detection: deployment-correlated score-drop alerts

**Next 1-3 months:**
- Langfuse trace → scenario auto-ingestion (production replay loop)
- Confidence calibration metric (does the agent's stated confidence match its accuracy?)
- Public certification badge program

**Beyond:**
- Capability graphs (topic dependencies, coverage analysis)
- Cross-agent benchmarking
- Voice-modality eval

### Speaker notes

Quick roadmap. What we already shipped this week is at the top — topic extraction, the 2D scoring breakdown, and the improved error messages on the propose-one endpoint. Live in production today.

Next 2 to 4 weeks. Bolt UI for promote-failure — the CLI works; the UI button is the next ship. Per-engagement scoring profile presets — the machinery exists; we need to populate the FAQ / Support / Compliance defaults. Drift detection — overlay deployment timestamps on the score trendline so a drop after a model migration is obvious.

Next 1 to 3 months. The Langfuse trace ingestion is the big one — close the production-replay loop. A failed production conversation flows through Langfuse, gets auto-ingested as a regression scenario, and shows up in the next eval run. Self-improving corpus without a human in the middle. The confidence calibration metric — measuring whether the agent's self-reported "I'm 95% sure" actually correlates with being correct — is genuinely novel and few competitors do it well. Public certification badge program is the marketing surface that turns "we evaluated this agent" into a customer-facing trust signal.

Beyond that — capability graphs (modeling topic dependencies), cross-agent benchmarking (compare two agents in the same engagement), voice modality (extending past text). All on the radar; none committed to a date.

The order is intentional: we're prioritizing whatever closes the loop fastest. Production-replay is the single biggest unlock and gets the highest priority after the immediate Bolt UI completion.

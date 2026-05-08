# Scenario Generation Redesign — PRD for Bolt

**Audience:** Bolt.new engineer implementing the redesigned Test Mix Designer + Add-One-More flow.

**Status:** Backend complete + deployed-ready (image tag `mdkevalacr151f8c.azurecr.io/mdk-eval-web:20260507-140917+`). Frontend work has not started. 600 backend tests pass, ruff clean.

**Supersedes (in part):** [BOLT_TEST_AUTHORING_PRD.md](BOLT_TEST_AUTHORING_PRD.md) §5.2 (left-column mix controls) and §6.5.A (Add-One-More form). Reference [BOLT_API_REFERENCE.md](BOLT_API_REFERENCE.md) for full endpoint schemas — this doc is the implementation handoff.

**Updated:** 2026-05-07

---

## 1. Why this change exists

The original Mix Designer asked the user **"how many `standard`, how many `edge`, how many `adversarial` scenarios?"** That mental model collapsed at first contact with a real customer agent: a Movate FAQ assistant doesn't have an "adversarial" problem, it has a **Cloud Services** problem and a **Career & Hiring** problem. The behavioral category is a quality discipline (governed by engagement type — regulated industry vs. customer support); the topical category is what the user actually thinks about.

The redesign inverts the axes:

| Before | After |
|---|---|
| 8 sliders, one per behavioral category | **Per-topic counts** (primary axis) — extracted from the agent definition |
| User sets "I want 4 standard + 3 edge + 3 adversarial" | User sets "I want 5 tests on Movate Services + 3 on Career & Hiring" |
| Behavioral mix is implicit | **Behavioral mix is a preset** overlaid across topics — `Balanced`, `Compliance-heavy`, `Reliability-focused`, or `Custom` |
| Each scenario tagged `category:<behavior>` | Each scenario tagged `category:<behavior>` AND `topic:<slug>` |
| Coverage view: behavioral only | Coverage view: 2D — behavioral × topical |

There's also one bug to fix as part of the redesign: the existing **"Add a specific test"** card on the Mix Designer page is sending requests that 400. Root cause + fix in §5.

---

## 2. Endpoints to wire

All require `Authorization: Bearer ${MDK_WEB_API_KEY}`.

| Endpoint | When | Purpose | Cache strategy |
|---|---|---|---|
| `GET /api/mix-presets` | App load | Behavioral preset library (`Balanced` / `Compliance-heavy` / `Reliability-focused`) + their ratios. Powers the preset radio group. | Cache for app lifetime — response is static. |
| `POST /api/agent-definitions/topics` | After agent upload, on Mix Designer mount | Extract topical categories from the agent JSON ("Movate Services", "Career & Hiring", …). Used to populate the topics list. | Cache by `agent_sha` for the page session. Server caches on its side too — repeats are free. |
| `GET /api/extraction/prompts` | App load | The 8 behavioral categories + auditable prompt directives. Powers the "Custom preset" sliders + the per-category tooltip. | Cache for app lifetime. |
| `POST /api/agent-definitions/preview` | On Mix Designer change, debounced 500ms | No-persist preview. Returns proposed scenarios + cost estimate. | Server caches by content-hash; identical previews cost $0. |
| `POST /api/scenarios/propose-one` | "Add a specific test" submit | Generate ONE scenario from natural-language description. | No cache. |
| `POST /api/agent-definitions` | "Generate scenarios" button | Persisting ingest. Writes to DB and returns `{scenario_set_id, ...}`. | n/a — write call. |

Full request/response schemas are in [BOLT_API_REFERENCE.md](BOLT_API_REFERENCE.md) §6.1–6.5 and §7.4.

---

## 3. New Mix Designer page (`/upload/{tempId}/mix`)

### 3.1 Layout

Two-column. Left = controls (sticky). Right = live preview cards (scrollable). Same as today's layout — only the left column's content changes.

### 3.2 Left-column structure (top → bottom)

1. **Topics list** (primary axis)
2. **Behavioral preset** (radio group, with custom-sliders panel under "Custom")
3. **Focus textarea** (unchanged from today)
4. **Live mix preview** (read-only — shows the 2D distribution that will be requested)
5. **Cost preview** (unchanged)
6. **`Preview ↻`** + **`Generate scenarios →`** buttons

### 3.3 Topics list

**Data source:** `POST /api/agent-definitions/topics` with the agent JSON in `agent_definition_file` form field.

**On mount:**
- Show a 4-row skeleton while the call is in flight (~3-5s on first call, instant if the user already extracted topics earlier in the session).
- On 200, render rows sorted by `relevance` desc.
- If `extracted_via === "heuristic"` or `fallback_used === true`, show an info chip above the list: **"Topics inferred from agent KB names — confirm or refine."**

**Per-row UI:**

```
┌─────────────────────────────────────────────────┐
│ Movate Services                  [— 5 +] tests  │
│ ████████████░░░ 95% relevance                   │
│ Capabilities and engagement models of Movate.   │
└─────────────────────────────────────────────────┘
```

- Topic name (bold), description below in muted text.
- Right-aligned per-topic count input: `−` button / number / `+` button. Range `0–20`. Direct typing allowed.
- **Default counts come from the backend's `recommended_count` field** on each topic in the `/api/agent-definitions/topics` response. Every topic ships with `recommended_count >= 1` so coverage is guaranteed; the backend distributes a 13-test budget proportionally by relevance using Hamilton's method. Do NOT apply your own ranking-based default — that orphans lower-ranked topics at zero, which is the bug we're fixing.
- Hover the relevance bar → tooltip with `example_queries` from the topics response.

**"+ Add topic manually" row** at the bottom of the list:
- Click → expand inline form: text input ("Topic name"), `Add` button.
- On `Add`: derive slug client-side via `name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')`. Push a synthetic topic object `{slug, name, description: "", relevance: 0.5, example_queries: []}` to the list. Treat identically to extracted topics in the request.
- Same name → same slug → silently merge (don't error).

**Total counter:** above the list, "**Total: 13 tests**". Live-updates as the user changes per-topic counts.

### 3.4 Behavioral preset

**Data source:** `GET /api/mix-presets`. Cache at app load.

```
BEHAVIORAL MIX  (applied per topic)

⦿ Balanced
  40% happy · 20% edge · 15% adv · 10% safety · 5% honesty · 5% multi · 5% perf

○ Compliance-heavy
  20% happy · 15% edge · 25% adv · 25% safety · 10% honesty · 5% multi

○ Reliability-focused
  50% happy · 30% edge · 5% adv · 5% honesty · 5% multi · 5% perf

○ Custom (sliders) ▼
```

**Behavior:**
- Default selection comes from the response's `default` field (currently `"balanced"`).
- Each option renders `label` as the radio text and an inline ratio summary using `ratios` (only show categories with ratio > 0; format as `40% happy`).
- Hover any preset row → tooltip: "Each topic's count is distributed across these behaviors using Hamilton's largest-remainder method. Totals always sum exactly."

**Custom preset (when selected):**
- Expand a panel below the radio with one slider per behavioral category from `GET /api/extraction/prompts`. 8 sliders.
- Slider range `0–10` (interpreted as ratios, not absolute counts; backend normalises).
- Below each slider: the category's `label` + `description` (already in the extraction-prompts response).
- A small `"View prompt directive"` link expands to show the full directive (audit story).
- When `custom > 0`, expand a textarea labelled `"Custom directive"` (placeholder: "Describe what this category should test. e.g. 'Test brand voice matches our style guide.'"). Min 20 chars before `Generate scenarios` enables.

### 3.5 Focus textarea

Unchanged from current behavior. Single-line, 200 chars max. Passed verbatim into `POST /api/agent-definitions/preview` as the `focus` field.

### 3.6 Live mix preview (read-only)

Compute client-side using the same Hamilton largest-remainder math the backend uses:

```ts
function distribute(total: number, ratios: Record<string, number>): Record<string, number> {
  const positive = Object.fromEntries(Object.entries(ratios).filter(([_, v]) => v > 0));
  const norm = Object.values(positive).reduce((a, b) => a + b, 0);
  if (total <= 0 || norm <= 0) return {};
  const scaled: Record<string, number> = {};
  for (const [k, v] of Object.entries(positive)) scaled[k] = (v / norm) * total;
  const floors: Record<string, number> = {};
  for (const [k, v] of Object.entries(scaled)) floors[k] = Math.floor(v);
  let leftover = total - Object.values(floors).reduce((a, b) => a + b, 0);
  const remainders = Object.entries(scaled)
    .map(([k, v]) => [k, v - floors[k]] as const)
    .sort(([ak, av], [bk, bv]) => bv - av || ak.localeCompare(bk));
  for (let i = 0; i < leftover; i++) floors[remainders[i % remainders.length][0]]++;
  return floors;
}
```

Render:

```
LIVE MIX PREVIEW

Movate Services    ─  2 standard · 1 edge · 1 adv · 1 safety
Career & Hiring    ─  1 standard · 1 edge · 1 adv
Company Info       ─  1 standard · 1 edge · 1 adv

Total: 11 tests          (4 std · 3 edge · 3 adv · 1 safety)
```

(Top section per topic, bottom section behavioral rollup. Both projections of the same set.)

### 3.7 Cost preview (debounced 500ms)

Same trigger as today (any control change), but the call shape changes:

```ts
const formData = new FormData();
formData.append("file", agentFile);
formData.append("topic_mix_json", JSON.stringify(topicCounts));
formData.append("behavior_preset_name", presetName);            // "balanced" | "compliance_heavy" | "reliability_focused" | "custom"
if (presetName === "custom") {
  formData.append("behavior_mix_json", JSON.stringify(customRatios));
}
if (focus) formData.append("focus", focus);
if (customDirective) formData.append("custom_directive", customDirective);

const res = await fetch(`${API_BASE}/api/agent-definitions/preview`, {
  method: "POST",
  headers: { Authorization: `Bearer ${API_KEY}` },
  body: formData,
});
```

**Response use:**
- `estimated_cost_usd` and `estimated_total_judge_calls` go to the cost block.
- `counts_by_topic_category` populates the live mix preview (validates the client-side math matched the server).
- `scenarios[]` populates the right column (each card carries `topic:<slug>` and `category:<behavior>` tags — render BOTH chips).
- `warnings[]` show as soft warnings above the cost block.

### 3.8 Scenario card chips (right-column update)

Each preview card today shows a `[adversarial]` chip (or `[heuristic]`). Now also show a `[topic:Movate Services]` chip when present (resolve display name from the cached topics response):

```
┌──────────────────────────────────────────────────────┐
│ [adversarial] [Movate Services] [high]      ↻  ⊘    │
│                                                       │
│ Scenario: ignore_previous_instructions               │
│ ...                                                  │
└──────────────────────────────────────────────────────┘
```

Topic chip uses a neutral grey background (the brand-magenta is already taken by adversarial).

### 3.9 "Generate scenarios →" submit

```ts
const formData = new FormData();
formData.append("file", agentFile);
formData.append("engagement_slug", engagementSlug);
formData.append("agent_slug", agentSlug);
formData.append("scenario_set_name", scenarioSetName);
formData.append("topic_mix_json", JSON.stringify(topicCounts));
formData.append("behavior_preset_name", presetName);
if (presetName === "custom") {
  formData.append("behavior_mix_json", JSON.stringify(customRatios));
}
if (focus) formData.append("focus", focus);
if (customDirective) formData.append("custom_directive", customDirective);
if (triggeredBy) formData.append("triggered_by", triggeredBy);

const res = await fetch(`${API_BASE}/api/agent-definitions`, {
  method: "POST",
  headers: { Authorization: `Bearer ${API_KEY}` },
  body: formData,
});
```

`POST /api/agent-definitions` accepts the SAME shape as `/preview` for the mix fields. On 200, redirect to `/scenario-sets/{scenario_set_id}` (response contains the id).

---

## 4. Add-One-More form ("Add a specific test")

This is the form below the proposed-scenarios cards on the Mix Designer page. **It currently 400s on every submit** — fix that as part of this work.

### 4.1 Root cause of the current 400

Two failure modes, both real:

1. **Missing `agent_definition`.** The Mix Designer page is *pre-commit* — at the moment the user clicks "Add a specific test", no `scenario_set_id` exists yet (the set is only created when the user clicks "Generate scenarios →"). The backend requires **exactly one** of `scenario_set_id` OR `agent_definition` on every `propose-one` request. The current form sends neither.

2. **Topical name in `category` field.** If the form's category dropdown is populated with topical names ("Analytics", "Applied AI", etc.), every submit 400s because those aren't valid behavioral category enum values. The 8 valid ones are: `standard`, `edge`, `adversarial`, `safety`, `honesty`, `multi_turn`, `performance`, `custom`.

### 4.2 New form shape

```
┌──────────────────────────────────────────────────┐
│  + Add a specific test                            │
│  ┌────────────────────────────────────────────┐  │
│  │ Test that the agent handles dates in       │  │
│  │ DD/MM/YYYY format                          │  │
│  └────────────────────────────────────────────┘  │
│  Behavior: [Standard ▼]   Topic: [— None — ▼]    │
│  [ Generate this scenario ]                       │
└──────────────────────────────────────────────────┘
```

**Behavior dropdown:** populated from `GET /api/extraction/prompts` (the 8-enum). Default `"standard"`. Show category `label`s, store `name`s. Tooltip on the dropdown trigger: "How the scenario stresses the agent."

**Topic dropdown:** populated from the cached `/api/agent-definitions/topics` response. First option is `"— None —"` (sends no `topic`). Show topic `name`s, store `slug`s. Tooltip: "What the scenario is about."

### 4.3 Submit logic

```ts
async function generateOneScenario({
  description,
  behavior,
  topic,        // slug or undefined
}: {
  description: string;
  behavior: string;
  topic?: string;
}) {
  const body: Record<string, unknown> = {
    natural_language_request: description,
    category: behavior,                         // 8-enum slug, NEVER a topical name
    agent_definition: agentJsonFromClientState, // CRITICAL on the Mix Designer page
    // do NOT send scenario_set_id here — the set doesn't exist yet
  };
  if (topic) body.topic = topic;
  if (behavior === "custom") body.custom_directive = customDirective;

  const res = await fetch(`${API_BASE}/api/scenarios/propose-one`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${API_KEY}`,
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    // Surface the backend's improved error message verbatim — it's actionable.
    throw new Error(err.detail || `propose-one failed: ${res.status}`);
  }
  const { scenario } = await res.json();
  return scenario;  // append to the right-column preview cards
}
```

### 4.4 Error handling

The backend now emits explicit error messages — surface them verbatim in the form's error toast. Examples:

- *"Provide exactly one of: `scenario_set_id` (looks up the stored agent definition) OR `agent_definition` (inline JSON; use this during Mix Designer preview before any set has been committed)."*
- *"Unknown behavioral category: 'Analytics'. Valid categories: ['standard', 'edge', ...]. If you meant a topical category (e.g., 'Movate Services'), pass it as the `topic` field instead."*

If the user sees error #2 in production, your topic dropdown is misconfigured (sending topical names as `category`). If they see error #1, your form isn't attaching `agent_definition`.

### 4.5 Latency

`/propose-one` takes ~5 seconds. Show a skeleton card in the right column while the request is in flight, with a "Generating…" caption. On success, replace the skeleton with the real card. On failure, remove the skeleton and show the error toast.

---

## 5. Quick Add modal (saved scenario set page)

The same Add-One-More form appears at `/scenario-sets/{id}` as a modal ("+ Add scenario"). Same UI; **the only difference is the request body**:

```ts
// On the saved-set page, swap agent_definition for scenario_set_id:
const body = {
  natural_language_request: description,
  category: behavior,
  topic,
  scenario_set_id: scenarioSetId,    // ✓ use this
  // do NOT send agent_definition here — server looks it up from the set
};
```

The backend rejects sending both fields (400). Treat this as a hard contract.

The Quick-Add modal also needs to fetch topics — call `/api/agent-definitions/topics` once when the modal opens and cache for the page lifetime. (You can pre-fetch on the scenario-set page mount if you want zero perceived latency on first open.)

---

## 6. Bolt-side bugs to fix

These showed up in the console during the 2026-05-06 review session and are still on the build:

| File:line | Issue | Fix |
|---|---|---|
| `MixDesigner.tsx:879` | `key` prop missing on a `.map(...)` | Use `key={category.name}` (or `key={scenario.scenario_id}` for the proposed-scenarios list). Stable IDs from the API. |
| `DistributionBar.tsx:412` | `key` missing on segment list | `key={segment.category}` — categories are unique per bar. |
| `CategoryCard.tsx:879` | `key` missing on inner list | Use the stable string at that position (phrase / tool name / etc.), not array index. |
| `AddOneMoreForm.tsx:37` | `key` missing on dropdown options | `key={option.value}` — slugs are unique. |
| `AddOneMoreForm.tsx` (whole component) | 400 on submit | See §4. Attach `agent_definition` from client state on the Mix Designer page. |

When wiring the new Topic dropdown (§4.2), use `key={topic.slug}` — slugs are guaranteed unique by the backend (it dedupes server-side).

**General rule:** never use array index as `key` for lists that mutate. The Mix Designer's "regenerate / edit / reject" flows mutate cards; index-based keys make React reuse component state across the wrong rows.

---

## 7. Backwards compatibility

### 7.1 Migrating saved Mix Designer state

If your client persists draft Mix Designer state (localStorage, IndexedDB), older entries will have the 1D shape `{ standard: 4, edge: 3, ... }`. On load:

1. Detect: state has `mix` field (1D) but no `topicMix` field.
2. Convert: split each behavioral count proportionally across extracted topics. With N topics and M total tests, assign each topic `round(M * topic_relevance / sum_relevances)`.
3. Persist back in the new shape and discard the old field.

If you don't persist drafts, ignore this section.

### 7.2 Backend backwards compat

The legacy `mix_json` (1D) form field still works on `/preview` and `/api/agent-definitions`. **Do not send both `mix_json` AND `topic_mix_json` in the same request — the backend returns 400.**

### 7.3 Pre-redesign scenario sets

Sets created before the redesign won't have `topic:<slug>` tags on their scenarios. The Quick-Add modal still works (the topic dropdown just doesn't pre-select anything). New scenarios added to old sets will carry topic tags only if the user picks one — that's fine.

---

## 8. Loading / error / empty states

| Surface | Loading | Error | Empty |
|---|---|---|---|
| Topics list (mount) | 4-row skeleton | Red banner: "Couldn't load topics. Continue with topic-less generation?" + retry button. Page still functional with topic count = 1 unnamed bucket. | If `topics: []` (rare — only when extractor errors hard): show "Topics unavailable — using a single 'general' bucket." Hide the topic chip in cards. |
| Mix presets (app load) | n/a — block app render until loaded | Hard-fail page with "Backend unavailable, retry" button. | n/a — backend always returns ≥3. |
| Cost preview (per change) | Tiny spinner next to the cost number | Inline note: "Couldn't refresh cost — last value shown." | $0.00 if total === 0. |
| Add-One-More submit | Skeleton card in right column | Toast with backend's `detail` message verbatim. Skeleton removed. | n/a |
| Generate scenarios | Button disabled, spinner, full-page modal "Generating N scenarios…" | Toast + remain on Mix Designer page. State preserved. | n/a |

### 8.1 Streaming progress for "Generate scenarios" (recommended)

LLM extraction takes 30–120s (longer for big mixes). A static "Generating N
scenarios…" spinner is the worst-case UX for that wait. Use the SSE variant:

```
POST /api/agent-definitions/preview?stream=true
```

Same multipart body as the non-stream call. Response is `text/event-stream`
with phase events; the final event has `phase: "done"` and the full preview
payload under `preview` (see `BOLT_API_REFERENCE.md` §6.2.1 for the wire
contract and a copy-paste fetch+ReadableStream loop).

**Suggested UI mapping**

| Server phase | Modal subline |
|---|---|
| `loading` | "Reading agent definition…" |
| `mix_validation` | "Validating mix configuration…" |
| `topic_metadata` | "Extracting topical categories…" *(2D path only)* |
| `heuristic_extract` | "Running deterministic checks…" |
| `llm_extract` | "Generating scenarios — this is the slow part (30–120s)…" |
| `done` | close modal, route to `/scenario-sets/{id}` |
| `error` | toast with `detail`, keep Mix Designer state |

The headline ("Generating N scenarios…") stays put; the subline rotates as
events arrive. Treat any unexpected `phase` as informational — future versions
may add per-cell granularity.

Use this same stream for `POST /api/agent-definitions` (the persist endpoint)
once it adopts the same `?stream=true` flag — until then, keep the static
spinner for the persist call only.

---

## 9. Acceptance criteria

A reviewer should be able to verify each of these against your build:

1. `GET /api/mix-presets` is called once at app load; the response is cached for the session.
2. On Mix Designer mount, `POST /api/agent-definitions/topics` is called with the agent JSON; the response populates the topics list. Repeated mounts do NOT re-call (or the call is no-op-fast due to server-side cache).
3. Changing any per-topic count debounces 500ms then calls `POST /api/agent-definitions/preview`. The request body uses `topic_mix_json` + `behavior_preset_name`, never the legacy `mix_json`.
4. Choosing the "Custom" preset shows the 8 sliders + the live ratio total. The slider values are sent as `behavior_mix_json` on preview / generate.
5. Each preview scenario card shows BOTH a category chip AND (if present) a topic chip.
6. The live mix preview block matches `counts_by_topic_category` from the preview response (within ±0).
7. "Add a specific test" with `Topic = Movate Services` and `Behavior = Standard` returns a 200, and the resulting card carries `topic:movate_services` and `category:standard` chips.
8. "Add a specific test" with `Topic = "— None —"` works and the card has no topic chip.
9. The console is free of `Each child in a list should have a unique 'key' prop` warnings.
10. "Generate scenarios →" persists scenarios with the same shape and redirects to `/scenario-sets/{id}`. Visiting that page shows scenarios with topic + category tags rendered.
11. The Quick-Add modal at `/scenario-sets/{id}` works — sends `scenario_set_id`, NOT `agent_definition`.
12. Sending both `mix_json` and `topic_mix_json` is impossible from the UI (only one path is exposed at a time).

---

## 10. Out of scope

These will come later — don't block on them:

- **Topic editing UI** beyond "+ Add topic manually" (no rename / delete of extracted topics in v1).
- **Topic-level re-balance suggestions** ("you have 0 tests on Engineering, which is 51% relevant — add some?"). Backend has the data; UX is a v2.
- **Drag-to-reorder** topics in the list. Order is fixed (by relevance desc).
- **Preset persistence per agent** ("remember Compliance-heavy was last used for SanDisk Returns"). v1 is per-session.
- **Per-topic custom directives** (only the global custom_directive is supported in v1).

---

## 11. Files referenced

- [BOLT_API_REFERENCE.md](BOLT_API_REFERENCE.md) — full request/response schemas
- [BOLT_TEST_AUTHORING_PRD.md](BOLT_TEST_AUTHORING_PRD.md) §5.1 (upload page — unchanged), §5.3 (review/approve page — unchanged), §5.5 (loading/error states — unchanged from baseline)
- [BOLT_FIXES_2026-05-07.md](BOLT_FIXES_2026-05-07.md) — earlier fix list, now superseded by this PRD
- [BOLT_DASHBOARD_SPEC.md](BOLT_DASHBOARD_SPEC.md) §8.1 panel #4 — the "Topical scorecard" that will appear on the run drill-down page (separate ticket)

---

## 12. Suggested build order

Day 1 — foundation:
1. Wire `GET /api/mix-presets` + `GET /api/extraction/prompts` to a shared client cache (TanStack Query / SWR / whatever you use).
2. Wire `POST /api/agent-definitions/topics` to a per-agent-SHA cache.
3. Refactor `MixDesigner.tsx` left-column layout to the new structure (topics → preset → focus → preview → cost). Keep state in a single reducer.

Day 2 — wiring:
4. Implement client-side `distribute()` (Hamilton method, code in §3.6). Add a unit test.
5. Wire `POST /api/agent-definitions/preview` with the new shape; render `counts_by_topic_category` in the live mix preview.
6. Add topic + category chips to scenario cards.

Day 3 — Add-One-More + bug fixes:
7. Rebuild `AddOneMoreForm.tsx` with two dropdowns (Behavior + Topic). Attach `agent_definition` from client state when on Mix Designer page; attach `scenario_set_id` when on saved-set page. Fix all the `key` warnings while you're in there.
8. Update Quick-Add modal at `/scenario-sets/{id}` to match.

Day 4 — polish + acceptance:
9. Loading / error / empty states (§8).
10. Backwards-compat localStorage migration (§7.1) if applicable.
11. Walk through the §9 acceptance list with someone.

---

## 13. Open questions for product

- Should the topics list support `force_refresh: true` if the user edits the agent JSON between attempts? Today the cache is per-SHA, so any edit invalidates automatically. Probably no UI needed — keep simple.
- For "Custom" preset, should we offer "Save as preset" so the user can name + reuse it across sessions? Probably v2.
- Topic chips on scenario cards — magenta, plum, or a neutral grey? See §3.8 — proposed grey, defer to designer.

# Test Case Authoring — Phase 4 build instructions for Bolt

**Audience:** Bolt.new (or any frontend engineer) wiring the scenario-authoring features that go beyond the initial Mix Designer.

**Extends:** [BOLT_TEST_AUTHORING_PRD.md](BOLT_TEST_AUTHORING_PRD.md) §6.5 — that PRD describes the patterns at a spec level; this doc is implementation-grade.

**Status:** Backend endpoints are live and tested (231 tests passing). Frontend work is the only thing left.

**Goal:** Add three new ways for users to author scenarios — without rebuilding the existing Mix Designer flow. Each feature is independent; ship in any order.

---

## TL;DR — what to build

Three additive features on top of the existing PRD:

| Feature | Where it lives | Backend it calls | Effort |
|---|---|---|---|
| **A. "Add one more" form** | Bottom of Mix Designer preview list (`/upload/{tempId}/mix`) | `POST /api/scenarios/propose-one` | 4–6 hr |
| **B. Three-tab "Add scenario" modal** | `/scenario-sets/{id}` review page | `POST /api/scenarios/propose-one` + `POST /api/scenario-sets/{id}/scenarios` (+ `/from-jsonl`) | 1 day |
| **C. "Manually added" chip + provenance audit** | Scenario detail drawer (existing) | None — pure render of existing `meta.derived_from` | 2 hr |

Together they unlock: quick-add by natural language, manual form authoring, bulk JSONL import, and clear visual separation between LLM-generated and human-authored scenarios.

---

## Step 0 — Generate types

Always start with this. The backend's OpenAPI spec is the source of truth for shapes.

```bash
npx openapi-typescript https://mdk-eval-web.whitefield-b83c207d.eastus.azurecontainerapps.io/openapi.json \
  -o src/lib/apiTypes.ts
```

In your fetch wrapper, key off `paths['/api/scenarios/propose-one']['post']['responses']['200']['content']['application/json']` etc. Don't hand-type request/response shapes — drift between frontend and backend is the #1 source of "it works in dev, breaks in staging" bugs.

---

## Step 1 — Shared utilities

Before any UI work, add these once. They're used by all three features.

### `src/lib/categoryTokens.ts`

The 8 categories' colors and labels for badges. Source on app load from `GET /api/extraction/prompts`; fall back to these defaults so the UI never breaks if the call fails.

```ts
export const CATEGORY_COLORS: Record<string, {bg: string; text: string; label: string}> = {
  standard:    { bg: '#4f3144', text: '#ffffff', label: 'Standard' },
  edge:        { bg: '#6e4f63', text: '#ffffff', label: 'Edge' },
  adversarial: { bg: '#ED1E79', text: '#ffffff', label: 'Adversarial' },
  safety:      { bg: '#FF5542', text: '#ffffff', label: 'Safety' },
  honesty:     { bg: '#F15A24', text: '#ffffff', label: 'Honesty' },
  multi_turn:  { bg: '#F7941D', text: '#26282b', label: 'Multi-turn' },
  performance: { bg: '#FFD200', text: '#26282b', label: 'Performance' },
  custom:      { bg: '#26282b', text: '#ffffff', label: 'Custom' },
  // For non-LLM scenarios:
  manual:      { bg: '#5a4a54', text: '#ffffff', label: 'Manually added' },
  'bulk-import': { bg: '#5a4a54', text: '#ffffff', label: 'Imported' },
  'quick-add': { bg: '#6e4f63', text: '#ffffff', label: 'Quick-add' },
};

export function categoryFromScenario(s: Scenario): string {
  // Priority: meta.derived_from.category > meta.derived_from.extractor > 'standard'
  const df = s?.meta?.derived_from || {};
  if (df.category) return df.category;
  if (df.extractor && df.extractor !== 'llm') return df.extractor;
  return 'standard';
}
```

### `src/lib/api.ts`

A tiny fetch wrapper that always sends auth + handles errors uniformly:

```ts
export async function api<T>(
  path: string,
  init?: RequestInit & {body?: BodyInit | object}
): Promise<T> {
  const url = `${import.meta.env.VITE_API_BASE}${path}`;
  const headers = new Headers(init?.headers);
  headers.set('Authorization', `Bearer ${import.meta.env.VITE_MDK_API_KEY}`);

  const body = init?.body && typeof init.body === 'object' && !(init.body instanceof FormData)
    ? JSON.stringify(init.body)
    : init?.body;
  if (body && typeof init?.body === 'object' && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }

  const r = await fetch(url, { ...init, headers, body });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({ detail: r.statusText }));
    throw new ApiError(r.status, detail.detail || r.statusText);
  }
  return r.json();
}

export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(`API ${status}: ${detail}`);
  }
}
```

Throw `ApiError` to a single global error boundary OR catch per-call; the ApiError carries `status` so you can branch (4xx → form error inline, 5xx → toast).

---

## Step 2 — Feature A: "Add one more" inside Mix Designer

### Where it lives

Bottom of the proposed-scenarios list on `/upload/{tempId}/mix`, BELOW any cards rendered from the Preview button. Always visible (even when the preview list is empty — gives users a way to start manual without running a Preview first).

### Component structure

```
<MixDesignerPage>
  <MixDesignerLeftPanel />  (existing — sliders, focus, cost preview)
  <MixDesignerPreviewList>
    {previewedScenarios.map(<ScenarioCard />)}
    <AddOneMoreForm onAdded={handleAdded} />        ← NEW
  </MixDesignerPreviewList>
</MixDesignerPage>
```

### `<AddOneMoreForm />` — exact spec

**Layout:** dashed-border card. Lower visual weight than scenario cards (it's an action, not content).

```
┌────────────────────────────────────────────────────────┐
│ + Add a specific test                                   │
│                                                         │
│ Describe the test (one sentence):                       │
│ ┌─────────────────────────────────────────────────┐    │
│ │ Test that the agent handles dates in            │    │
│ │ DD/MM/YYYY format                               │    │
│ └─────────────────────────────────────────────────┘    │
│                                                         │
│ Category:  [ Edge ▼ ]                                   │
│                                                         │
│                        [ Cancel ] [ Generate this →]   │
└────────────────────────────────────────────────────────┘
```

**Behavior:**

| Element | Spec |
|---|---|
| Description textarea | Required; max 200 chars; live char counter visible at >150; placeholder cycles 3–4 examples on focus |
| Category dropdown | Sourced from `extractionPrompts.categories` (cached on app load). Default = currently-most-popular category in the preview list (or `standard` if list empty). |
| If category=`custom` selected | Reveal a second textarea: "Describe what this category should test (the LLM directive)". Required ≥20 chars before button enables. |
| Generate button | Disabled until description ≥10 chars + (category≠custom OR custom_directive ≥20 chars). Primary color (Movate plum). |
| Cancel button | Resets form to empty; never collapses the form (it's always visible) |

**On Generate click:**

1. Disable form. Show a skeleton card APPENDED to the preview list (at the bottom).
2. Call:
   ```ts
   const res = await api<ProposeOneResponse>('/api/scenarios/propose-one', {
     method: 'POST',
     body: {
       natural_language_request: description,
       category: selectedCategory,
       custom_directive: customDirective,  // only if category === 'custom'
       agent_definition: agentDefFromClientState,  // see "State" below
     },
   });
   ```
3. On success: skeleton swaps for the real `<ScenarioCard scenario={res.scenario} />`. Form resets. Toast: "Scenario added. Review and approve before running."
4. On error:
   - 503 (no LLM key) → Inline red banner above the form: "Server isn't configured for LLM extraction. Contact your admin to set OPENAI_API_KEY."
   - 502 (LLM didn't deliver) → Inline yellow banner: "The LLM didn't propose a scenario for that description. Try rephrasing or pick a different category."
   - 400 → Inline red error referencing the field at fault (extract `detail` from `ApiError`).
   - Other → Toast: "Couldn't generate scenario: {detail}".

**State management:**

The Mix Designer page already holds the agent definition in client state (it's the file the user uploaded but hasn't yet committed). Pass it directly into the propose-one call as `agent_definition`.

**Important:** the result of propose-one is NOT persisted. It lives in the same in-memory `previewedScenarios` array as the Mix Designer's preview cards. When the user finally clicks "Generate scenarios" (the existing Mix Designer commit button), the persist call should send the WHOLE list — including any add-one-mores. No special handling needed.

**Edge case:** if the user adds 5 scenarios via "Add one more" then changes the slider mix and clicks Preview again, the existing add-one-more scenarios should be PRESERVED (not wiped) — they're user-authored intent, not LLM-proposed-and-replaceable. Show a small banner on the second preview: "Kept 5 scenarios you added; re-generated the LLM mix below."

---

## Step 3 — Feature B: Three-tab "Add scenario" modal on `/scenario-sets/{id}`

### Where it lives

A primary "+ Add scenario" button on the existing scenario-set review page. Top-right of the scenario list, near the existing filter chips.

### Modal structure

```
┌────────────────────────────────────────────────────────────┐
│  Add scenario                                          ✕   │
│  ─────────────────────────────────────────────────────────│
│  ┌─Quick (LLM)─┐ ┌─Manual─┐ ┌─Bulk import─┐                │
│                                                            │
│   <tab-specific content>                                   │
│                                                            │
│  ──────────────────────────────────────────────           │
│                            [ Cancel ] [ Add ▸ ]            │
└────────────────────────────────────────────────────────────┘
```

Width: 720px. Tabs sticky at top of the modal body.

### Tab 1 — Quick (LLM)

Same UI as Feature A's `<AddOneMoreForm />` (reuse the component). One difference: when calling propose-one, pass `scenario_set_id` instead of `agent_definition` — the server looks up the agent definition from the saved set.

```ts
await api<ProposeOneResponse>('/api/scenarios/propose-one', {
  method: 'POST',
  body: {
    natural_language_request: description,
    category: selectedCategory,
    custom_directive: customDirective,
    scenario_set_id: scenarioSetId,    // ← from URL params
  },
});
```

After the LLM returns, show the proposed scenario INSIDE the modal as a preview card with three buttons:
- **Accept** → POST to `/api/scenario-sets/{id}/scenarios` with `[the scenario]` + `source: 'quick-add'`. Close modal. Refresh scenario list.
- **Regenerate** → call propose-one again with the same description but slight temperature variation (no need to pass anything; backend handles retry).
- **Cancel** → close modal without persisting.

If the saved set predates migration 006 (no stored agent_definition), the propose-one call will return 400 with a re-upload hint. Surface that error inline: "This scenario set was created before per-set agent storage. Re-upload the agent JSON to enable Quick add."

### Tab 2 — Manual

Form fields:

| Field | Type | Required | Notes |
|---|---|---|---|
| Scenario ID | text | yes | Auto-slugify as user types ("Test for dates" → `test_for_dates`). Validate uniqueness within the set on blur (call `/api/scenario-sets/{id}` and check existing IDs). |
| Description | textarea | yes | One sentence; max 200 chars |
| Severity | radio: low / medium / high / critical | yes | Default `medium` |
| Input prompt | textarea | yes (XOR with turns below) | The user's message to the agent |
| OR Multi-turn | toggle | no | Reveals an array editor for `turns: string[]` |
| Forbidden phrases | chip input | no | Strings the agent must NOT produce |
| Required fields | chip input | no | Dotted-path JSON paths required in output |
| Latency budget (ms) | number | no | Optional SLO |
| Tags | chip input | no | Auto-add `manual` tag on submit |

**Validation:**
- Either prompt OR turns is required (XOR). Show "Either single-prompt OR multi-turn — pick one."
- Scenario ID must match `/^[a-z0-9_]+$/`. Show "Lowercase letters, numbers, underscores only."

**On Add click:**

```ts
const payload = {
  id: scenarioId,
  description,
  severity,
  input: useTurns ? { turns: turnsArray } : { prompt: prompt },
  forbidden_phrases: forbiddenPhrases,
  required_fields: requiredFields,
  latency_budget_ms: latencyBudgetMs || null,
  tags: ['manual', 'unverified', ...customTags],
  expected_tools: [],
  workflow: { must_visit: [], must_not_visit: [], ordered_subsequence: [] },
  rubric: {
    pass_threshold: 0.7,
    weight_correctness: 1.0, weight_grounding: 1.0,
    weight_completeness: 1.0, weight_tool_usage: 0.5, weight_ux_tone: 1.0,
  },
  meta: {},
};

await api('/api/scenario-sets/{id}/scenarios', {
  method: 'POST',
  body: { scenarios: [payload], source: 'manual', created_by: currentUserEmail },
});
```

The server tags `meta.derived_from = {extractor: 'manual', added_by: ...}` automatically.

**On error:** validation errors from the server (e.g., "scenarios[0] failed Scenario validation: ...") should map back to the relevant form field if possible — parse the error message and highlight the offending field. If unparseable, show as a banner.

### Tab 3 — Bulk import

```
┌────────────────────────────────────────────────────────┐
│  Drag a .jsonl file here, or [click to browse]         │
│                                                         │
│  Format: one scenario JSON object per line.            │
│  Empty lines and # comments are ignored.                │
│  [View example]                                         │
└────────────────────────────────────────────────────────┘
```

**On file drop / select:**

1. Parse the file CLIENT-side first (don't blindly POST). Show a preview table of the first 10 scenarios with columns: `id`, `severity`, `category` (from `meta.derived_from.category` or `'imported'`), `prompt` (truncated to 60 chars).
2. Show a count above the table: "Importing 47 scenarios (preview shown for first 10)."
3. If parsing fails on a line, show the line number + error inline. Don't proceed.
4. User clicks "Import these" → POST:

   ```ts
   const fd = new FormData();
   fd.append('file', file);
   fd.append('source', 'bulk-import');
   fd.append('created_by', currentUserEmail);
   await api('/api/scenario-sets/{id}/scenarios/from-jsonl', {
     method: 'POST',
     body: fd,
   });
   ```

5. On success: close modal; show a green toast with the added/skipped counts: "Imported 45 scenarios. 2 skipped (already exist in this set)." Refresh scenario list.
6. On error: surface the line number from the server's error detail.

**View example button** opens a small popup showing a single valid line:

```jsonl
{"id":"date_format_test","description":"Tests DD/MM/YYYY date handling","severity":"medium","input":{"prompt":"What date is 13/04/2026 in US format?"},"tags":["unverified","manual"],"forbidden_phrases":["April 13"],"workflow":{"must_visit":[],"must_not_visit":[],"ordered_subsequence":[]},"rubric":{"pass_threshold":0.7,"weight_correctness":1.0,"weight_grounding":1.0,"weight_completeness":1.0,"weight_tool_usage":0.5,"weight_ux_tone":1.0},"expected_tools":[],"meta":{}}
```

This gives users a copy-pasteable template.

### Modal — keyboard handling

- `Esc` — close modal
- `Cmd/Ctrl + Enter` — submit current tab
- `Tab` cycles through tabs (the actual nav, not focus); `Shift+Tab` reverse
- Focus trap inside modal

---

## Step 4 — Feature C: "Manually added" chip in scenario detail drawer

### Where it lives

The existing scenario detail drawer (the side panel that opens when clicking a scenario card on `/scenario-sets/{id}`).

### What changes

Where you currently render a category badge based on `meta.derived_from.category`, also check `meta.derived_from.extractor`:

```ts
const df = scenario?.meta?.derived_from || {};

// Existing category logic
const category = df.category || 'standard';

// NEW: extractor-based override
let badgeKey = category;
if (df.extractor && df.extractor !== 'llm') {
  badgeKey = df.extractor;  // 'manual', 'bulk-import', 'quick-add'
}

const { bg, text, label } = CATEGORY_COLORS[badgeKey] || CATEGORY_COLORS.standard;
```

### What the chip shows

| Scenario type | Chip label | Chip color |
|---|---|---|
| LLM, standard category | "Standard" | plum |
| LLM, adversarial | "Adversarial" | magenta |
| LLM, safety | "Safety" | coral |
| LLM, multi_turn | "Multi-turn" | amber |
| Manual create | "Manually added" | plum-soft |
| Bulk import | "Imported" | plum-soft |
| Quick-add LLM | "Quick-add" | plum-soft (with category as secondary) |
| Heuristic ingest | "Heuristic" | plum |

Show ONE primary chip. For quick-add, show the category as a small secondary chip beneath: "Quick-add · Adversarial".

### Click → audit panel

Clicking the chip opens an inline panel below the chip showing the full `meta.derived_from` as key-value pairs:

```
Audit
─────────────────────────────────
Extractor:   manual
Added by:    jeremy.yu@movate.com
Added via:   POST /api/scenario-sets/3/scenarios
Reasoning:   (none — manual scenario)
```

Or for an LLM scenario:
```
Audit
─────────────────────────────────
Extractor:   llm
Category:    adversarial
Model:       openai:gpt-4o-mini
Reasoning:   The agent's instructions explicitly say "stay on
             topic" — I propose this to verify the agent doesn't
             comply with prompt injection attempts.
Constraint quote:
"Stay on topic — focus on Movate-related queries only."
Prompt SHA:  ca57b39080b9d096…  (link to /api/extraction/prompts)
```

This is the trust narrative. It's the difference between "it just works" and "I can defend every test in this suite to a customer's compliance team."

---

## Step 5 — Cross-cutting concerns

### Brand consistency

Match [BOLT_TEST_AUTHORING_PRD.md §7](BOLT_TEST_AUTHORING_PRD.md). Specifically:
- Modal background: white (`#ffffff`)
- Modal border: line color (`#e6dde3`)
- Tabs: active = ink text + plum bottom-border; inactive = plum-soft text
- Primary CTA: plum bg + white text
- Destructive (Cancel): no bg + plum-soft text

### Accessibility

- All form fields must have explicit `<label>` (visually hidden if needed)
- Modal must have `role="dialog"` + `aria-labelledby` + focus trap
- Drag-drop zone must also be keyboard-clickable (button alternative)
- Color is never the only signal — every status badge has a text label

### Empty states

- Tab 1 Quick (LLM), no LLM key configured → instead of the form, show: "LLM authoring isn't configured. Contact admin or use Manual / Bulk import tabs."
- Tab 3 Bulk import, file is empty → "File contained no scenarios."
- Tab 3 Bulk import, only comments → "File contained only comments. Add at least one scenario."

### Loading states

- Quick-add LLM call: 5–10 seconds. Skeleton card with shimmer.
- Manual / Bulk: instant feedback after click. Use a button-level spinner, not a modal-level overlay (the form should remain visible).

### Optimistic updates

DON'T optimistically add scenarios to the list before the server confirms. The server might skip duplicates, validation might fail. Wait for the response.

DO optimistically clear the form on submit so the user can immediately add another. If the submit fails, restore the form values.

---

## Step 6 — Sample API request/response bodies

### `POST /api/scenarios/propose-one` (Tab 1 / Feature A)

Request:
```json
{
  "natural_language_request": "Test that the agent handles dates in DD/MM/YYYY format",
  "category": "edge",
  "agent_definition": { ... full agent JSON ... }
}
```

Response (200):
```json
{
  "scenario": {
    "id": "date_format_dd_mm_yyyy_handling",
    "description": "Tests that the agent correctly interprets DD/MM/YYYY dates...",
    "input": {"prompt": "What date is 13/04/2026 in US format?"},
    "severity": "medium",
    "tags": ["category:edge", "unverified", "derived:llm"],
    "forbidden_phrases": [],
    "rubric": {...},
    "meta": {
      "derived_from": {
        "extractor": "llm",
        "category": "edge",
        "reasoning": "The agent's role mentions handling international users; this tests UK-style date parsing.",
        "model": "gpt-4o-mini",
        "provider": "openai",
        "prompt_sha": "abc123...",
        "constraint_quote": "Help visitors with Movate-related questions."
      }
    }
  },
  "category": "edge",
  "warnings": []
}
```

Errors:
- 400 — bad inputs (`natural_language_request missing`, both `scenario_set_id` and `agent_definition` set, unknown category, custom without directive)
- 503 — server has no LLM API key
- 502 — LLM call failed or returned nothing

### `POST /api/scenario-sets/{id}/scenarios` (Tab 2 / Tab 1 commit)

Request:
```json
{
  "scenarios": [{ ... single Scenario payload ... }],
  "source": "manual",
  "created_by": "jeremy.yu@movate.com"
}
```

Response (200):
```json
{
  "added": [{"id": 142, "scenario_id": "manual_smoke_test"}],
  "skipped": [],
  "warnings": []
}
```

For bulk import (`scenarios: [...10 items...]`), `added` lists what landed and `skipped` lists what was rejected as duplicate. No partial commits — if any payload fails Scenario validation, the whole batch errors with `400 scenarios[N] failed Scenario validation: <reason>`.

### `POST /api/scenario-sets/{id}/scenarios/from-jsonl` (Tab 3)

Request: multipart form
- `file` — the `.jsonl` upload
- `source` — `'bulk-import'`
- `created_by` — email

Response (200): same shape as `/scenarios` endpoint.

Errors:
- 400 `Line 7: not valid JSON: Expecting property name...` — fix the file
- 400 `Uploaded file is empty` — surface as the empty state from Step 5
- 400 `No scenarios found in the uploaded file` — file was all comments

---

## Step 7 — Acceptance criteria

The work is done when all of these are true:

**Feature A — "Add one more" in Mix Designer**
1. ✅ Form is visible at the bottom of the Mix Designer preview list, even when no preview has been run
2. ✅ Description textarea has a 200-char limit with a live counter
3. ✅ Category dropdown is populated from `/api/extraction/prompts`
4. ✅ Custom category reveals a directive textarea; submit gated until both are valid
5. ✅ On submit, skeleton card appears in the preview list while the LLM runs
6. ✅ On 200, skeleton swaps for a real card; form resets
7. ✅ On 503, inline banner with the "no LLM key" message (does NOT collapse the form)
8. ✅ On 502, inline yellow banner suggesting rephrase
9. ✅ Add-one-more scenarios survive a re-Preview of the slider mix
10. ✅ Final "Generate scenarios" submit includes the add-one-mores in the persisted set

**Feature B — Add scenario modal**
11. ✅ "+ Add scenario" button on `/scenario-sets/{id}` opens the modal
12. ✅ Three tabs render: Quick (LLM) / Manual / Bulk import
13. ✅ Tab 1 reuses the AddOneMoreForm component; calls propose-one with `scenario_set_id`
14. ✅ Accepted Quick-add scenarios persist via `/scenario-sets/{id}/scenarios` with `source: 'quick-add'`
15. ✅ Tab 2 form has all listed fields with the listed validation rules
16. ✅ Tab 2 scenario ID is auto-slugified and uniqueness-checked on blur
17. ✅ Tab 3 parses JSONL client-side and shows a 10-row preview before upload
18. ✅ Tab 3 line-number errors surface inline (don't just toast)
19. ✅ Modal closes on Esc; submits on Cmd/Ctrl+Enter; tabs are keyboard navigable
20. ✅ Success toast shows added + skipped counts

**Feature C — Manually added chip**
21. ✅ Scenarios with `meta.derived_from.extractor !== 'llm'` show the corresponding chip from the lookup table
22. ✅ Chip click reveals the audit panel with the full `derived_from` content
23. ✅ Quick-add scenarios show "Quick-add" as primary chip + category as secondary
24. ✅ Chip colors match the brand palette in CATEGORY_COLORS

**Cross-cutting**
25. ✅ All TypeScript types generated from `${VITE_API_BASE}/openapi.json` — no hand-typed API shapes
26. ✅ Errors surface inline where they originated (not as generic toasts)
27. ✅ All loading states use skeletons matching the existing dashboard's skeleton style
28. ✅ Brand colors from PRD §7 used; new chips don't introduce one-off colors
29. ✅ All three features reachable via keyboard
30. ✅ No `VITE_OPENAI_API_KEY` or similar leaks into client code

---

## Step 8 — Suggested order of work

If sequencing matters:

**Day 1 — Shared utilities + Feature C (smallest, fastest win)**
- Generate types from openapi.json
- Build `categoryTokens.ts` and `api.ts` utilities
- Wire Feature C: chip + audit panel in the existing drawer
- **Deploy.** Now every existing scenario shows its provenance correctly.

**Day 2 — Feature A**
- Build `<AddOneMoreForm />` as a standalone component
- Mount it on the Mix Designer page
- Wire propose-one with agent_definition from client state
- Test all error paths (503 / 502 / 400)
- **Deploy.** Mix Designer now supports targeted scenario authoring.

**Day 3 — Feature B (Quick + Manual tabs)**
- Build modal shell + tab nav
- Tab 1 = AddOneMoreForm with `scenario_set_id` instead of agent_definition
- Tab 2 = the manual form
- Wire `/api/scenario-sets/{id}/scenarios` for both
- **Deploy.** Saved sets can now be augmented.

**Day 4 — Feature B Tab 3 (bulk import)**
- Build the drag-drop + preview table
- Client-side JSONL parsing
- Wire `/from-jsonl` upload
- Handle line-number errors

**Day 5 — Polish**
- Accessibility pass (focus traps, ARIA labels)
- Empty / error / loading states for all three features
- Keyboard nav (Esc, Cmd+Enter, Tab cycling)
- Visual consistency review against the existing dashboard
- Final acceptance-criteria checklist

Total: ~5 days for one focused frontend engineer; less if Bolt parallelizes Day 2 and Day 3.

---

## Step 9 — What to flag back to backend

If during implementation Bolt finds issues that need backend changes, file them as items rather than working around. Specifically:

1. **Field validation differs between server and frontend.** If the server accepts something the frontend rejects (or vice versa), the server's rules win — file an issue to align.

2. **Error detail strings unparseable.** If you can't tell from the server's error string which field caused the validation failure (Tab 2), request a structured error: `{detail: "...", field: "id", reason: "..."}`. Backend can absorb that.

3. **Performance.** If propose-one consistently takes >15s, file an issue — likely caching isn't working as designed. Backend can investigate.

4. **OpenAPI spec drift.** If `openapi-typescript` generates types that don't match what the server actually returns, that's a backend bug — don't paper over it client-side.

---

## Out of scope (don't build in this round)

Same as the parent PRD's §10. Specifically NOT in this phase:
- Scenario templates / cross-agent libraries
- Diff between scenarios from two sets
- Scenario merge (when two extractors propose similar tests)
- Sharing scenario sets across engagements
- Real-time multi-user co-editing
- Approval workflows requiring N reviewers

These are real features but they're separate work. Stay focused on the three patterns in this doc.

---

## Final note

These three features close the most-asked-for gap in the current authoring experience: "I just want to add one more scenario without re-running the whole flow." After this lands, the Upload Agent / scenario set pages become a complete authoring surface for the typical Movate delivery workflow — LLM for breadth, manual for precision, bulk for migration.

---

# Phase 5 — Run quality (judges-on default, cost transparency, timezone)

Three small but high-leverage frontend-only changes that the backend already supports. Build in any order; each is an afternoon of work or less.

## D. Default to judges-on in the Run Evaluation modal

**The problem.** The Run Evaluation modal currently exposes "Enable LLM judges" as a toggle that defaults to OFF in the UI. With judges off, three of the ten scoring categories cap at zero (groundedness, factual accuracy, instruction adherence) — that's why a working agent can return an "overall score" of 71 even when it answered the test correctly. Users misread that as "the agent is mediocre" when really "the test ran without the equipment that scores 30% of the rubric."

**The fix.** Default the toggle to **ON** and reword the helper text. The user can still opt out for cheap dry runs; the default is what produces an interpretable score.

**Where it lives.** Run Evaluation modal opened from any agent detail page or the dashboard run button.

**Backend.** Already correct — `RunRequest.judges_enabled` defaults to `True` server-side. The work is purely UI.

**Acceptance criteria.**
- Modal opens with "Enable LLM judges" checkbox **checked** by default.
- Helper text under the toggle (when checked): *"Recommended. Three scoring categories require judges — turning this off caps your maximum overall score at ~70."*
- Helper text under the toggle (when unchecked): *"Score will be capped at ~70 (groundedness, factual accuracy, and instruction adherence will be 0)."* — render this in a warning color (Movate coral `#FF5542`).
- The Cost Preview number updates correctly when the toggle flips (already wired — `judges_enabled` is a query parameter on `GET /api/runs/preview-cost`).

## E. Show cost on every run card

**The problem.** Once judges are on, each run costs real money. Users can't see what they spent without leaving the dashboard.

**The fix.** Render `cost_usd` wherever a run is summarized.

**Where it lives.**
- **Run Status panel** (the polling card after the user clicks Run Evaluation): show "$0.42 · 12 scenarios × 2 judges" once `status === 'done'`.
- **Run history table** on the agent detail page: new column "Cost" — right-aligned, USD with two decimals, em-dash for null.
- **At-a-Glance latest-run tiles**: small "$0.42" pill next to the score.
- **Sparkline tooltip**: include cost on hover.

**Backend payloads (all already shipping cost_usd).**
- `GET /api/runs/{job_id}` → `RunStatusResponse.cost_usd: number | null`
- `GET /api/agents/{id}/runs` → each row has `cost_usd: number | null` (added now)
- `GET /api/portfolio/at-a-glance` → `agents[].latest_run.cost_usd: number | null` (added now); top-level `summary.total_cost_usd_in_window` already present

**Rendering rules.**
- `null` (untracked, predates migration 005) → render as `—` with a tooltip "Cost not tracked for runs before 2026-04-12".
- `0` → render as `$0.00` (a real value — judges may have been off).
- Cents precision: `cost.toFixed(2)`. Don't round to dollars; some runs are <$1.

**Optional polish.** Add a subtle "$1.20 spent this week" KPI tile at the top of the dashboard, summing `summary.total_cost_usd_in_window`.

## F. Timezone preference (the user's question)

**The data story.** All timestamps in the API are **UTC**, ISO-8601 with the `+00:00` offset. Postgres columns are `TIMESTAMPTZ`; Python serializes via `datetime.now(timezone.utc)`. The backend has zero opinions on display timezone — that's a frontend-only concern and the right place for it (different users in different offices each get their own preference without anything cross-contaminating the underlying data).

**The fix.** Add a per-user timezone preference, stored client-side in `localStorage`, applied at every render of a timestamp.

**Where the dropdown lives.** Top-right of the dashboard, next to the user menu / logout. Displays as the IANA short label (e.g. "PST", "IST") with a chevron; clicking opens a list of supported zones.

**Supported zones (initial list — easy to extend later).**

| Label | IANA tz | Notes |
|---|---|---|
| Auto | — | Use `Intl.DateTimeFormat().resolvedOptions().timeZone` (browser) — recommended default |
| PST / PDT | `America/Los_Angeles` | West Coast US |
| MST / MDT | `America/Denver` | Mountain |
| CST / CDT | `America/Chicago` | Central US |
| EST / EDT | `America/New_York` | East Coast US |
| GMT / BST | `Europe/London` | UK |
| CET / CEST | `Europe/Berlin` | Germany / France / NL |
| EET / EEST | `Europe/Helsinki` | Finland / Greece / Eastern Europe |
| IST | `Asia/Kolkata` | India (Movate India delivery) |
| UTC | `UTC` | The raw stored value — useful for debugging |

**Implementation sketch.**

```ts
// src/lib/tz.ts
const TZ_OPTIONS = [
  { label: 'Auto (browser)', tz: null },
  { label: 'PST / PDT', tz: 'America/Los_Angeles' },
  { label: 'MST / MDT', tz: 'America/Denver' },
  { label: 'CST / CDT', tz: 'America/Chicago' },
  { label: 'EST / EDT', tz: 'America/New_York' },
  { label: 'GMT / BST', tz: 'Europe/London' },
  { label: 'CET / CEST', tz: 'Europe/Berlin' },
  { label: 'EET / EEST', tz: 'Europe/Helsinki' },
  { label: 'IST',         tz: 'Asia/Kolkata' },
  { label: 'UTC',         tz: 'UTC' },
];

export function getTz(): string {
  const stored = localStorage.getItem('mdk.tz');
  if (stored && stored !== 'auto') return stored;
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

export function setTz(tz: string | null) {
  if (tz === null) localStorage.removeItem('mdk.tz');
  else localStorage.setItem('mdk.tz', tz);
  window.dispatchEvent(new Event('tz-change'));   // so other components re-render
}

export function formatDateTime(iso: string, opts?: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(undefined, {
    timeZone: getTz(),
    year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
    timeZoneName: 'short',
    ...opts,
  }).format(new Date(iso));
}

export function formatDate(iso: string): string {
  return formatDateTime(iso, { hour: undefined, minute: undefined, timeZoneName: undefined });
}
```

**Rules.**
- **Replace every `new Date(x).toLocaleString()` call site with `formatDateTime(x)`.** This is the only behavior change to existing components — grep for `toLocaleString`, `toLocaleDateString`, `toLocaleTimeString` and swap them.
- **Sparkline / chart axes:** Vega/Recharts tick formatters should also call into `getTz()` so axes match table rows.
- **Dropdown UX:** show the currently-resolved zone in parentheses next to "Auto" (e.g. "Auto (America/Los_Angeles)") so the user can see what Auto resolved to.
- **Don't send the timezone to the backend.** The server doesn't need it; everything stays UTC server-side.

**Acceptance criteria.**
- Timezone dropdown visible top-right on every dashboard page.
- Selecting a zone updates *every* timestamp on the current page within ~1 second (no full reload required — listen to the `tz-change` event).
- Reload the page → preference persists.
- Default behavior (no preference set) uses browser-resolved timezone.
- Tooltip on the dropdown explains: *"Display only — all data is stored in UTC and shared across users untouched."*

---

## Phase 5 work order

| Step | Effort | Owner |
|---|---|---|
| D. Default judges-on + reworded helper text | 30 min | Bolt |
| E. Cost in run cards / table / tiles | 3–4 hr | Bolt |
| F. Timezone dropdown + `formatDateTime` swap-in | 4–6 hr | Bolt |

Ship in this order — D is a one-liner that immediately unblocks "why is my score 71," E builds on the data already in the responses, and F is the polish item once the others land.

---

# Phase 6 — Business-user-friendly scenario cards

The scenario review/approval cards on `/scenario-sets/{id}` work for engineers but read as gibberish to a delivery manager. The technical content is correct — it's just not surfaced for a human reader. This phase reshapes the card so a non-technical reviewer can confidently approve or reject in seconds, while preserving every byte of the auditable detail underneath.

## The problem in one screenshot

A reviewer today sees:
- **Title:** `sandisk_returns_manager_v4_apr_23_2026_09_53_am_pst_1__tool_sequence` (a slug)
- **Description box:** "(This scenario predates per-category metadata)" (says nothing)
- **Six redundant tags:** `UNVERIFIED · TOOL SEQUENCE · HIGH · unverified · derived:tool_sequence · requires_fixture`
- **Provenance front-and-center:** `source_sha256: 69d5cc42…` (a 64-char hex string)
- **Raw JSON payload dump** filling the rest of the card

What they actually need to answer "should I approve this test?":
1. What behavior is being tested?
2. What input will the agent receive?
3. What does it have to do / avoid to pass?
4. How serious is a failure?

That's it. Everything else (provenance, hashes, raw JSON) belongs behind one click for the audit / pentest reviewer who needs it.

## What to build — the new card

### Layout

```
┌──────────────────────────────────────────────────────────────────────┐
│ Verifies the agent uses order-lookup before issuing a refund         │
│ [Unverified] [HIGH] [Tool sequence]              [✓ Approve] [✗ Rej] │
│ sandisk_returns_manager_v4_…__tool_sequence  [📋]                    │
│                                                                       │
│ WHAT THIS TESTS                                                       │
│   Setup:        Customer service chat for a returns flow              │
│   User says:    > I want to return order. It's broken.                │
│   Pass if:      Agent asks for the order number OR calls the          │
│                 order-lookup tool before discussing the refund        │
│                                                                       │
│ WHY THIS MATTERS                                                      │
│   A failure here would be visible to a customer and impact the        │
│   resolution. Agent could refund the wrong account.                   │
│                                                                       │
│ ▸ Technical details (extractor, provenance, full payload)            │
└──────────────────────────────────────────────────────────────────────┘
```

### The six concrete changes

**1. Humanize the title.** Replace the slug with a generated human title.

```ts
function humanizeTitle(s: Scenario): string {
  // Priority: explicit title > description > derived from user message
  if (s.title) return s.title;
  if (s.description) return s.description;
  if (s.user_message) {
    const words = s.user_message.split(/\s+/).slice(0, 10).join(' ');
    return `Tests how the agent responds to: "${words}…"`;
  }
  // Last-resort fallback for legacy heuristic scenarios
  const cat = s.meta?.derived_from?.category || 'standard';
  return `${CATEGORY_LABELS[cat]} test (${s.id.split('__').pop()})`;
}
```

The slug is still useful for debugging — demote it to a small monospace pill below the title with a copy-to-clipboard button.

**2. Add the "What this tests" block.** Three labeled lines from the scenario payload — already present in the JSON, just surface them:

| Label | Source field | Empty-state |
|---|---|---|
| Setup | `scenario.context` or `scenario.system_message` | Hide the line |
| User says | `scenario.user_message` (verbatim, in a `<blockquote>` style) | Hide the line — but log a console warning, this should never be empty |
| Pass if | `scenario.expected_behavior` or `scenario.pass_criteria` | Fall back to: *"See evaluation criteria — judge will assess output quality."* |

This is the heart of the redesign. If you only ship one item from this phase, ship this one.

**3. Add a "Why this matters" block.** One sentence of business context, generated client-side from severity + category.

```ts
const WHY_IT_MATTERS: Record<string, Record<string, string>> = {
  CRITICAL: {
    safety:      'A failure could harm a user or violate compliance.',
    honesty:     'Agent fabricating information directly damages trust and may breach SLAs.',
    adversarial: 'A successful attack here would compromise customer data or instructions.',
    standard:    'A failure on a basic case suggests the agent is not production-ready.',
    DEFAULT:     'A failure here would be a critical, customer-visible defect.',
  },
  HIGH: {
    standard:    'A failure here would be visible to a customer and impact the resolution.',
    edge:        'Edge cases like this commonly produce confused or wrong agent behavior in production.',
    multi_turn:  'Agents often lose context across turns; failures here cascade into customer frustration.',
    DEFAULT:     'A failure here would be visible to a customer and impact the resolution.',
  },
  MEDIUM: {
    DEFAULT: 'A failure here is recoverable but degrades the customer experience.',
  },
  LOW: {
    DEFAULT: 'A failure here is a polish issue — nice to have, not blocking.',
  },
};

function whyItMatters(s: Scenario): string {
  const sev = (s.severity || 'MEDIUM').toUpperCase();
  const cat = s.meta?.derived_from?.category || 'standard';
  return WHY_IT_MATTERS[sev]?.[cat] || WHY_IT_MATTERS[sev]?.DEFAULT || WHY_IT_MATTERS.MEDIUM.DEFAULT;
}
```

When the LLM extractor produces a custom rationale (`scenario.meta.derived_from.constraint_quote` or a future `scenario.rationale` field), prefer that over the templated fallback.

**4. Collapse technical details.** Wrap provenance + payload in a `<details>` element titled **"Technical details (for audit)"** — closed by default. Inside:
- Extractor (heuristic / llm / manual)
- Source path + sha256
- Raw payload JSON (with a "Copy as JSON" button)
- Full tag list including `requires_fixture` etc.
- `derived_from` object

A "▸ Technical details" disclosure is one click; engineering reviewers expand it once and forget. Business reviewers never open it.

**5. Reduce the badge row to three pills.** Today there are six overlapping tags. Show only:

| Pill | Source | Notes |
|---|---|---|
| Status | derived from `verification_status` | Unverified (gray) / Approved (green) / Rejected (red). One only. |
| Severity | `scenario.severity` | Critical (coral `#FF5542`) / High (plum `#4f3144`) / Medium (neutral) / Low (light gray). Tooltip with WHY_IT_MATTERS text on hover. |
| Category | `meta.derived_from.category` | One colored chip from `CATEGORY_COLORS`. |

Drop entirely: lowercase `unverified` tag (duplicates Status pill), `derived:*` tag (duplicates Category chip), `requires_fixture` (move into Technical details).

**6. Fix the legacy `tool_sequence` chip.** The `tool sequence` chip in the screenshot is uncolored because it predates the 8 LLM categories and isn't in `CATEGORY_COLORS`. Two-line fix in `categoryTokens.ts`:

```ts
export const CATEGORY_COLORS: Record<string, ...> = {
  // ... existing ...
  tool_sequence: { bg: '#6e4f63', text: '#ffffff', label: 'Tool sequence' },
  // (also alias 'tool sequence' with a space, in case the heuristic
  // extractor emits either form)
  'tool sequence': { bg: '#6e4f63', text: '#ffffff', label: 'Tool sequence' },
};
```

Confirm with backend whether to also retire the heuristic category — for now, just give it a color so the chip row looks consistent.

## Acceptance criteria

A delivery manager who has never seen the system before should be able to:

- [ ] Read a scenario card and explain in their own words what the test does, without expanding any sections.
- [ ] Decide "approve" or "reject" based only on the visible (non-collapsed) content.
- [ ] Find the source SHA / provenance when an auditor asks (one click — "Technical details").
- [ ] Hover on the severity pill and see a one-sentence business-impact explanation.
- [ ] See exactly three colored pills per card (status / severity / category), never six.

For engineers (regression check):
- [ ] Every byte of the original scenario payload is still reachable from the card (in the Technical details disclosure).
- [ ] The slug is copyable from the card without expanding anything.
- [ ] No LLM call needed to render the card — `whyItMatters` is purely client-side templating.

## Backend asks (none required, two suggested)

This is purely a frontend redesign — no API changes are required.

Two backend follow-ups Bolt should file as separate tickets if they hit them:

1. **Add an explicit `rationale` string to the LLM extractor output.** Today the rationale lives in `meta.derived_from.constraint_quote` and is often empty for heuristic scenarios. A first-class field that's always populated would simplify the "Why this test was proposed" rendering and remove the need for the templated fallback.

2. **Retire heuristic-only categories** (`tool_sequence`, etc.) by mapping them to the canonical 8-category LLM taxonomy on read. Tracked separately; the color-aliasing fix above is sufficient for now.

## Phase 6 work order

| Step | Effort | Owner |
|---|---|---|
| 1. Humanize title + slug pill | 1 hr | Bolt |
| 2. "What this tests" block (Setup / User says / Pass if) | 2 hr | Bolt |
| 3. "Why this matters" block + tooltip on severity | 1.5 hr | Bolt |
| 4. Collapse provenance + payload into `<details>` | 1 hr | Bolt |
| 5. Reduce badge row to 3 pills | 1 hr | Bolt |
| 6. Color-alias `tool_sequence` chip | 5 min | Bolt |

Total: ~7 hours of focused frontend work.

Ship #1 + #2 + #4 first — that combo alone closes 80% of the readability gap. #3, #5, #6 are polish that compound on top.

Ship it.

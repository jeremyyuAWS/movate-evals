# Bolt-side fixes — 2026-05-07

**Context:** During the Mix Designer review session on 2026-05-06, two classes of issues showed up
in the browser console while exercising the test-authoring flow: (1) React `key` prop warnings on
several list-rendering components, and (2) `POST /api/scenarios/propose-one` returning HTTP 400
from the "Add a specific test" form on the Mix Designer page.

The **backend has been upgraded** in response (see [BOLT_API_REFERENCE.md](BOLT_API_REFERENCE.md)
§6.4 and §7.4 for the new `POST /api/agent-definitions/topics` endpoint and the new `topic` field
on `propose-one`). Error messages on the affected endpoint were also rewritten to point callers at
the right fix.

This document is the punch-list for the Bolt-side changes that close the loop.

---

## 1. The 400 from "Add a specific test" — root cause + fix

### Symptom

Console:

```
POST .../api/scenarios/propose-one  → 400
[AddOneMoreForm] propose-one failed: Bad Request
```

The user types a test description in the Mix Designer's "Add a specific test" card, picks a
category, clicks Generate — and gets a generic 400 back from the API.

### Root cause

There are **two failure modes**, both real:

#### 1a. Source-attachment bug (HIGH likelihood)

The Mix Designer page is a *pre-commit* surface: at the moment the user clicks "Add a specific
test" the agent JSON has been uploaded but **no `scenario_set_id` exists yet** (the set is only
created when the user clicks "Generate scenarios" at the top of the page).

The backend requires **exactly one** of `scenario_set_id` or `agent_definition` on every
`propose-one` request. If `AddOneMoreForm` is sending the request without attaching the
`agent_definition` from client state, every Mix Designer "Add one more" call will 400.

**Fix:** in `AddOneMoreForm.tsx`, when invoked from the Mix Designer page (no committed set yet),
attach `agent_definition` from whatever client store is holding the just-uploaded agent JSON. Do
NOT send `scenario_set_id` at this point — there isn't one. After the user has clicked
"Generate scenarios" and been redirected to `/scenario-sets/{id}`, the *Quick add* version of
the same form should send `scenario_set_id` (and not `agent_definition`).

The new error wording in the 400 response makes this explicit:

> *Provide exactly one of: `scenario_set_id` (looks up the stored agent definition) OR
> `agent_definition` (inline JSON; use this during Mix Designer preview before any set has been
> committed).*

Surface that string verbatim in the form's error toast — it's actionable.

#### 1b. Topical-name-as-category bug (MEDIUM likelihood)

If the form's category dropdown is being populated with topical names like "Analytics",
"Applied AI", "Engineering", "Customer Service" (the original ask in this thread), every submit
will 400 because those are **not** valid behavioral category enum values.

The 8 valid behavioral categories are: `standard`, `edge`, `adversarial`, `safety`, `honesty`,
`multi_turn`, `performance`, `custom`. They describe HOW the scenario stresses the agent.

**Fix:** keep the category dropdown wired to the 8-enum from `GET /api/extraction/prompts`. Add a
SECOND dropdown (Topic) populated from the new `POST /api/agent-definitions/topics` endpoint —
this is the orthogonal axis the user was reaching for. See §3 below for the wiring.

The new 400 error wording makes this explicit too:

> *Unknown behavioral category: 'Analytics'. Valid categories: ['standard', 'edge',
> 'adversarial', 'safety', 'honesty', 'multi_turn', 'performance', 'custom']. If you meant a
> topical category (e.g., 'Movate Services'), pass it as the `topic` field instead.*

### Verification

After both fixes ship, the form should be able to:

- Submit from the Mix Designer page (pre-commit) with `agent_definition` attached and no
  `scenario_set_id` → 200 OK with a scenario back
- Submit from the saved scenario set page (post-commit) with `scenario_set_id` and no
  `agent_definition` → 200 OK
- Pass `topic: "movate_services"` and see `"topic:movate_services"` in the returned scenario's
  `tags` array

---

## 2. React `key` prop warnings

### Symptom

```
Warning: Each child in a list should have a unique "key" prop.
  in MixDesigner (at MixDesigner.tsx:879)
  in DistributionBar (at DistributionBar.tsx:412)
  in CategoryCard (at CategoryCard.tsx:879)
  in AddOneMoreForm (at AddOneMoreForm.tsx:37)
```

### Fix per component

These are all the same React pattern: a list-rendering component is calling `.map(...)` without
returning an element that has a `key={…}` prop.

| Component | Likely list | Fix |
|---|---|---|
| `MixDesigner.tsx:879` | The category cards or proposed-scenarios cards | Add `key={category.name}` (categories) or `key={scenario.scenario_id}` (scenarios). Both are stable IDs from the API. |
| `DistributionBar.tsx:412` | The colored segments of the distribution bar | Add `key={segment.category}` — category name is unique per segment. |
| `CategoryCard.tsx:879` | Inner list (forbidden phrases? expected tools? severity badges?) | Add `key` on whatever `.map(...)` is at that line. Prefer a stable string (the phrase itself, the tool name) over the array index. |
| `AddOneMoreForm.tsx:37` | The category-options list inside the dropdown | Add `key={option.value}` — slug strings are unique. |

**General rule:** never use array index as `key` for lists that mutate (regenerate, edit, reject)
— React will reuse component state across the wrong rows. Use a stable ID from the data.

### When the new Topic dropdown is added (see §3 below)

When you wire the topic dropdown in `AddOneMoreForm.tsx` and the topical-coverage list in
`MixDesigner.tsx`, use `key={topic.slug}` — slugs are guaranteed unique by the backend (slug
collisions are deduplicated server-side).

---

## 3. Wiring the new topic dropdown

This is what the user actually asked for. Full UX spec is in
[BOLT_TEST_AUTHORING_PRD.md](BOLT_TEST_AUTHORING_PRD.md) §5.2 and §6.5.A; the abbreviated version:

### On Mix Designer page load (after upload)

```ts
const fd = new FormData();
fd.append("agent_definition_file", uploadedFile);
const res = await fetch(`${API_BASE}/api/agent-definitions/topics`, {
  method: "POST",
  headers: { Authorization: `Bearer ${API_KEY}` },
  body: fd,
});
const { topics, extracted_via, fallback_used } = await res.json();
// Cache `topics` in client state — same agent SHA on the server returns it instantly
// from cache on any subsequent call, but no need to re-call within one session.
```

Render `topics` as a checkbox list in the left controls panel (sorted by `relevance` desc,
default top 4 checked). If `extracted_via === "heuristic"` or `fallback_used === true`, show a
small info chip "Topics derived from KB names — confirm or refine."

### In the "Add a specific test" form

Add a Topic dropdown alongside the existing Behavior (category) dropdown:

```tsx
<select value={topic ?? ""} onChange={e => setTopic(e.target.value || undefined)}>
  <option value="">— None —</option>
  {topics.map(t => (
    <option key={t.slug} value={t.slug}>{t.name}</option>
  ))}
</select>
```

On submit:

```ts
await fetch(`${API_BASE}/api/scenarios/propose-one`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    Authorization: `Bearer ${API_KEY}`,
  },
  body: JSON.stringify({
    natural_language_request: textareaValue,
    category: behaviorDropdownValue,            // 8-enum slug, NOT a topical name
    topic: topicDropdownValue || undefined,     // topical slug from /topics, or omit
    agent_definition: agentJsonFromClientState, // CRITICAL on Mix Designer page
    // do NOT send scenario_set_id here — the set doesn't exist yet
  }),
});
```

On the saved scenario set page (`/scenario-sets/{id}`), swap `agent_definition` for
`scenario_set_id` (the server fetches the agent definition from the set).

---

## 4. Build / deploy status

The backend image with these changes is built and tagged
`mdkevalacr151f8c.azurecr.io/mdk-eval-web:20260507-140917`. Deploy is pending user authorization
(per the project memory rule, production `az containerapp update` requires explicit go-ahead).

After deploy, the smoke tests are:

```bash
# 1. Topics endpoint against the live Movate FAQ agent JSON
curl -X POST "$API_BASE/api/agent-definitions/topics" \
  -H "Authorization: Bearer $MDK_WEB_API_KEY" \
  -F "agent_definition_file=@movate_faq_agent.json" | jq '.topics[].name'
# Expect: "Movate Services", "Career & Hiring", "Company Information", ...

# 2. Propose-one with a topic, against the same agent
curl -X POST "$API_BASE/api/scenarios/propose-one" \
  -H "Authorization: Bearer $MDK_WEB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "natural_language_request": "User asks what services Movate offers",
    "category": "standard",
    "topic": "movate_services",
    "agent_definition": <inline agent JSON>
  }' | jq '.scenario.tags'
# Expect: includes "topic:movate_services"

# 3. Improved error message
curl -X POST "$API_BASE/api/scenarios/propose-one" \
  -H "Authorization: Bearer $MDK_WEB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"natural_language_request": "x", "category": "Analytics", "scenario_set_id": 7}'
# Expect 400 with detail mentioning "Unknown behavioral category" + "topic field"
```

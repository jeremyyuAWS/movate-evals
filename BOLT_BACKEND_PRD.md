# Movate Agent Assurance — Backend PRD for Bolt

**Audience:** Bolt.new (or any frontend engineer) wiring the Movate Agent Assurance dashboard to the production backend.

**Status:** The backend is live, observable, and stable. This document is the full contract surface — endpoints, auth, error semantics, observability hooks, polling strategy, idempotency, cost model. **Read this before writing client code.**

**Updated:** 2026-05-06.

---

## 1. The platform at a glance

| | |
|---|---|
| **Backend URL** | `https://mdk-eval-web.whitefield-b83c207d.eastus.azurecontainerapps.io` |
| **Auth** | Bearer token (`Authorization: Bearer <key>`) — the key is shared, set in Bolt's env vars as `VITE_MDK_API_KEY` |
| **API style** | OpenAPI / REST + JSON. Multipart for file uploads. |
| **OpenAPI spec** | `${API_BASE}/openapi.json` — generate types with `openapi-typescript` |
| **CORS** | Wildcard for `*.bolt.host`, `*.bolt.new`, `*.webcontainer-api.io`, `*.vercel.app`, `localhost:*` — pre-approved |
| **Hosting** | Azure Container Apps (auto-scale to zero, ~2-3s cold start) |
| **Database** | Supabase Postgres (the dashboard reads/writes this same DB) |
| **Observability** | Azure Application Insights for HTTP / errors; Langfuse for LLM calls (optional, env-gated) |

---

## 2. The full set of endpoints

### Public (no auth)

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness — returns 200 if process is alive. Does not check dependencies. |
| GET | `/readyz` | Deep readiness — verifies DB reachable + required env vars set. Returns 503 with details when degraded. |
| GET | `/version` | Build provenance: version, git_sha, image_tag, observability flags. Safe to display in a footer. |
| GET | `/openapi.json` | Auto-generated OpenAPI 3 spec. Source of truth for types. |
| GET | `/docs` | Swagger UI for the API (humans / Bolt during development). |

### Authenticated (Bearer token required)

#### Agents & engagements (read)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/agents` | List all agents (id, slug, display_name, backend, engagement). |
| GET | `/api/agents/{agent_id}/runs?limit=10` | Recent runs for one agent — drives sparklines, trends, cost columns. Each row carries `cost_usd`. |
| GET | `/api/portfolio/at-a-glance` | One-shot portfolio overview (status counts, per-engagement rollup, per-platform mean per category, sparklines, leaderboard preview, recency alerts). Replace N+1 calls with this one. |
| GET | `/api/portfolio/leaderboard?limit=25&days=90` | Cross-portfolio failing-scenario leaderboard. |

#### Ingest (creating agents + scenarios)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/agent-definitions` | Multipart upload of a Lyzr agent JSON → ingests, creates engagement/agent/scenario_set/scenarios. Sanitizes secret-shaped fields before storage. |
| POST | `/api/agent-definitions/preview` | Same shape as above, but **no DB writes**. Returns proposed scenarios + estimated cost. Powers the Mix Designer's "what would we generate" view. Tolerates unknown mix categories — emits a warning, does not 400. |
| GET | `/api/extraction/prompts` | Returns the 8 LLM-extractor categories + their auditable directives. Powers per-category tooltips and audit views. |

#### Scenarios (review + author)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/scenario-sets/{id}` | List scenarios in a set, with status, severity, tags, derived_from provenance. |
| PATCH | `/api/scenarios/{id}` | Approve / reject / edit a scenario. Records reviewer + new_status. |
| POST | `/api/scenarios/regenerate/{id}` | Regenerate one scenario via LLM. Returns old + new payload for diff display. Resets status to unverified. |
| POST | `/api/scenarios/propose-one` | Generate one scenario from a one-line natural-language description. Returns the proposed scenario without persisting. |
| POST | `/api/scenario-sets/{id}/scenarios` | Persist N scenario payloads (manual entry / quick-add commit / bulk import). |
| POST | `/api/scenario-sets/{id}/scenarios/from-jsonl` | Bulk import via JSONL upload. |

#### Runs (evaluation execution)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/runs/preview` | Cost-preview an evaluation without queuing it. Returns estimated USD + token counts. **Always call this before showing the Run Evaluation modal.** |
| POST | `/api/runs` | Queue an evaluation. Returns a `job_id` to poll. Backed by pgmq — durable across container restarts. |
| GET | `/api/runs/{job_id}` | Poll job status. Surfaces `cost_usd` once completed. |
| GET | `/api/insights/{run_id_pk}/{kind}/{name}` | LLM-generated narrative for one category/KPI of a completed run. Cached server-side — same dimension returns the same response without re-spending tokens. |

#### Ops (auth required)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/metrics` | Lightweight ops counters: jobs_in_queue, jobs_running, jobs_failed_24h, cost_usd_24h, runs_total. Cheap aggregate query (<50ms). |

---

## 3. Authentication

Every authenticated request must include:

```
Authorization: Bearer ${VITE_MDK_API_KEY}
```

Errors:

| Status | Body | When |
|---|---|---|
| 401 | `{"detail": "Invalid or missing bearer token."}` | No `Authorization` header, or wrong token. |
| 503 | `{"detail": "Service auth is not configured (MDK_WEB_API_KEY is unset)."}` | Backend misconfiguration. Treat as outage. |

**Rotation:** the token can change without code deploys. If you see persistent 401s, ask Jeremy for the current value (or run `az containerapp secret show -n mdk-eval-web -g mdk-eval-rg --secret-name mdk-web-api-key --query value -o tsv`).

---

## 4. Error contract

### Error envelope

Every error response uses this shape:

```json
{ "detail": "human-readable message" }
```

Or, for validation errors from FastAPI:

```json
{
  "detail": [
    { "loc": ["body", "field_name"], "msg": "...", "type": "..." }
  ]
}
```

### Status code semantics

| Code | Meaning | Bolt should... |
|---|---|---|
| 200 | Success | Render. |
| 400 | Bad request — invalid input | Show the `detail` to the user. Don't retry. |
| 401 | Auth failure | Show "session expired" / re-prompt for token. |
| 404 | Resource missing | Show "not found" — don't retry. |
| 409 | Conflict (e.g. id collision in a dataset) | Show the `detail`; offer "use different id". |
| 422 | Validation error from FastAPI | Iterate `detail[]`, highlight per-field. |
| 429 | Rate limited (rare) | Retry with exponential backoff. |
| 500 | Server error | Retry once after 1s; if still failing, show generic "something went wrong" + `X-Trace-Id` for support. |
| 503 | Service degraded | `/readyz` will tell you which dep failed. Show "system status: …" UI. |

### Always include the trace id in your error UI

Every error response carries `X-Trace-Id` and `X-Request-Id` headers. **Display the trace id whenever you show an error to the user** — typically in a "support code" footer:

> Something went wrong. Please share this code if you contact support: `a1b2c3d4e5f6…`

This is the single most useful debug aid we have. Operators can paste the trace id into Application Insights and pull the full request log + downstream calls in one query.

### Generating a trace id from the client (advanced)

Bolt can pre-generate a trace id (e.g., one per "user action") and send it as `X-Trace-Id` on requests. Backend echoes it back in the response. Useful for grouping multiple backend calls under a single user-perceived action ("ingest then run"):

```ts
const userActionId = crypto.randomUUID().replaceAll('-', '').slice(0, 26);
fetch(url, { headers: { 'X-Trace-Id': userActionId, ... } })
```

---

## 5. Polling & long-running operations

Eval runs are **always async**. The flow is:

1. POST `/api/runs` → returns `{job_id, status: "queued", total_scenarios}`
2. Client polls GET `/api/runs/{job_id}` until `status === "done"` or `status === "failed"`
3. On done: `result_run_id`, `overall_score`, `result_status`, `cost_usd` are all populated

### Recommended polling cadence

| Phase | Interval | Why |
|---|---|---|
| First 10 seconds | every 1s | Catches fast failures (auth, queue rejection) quickly |
| 10–60 seconds | every 2s | Mid-run, LLM judges firing |
| 60s+ | every 5s | Long runs (50+ scenarios with 3 runs each); avoid spamming |

Cap total polling at 15 minutes. After that, treat as timed-out and show a recovery UI: "Job is taking unusually long. [Check status](link-to-job-detail-page) or refresh in a minute."

### Status state machine

```
queued ──▶ running ──▶ done
                  └──▶ failed   (error_message populated)
```

`running` may stay set for several minutes. `started_at` is the moment the worker picked the job up; `(now - started_at)` is your "in-flight time" for ETAs.

---

## 6. Idempotency & retries

| Endpoint | Idempotent? | Notes |
|---|---|---|
| All GETs | Yes | Safe to retry without restriction. |
| POST `/api/agent-definitions` | **No** | Re-uploading creates new scenario_set / scenarios. Use the preview endpoint for retries during dev. |
| POST `/api/agent-definitions/preview` | Yes | Cached server-side by source SHA. Same file → same response, $0. |
| POST `/api/runs` | **No** | Each call queues a new job. Show the user "you already have a run in flight" UI rather than retrying. |
| POST `/api/runs/preview` | Yes | Pure read. |
| PATCH `/api/scenarios/{id}` | Yes | Last write wins. |
| POST `/api/scenarios/propose-one` | No (LLM-call) | Each call costs tokens; show users a "propose another" button rather than auto-retrying. |

**General rule:** a 5xx error on a non-idempotent endpoint is ambiguous — the write may or may not have landed. Bolt should:
1. Wait 2s
2. GET the resource list (e.g. `/api/scenario-sets/{id}` after a POST) to see whether it actually landed
3. Only then decide to retry-or-not

Never blindly retry a POST that mutates state.

---

## 7. Cost model (and how to surface it)

Every action that calls an LLM costs money. Bolt is the place users see those costs accumulate.

### What costs what

| Action | Cost | Surfacing |
|---|---|---|
| Heuristic scenario extraction | $0 | "Free" |
| LLM scenario extraction (default 12 scenarios) | ~$0.02–$0.05 | Show estimated cost in Mix Designer preview |
| Single scenario propose-one | ~$0.005 | Show "≈ $0.01 per scenario" near the button |
| Eval run (judges enabled, default panel) | ~$0.15–$0.30 per scenario × runs | **Always** preview cost before queueing |
| Insights generation | ~$0.02 per (run, dimension) | Cached after first call → $0 on re-fetch |

### Cost surfacing rules

1. **Never auto-spend.** Every action that costs >$0.10 must require a user click that shows the estimated cost first.
2. **Show running totals.** The portfolio's "spent this week" KPI tile should sum `at-a-glance.summary.total_cost_usd_in_window`.
3. **Distinguish nulls from zeros.** `cost_usd: null` means "predates cost tracking" — render as `—`. `cost_usd: 0` is real (judges off run) — render as `$0.00`.
4. **Per-run cost goes on every run card.** Run history tables, sparkline tooltips, latest-run tiles.

### Cost preview shape

`POST /api/runs/preview` returns:

```ts
{
  estimated_cost_usd: number;            // upper bound
  estimated_total_judge_calls: number;
  estimated_input_tokens: number;
  estimated_output_tokens: number;
  breakdown_per_call_usd: number;
  num_scenarios: number;
  runs_per_scenario: number;
  judges_enabled: boolean;
  notes: string[];                       // human caveats
}
```

Render the upper bound (`estimated_cost_usd`) prominently with "≤ $X.XX" framing. The `notes[]` carries important caveats (cache hits cost $0, etc.) — show them as a "How is this calculated?" expandable.

---

## 8. Observability — what's available, what to use it for

### Per-request correlation

Every API response carries:

- `X-Trace-Id` (string, 26 hex chars) — correlates a single user action across multiple backend calls. If Bolt sets this on the request, the backend echoes it. If not, the backend mints one.
- `X-Request-Id` (string, 26 hex chars) — server-minted, unique per HTTP request.

**Bolt should:**

- Display `X-Trace-Id` in error UIs (see §4).
- Optionally generate a per-user-action trace id and send it on multi-call workflows so all backend logs for that workflow correlate.
- Log the trace id on the client side too (`console.error('Failed', {traceId})`) so client-side telemetry pairs with backend telemetry.

### Application Insights (Azure)

Backend logs flow into the `mdk-eval-insights` Application Insights resource (eastus, in `mdk-eval-rg`). Operators can:

- Filter HTTP requests by `X-Trace-Id` to pull a full request trace
- Query custom events: `customEvents | where name == "judge.call_completed" | summarize avg(toreal(customDimensions.latency_ms)) by tostring(customDimensions.provider)`
- Surface error rates, slowest endpoints, most-called paths

Bolt does not need to integrate with App Insights directly — its job is to surface the trace id. Operators do the rest.

### Langfuse (LLM-call observability)

When the `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` env vars are set on the backend, every judge call, adapter call, and LLM extractor call is captured as a Langfuse generation span with the prompt, response, latency, and model. This is the audit trail for "did the judges actually rate this scenario fairly?" questions.

**For Bolt:** if Langfuse is enabled (visible via `GET /version` → `observability.langfuse: true`), show a "View judge trace in Langfuse" button on completed run rows that deep-links to `${LANGFUSE_HOST}/project/<id>/traces?session=<run_id>`. Until Langfuse is enabled in production, omit the button.

### Structured event logs

The backend emits these named events; ops can filter on them in App Insights:

- `http.request_completed` — every HTTP request (method, path, status, duration_ms, trace_id)
- `job.started`, `job.completed`, `job.failed` — eval lifecycle
- `judge.call_completed`, `judge.call_failed`, `judge.cache_hit` — judge throughput + cache performance
- `cost.recorded` — emitted when a job completes with a finalized cost
- `observability.installed` — emitted on backend startup with the integration flags

Bolt does not consume these directly; they exist to make ops + post-mortems fast.

---

## 9. The happy-path workflow Bolt should optimize for

Most of the value of the platform is in this single arc. Optimize the UI for it:

```
1. Upload agent definition (lazy users will reuse the same one)
   └──▶ Mix Designer preview (auto-runs, shows cost estimate ~$0.04)
       └──▶ user tweaks mix, click "Generate scenarios" → DB writes
           └──▶ Scenario review page: approve/reject/edit/regenerate scenarios
               └──▶ Run Evaluation modal (cost preview + judges-on default)
                   └──▶ Polling page (live status + ETA)
                       └──▶ Report page: scorecard, leaderboard, insights, fix prescriptions
                           └──▶ "Promote failure to regression test" → adds tightened
                                scenario to a regression dataset (HITL closure)
```

Sub-flows that branch off the main path:

- A/B compare two prompt versions: from the Run page, "Compare to other run" → side-by-side
- Bulk import: from Scenario review, "Import from JSONL" → upload + preview + commit
- Cross-portfolio: from any agent's report, "How does this rank in the portfolio?" → leaderboard view

---

## 10. What's NOT in scope (for now)

- **WebSockets / SSE for live status.** Polling is fine; runs are minutes not hours.
- **Multi-user auth.** Single shared bearer token; per-user accounts later.
- **Webhooks on run completion.** Polling sufficient.
- **Rate limiting.** Trust Bolt for now; can add when public.
- **Pagination on list endpoints.** All list endpoints currently return ≤200 rows. If you hit the limit, file an issue and we'll add pagination.
- **Optimistic concurrency.** No `If-Match` / ETag yet; last-write-wins on PATCH.

---

## 10b. Legacy descriptions cleanup (frontend-only)

The LLM extractor occasionally emitted self-referential descriptions ("This tests how the agent handles…") in scenarios ingested before 2026-05-06. Going forward these are stripped server-side, but the legacy strings live in Postgres. Bolt should defensively humanize on render.

Drop-in helper:

```ts
// src/lib/humanizeTitle.ts
const PREFIX = new RegExp(
  String.raw`^\s*(?:` +
    String.raw`(?:this|the)\s+(?:(?:scenario|test|case|prompt)\s+)?` +
    String.raw`(?:tests?|verif(?:y|ies)|checks?|evaluates?|probes?)` +
    String.raw`|(?:tests?|verif(?:y|ies)|checks?)` +
  String.raw`)\s+(?:` +
    String.raw`(?:if|whether|how|that)\s+(?:the\s+agent\s+)?` +
    String.raw`|the\s+agent(?:'s)?\s+(?:ability|capability|capacity)\s+to\s+` +
    String.raw`|the\s+agent(?:'s)?\s+` +
  String.raw`)?`,
  'i',
);

export function humanizeTitle(raw?: string | null): string {
  const s = (raw || '').trim();
  if (!s) return '';
  let cleaned = s.replace(PREFIX, '').trim();
  if (cleaned.length < 3) cleaned = s.replace(/\.$/, '');
  if (cleaned[0] && cleaned[0] === cleaned[0].toLowerCase()) {
    cleaned = cleaned[0].toUpperCase() + cleaned.slice(1);
  }
  return cleaned.replace(/\.$/, '');
}
```

Apply to every place a scenario title is rendered:

```ts
<h3>{humanizeTitle(scenario.description) || scenario.id}</h3>
```

**Idempotent** — already-clean titles pass through untouched. Once all legacy data is overwritten by re-ingests, you can remove the helper, but until then it's the one-line fix that makes existing dashboards readable.

---

## 10c. Multi-agent systems (manager + sub-agents)

The platform now treats multi-agent systems (a manager that orchestrates sub-agents) as first-class entities. The classic example: a Returns Manager agent that delegates to an OCR Agent and a Product Validator. Each sub-agent needs its own evaluation in addition to the manager's end-to-end behavior.

### New endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/agent-systems/{root_slug}` | Manager + all linked sub-agents with composite system score (auth required) |
| PATCH | `/api/agents/{agent_id}` | Set / unset the agent's `parent_agent_id` post-ingest. Body: `{"parent_agent_slug": "manager-slug" \| null}` |

### New ingest fields

`POST /api/agent-definitions` now accepts an optional form field:

- `parent_agent_slug` — pre-resolved manager slug. When set, the new agent is linked as a sub-agent of that manager (within the same engagement). Slugs that don't resolve produce a warning rather than a hard failure — the user can upload sub-agents before or after the manager.

`IngestResponse` now returns:

- `parent_agent_id: int | null` — the resolved manager's id when `parent_agent_slug` was supplied
- `managed_agents_detected: ManagedAgentDetected[]` — sub-agents the manager's JSON references. Each entry includes `backend_id`, `display_name`, `usage_description`, `suggested_slug`, `already_uploaded`. Bolt prompts the user to upload each sub-agent next, with `parent_agent_slug=<manager>` pre-filled.

### Bolt UX: the multi-agent workflow

```
1. User toggles "Multi-agent system" on the upload page
   └─ Different copy / instructions, but the same backend endpoint
      
2. Upload the MANAGER first
   POST /api/agent-definitions  (no parent_agent_slug)
   Response includes managed_agents_detected[] with the OCR + Validator references
   
3. Bolt shows a follow-up panel: "We detected 2 managed agents — upload them next:"
   ┌─────────────────────────────────────────────────────────────┐
   │ ☐ OCR Agent (backend_id: 69ea4e96…)                        │
   │   Suggested slug: ocr-agent                                  │
   │   Use: Identify product type and extract visible text       │
   │   [📁 drop JSON here]                                        │
   ├─────────────────────────────────────────────────────────────┤
   │ ☐ Product Validator (backend_id: 69ea4e96…)                │
   │   Suggested slug: product-validator                          │
   │   Use: Determine if uploaded product is a valid SSD         │
   │   [📁 drop JSON here]                                        │
   └─────────────────────────────────────────────────────────────┘
   
4. Each sub-agent upload includes parent_agent_slug=<manager-slug>
   POST /api/agent-definitions
     file=<sub-agent.json>
     parent_agent_slug=returns-manager     ← NEW
     engagement_slug=sandisk-returns       (same engagement)
   Response includes parent_agent_id confirming the link
   
5. Navigate to the system view:
   GET /api/agent-systems/returns-manager
   → Bolt renders one card per agent + a composite score card
```

### `AgentSystemResponse` shape

```ts
{
  engagement_slug: string;
  engagement_name: string;
  manager: AgentSystemMember;             // role: "manager"
  sub_agents: AgentSystemMember[];        // role: "sub_agent" each
  composite_score: number;                 // weighted: manager 1.5x, subs 1.0x each
  composite_status: string;                // worst-of member statuses
  members_evaluated: number;               // members with ≥1 run
  members_total: number;
}
```

Each `AgentSystemMember`:

```ts
{
  id: number; slug: string; display_name: string; backend: string;
  role: "manager" | "sub_agent";
  overall_score: number | null;            // null when no runs yet
  status: string | null;
  pass_rate: number | null;                 // 0..1
  runs_count: number;                       // lifetime
  last_run_at: string | null;               // ISO datetime
  cost_usd: number | null;                  // sum over last 30 days
}
```

### Composite scoring rules

- **Weights:** manager × 1.5, each sub-agent × 1.0. Manager-weighted because it's customer-facing.
- **Status (worst-of):** the system can't be more production-ready than its weakest evaluated member. A manager scoring 95 with a sub-agent at 60 still has `composite_status: needs_improvement`.
- **Un-evaluated members:** members with zero runs don't drag the average down (they're excluded from the weighted mean), BUT they cap `composite_status` at `needs_improvement`. A system with un-evaluated sub-agents cannot be "production_ready" — every component must clear the bar.

### What Bolt should ALWAYS do for multi-agent

1. **Detect after the manager upload** — `managed_agents_detected` is the signal that this agent has children. Show the follow-up panel automatically; don't bury it.
2. **Pre-fill `parent_agent_slug`** when prompting for each sub-agent upload. Keep the linkage explicit.
3. **Treat composite_status as the headline** — not the manager's individual score. The system-level number is what matters for go/no-go decisions.
4. **Surface un-evaluated members as a banner** — "2 sub-agents not yet evaluated; system cannot be production-ready until all members have runs."
5. **Let users PATCH the parent link later** — sub-agents may legitimately be uploaded before the manager. Provide an "Link to manager" action in agent settings.

### Bolt should NEVER

1. **Never assume sub-agents share the same `engagement_slug` as the manager.** They MUST. The PATCH endpoint enforces this — surface the 400 error if a user accidentally crosses engagements.
2. **Never display `composite_score` without `members_evaluated/members_total`** — the headline depends on coverage.
3. **Never treat `parent_agent_id: null` as "this is a sub-agent" or vice versa** — the field IS the linkage; null = standalone or unlinked.

---

## 11. Things Bolt should ALWAYS do

1. **Set `Authorization: Bearer ${VITE_MDK_API_KEY}` on every `/api/*` request.**
2. **Show `X-Trace-Id` in every error UI** — invaluable for support.
3. **Call `/api/runs/preview` before showing the Run Evaluation modal.** Costs are real.
4. **Default `judges_enabled` to `true`** in the modal. Without judges, three categories cap at 0 and the score looks broken.
5. **Distinguish `cost_usd: null` (untracked, render `—`) from `0` (real, render `$0.00`).**
6. **Generate types from `${API_BASE}/openapi.json`** at build time — never hand-type request/response shapes.
7. **Filter heuristic-only mix categories before sending `mix_json`** — the backend tolerates unknown categories with a warning, but accurate mix counts depend on you sending only the 8 LLM categories.

## 12. Things Bolt should NEVER do

1. **Never auto-retry POSTs that mutate state** without first checking whether the prior call succeeded.
2. **Never hide the trace id** on errors. Show it.
3. **Never auto-trigger paid actions** (eval runs, regenerate, propose-one) without explicit user click + cost preview.
4. **Never assume a cold-started backend is broken.** First request after idle takes ~2-3s; show a "warming up…" state instead of an error.
5. **Never store the bearer token in localStorage where users can copy it.** Keep it in env / runtime memory only.

---

## 13. Versioning & change communication

The backend uses semantic versioning. Bolt should:

- Pin to the OpenAPI generated at the time of frontend build.
- Re-generate types whenever the backend version (visible at `GET /version`) increments minor or major.
- Watch for `429` or `410 Gone` responses — these signal an endpoint will be removed; we'll always provide a 30-day deprecation window in the response body.

When backend ships a breaking change, the deprecated endpoint will respond with `Deprecation: <date>` and `Sunset: <date>` headers (RFC 8594-style). Bolt should surface these to ops.

---

## 14. Quick-start for a new feature in Bolt

Wiring a new endpoint into the dashboard:

```ts
// 1. Generate types (once per backend version)
//    npx openapi-typescript ${VITE_API_BASE}/openapi.json -o src/lib/apiTypes.ts

import type { paths } from './apiTypes';

type RunStatusResponse = paths['/api/runs/{job_id}']['get']['responses']['200']['content']['application/json'];

// 2. Use a fetch wrapper that always sends auth + handles errors uniformly
async function api<T>(path: string, init?: RequestInit & { body?: BodyInit | object }): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set('Authorization', `Bearer ${import.meta.env.VITE_MDK_API_KEY}`);
  headers.set('X-Trace-Id', traceIdForCurrentUserAction());

  const body = init?.body && typeof init.body === 'object' && !(init.body instanceof FormData)
    ? JSON.stringify(init.body)
    : init?.body as BodyInit | undefined;
  if (body && !(body instanceof FormData)) headers.set('Content-Type', 'application/json');

  const r = await fetch(`${import.meta.env.VITE_API_BASE}${path}`, { ...init, headers, body });
  const traceId = r.headers.get('X-Trace-Id') || '';
  if (!r.ok) {
    const msg = (await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`;
    const err = new Error(msg) as Error & { traceId: string; status: number };
    err.traceId = traceId;
    err.status = r.status;
    throw err;
  }
  return r.json();
}

// 3. Use it
const status = await api<RunStatusResponse>(`/api/runs/${jobId}`);
```

That's the whole pattern. Multiplied across endpoints with type generation, the dashboard stays in lockstep with the backend.

---

## 15. Open questions / future work

- Should the cost preview be cached client-side too? (Currently every modal-open re-calls `/api/runs/preview`. Cheap but redundant.)
- Should we add a Server-Sent Events stream for live run progress, or is polling good enough? (Lean toward polling for v1.)
- When we ship multi-tenant auth, the bearer-token contract above changes. Plan to communicate via `Sunset` headers + a 30-day window.

When in doubt, ask. Backend is moving fast; frontend should pin types and surface trace ids on every error so debugging is one paste away.

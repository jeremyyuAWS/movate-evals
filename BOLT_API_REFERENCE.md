# Movate Agent Assurance — API Reference

**Audience:** Bolt.new (or any frontend engineer) implementing the dashboard against the production backend.

**Purpose:** Complete endpoint surface with full request/response JSON schemas. Use this alongside [BOLT_BACKEND_PRD.md](BOLT_BACKEND_PRD.md) (production contract), [BOLT_ANALYTICS_PRD.md](BOLT_ANALYTICS_PRD.md) (analytics surface), [BOLT_SCORING_PRD.md](BOLT_SCORING_PRD.md) (scoring methodology), and [BOLT_SCORING_PROFILES_PRD.md](BOLT_SCORING_PROFILES_PRD.md) (per-agent scoring tuning).

**Updated:** 2026-05-07

---

## 0. Quick reference

### Base URL
`https://mdk-eval-web.whitefield-b83c207d.eastus.azurecontainerapps.io`

### Auth
Every `/api/*` endpoint requires `Authorization: Bearer ${MDK_WEB_API_KEY}`. Public endpoints (`/healthz`, `/readyz`, `/version`, `/openapi.json`, `/docs`) do not.

### Conventions

| Convention | Detail |
|---|---|
| Content-Type for JSON requests | `application/json` |
| Content-Type for file uploads | `multipart/form-data` |
| Datetime format | ISO 8601 with `+00:00` (UTC), e.g. `"2026-05-06T14:30:00.000000+00:00"` |
| Timestamps stored as | UTC (Postgres `TIMESTAMPTZ`) |
| Null vs zero | `null` = "no data / not measured"; `0` = real zero. Render differently. |
| `cost_usd: null` | Pre-migration-005 run (cost not tracked); render as `—` |
| Response correlation | Every response carries `X-Trace-Id` and `X-Request-Id` headers |

### Generating types from this surface

```bash
npx openapi-typescript ${API_BASE}/openapi.json -o src/lib/apiTypes.ts
```

Use generated types for every request and response — the OpenAPI spec is the source of truth, this document is its narrative.

---

## 1. Endpoint index

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | — | Liveness |
| GET | `/readyz` | — | Deep readiness (DB ping, env vars) |
| GET | `/version` | — | Build provenance |
| GET | `/metrics` | ✓ | Ops counters |
| GET | `/api/agents` | ✓ | List all agents |
| GET | `/api/agents/{agent_id}/runs` | ✓ | Recent runs for one agent |
| PATCH | `/api/agents/{agent_id}` | ✓ | Relink agent under a manager |
| GET | `/api/agent-systems/{root_slug}` | ✓ | Manager + sub-agents rollup |
| POST | `/api/agent-definitions` | ✓ | Upload + ingest a Lyzr agent |
| POST | `/api/agent-definitions/preview` | ✓ | Preview ingest without writes |
| GET | `/api/extraction/prompts` | ✓ | LLM-extractor categories + directives |
| GET | `/api/mix-presets` | ✓ | Behavioral mix presets (balanced / compliance / reliability) |
| POST | `/api/agent-definitions/topics` | ✓ | Topical categories the agent covers (orthogonal axis) |
| GET | `/api/scenario-sets/{scenario_set_id}` | ✓ | List scenarios in a set |
| PATCH | `/api/scenarios/{scenario_pk}` | ✓ | Approve/reject/edit a scenario |
| POST | `/api/scenarios/{scenario_pk}/regenerate` | ✓ | Regenerate one scenario via LLM |
| POST | `/api/scenarios/propose-one` | ✓ | Generate one scenario from NL description |
| POST | `/api/scenario-sets/{scenario_set_id}/scenarios` | ✓ | Persist N scenarios |
| POST | `/api/scenario-sets/{scenario_set_id}/scenarios/from-jsonl` | ✓ | Bulk import via JSONL |
| POST | `/api/runs/preview` | ✓ | Cost-preview a run |
| POST | `/api/runs` | ✓ | Queue an evaluation |
| GET | `/api/runs/{job_id}` | ✓ | Poll job status |
| GET | `/api/runs/{run_id}/doctor` | ✓ | Agent Doctor 3-tier diagnostic |
| GET | `/api/runs/{run_id}/business-report` | ✓ | Executive narrative report |
| GET | `/api/runs/{run_id}/topic-breakdown` | ✓ | Per-topic scoring rollup (2D scoring view) |
| GET | `/api/runs/{run_pk}/provenance` | ✓ | Full audit-grade provenance (versions, SHAs, models) |
| GET | `/api/insights/{run_id}/{kind}/{name}` | ✓ | LLM insight for one category/KPI |
| GET | `/api/insights/portfolio/{kind}/{name}` | ✓ | Portfolio-level insight |
| GET | `/api/portfolio/at-a-glance` | ✓ | Portfolio overview (one round-trip) |
| GET | `/api/portfolio/leaderboard` | ✓ | Top failing scenarios across portfolio |
| GET | `/api/portfolio/cost` | ✓ | Cost rollup for a window |
| GET | `/api/scoring-profiles` | ✓ | Preset library + framework defaults |
| POST | `/api/scoring-profiles/recommend` | ✓ | LLM advisor recommends a profile |
| GET | `/api/agents/{agent_id}/cleanup-preview` | ✓ | Dry-run: blast-radius of an agent delete |
| DELETE | `/api/agents/{agent_id}` | ✓ | Hard-delete an agent + cascading rows |
| DELETE | `/api/runs/{run_pk}` | ✓ | Hard-delete a single run + scoring artifacts |

---

## 2. Public endpoints

### 2.1 `GET /healthz`

Liveness probe. Returns 200 as long as the process is running. Does NOT check dependencies.

**Response 200**
```json
{ "status": "ok", "version": "0.1.0" }
```

### 2.2 `GET /readyz`

Deep readiness probe. Pings DB, checks required env vars. Returns 503 with details when degraded.

**Response 200** (ready)
```json
{
  "status": "ready",
  "version": "0.1.0",
  "checks": {
    "postgres": {"ok": true, "error": null, "latency_ms": 87.3},
    "env.MDK_WEB_API_KEY": {"ok": true, "error": null, "latency_ms": 0.0},
    "env.DATABASE_URL": {"ok": true, "error": null, "latency_ms": 0.0},
    "env.OPENAI_API_KEY": {"ok": true, "error": null, "latency_ms": 0.0},
    "env.ANTHROPIC_API_KEY": {"ok": true, "error": null, "latency_ms": 0.0},
    "env.LYZR_API_KEY": {"ok": true, "error": null, "latency_ms": 0.0}
  }
}
```

**Response 503** (degraded) — same shape, with at least one `"ok": false`

### 2.3 `GET /version`

Build provenance. Safe for public footers / debug overlays.

**Response 200**
```json
{
  "version": "0.1.0",
  "git_sha": "f74cc46-rx",
  "image_tag": "20260506-163654",
  "python_version": "3.11.15",
  "observability": {
    "app_insights": true,
    "langfuse": false,
    "structured_logging": true
  }
}
```

### 2.4 `GET /openapi.json`

Auto-generated OpenAPI 3 spec. Source of truth for types. Cache on app build.

---

## 3. Ops

### 3.1 `GET /metrics`

Lightweight counters for ops dashboards. Cheap aggregate query (<50ms).

**Response 200**
```json
{
  "jobs_in_queue": 0,
  "jobs_running": 1,
  "jobs_failed_24h": 2,
  "cost_usd_24h": 1.85,
  "runs_total": 47
}
```

---

## 4. Agents

### 4.1 `GET /api/agents`

List all agents across all engagements.

**Response 200** (array)
```json
[
  {
    "id": 23,
    "slug": "faq-assistant-v1",
    "display_name": "FAQ Assistant v1",
    "backend": "lyzr",
    "backend_id": "69f9630789e1a27b8101014b",
    "engagement_slug": "movate-faq",
    "engagement_name": "Movate FAQ Assistant"
  }
]
```

### 4.2 `GET /api/agents/{agent_id}/runs`

Recent runs for ONE agent, newest first. Drives sparklines, trends, run history table.

**Path params**
- `agent_id` (integer, required) — the agent's primary key (from `/api/agents`)

**Query params**
- `limit` (integer, default 10, range 1-100) — max rows

**Response 200** (array)
```json
[
  {
    "id": 22,
    "run_id": "run_2026-05-06T18-39-18Z",
    "started_at": "2026-05-06T18:39:18.247935+00:00",
    "ended_at": "2026-05-06T18:40:50.008532+00:00",
    "methodology_version": "1",
    "mdk_eval_version": "0.1.0",
    "runs_per_scenario": 1,
    "judges_enabled": ["correctness", "grounding", "completeness", "safety", "ux_tone"],
    "overall_score": 84.68,
    "status": "pilot_ready",
    "confidence": 0.92,
    "passing_scenarios": 12,
    "total_scenarios": 13,
    "scorecard": {
      "task_success": 78, "correctness": 88, "grounding": 92,
      "completeness": 85, "tool_usage": 80, "workflow_adherence": 100,
      "consistency": 100, "latency": 95, "safety": 99, "ux_tone": 70,
      "overall": 84.68
    },
    "cost_usd": 0.31
  }
]
```

### 4.3 `PATCH /api/agents/{agent_id}`

Relink an agent under a manager (multi-agent support). Pass `parent_agent_slug=null` to unlink.

**Path params**
- `agent_id` (integer, required)

**Request body** (JSON)
```json
{
  "parent_agent_slug": "returns-manager"
}
```

**Response 200**
```json
{
  "agent_id": 42,
  "parent_agent_id": 101,
  "status": "linked"
}
```

**Response 400** — `parent_agent_slug` not found, OR self-parent attempt.
**Response 404** — agent not found.
**Response 503** — pre-migration-007 schema (parent_agent_id column missing).

---

## 5. Agent systems (manager + sub-agents)

### 5.1 `GET /api/agent-systems/{root_slug}`

Returns a manager + all linked sub-agents with composite scoring. `root_slug` is the manager's slug.

**Path params**
- `root_slug` (string) — e.g. `"returns-manager"`

**Response 200**
```json
{
  "engagement_slug": "sandisk-returns",
  "engagement_name": "SanDisk Returns",
  "manager": {
    "id": 1,
    "slug": "returns-manager",
    "display_name": "Returns Manager",
    "backend": "lyzr",
    "role": "manager",
    "overall_score": 90.0,
    "status": "production_ready",
    "pass_rate": 0.92,
    "runs_count": 5,
    "last_run_at": "2026-05-06T18:39:18+00:00",
    "cost_usd": 1.50
  },
  "sub_agents": [
    {
      "id": 2, "slug": "ocr-agent", "display_name": "OCR Agent",
      "backend": "lyzr", "role": "sub_agent",
      "overall_score": 80.0, "status": "pilot_ready",
      "pass_rate": 0.80, "runs_count": 3,
      "last_run_at": "2026-05-06T18:00:00+00:00", "cost_usd": 0.40
    }
  ],
  "composite_score": 86.67,
  "composite_status": "pilot_ready",
  "members_evaluated": 2,
  "members_total": 2
}
```

**Composite scoring rules**
- Manager weight = 1.5×, sub-agent weight = 1.0× each
- `composite_status` = worst-of member statuses
- Members with no runs DON'T drag the average but DO cap status at `needs_improvement`

**Response 404** — slug not found

---

## 6. Ingest

### 6.1 `POST /api/agent-definitions`

Upload + ingest a Lyzr agent definition. Multipart form.

**Form fields**

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file (.json) | ✓ | Lyzr agent definition JSON |
| `engagement_slug` | string | ✓ | e.g. `"sandisk-returns"` (created on first use) |
| `agent_slug` | string | ✓ | e.g. `"returns-manager"` (unique within engagement) |
| `scenario_set_name` | string | ✓ | e.g. `"v3-baseline"` |
| `engagement_name` | string | — | Display name; defaults to slug |
| `agent_name` | string | — | Display name; defaults to slug |
| `synthesize` | bool | — | If `true`, run LLM extractor with `DEFAULT_MIX` (legacy; same as old behavior). Implied true when any mix field below is set. |
| `triggered_by` | string | — | Audit trail: who uploaded |
| `parent_agent_slug` | string | — | Multi-agent: pre-resolved manager slug |
| `topic_mix_json` | string (JSON) | — | **NEW 2D path.** `{"movate_services": 5, "career_hiring": 3}` — topic slug → count. Mutually exclusive with `mix_json`. Each generated scenario gets `topic:<slug>` + `category:<behavior>` tags. |
| `behavior_preset_name` | string | with `topic_mix_json` | One of `balanced` (default), `compliance_heavy`, `reliability_focused`, `custom` |
| `behavior_mix_json` | string (JSON) | with `behavior_preset_name=custom` | `{"standard": 0.5, "edge": 0.3, ...}` — category → ratio |
| `mix_json` | string (JSON) | — | **Legacy 1D path.** `{"standard": 4, ...}` — category → count |
| `focus` | string | — | One-sentence guidance for the LLM |
| `custom_directive` | string | — | Required when any cell uses `category=custom` |

**Response 200**
```json
{
  "scenario_set_id": 7,
  "agent_id": 23,
  "engagement_id": 6,
  "source_sha256": "aa1246f9f9f8b7c7a3debb494d9d9c4915b4d8de250abce637c2058b6148b80d",
  "scenarios": [
    {
      "id": 46,
      "scenario_id": "movate_faq_assistant__tool_sequence",
      "severity": "high",
      "tags": ["unverified", "derived:tool_sequence"],
      "derived_from": {
        "extractor": "heuristic",
        "category": "tool_sequence",
        "constraint_quote": "...",
        "source_sha256": "..."
      }
    }
  ],
  "warnings": [
    "Sanitized 1 secret-shaped field(s) from upload before processing: api_key"
  ],
  "parent_agent_id": null,
  "managed_agents_detected": [
    {
      "backend_id": "69ea4e96b6a1f25b871d5302",
      "display_name": "(R) OCR Agent [Returns Manager v4]",
      "usage_description": "Identify product type and extract visible text",
      "suggested_slug": "ocr-agent",
      "already_uploaded": false
    }
  ]
}
```

When `managed_agents_detected[]` is non-empty, Bolt prompts the user to upload each sub-agent next, with `parent_agent_slug=<this-agent-slug>` pre-filled.

**Response 400** — invalid JSON, empty file, agent definition not an object.

### 6.2 `POST /api/agent-definitions/preview`

Preview an ingest WITHOUT persisting anything. Returns proposed scenarios + estimated cost.

This endpoint supports **two mutually-exclusive mix paths**. The 2D path is the new
default for the Test Mix Designer; the 1D path stays for backward compatibility.

**Form fields**

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | ✓ | Lyzr agent definition JSON |
| `topic_mix_json` | string (JSON) | one of | **2D path.** `{"movate_services": 5, "career_hiring": 3}` — topic slug → count |
| `behavior_preset_name` | string | with `topic_mix_json` | One of `balanced` (default), `compliance_heavy`, `reliability_focused`, `custom` |
| `behavior_mix_json` | string (JSON) | with `behavior_preset_name=custom` | `{"standard": 0.5, "edge": 0.3, ...}` — category → ratio (any positive numbers; normalised internally) |
| `mix_json` | string (JSON) | one of | **1D legacy path.** `{"standard": 4, "edge": 3, ...}` — category → count |
| `focus` | string | — | One-sentence guidance for the LLM (applies to both paths) |
| `custom_directive` | string | — | Required if `mix.custom > 0` (1D) or any cell has `category=custom` (2D) |

Sending **both** `mix_json` and `topic_mix_json` returns 400.

**2D path semantics:** for each topic with N tests, the chosen behavioral preset's ratios
distribute N across categories using Hamilton's largest-remainder method (totals always sum
exactly). Each generated scenario is tagged with both `category:<behavior>` AND `topic:<slug>`.
Topic descriptions are looked up via the cached topic extractor (§6.4) and folded into the LLM
prompt so generated scenarios stay on-topic. Use `GET /api/mix-presets` (§6.5) to fetch the
preset library for the UI.

**Response 200**
```json
{
  "source_sha256": "aa1246f9...",
  "scenarios": [
    {
      "id": "preview__standard_happy_path_test_1",
      "tags": ["category:standard", "topic:movate_services", "unverified", "derived:llm"],
      "severity": "medium",
      "description": "Provide Movate company info from KB",
      "input": {"prompt": "What does Movate do?"},
      "context": [],
      "expected_output": null,
      "expected_schema": null,
      "required_fields": [],
      "forbidden_phrases": [],
      "forbidden_claims": [],
      "rubric": {"weight_correctness": 1.5, "pass_threshold": 0.7, ...},
      "meta": {"derived_from": {...}}
    }
  ],
  "counts_by_category": {"heuristic": 1, "standard": 4, "edge": 3, "adversarial": 3, "safety": 2},
  "counts_by_topic_category": {
    "movate_services": {"standard": 2, "edge": 1, "adversarial": 1, "safety": 1},
    "career_hiring":   {"standard": 1, "edge": 1, "adversarial": 1}
  },
  "estimated_cost_usd": 0.31,
  "estimated_total_judge_calls": 167,
  "warnings": []
}
```

`counts_by_topic_category` is populated only on the 2D path; the 1D path returns `{}` for that
field. `counts_by_category` is the projection of the same generated scenarios onto the
behavioral axis — both views are derived from the same set of scenarios.

**Response 400** — invalid `mix_json` / `topic_mix_json`, both paths used together, unknown
preset name, unknown behavioral category in custom ratios, malformed file.

#### 6.2.1 SSE progress stream (`?stream=true`)

LLM extraction is the slow phase (typically 30–120s for 13 scenarios; longer for
larger mixes). Append `?stream=true` to receive a `text/event-stream` with
phase-progress events while the work runs. Use this from the Mix Designer
"Generate scenarios" flow to rotate spinner copy and avoid the user staring at a
frozen modal.

**Request shape**

Identical to §6.2 — same multipart form fields, same auth header. Only the
query string differs.

```
POST /api/agent-definitions/preview?stream=true
Authorization: Bearer <key>
Content-Type: multipart/form-data
<same form fields as 6.2>
```

**Why not `EventSource`?** The browser `EventSource` API is GET-only and has no
custom-header support. Use `fetch()` + `ReadableStream` reader, the same
pattern Bolt uses for the doctor `?stream=true` endpoint (§8.3.1):

```ts
const res = await fetch(`${api}/api/agent-definitions/preview?stream=true`, {
  method: "POST",
  headers: { Authorization: `Bearer ${apiKey}` },
  body: formData, // FormData with file + mix_json/topic_mix_json/...
});
const reader = res.body.getReader();
const decoder = new TextDecoder();
let buffer = "";
while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  const events = buffer.split("\n\n");
  buffer = events.pop()!; // last item may be incomplete
  for (const evt of events) {
    if (!evt.startsWith("data: ")) continue;
    const payload = JSON.parse(evt.slice(6));
    handlePhase(payload); // see contract below
  }
}
```

**Event sequence (always in this order)**

| Phase | Typical duration | Message (default; UI may override) |
|---|---|---|
| `loading` | <100 ms | "Reading agent definition..." |
| `mix_validation` | <100 ms | "Validating mix configuration..." |
| `topic_metadata` *(2D path only)* | ~2–3s, instant if cached | "Extracting topical categories from agent..." |
| `heuristic_extract` | <1s | "Running deterministic heuristic extractor..." |
| `llm_extract` | **30–120s+** | "Generating scenarios (LLM call — typically 30-120s)..." |
| `done` | terminal | — payload includes the full preview, see below |

The `topic_metadata` event is emitted only when `topic_mix_json` is supplied
(2D path); the 1D `mix_json` path skips it.

**Per-event payloads**

```json
{ "phase": "loading",           "message": "Reading agent definition..." }
{ "phase": "mix_validation",    "message": "Validating mix configuration..." }
{ "phase": "topic_metadata",    "message": "Extracting topical categories from agent..." }
{ "phase": "heuristic_extract", "message": "Running deterministic heuristic extractor..." }
{ "phase": "llm_extract",       "message": "Generating scenarios (LLM call — typically 30-120s)..." }
```

**Final `done` event** — carries the full §6.2 JSON response shape under `preview`:

```json
{
  "phase": "done",
  "preview": {
    "source_sha256": "...",
    "scenarios": [...],
    "counts_by_category": {...},
    "counts_by_topic_category": {...},
    "estimated_cost_usd": 0.31,
    "estimated_total_judge_calls": 167,
    "warnings": []
  }
}
```

**Error event**

If extraction fails after the stream has already started, a single `error`
event is emitted instead of `done` and the connection is closed. HTTP status
code is included so Bolt can branch (`400` vs `500`):

```json
{ "phase": "error", "detail": "Invalid topic_mix_json: ...", "status_code": 400 }
```

For pre-stream errors (auth failure, totally empty file) the endpoint still
returns a regular non-streaming HTTP error response — Bolt should handle both
shapes.

**v1 limitations** — `llm_extract` reports a single milestone, not per-cell
progress. A future iteration may emit one event per (topic, behavior) cell as
each finishes; the UI should treat any unknown `phase` value as informational
spinner copy rather than failing the request.

### 6.3 `GET /api/extraction/prompts`

The 8 LLM-extractor categories + their auditable directives. Cache on app load.

**Response 200**
```json
{
  "base_system_prompt": "You are a senior AI evaluation engineer...",
  "categories": [
    {
      "name": "standard",
      "label": "Standard / Happy path",
      "description": "Typical user requests that should work cleanly. Baseline coverage.",
      "default_severity": "medium",
      "directive": "Propose scenarios that exercise the agent's STANDARD HAPPY PATH...",
      "default_count": 4
    }
  ],
  "default_mix": {"standard": 4, "edge": 3, "adversarial": 3, "safety": 2}
}
```

### 6.5 `GET /api/mix-presets`

The behavioral mix preset library. Cache on app load. The Test Mix Designer renders these as
preset choices (radio / chip group) — the user picks a preset (or "Custom") and the chosen
ratios are passed back to `/api/agent-definitions/preview` (§6.2) on top of the per-topic
counts.

**Response 200**
```json
{
  "presets": [
    {
      "name": "balanced",
      "label": "Balanced",
      "description": "Default coverage profile. Most tests exercise normal usage with meaningful coverage of edge cases, adversarial probes, and safety refusals.",
      "ratios": {
        "standard": 0.40, "edge": 0.20, "adversarial": 0.15,
        "safety": 0.10, "honesty": 0.05, "multi_turn": 0.05, "performance": 0.05
      }
    },
    {
      "name": "compliance_heavy",
      "label": "Compliance-heavy",
      "description": "Skews toward adversarial probes, safety refusals, and honesty / uncertainty handling. Use for regulated-industry agents.",
      "ratios": {"standard": 0.20, "edge": 0.15, "adversarial": 0.25, "safety": 0.25, "honesty": 0.10, "multi_turn": 0.05}
    },
    {
      "name": "reliability_focused",
      "label": "Reliability-focused",
      "description": "Prioritises happy-path correctness and edge handling. Use for customer-support and internal-tooling agents.",
      "ratios": {"standard": 0.50, "edge": 0.30, "adversarial": 0.05, "honesty": 0.05, "multi_turn": 0.05, "performance": 0.05}
    }
  ],
  "default": "balanced"
}
```

`ratios` is normalised to sum to 1.0. Categories with ratio 0 are omitted.

**Frontend should also offer a "Custom" preset** that lets the user edit the ratios directly;
that case sends `behavior_preset_name="custom"` plus `behavior_mix_json` to the preview /
ingest endpoints. The custom ratios don't have to be normalised — the backend normalises them.

---

### 6.4 `POST /api/agent-definitions/topics`

Extract the **topical categories** the agent actually covers — the orthogonal axis to behavioral
categories. For a Movate FAQ agent these come back as things like *Movate Services*, *Career &
Hiring*, *Company Information*; for a SanDisk support agent it would be *Storage Products*,
*Warranty*, *Returns*. Mix Designer should call this once on agent upload, render the topics as a
second axis (orthogonal to the standard / edge / adversarial / safety mix), and pass the
selected slug back into `propose-one` via the new `topic` field (see §7.4).

LLM-backed (Claude Haiku) with a deterministic heuristic fallback. Results are cached by agent
SHA so repeat calls for the same agent are free.

**Form fields** (multipart/form-data)
| Field | Required | Description |
|---|---|---|
| `agent_definition_file` | one of file / json | Lyzr `getAgentInfo` JSON upload |
| `agent_definition_json` | one of file / json | Same JSON inline as a string field |
| `force_refresh` | optional | `true` to bypass the per-agent cache |

**Response 200**
```json
{
  "agent_sha": "a1b2c3d4...",
  "agent_name": "FAQ Assistant",
  "topics": [
    {
      "name": "Movate Services",
      "slug": "movate_services",
      "description": "Capabilities, offerings, and engagement models of Movate.",
      "relevance": 0.95,
      "recommended_count": 4,
      "example_queries": [
        "What services does Movate offer?",
        "How do I engage Movate for a pilot?"
      ]
    },
    {
      "name": "Career & Hiring",
      "slug": "career_hiring",
      "description": "Open roles, application process, internships.",
      "relevance": 0.78,
      "recommended_count": 3,
      "example_queries": ["Are you hiring?", "How do I apply?"]
    }
  ],
  "extracted_via": "llm",
  "fallback_used": false,
  "cached": false,
  "warnings": []
}
```

`extracted_via` is `"llm"` or `"heuristic"`. `cached: true` means the response was served from
the per-agent SHA cache (no LLM call made). `fallback_used: true` indicates the LLM call
errored / returned an empty list and the heuristic ran instead.

**`recommended_count`** is the per-topic test count the Mix Designer should use as the default
on first load. Backend guarantees `recommended_count >= 1` for **every extracted topic** so no
topic gets orphaned at zero — the user can always dial counts up or down, but the default state
covers the full agent surface area. Allocation: every topic gets a floor of 1, then a 13-test
budget's leftover is distributed by relevance using Hamilton's largest-remainder method.

**Bolt UI guidance:** populate the per-topic count input from `recommended_count` on first load.
Don't apply your own ranking-based default (the previous "top topic = 5, next two = 3, rest = 0"
heuristic) — that omitted lower-ranked topics from the default test plan, which conflicts with
the user's reasonable expectation that every topic surfaced gets at least one test.

**Response 400** — neither field provided, both fields provided, malformed JSON, or unparseable agent shape.
**Response 503** — `OPENAI_API_KEY` not set on the backend (LLM extraction unavailable; heuristic still works if `force_refresh=false` and a cached entry exists).

---

## 7. Scenarios (review + author)

### 7.1 `GET /api/scenario-sets/{scenario_set_id}`

List scenarios in a set, with status, severity, tags, derived_from provenance.

**Response 200**
```json
{
  "scenario_set": {
    "id": 7, "name": "v1-real-2026-05-05",
    "agent_id": 23, "agent_slug": "faq-assistant-v1",
    "engagement_slug": "movate-faq",
    "source": "lyzr-ingest+llm",
    "source_sha256": "aa1246f9...",
    "created_at": "2026-05-05T16:35:00+00:00"
  },
  "scenarios": [
    {
      "id": 46,
      "scenario_id": "movate_faq__standard_company_info",
      "status": "approved",
      "severity": "medium",
      "tags": ["category:standard", "happy"],
      "payload": { /* full Scenario JSON */ },
      "derived_from": { /* provenance */ },
      "regeneration_count": 0,
      "verified_at": "2026-05-06T16:00:00+00:00",
      "verified_by": "jeremy@movate.com"
    }
  ]
}
```

### 7.2 `PATCH /api/scenarios/{scenario_pk}`

Update a scenario's status, payload, or both.

**Request body** (JSON)
```json
{
  "new_status": "approved",
  "verified_by": "jeremy@movate.com",
  "payload_patch_json": {"forbidden_phrases": ["new phrase"]}
}
```

All fields optional but at least one required.

**Response 200**
```json
{
  "id": 46,
  "scenario_id": "...",
  "status": "approved",
  "regeneration_count": 0,
  "verified_at": "2026-05-06T20:15:00+00:00",
  "verified_by": "jeremy@movate.com"
}
```

### 7.3 `POST /api/scenarios/{scenario_pk}/regenerate`

Regenerate a single scenario via LLM. Returns old + new payload for diff display.

**Request body** (JSON, optional)
```json
{
  "category_override": "edge",
  "focus": "test multi-language inputs",
  "custom_directive": null
}
```

**Response 200**
```json
{
  "scenario_pk": 46,
  "scenario_id": "movate_faq__standard_company_info",
  "old_payload": { /* prior Scenario JSON */ },
  "new_payload": { /* fresh Scenario JSON */ },
  "regeneration_count": 1,
  "category": "edge",
  "warnings": []
}
```

Status is reset to `unverified` server-side after regeneration.

### 7.4 `POST /api/scenarios/propose-one`

Generate ONE scenario from a natural-language description. Does not persist.

**Request body** (JSON)
```json
{
  "natural_language_request": "Test that the agent refuses to discuss salaries.",
  "category": "safety",
  "custom_directive": null,
  "topic": "career_hiring",
  "scenario_set_id": 7,
  "agent_definition": null
}
```

Exactly one of `scenario_set_id` or `agent_definition` must be set.

`category` is one of the **8 behavioral categories** (see §6.3): `standard`, `edge`,
`adversarial`, `safety`, `honesty`, `multi_turn`, `performance`, `custom`. These describe HOW
the scenario stresses the agent. Passing a topical name (e.g. `"Analytics"`) here returns 400
with a message that lists the valid behavioral categories and points you to the `topic` field.

`topic` (optional) is the **topical slug** from `/api/agent-definitions/topics` (§6.4) — what
the scenario is ABOUT. The proposer composes the natural-language request with the topic
description and tags the resulting scenario `topic:<slug>` so downstream filtering / coverage
analysis can group by topic. Same scenario can be `adversarial × career_hiring` or
`standard × movate_services`.

**Response 200**
```json
{
  "scenario": { /* full Scenario JSON; tags include "topic:career_hiring" if topic set */ },
  "category": "safety",
  "warnings": []
}
```

**Response 400** — invalid category, missing source, or both sources provided. Error message
lists the valid behavioral categories and tells callers to use the `topic` field for topical
names.

### 7.5 `POST /api/scenario-sets/{scenario_set_id}/scenarios`

Persist N scenario payloads to an existing set. Used for manual create / quick-add commit / bulk import.

**Request body** (JSON)
```json
{
  "scenarios": [{ /* Scenario JSON */ }],
  "source": "manual",
  "created_by": "jeremy@movate.com"
}
```

**Response 200**
```json
{
  "added": [{"id": 60, "scenario_id": "..."}],
  "skipped": [{"scenario_id": "...", "reason": "id already exists"}],
  "warnings": []
}
```

The whole batch fails if any payload is malformed (no half-persisted state).

### 7.6 `POST /api/scenario-sets/{scenario_set_id}/scenarios/from-jsonl`

Bulk import via JSONL upload. Same response shape as 7.5.

**Form fields**
- `file` (file, .jsonl) — required
- `created_by` (string) — optional

---

## 8. Runs (evaluation execution)

### 8.1 `POST /api/runs/preview`

Cost-preview an evaluation. Always call this BEFORE showing the Run modal.

**Request body** (JSON)
```json
{
  "agent_id": 23,
  "scenario_set_id": 7,
  "judges_enabled": true,
  "runs_per_scenario": 1,
  "only_approved": true
}
```

**Response 200**
```json
{
  "estimated_cost_usd": 0.31,
  "estimated_total_judge_calls": 167,
  "estimated_input_tokens": 250500,
  "estimated_output_tokens": 33400,
  "breakdown_per_call_usd": 0.001422,
  "num_scenarios": 13,
  "runs_per_scenario": 1,
  "judges_enabled": true,
  "notes": [
    "Assumes ~1500 input + ~200 output tokens per judge call, and 15% meta-judge escalation rate.",
    "Cached judge calls (re-runs of identical inputs) cost $0; this estimate is the upper bound."
  ]
}
```

### 8.2 `POST /api/runs`

Queue an evaluation. Returns a `job_id` to poll.

**Request body** (JSON) — same as preview:
```json
{
  "agent_id": 23,
  "scenario_set_id": 7,
  "judges_enabled": true,
  "runs_per_scenario": 1,
  "only_approved": true,
  "triggered_by": "jeremy@movate.com"
}
```

**Response 200**
```json
{
  "job_id": "job-b5faf50e37074675",
  "status": "queued",
  "total_scenarios": 13
}
```

### 8.3 `GET /api/runs/{job_id}`

Poll job status.

**Response 200**
```json
{
  "job_id": "job-b5faf50e37074675",
  "status": "done",
  "judges_enabled": true,
  "runs_per_scenario": 1,
  "triggered_by": "jeremy@movate.com",
  "created_at": "2026-05-05T15:27:06.954387+00:00",
  "started_at": "2026-05-05T15:27:09.879360+00:00",
  "ended_at": "2026-05-06T18:40:50.008532+00:00",
  "total_scenarios": 13,
  "completed_scenarios": 13,
  "error_message": null,
  "result_run_id": "run_2026-05-06T18-39-18Z",
  "overall_score": 84.68,
  "result_status": "pilot_ready",
  "cost_usd": 0.31
}
```

State machine: `queued → running → done` (or `→ failed` with `error_message`).

### 8.4 `GET /api/runs/{run_id}/doctor`

**NEW — Agent Doctor (Rx)**: 3-tier diagnostic. Tier 1 (executive summary + headline action) for delivery managers, Tier 2 (3 prescriptions, cited and confidence-tagged) for engineers, Tier 3 (specific suggested changes) for the next edit.

**Path params**
- `run_id` (integer) — the run.id PK (from `/api/agents/{id}/runs[*].id`, not the timestamped `run_id` string)

**Response 200**
```json
{
  "run_id": "run_2026-05-06T18-39-18Z",
  "overall_score": 84.68,
  "status": "pilot_ready",
  "executive_summary": "The FAQ Assistant is pilot-ready at 85/100 with 92% of scenarios passing. The biggest risk is occasional hallucination on factual queries; this should be addressed before full launch.",
  "headline_action": "Force grounding citations on every KB-backed response.",
  "prescriptions": [
    {
      "title": "Force grounding citations",
      "diagnosis": "5 of 13 scenarios show hallucinations because the agent answers without citing source documents.",
      "treatment": "Add to the system prompt: 'Always cite the section of the knowledge base your answer is grounded in.' Reject responses without citations.",
      "expected_impact": "Should fix 4-5 grounding failures, lifting overall score from 84 to ~89.",
      "confidence": "high",
      "cited_scenarios": ["demo_001", "demo_006"],
      "cited_findings": ["hallucination", "missing_step"]
    }
  ],
  "specific_changes": [
    {
      "target": "agent_instructions",
      "change": "Add: 'Before responding, verify every factual claim is supported by the knowledge base. If you cannot ground a claim, refuse or acknowledge uncertainty.'",
      "rationale": "Forces the agent to cite or refuse rather than hallucinate."
    }
  ],
  "confidence": "high",
  "source": "llm",
  "prompt_sha": "abc123def456...",
  "notes": []
}
```

**Field semantics**
- `source` — `"llm"` (fresh LLM call), `"cached"` (served from cache, $0), `"template"` (LLM unavailable, deterministic fallback)
- `confidence` — `"low"` / `"medium"` / `"high"` — LLM's stated confidence in its diagnosis
- `prompt_sha` — SHA-256 of the doctor's system prompt (audit trail; tied to manifest)
- `cited_scenarios` / `cited_findings` — what fed the prescription (Bolt should let users click these to drill in)

**Cost:** ~$0.02-0.05 first call per run; $0 subsequent.

**Response 404** — run not found, or run has no scenarios yet.

### 8.5 `GET /api/runs/{run_id}/business-report`

Executive-facing narrative report. See [BOLT_SCORING_PRD.md §14](BOLT_SCORING_PRD.md) for full Bolt UX spec.

**Response 200**
```json
{
  "run_pk": 22,
  "run_id": "run_2026-05-06T18-39-18Z",
  "agent_slug": "faq-assistant-v1",
  "agent_display_name": "FAQ Assistant v1",
  "overall_score": 84.68,
  "overall_score_ci": null,
  "status": "pilot_ready",
  "pass_rate": 0.923,
  "pass_rate_ci": null,
  "total_scenarios": 13,
  "passing_scenarios": 12,
  "headline": "FAQ Assistant v1 scored 85 — Pilot Ready, with 5 fixable issues before full production launch.",
  "executive_narrative": "The FAQ Assistant v1 is pilot-ready with a strong 92% pass rate...",
  "narrative_source": "llm",
  "top_wins": [
    {"category": "latency", "score": 100.0},
    {"category": "consistency", "score": 100.0},
    {"category": "safety", "score": 99.85}
  ],
  "top_losses": [
    {"kind": "category", "label": "ux_tone", "score": 64.54},
    {"kind": "category", "label": "task_success", "score": 67.48},
    {"kind": "failure_cluster", "label": "The agent makes up information not in your knowledge base",
     "scenarios_affected": 5, "severity": "high"}
  ],
  "failure_clusters": [
    {
      "failure_class": "hallucination", "label": "Hallucination",
      "count": 5, "severity": "high",
      "example_scenario_ids": ["s1","s2"], "suggested_fix": "Force citations",
      "business_label": "The agent makes up information not in your knowledge base",
      "customer_impact": "Customers receive confident-sounding but incorrect answers...",
      "business_fix": "Force the agent to cite its sources..."
    }
  ],
  "risk_register": [
    {
      "risk": "Recurring failure mode: Hallucination",
      "severity": "high", "likelihood": "medium",
      "mitigation": "Tighten retrieval; add chain-of-verification.",
      "business_label": "The agent makes up information not in your knowledge base",
      "customer_impact": "...",
      "business_mitigation": "..."
    }
  ],
  "what_to_fix_first": [
    {
      "rank": 1,
      "issue": "The agent makes up information not in your knowledge base",
      "leverage_text": "fixes 5 affected scenarios",
      "severity": "high",
      "fix": "Force the agent to cite its sources...",
      "kind": "failure_cluster"
    }
  ],
  "production_recommendation": "pilot_ready",
  "production_recommendation_text": "Recommend a controlled rollout to internal users while the team addresses the priority issues..."
}
```

### 8.6 `GET /api/runs/{run_id}/topic-breakdown`

Per-topic scoring rollup for a completed run. Groups all scenario results by their
`topic:<slug>` tag and returns mean score / pass rate / failure count / max severity per topic,
plus a category breakdown showing how each behavioral category did *within* that topic. Topics
are sorted worst-first.

This is the **2D scoring view** the topic-mix path was designed to enable: "for Movate Services
your standard scenarios are at 96 but adversarial are at 71." Customers who read the scorecard
want to know where in their *business* the agent is weak, not just "the agent's safety category
is at 78%."

Cost: zero LLM calls. Pure SQL groupby + Python aggregation; safe to call repeatedly.

**Path params**
- `run_id` (integer) — `run.id` PK (not the timestamped `run_id` string).

**Query params**
- `topic_names` (string, optional) — JSON object mapping `topic_slug -> display_name`. If
  absent, every `display_name` equals its `slug` (with `(untagged)` for the untagged bucket).
  Pass this when you've already called `/api/agent-definitions/topics` and want server-side
  rendering of names.

**Response 200**
```json
{
  "run_pk": 24,
  "run_id": "run_2026-05-07T14-30-00Z",
  "topics": [
    {
      "slug": "career_hiring",
      "display_name": "Career & Hiring",
      "scenarios_count": 3,
      "mean_score": 62.0,
      "pass_rate": 0.667,
      "failures_count": 2,
      "severity_max": "high",
      "category_breakdown": {
        "standard":    {"mean_score": 70.0, "pass_rate": 0.7,  "scenarios_count": 1},
        "adversarial": {"mean_score": 60.0, "pass_rate": 0.5,  "scenarios_count": 1},
        "safety":      {"mean_score": 56.0, "pass_rate": 0.8,  "scenarios_count": 1}
      },
      "scenario_ids": ["car_1", "car_2", "car_3"]
    },
    {
      "slug": "movate_services",
      "display_name": "Movate Services",
      "scenarios_count": 5,
      "mean_score": 92.5,
      "pass_rate": 1.0,
      "failures_count": 0,
      "severity_max": "medium",
      "category_breakdown": {"standard": {"mean_score": 95.0, "pass_rate": 1.0, "scenarios_count": 3}},
      "scenario_ids": ["svc_1", "svc_2", "svc_3", "svc_4", "svc_5"]
    },
    {
      "slug": "untagged",
      "display_name": "(untagged)",
      "scenarios_count": 1,
      "mean_score": 100.0,
      "pass_rate": 1.0,
      "failures_count": 0,
      "severity_max": "low",
      "category_breakdown": {"uncategorised": {"mean_score": 100.0, "pass_rate": 1.0, "scenarios_count": 1}},
      "scenario_ids": ["legacy_heuristic_1"]
    }
  ],
  "untagged_count": 1,
  "total_scenarios": 9
}
```

`topics[]` is sorted worst-first (lowest `mean_score`). The `untagged` bucket appears in
position determined by its score, not pinned at the top — Bolt should treat it as just another
topic. The `category_breakdown` keys are the same 8 behavioral categories used elsewhere in the
API (with `uncategorised` for scenarios missing a `category:<x>` tag, e.g. heuristic-extracted
ones).

**Response 200 with empty `topics`** — run exists but has no `scenario_aggregate` rows yet
(still running, or no scenarios produced). Total fields are 0; not an error.

**Response 400** — `topic_names` is malformed JSON or not an object of `str -> str`.

**Response 404** — run pk does not exist.

### 8.7 `GET /api/runs/{run_pk}/provenance`

Full audit-grade provenance for a completed run. Powers Bolt's Run Detail →
**Provenance tab**. Returns every fingerprint that affects the run's score,
library versions snapshotted at run time AND at query time (so auditors can
spot drift), plus the LLM models the post-run analysis surfaces currently
default to.

**Path params**
- `run_pk` (integer) — the `run.id` PK (not the timestamped `run_id` string)

**Response 200**
```json
{
  "run_pk": 8,
  "run_id": "run_2026-05-05T03-29-13Z",
  "agent_id": 1,
  "agent_slug": "faq-assistant-v3",

  "run": {
    "schema_version": "1.0",
    "methodology_version": "1.0",
    "mdk_eval_version": "0.1.0",
    "manifest_sha256": "1a93e1580d623b2d51b3091a74610e6f...",
    "dataset_sha256": "1d2242ad28e18aa5ca6e59897ac028c7...",
    "config_sha256": "5eb3d876187970...",
    "started_at": "2026-05-05T03:29:13+00:00",
    "ended_at": "2026-05-05T03:31:00+00:00",
    "ingested_at": "2026-05-05T03:32:00+00:00"
  },

  "scoring": {
    "judges_enabled": ["correctness", "grounding", "safety", "ux_tone"],
    "judge_models": {
      "correctness": "openai:gpt-4o",
      "grounding": "anthropic:claude-haiku-4-5"
    },
    "judge_prompts_sha256": {
      "correctness": "abc123...",
      "grounding": "def456..."
    },
    "meta_judge_model": "anthropic:claude-sonnet-4-6",
    "arbitration_threshold": 0.04,
    "runs_per_scenario": 1
  },

  "tool_versions_at_run_time": {
    "openai": "2.35.1",
    "anthropic": "0.100.0",
    "deepeval": "2.5.5",
    "ragas": "not-installed",
    "trulens-eval": "not-installed",
    "langfuse": "2.60.10",
    "opentelemetry-api": "1.41.1",
    "httpx": "0.28.1",
    "pydantic": "2.13.4",
    "jinja2": "3.1.6",
    "mdk-eval": "0.1.0"
  },

  "tool_versions_at_query_time": { ... same shape, current server values ... },

  "downstream_llm_models": [
    {
      "provider": "anthropic",
      "model": "claude-haiku-4-5-20251001",
      "max_tokens": 2048,
      "purpose": "topic_extractor (pre-run; categorizes agent into business topics)"
    },
    {
      "provider": "openai",
      "model": "gpt-4o-mini",
      "max_tokens": 1024,
      "purpose": "category/KPI insights (per-dimension drill-down narrative)"
    },
    {
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "max_tokens": 4096,
      "purpose": "agent_doctor (3-tier diagnostic: exec summary + prescriptions + specific changes)"
    },
    {
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "max_tokens": 4096,
      "purpose": "business_report (executive narrative + production recommendation)"
    }
  ],

  "triggered_by": "jeremy@movate.com",
  "ci_url": null,
  "notes": null
}
```

**Field semantics:**

- `run.*` — schema/methodology/mdk_eval/manifest/dataset/config fingerprints. Reproducible: an auditor with these can replay the exact run.
- `scoring.*` — judge models + prompt SHAs + arbitration policy. Pinned to the run; immutable.
- `tool_versions_at_run_time` — packages installed in the worker when this run executed. **Pinned; immutable.** Includes `"not-installed"` sentinel for libraries the eval doesn't use (e.g., `ragas`, `trulens-eval` if the RAG metrics weren't enabled for that run).
- `tool_versions_at_query_time` — packages installed RIGHT NOW on the API server. Differences from `at_run_time` highlight drift since the run; surface as a small "version drift" chip if any package's version differs.
- `downstream_llm_models` — what the doctor / insights / topic extractor / business report would use IF called against this run RIGHT NOW. Reflects current server defaults, NOT historical fact for this run. (Per-call audit lives in each surface's `prompt_sha` response field.)
- `triggered_by` / `ci_url` / `notes` — operator metadata.

**Cost:** $0 — all fields read from the `run` table or introspected via `importlib.metadata`. No LLM calls.

**Response 404** — run pk does not exist.

### 8.8 `GET /api/insights/{run_id}/{kind}/{name}`

LLM insight for one category or KPI.

**Path params**
- `run_id` (integer) — run.id PK
- `kind` — `"category"` or `"kpi"`
- `name` — e.g. `"correctness"`, `"accuracy"`, `"safety"`, `"reliability"`, `"helpfulness"`, `"speed"`

**Response 200**
```json
{
  "title": "Correctness",
  "score": 85.5,
  "narrative": "Correctness is high overall (88) but slips on the 3 multi-question scenarios...",
  "top_offenders": [
    {"scenario_id": "s1", "score": 60, "why": "Skipped the second sub-question"}
  ],
  "suggested_fixes": ["Add a planner step that enumerates sub-questions before answering"],
  "suggested_new_scenarios": [
    {"description": "Test agent on a 4-part question with explicit numbering"}
  ],
  "confidence": "high",
  "notes": []
}
```

### 8.9 `GET /api/insights/portfolio/{kind}/{name}`

Same shape as 8.6, but aggregated across all agents in the portfolio.

---

## 9. Portfolio analytics

### 9.1 `GET /api/portfolio/at-a-glance`

The single most important analytics endpoint. One round-trip → everything the portfolio overview needs.

**Query params**
- `sparkline_runs` (1-100, default 10) — last N runs per agent for sparklines
- `days` (1-365, default 30) — window for cost rollup + leaderboard
- `leaderboard_limit` (0-25, default 5) — top failing scenarios
- `stale_after_days` (default 7) — agents not run in this many days are flagged stale

**Response 200** — see [BOLT_ANALYTICS_PRD.md §3.1](BOLT_ANALYTICS_PRD.md) for the full shape (~30 fields). Key:

```json
{
  "generated_at": "2026-05-06T20:00:00+00:00",
  "window_days": 30,
  "summary": {
    "total_agents": 5,
    "total_engagements": 3,
    "status_counts": {"pilot_ready": 3, "needs_improvement": 2},
    "total_runs_in_window": 47,
    "total_cost_usd_in_window": 12.50,
    "runs_without_cost": 2
  },
  "agents": [{
    "id": 23, "slug": "faq-assistant-v1", "display_name": "FAQ Assistant v1",
    "backend": "lyzr",
    "engagement_id": 6, "engagement_slug": "movate-faq", "engagement_name": "Movate FAQ",
    "latest_run": {
      "run_id_pk": 22, "run_id": "run_2026-05-06T18-39-18Z",
      "started_at": "2026-05-06T18:39:18+00:00", "ended_at": "2026-05-06T18:40:50+00:00",
      "overall_score": 84.68, "status": "pilot_ready", "confidence": 0.92,
      "passing_scenarios": 12, "total_scenarios": 13,
      "scorecard": { /* 10 categories */ },
      "judges_enabled": true,
      "cost_usd": 0.31
    },
    "sparkline": [84.68, 82.0, 79.5],
    "delta_vs_prior": 2.68,
    "days_since_last_run": 0,
    "stale": false
  }],
  "engagements": [{ "slug": "...", "display_name": "...",
    "agent_count": 2, "mean_score": 78.5, "status_counts": {...}}],
  "platforms": [{"name": "lyzr", "agent_count": 4, "mean_score": 82.3,
    "mean_per_category": {"correctness": 86, "grounding": 80, ...}}],
  "leaderboard_preview": [{
    "scenario_id": "halluc_trap", "agents_affected": 3,
    "mean_pass_rate": 0.0, "severity_max": "high",
    "dominant_failure_class": "hallucination"
  }],
  "recency_alerts": []
}
```

### 9.2 `GET /api/portfolio/leaderboard`

Cross-portfolio failing-scenario leaderboard. Groups by `scenario_id`.

**Query params**
- `limit` (1-100, default 25)
- `days` (1-365, default 90)

**Response 200** (array)
```json
[
  {
    "scenario_id": "hallucination_trap",
    "agents_affected": 3,
    "runs_observed": 7,
    "mean_pass_rate": 0.14,
    "mean_score": 45.0,
    "severity_max": "high",
    "dominant_failure_class": "hallucination",
    "examples": [{"agent_slug": "faq-v1", "pass_rate": 0.0}]
  }
]
```

### 9.3 `GET /api/portfolio/cost`

Daily/weekly cost rollup.

**Query params**
- `days` (1-365, default 30)
- `granularity` — `"day"` (default) or `"week"`

**Response 200**
```json
{
  "total_cost_usd": 12.50,
  "by_day": [{"bucket_start": "2026-05-01T00:00:00+00:00", "cost_usd": 0.40, "runs": 1}],
  "by_agent": [{"agent_slug": "faq-v1", "cost_usd": 5.20, "runs": 12}],
  "by_engagement": [{"engagement_slug": "movate-faq", "cost_usd": 5.20}],
  "runs_without_cost": 2
}
```

---

## 10. Scoring profiles

### 10.1 `GET /api/scoring-profiles`

Preset library + framework defaults. Cache 5 min client-side.

**Response 200**
```json
{
  "presets": [
    {
      "name": "faq_external",
      "label": "FAQ — External / Customer-Facing",
      "description": "For agents that talk directly to customers...",
      "enabled_categories": ["task_success", "correctness", "grounding", "completeness",
                             "tool_usage", "workflow_adherence", "consistency", "latency",
                             "safety", "ux_tone"],
      "weights": {"safety": 2.0, "ux_tone": 1.5, "grounding": 1.8},
      "status_bands": {},
      "pass_threshold": null,
      "hard_gates": {"safety_threshold": 0.97},
      "kind": "preset"
    }
  ],
  "default_weights": {
    "task_success": 2.0, "correctness": 1.5, "grounding": 1.5,
    "completeness": 1.2, "tool_usage": 1.2, "workflow_adherence": 1.0,
    "consistency": 1.0, "latency": 0.8, "safety": 1.5, "ux_tone": 0.6
  },
  "default_status_bands": {"production_ready": 90, "pilot_ready": 80, "needs_improvement": 70},
  "default_pass_threshold": 75.0,
  "default_hard_gates": {
    "critical_check_failure": true,
    "safety_threshold": 0.95,
    "latency_on_high_severity": true
  },
  "all_categories": ["task_success", "correctness", "grounding", "completeness",
                      "tool_usage", "workflow_adherence", "consistency", "latency",
                      "safety", "ux_tone"]
}
```

### 10.2 `POST /api/scoring-profiles/recommend`

LLM advisor reads an agent definition and recommends a profile + per-category overrides.

**Form fields** (one of):
- `file` (file) — agent definition JSON, OR
- `agent_definition_json` (string) — inline JSON

**Response 200**
```json
{
  "recommended_preset": "faq_external",
  "profile": {
    "name": "faq_external", "label": "FAQ — External", "description": "...",
    "enabled_categories": [...], "weights": {...},
    "status_bands": {}, "pass_threshold": null,
    "hard_gates": {"safety_threshold": 0.97}, "kind": "recommended"
  },
  "reasoning": "Agent is explicitly customer-facing (website visitors)...",
  "category_recommendations": [
    {"category": "safety", "weight": 2.0, "enabled": true,
     "rationale": "Customer-facing context requires elevated safety"}
  ],
  "confidence": "high",
  "notes": []
}
```

---

## 11. Delete & cleanup

Destructive operations. All hard-deletes — no soft-delete / archive flag. Bolt
is responsible for the "are you sure?" confirmation modal; the backend does
not enforce a `?confirm=true` gate. Use the `cleanup-preview` companion
endpoint to populate the modal with actual row counts.

### 11.1 `GET /api/agents/{agent_id}/cleanup-preview`

Dry-run. Returns the per-table row counts that would cascade-delete if you
called `DELETE /api/agents/{agent_id}`. Read-only; safe to call any time.

Use this to power the blast-radius warning in Bolt's confirmation modal:
*"Deleting this agent will also remove 4 runs, 13 scenarios, and 52 scoring
records. 1 sub-agent will be detached from this manager."*

**Response 200**
```json
{
  "agent": {
    "id": 23,
    "slug": "movate-faq-assistant",
    "display_name": "Movate FAQ Assistant",
    "backend": "lyzr",
    "backend_id": "abc-123-..."
  },
  "would_delete": {
    "scenario_sets": 1,
    "scenarios": 13,
    "runs": 4,
    "scenario_aggregates": 52,
    "scenario_runs": 52,
    "findings": 8,
    "failure_clusters": 3,
    "evaluation_summaries": 4
  },
  "would_orphan_sub_agents": [
    {"id": 24, "slug": "search-kb-tool", "display_name": "Search KB", "backend": "lyzr", "backend_id": "..."}
  ]
}
```

**Response 404** — agent doesn't exist.

`would_orphan_sub_agents` lists agents whose `parent_agent_id = this.id`. They
are NOT deleted by `DELETE /api/agents/{id}`; their `parent_agent_id` is set to
`NULL` instead. If Bolt wants to clean those up too, the UI should offer a
follow-up flow that calls DELETE on each.

### 11.2 `DELETE /api/agents/{agent_id}`

Hard-deletes the agent + everything cascading from it. No request body.

**Cascade (per FK schema):**

- `agent` → `scenario_set` → `scenario`
- `agent` → `run` → `evaluation_summary`, `scenario_aggregate`, `scenario_run`,
  `finding`, `failure_cluster`, `risk_item`

**Sub-agents** (rows with `parent_agent_id = this.id`) are NOT deleted — their
`parent_agent_id` is set to `NULL` (`ON DELETE SET NULL`). They appear in
`orphaned_sub_agents` so the caller can offer a follow-up cleanup flow.

**Sandbox-mode agents** (`is_sandbox=true`, per migration 009): the endpoint
ALSO calls Lyzr's `DELETE /v3/agents/{lyzr_id}` first to tear down the
ephemeral instance. Failure on the Lyzr side is surfaced as a warning in
`notes` but does NOT block the local delete — operator intent ("get this row
out of my DB") wins over Lyzr-side state-sync. A leaked Lyzr instance gets
caught by the 24h cleanup cron (Phase 2) or via Lyzr Studio.

**Response 200**
```json
{
  "agent": {
    "id": 23,
    "slug": "movate-faq-assistant",
    "display_name": "Movate FAQ Assistant",
    "backend": "lyzr",
    "backend_id": "abc-123-..."
  },
  "deleted": {
    "scenario_sets": 1,
    "scenarios": 13,
    "runs": 4,
    "scenario_aggregates": 52,
    "scenario_runs": 52,
    "findings": 8,
    "failure_clusters": 3,
    "evaluation_summaries": 4
  },
  "orphaned_sub_agents": [
    {"id": 24, "slug": "search-kb-tool", "display_name": "Search KB", "backend": "lyzr", "backend_id": "..."}
  ],
  "notes": [
    "Sandbox tear-down: Lyzr agent `abc-123-...` deleted."
  ]
}
```

`deleted` mirrors the shape of `would_delete` from §11.1 — gives Bolt an audit
log of the destructive action without a follow-up query.

`notes` is empty for non-sandbox agents. For sandbox agents it carries the
Lyzr-side tear-down result (success message OR a leak-warning string with the
backend_id and error class).

**Response 404** — agent doesn't exist.

### 11.3 `DELETE /api/runs/{run_pk}`

Hard-deletes a single run + its scoring artifacts. The agent stays. The
agent's other runs stay. Only this one run and its `scenario_aggregate` /
`scenario_run` / `finding` / `failure_cluster` / `evaluation_summary` rows are
removed.

> ⚠️ **`run_pk` is the integer `run.id` PK**, NOT the timestamped `run_id`
> string (e.g. `run_2026-05-05T03-29-13Z`). Use the integer from the run-list
> or run-detail responses.

**Audit trail:** the `web_run_job` row that triggered this run keeps its
history. Only its `result_run_id` is set to `NULL`. The job's status / queued
time / completed time / triggered_by are preserved so the operator can still
see "yes, this run was launched and finished, even though the artifacts are
gone."

**Response 200**
```json
{
  "run": {
    "id": 24,
    "run_id": "run_2026-05-05T03-29-13Z",
    "agent_id": 23,
    "started_at": "2026-05-05T03:29:13.000Z"
  },
  "deleted": {
    "scenario_aggregates": 13,
    "scenario_runs": 13,
    "findings": 2,
    "failure_clusters": 1,
    "evaluation_summaries": 1
  }
}
```

**Response 404** — run doesn't exist.

There is no `cleanup-preview` companion for runs — the cascade is small and
bounded, and Bolt already shows scenario-level counts on the run detail page
before delete.

### 11.4 What is NOT deletable

These intentionally have no DELETE endpoint:

- **Individual scenarios** — scenarios are immutable artifacts of a run. To
  remove one before a run, edit the scenario set via the §7 author/approve
  flow (mark it `rejected`, then re-author). After a run, deleting a scenario
  would corrupt the scoring rollup; delete the whole run instead.
- **Scenario sets** — coupled to an agent's history. Use `DELETE /api/agents`
  to remove an agent and its sets together, or contact backend ops.
- **Web run jobs** — preserved by design as the audit trail of "what was
  launched, by whom, when." The associated run can be deleted (which nulls
  `result_run_id`); the job row itself stays.

If Bolt needs any of the above, file a backend-side change request — these
are deliberate omissions, not gaps.

---

## 12. Error envelope

Every error response uses this shape:

```json
{ "detail": "human-readable message" }
```

For validation errors from FastAPI (422):
```json
{
  "detail": [
    {"loc": ["body", "field_name"], "msg": "...", "type": "..."}
  ]
}
```

### Status code semantics

| Code | Meaning | Bolt should... |
|---|---|---|
| 200 | Success | Render |
| 400 | Bad request — invalid input | Show `detail`; don't retry |
| 401 | Auth failure | Re-prompt for token / show "session expired" |
| 404 | Resource missing | Show "not found" — don't retry |
| 409 | Conflict (e.g. id collision) | Show `detail`; offer alternative |
| 422 | Validation error | Iterate `detail[]`, highlight per-field |
| 429 | Rate limited | Exponential backoff retry |
| 500 | Server error | Retry once after 1s; show generic + `X-Trace-Id` |
| 503 | Service degraded | Check `/readyz`; show "system status" UI |

### Always show `X-Trace-Id` in error UIs

Every response carries `X-Trace-Id` and `X-Request-Id`. Display the trace id whenever an error is shown to the user — operators paste it into Application Insights to pull the full request log.

---

## 13. Authoring flow checklist for Bolt

Concrete handover — what to wire and in what order:

```
1. App boot:
   GET /api/extraction/prompts            (cache 5min)
   GET /api/scoring-profiles              (cache 5min)
   GET /api/portfolio/at-a-glance          (cache 30s)

2. Upload Agent page:
   POST /api/scoring-profiles/recommend   (returns suggested preset; show reasoning)
   POST /api/agent-definitions/preview     (Mix Designer; shows scenarios + cost)
   POST /api/agent-definitions             (commit; on success surface managed_agents_detected for multi-agent)

3. Scenario Review page:
   GET /api/scenario-sets/{id}             (list + payload)
   PATCH /api/scenarios/{pk}               (approve/reject/edit)
   POST /api/scenarios/{pk}/regenerate     (LLM regen with diff)
   POST /api/scenarios/propose-one         (quick-add)
   POST /api/scenario-sets/{id}/scenarios  (manual / bulk persist)

4. Run Evaluation modal:
   POST /api/runs/preview                  (cost preview; ALWAYS call)
   POST /api/runs                          (queue)
   GET  /api/runs/{job_id}                 (poll: 1s ×10s, 2s ×60s, 5s after)

5. Run Detail page (after status="done"):
   GET /api/runs/{run_id}/doctor           (Tier-1 + Tier-2 + Tier-3 diagnostic — NEW)
   GET /api/runs/{run_id}/business-report  (executive view)
   GET /api/insights/{run_id}/category/{name}  (drill-down per category)

6. Portfolio page:
   GET /api/portfolio/at-a-glance          (KPI tiles, agent grid, sparklines)
   GET /api/portfolio/leaderboard          (Patterns tab)
   GET /api/portfolio/cost                 (cost charts)

7. Multi-agent system view (if applicable):
   GET /api/agent-systems/{root_slug}      (manager + sub-agents + composite)
   PATCH /api/agents/{id}                  (relink sub-agent post-ingest)

8. Admin / settings:
   GET /version                            (footer; build provenance)
   GET /api/metrics                        (system status tile)
   GET /readyz                             (degraded-state banner)
```

---

## 14. Versioning

The OpenAPI spec at `/openapi.json` is the source of truth. When the backend version (visible at `GET /version`) increments minor or major, regenerate types:

```bash
npx openapi-typescript ${API_BASE}/openapi.json -o src/lib/apiTypes.ts
```

Breaking changes will be communicated via `Sunset` and `Deprecation` headers (RFC 8594-style) on the deprecated endpoint, with a minimum 30-day window before removal.

Every change to the four PRDs ([Backend](BOLT_BACKEND_PRD.md), [Analytics](BOLT_ANALYTICS_PRD.md), [Scoring](BOLT_SCORING_PRD.md), [Scoring Profiles](BOLT_SCORING_PROFILES_PRD.md)) is dated; check the "Updated" line at the top.

---

## Appendix A — Common request patterns

### Multi-agent ingest flow

```ts
// 1. Upload manager
const fd = new FormData();
fd.append('file', managerJsonFile);
fd.append('engagement_slug', 'sandisk-returns');
fd.append('agent_slug', 'returns-manager');
fd.append('scenario_set_name', 'v1');
const r1 = await fetch(`${API_BASE}/api/agent-definitions`, {
  method: 'POST', headers: {Authorization: `Bearer ${KEY}`}, body: fd,
});
const { managed_agents_detected } = await r1.json();

// 2. For each detected sub-agent, prompt user, then upload
for (const sub of managed_agents_detected) {
  if (sub.already_uploaded) continue;
  const subFile = await promptUserForFile(sub.display_name);
  const fd2 = new FormData();
  fd2.append('file', subFile);
  fd2.append('engagement_slug', 'sandisk-returns');
  fd2.append('agent_slug', sub.suggested_slug);
  fd2.append('scenario_set_name', 'v1');
  fd2.append('parent_agent_slug', 'returns-manager');   // ← the link
  await fetch(`${API_BASE}/api/agent-definitions`, {
    method: 'POST', headers: {Authorization: `Bearer ${KEY}`}, body: fd2,
  });
}

// 3. Navigate to system view
const sys = await fetch(`${API_BASE}/api/agent-systems/returns-manager`,
  {headers: {Authorization: `Bearer ${KEY}`}}).then(r => r.json());
```

### Run + post-run diagnostic flow

```ts
// 1. Cost preview
const preview = await fetch(`${API_BASE}/api/runs/preview`, {
  method: 'POST',
  headers: {Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json'},
  body: JSON.stringify({agent_id, scenario_set_id, judges_enabled: true,
                         runs_per_scenario: 1, only_approved: true}),
}).then(r => r.json());
// Show preview.estimated_cost_usd to user; require confirmation

// 2. Queue
const { job_id } = await fetch(`${API_BASE}/api/runs`, {
  method: 'POST',
  headers: {Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json'},
  body: JSON.stringify({agent_id, scenario_set_id, judges_enabled: true,
                         runs_per_scenario: 1, only_approved: true,
                         triggered_by: 'jeremy@movate.com'}),
}).then(r => r.json());

// 3. Poll until done
let status;
while (status?.status !== 'done' && status?.status !== 'failed') {
  await sleep(2000);
  status = await fetch(`${API_BASE}/api/runs/${job_id}`,
    {headers: {Authorization: `Bearer ${KEY}`}}).then(r => r.json());
}

if (status.status === 'done') {
  // 4. Pull post-run artifacts in parallel
  const run_pk = status.result_run_id;   // or use the run.id from /api/agents/X/runs
  const [doctor, businessReport, insights] = await Promise.all([
    fetch(`${API_BASE}/api/runs/${run_pk}/doctor`, {headers: ...}).then(r => r.json()),
    fetch(`${API_BASE}/api/runs/${run_pk}/business-report`, {headers: ...}).then(r => r.json()),
    fetch(`${API_BASE}/api/insights/${run_pk}/category/correctness`, {headers: ...}).then(r => r.json()),
  ]);
  // Render Run Detail page
}
```

---

## Appendix B — Headers reference

### Request headers Bolt should set

| Header | Required | Purpose |
|---|---|---|
| `Authorization: Bearer <token>` | ✓ for `/api/*` | Auth |
| `Content-Type: application/json` | for JSON bodies | Type signaling |
| `X-Trace-Id` | optional | Pre-generated trace id; backend will echo back. Useful for grouping multi-call workflows |

### Response headers Bolt should consume

| Header | Always present | Purpose |
|---|---|---|
| `X-Trace-Id` | ✓ | Display in error UIs |
| `X-Request-Id` | ✓ | Same as above; unique per HTTP request |
| `Sunset` | when deprecated | RFC 8594; surface as deprecation banner |
| `Deprecation` | when deprecated | RFC 8594 |

---

This document is generated from the OpenAPI spec + handler docstrings. If a number here disagrees with the live `/openapi.json`, **the live spec is authoritative.**

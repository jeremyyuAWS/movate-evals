# Movate Agent Assurance — Scoring Profiles PRD for Bolt

**Audience:** Bolt.new (or any frontend engineer) building the scoring-configuration UI.

**Status:** Phase 1 + 2 + 3 all shipped to production. The catalog endpoint, LLM advisor, and runtime profile application are all live. Bolt can pre-select a profile on upload, persist it with the scenario_set, and `compute_run_scores` applies it on every run.

**Updated:** 2026-05-06.

> **Read alongside [BOLT_BACKEND_PRD.md](BOLT_BACKEND_PRD.md) and [BOLT_SCORING_PRD.md](BOLT_SCORING_PRD.md).**

---

## 1. The motivation

Different agent types have very different scoring needs:

| Agent type | What matters most | What's irrelevant |
|---|---|---|
| Customer-facing FAQ | Safety + tone (brand exposure direct) | (none) |
| Internal automation | Correctness + tool usage | UX tone |
| Data extractor / RAG | Schema strictness + grounding | UX tone, latency |
| Manager orchestrator | Workflow + tool usage | (default rest) |
| Compliance bot | Safety (raised gate to 99%) | UX tone, latency |

The framework's defaults are a generic FAQ-shaped agent. Other archetypes need overrides — different category weights, disabled categories, tighter gates. **Scoring profiles** are how that's expressed, both for users (pick a preset) and for the platform (an LLM that recommends one).

## 2. The two endpoints

### 2.1 `GET /api/scoring-profiles` — preset catalog

Returns the 5 curated presets + framework defaults. Cache 5 minutes client-side.

```ts
// Response
{
  presets: ScoringProfile[];                       // 5 curated
  default_weights: Record<category, number>;       // framework defaults for "preset overrides X" UI
  default_status_bands: { production_ready, pilot_ready, needs_improvement };
  default_pass_threshold: number;                  // 75
  default_hard_gates: {
    critical_check_failure: boolean,
    safety_threshold: number,
    latency_on_high_severity: boolean,
  };
  all_categories: string[];                        // the 10 known categories in canonical order
}
```

Each `ScoringProfile`:

```ts
{
  name: string;                                     // "faq_external" | "internal_tool" | ...
  label: string;                                    // human-readable
  description: string;                              // 1-sentence description
  enabled_categories: string[];                     // subset of the 10
  weights: Record<category, number>;                // PARTIAL overrides — defaults fill in the rest
  status_bands: Record<string, number>;             // PARTIAL overrides
  pass_threshold: number | null;                    // null → use default 75
  hard_gates: Record<string, any>;                  // PARTIAL overrides
  kind: "preset" | "custom" | "recommended";
}
```

### 2.2 `POST /api/scoring-profiles/recommend` — LLM advisor

Reads an agent definition and recommends a preset + per-category overrides with reasoning.

**Request** (multipart form OR inline JSON):

| Field | Type | Description |
|---|---|---|
| `file` | upload | Lyzr agent definition JSON (optional) |
| `agent_definition_json` | form string | Inline JSON if not uploading (also optional, but at least one of the two must be present) |

**Response:**

```ts
{
  recommended_preset: string;                       // one of the 5 preset names
  profile: ScoringProfile;                          // the recommended profile (kind="recommended")
                                                    //   - starts from the preset
                                                    //   - applies the per-category overrides
                                                    //   - ready to render / edit / submit
  reasoning: string;                                // 2-4 sentences citing agent traits
  category_recommendations: Array<{
    category: string;                               // "safety", "tool_usage", etc.
    weight: number | null;                          // override (null if just enabling/disabling)
    enabled: boolean;
    rationale: string;                              // why the advisor recommends this override
  }>;
  confidence: "low" | "medium" | "high";
  notes: string[];                                  // e.g., "served from cache", "heuristic fallback used"
}
```

**Cost:** ~$0.01–0.02 per uncached call. Cached by agent-definition SHA, so the same JSON returns the same recommendation for $0.

**Fallback:** if the LLM is unavailable (no Anthropic key, network error), a deterministic heuristic produces a reasonable recommendation. The endpoint always returns useful output. The `notes` field tells you which path was taken.

## 3. The Bolt UX flow

Insert this after "Identifiers" but before the Mix Designer. Below the existing form fields:

```
┌────────────────────────────────────────────────────────────────────────┐
│ SCORING PROFILE                                              ⓘ Help   │
│                                                                         │
│ ☑ Recommended (LLM)        FAQ — External / Customer-Facing            │
│   Safety + tone boosted; safety gate tightened to 0.97                 │
│                                                                         │
│ ○ FAQ External             Customer-facing — boosted safety + tone     │
│ ○ Internal Tool            Correctness-first; UX/tone disabled         │
│ ○ Data Extractor           Schema-strict; latency relaxed              │
│ ○ Manager Orchestrator     Workflow-first; orchestration heavy         │
│ ○ Compliance Bot           Safety ≥ 99%; refusal-mode                  │
│ ○ Custom...                Roll your own                               │
│                                                                         │
│ Why this preset:                                                       │
│ "Movate FAQ Assistant is described as a customer-facing FAQ agent      │
│  that uses the company KB. Boosted safety (brand exposure direct)      │
│  and tone (visible to external users); tool_usage de-weighted because  │
│  tools=[] and there are no managed_agents."                            │
│                                                                         │
│ ▾ Advanced — view per-category weights / thresholds (collapsed)        │
└────────────────────────────────────────────────────────────────────────┘
```

### 3.1 The data flow

```
1. User uploads agent JSON  →  POST /api/scoring-profiles/recommend (multipart file)
                              ↓
2. Backend returns {recommended_preset, profile, reasoning, ...}
                              ↓
3. Bolt pre-selects "Recommended (LLM)" radio with the returned profile
   and renders the reasoning paragraph + the recommended profile.label
                              ↓
4. User can:
     a. Click another preset radio (Bolt swaps `selected_profile = preset_X`)
     b. Click "Custom..." (Bolt opens advanced editor for full control)
     c. Click "Generate scenarios" with the recommended profile (default path)
                              ↓
5. (Phase 3) The chosen profile is sent on POST /api/runs as `scoring_profile`
   form data. The runtime applies it to compute_run_scores. Until Phase 3,
   the profile is informational only — user can see the recommendation but
   the actual run uses framework defaults.
```

### 3.2 Pre-fetching the catalog

Call `GET /api/scoring-profiles` on app load (or first navigation to the upload page) and cache the response in component state for 5 minutes. The catalog is identical across users; only changes on backend redeploys.

### 3.3 Advanced editor (collapsed by default)

When user clicks "Advanced":

```
PER-CATEGORY WEIGHTS                                 (resets to preset defaults)
  Task Success           [====●==== ] 2.5  ⓘ
  Correctness            [===●===== ] 1.5  ⓘ
  Grounding              [====●==== ] 1.8  ⓘ
  Completeness           [==●====== ] 1.2  ⓘ
  Tool Usage             [==●====== ] 1.2  ⓘ
  Workflow Adherence     [==●====== ] 1.0  ⓘ
  Consistency            [==●====== ] 1.0  ⓘ
  Latency                [=●======= ] 0.8  ⓘ
  Safety                 [=====●=== ] 2.0  ⓘ
  UX / Tone              [===●===== ] 1.5  ⓘ

ENABLED CATEGORIES                                   (☐ = exclude from composite)
  ☑ task_success    ☑ correctness   ☑ grounding   ☑ completeness   ☑ tool_usage
  ☑ workflow_adherence  ☑ consistency  ☑ latency  ☑ safety  ☑ ux_tone

THRESHOLDS
  Pass threshold (composite ≥):       [75]
  Production-ready (≥):                [90]
  Pilot-ready (≥):                     [80]
  Needs-improvement (≥):               [70]

HARD GATES
  ☑ Critical deterministic check fail → score = 0
  ☑ Safety judge below threshold       [0.97]    → cap at 30
  ☑ Latency exceeded on HIGH+ severity → cap at 65
```

Slider tooltips (the ⓘ icons) explain what each category measures — Bolt should pull these from `BOLT_SCORING_PRD.md` §3.2 verbatim.

When the user changes any field, set `kind = "custom"` and switch the radio to "Custom...".

### 3.4 Runtime application (Phase 3 — live)

When `compute_run_scores` runs, it now reads the resolved profile and applies:

| Profile field | Effect at scoring time |
|---|---|
| `weights_resolved` | Per-category composite weights (overrides framework defaults) |
| `enabled_categories` | Disabled categories still get scored (surfaced in the report) but don't contribute to the composite |
| `hard_gates_resolved.safety_threshold` | Safety judge threshold; below this clamps to ≤30 |
| `hard_gates_resolved.critical_check_failure` | Toggle the `final = 0` clamp on critical-severity check failures |
| `hard_gates_resolved.latency_on_high_severity` | Toggle the `≤65` clamp on HIGH/CRITICAL scenarios with latency breach |
| `pass_threshold_resolved` | Composite threshold for `passed = True` |

The default-no-profile path is byte-identical to pre-Phase-3 behavior — runs without a profile attached score exactly as before. Profile-driven runs see the overrides applied transparently.

### 3.5 How to send the chosen profile to a run (today)

The CLI takes the profile via `--scoring-profile` (a name or a JSON-string), and `compute_run_scores` accepts it as a kwarg. When Bolt is ready to wire this end-to-end, send the resolved profile as form data on the run-queue endpoint:

```ts
const formData = new FormData();
formData.append('file', agentJsonFile);
formData.append('engagement_slug', engagementSlug);
formData.append('agent_slug', agentSlug);
formData.append('scenario_set_name', scenarioSetName);
formData.append('scoring_profile_json', JSON.stringify(selectedProfile));

const response = await fetch(`${API_BASE}/api/agent-definitions`, {
  method: 'POST',
  headers: { Authorization: `Bearer ${apiKey}` },
  body: formData,
});
```

Persistence of the profile per scenario_set / engagement is a follow-up — the runtime application (Phase 3) is in production; the persistence layer (Phase 4) is the natural next step. For now Bolt can keep the profile client-side and resend on each run.

## 4. Things Bolt should ALWAYS do

1. **Default to the recommended preset** with the LLM's reasoning visible. Don't make the user think before they upload.
2. **Show the reasoning paragraph in plain English.** Don't just show "Preset: faq_external" — show *why*.
3. **Let the user override anything.** Custom mode is the escape hatch.
4. **Explain "Advanced" clearly** — frame it as "tweak per-category weights and thresholds," not "edit YAML." This is for non-engineer reviewers.
5. **Respect the `notes` field.** When `notes` mentions "heuristic fallback used", surface a small banner: "*LLM advisor was unavailable; recommendation produced by heuristic. Click 'Refresh recommendation' to retry.*"
6. **Send the agent's API key as `file` not as inline JSON** when possible — the `file` path goes through our sanitizer, which strips `api_key` / `llm_credential_id` fields before they touch any LLM cache. Inline JSON skips that.

## 5. Things Bolt should NEVER do

1. **Never hardcode the 5 preset names** — fetch from `/api/scoring-profiles` so additions / renames don't break Bolt.
2. **Never omit the `kind` field** when persisting a profile (Phase 3) — the backend uses it to know whether the user accepted the preset as-is or customized.
3. **Never validate weights / thresholds client-side and reject the user's input** — the backend is the source of truth. Submit the full profile, surface backend validation errors to the user.
4. **Never mark a preset as "the right choice"** without showing the advisor's reasoning. Choosing a profile is a judgment call; Bolt's UI should support the judgment, not pretend there's a single right answer.

## 6. Open questions / future work

- **Per-engagement default profile.** Today every agent upload gets a fresh recommendation. Worth letting an engagement set "all FAQ-style agents in this engagement default to the FAQ External preset" so users skip the picker after the first one.
- **Custom presets the user can save.** Right now "Custom..." is per-upload. Letting users save a custom profile under a name (and share it across an engagement) is a natural follow-up.
- **A/B preset comparison.** Run the same scenario set under two different profiles to see how the score changes — useful when picking the right preset for an ambiguous agent.

These are all valid Phase 4+ work. Phase 3 (actually applying the profile inside `compute_run_scores`) is the prerequisite for any of them.

## 7. Versioning

The preset library is part of the API surface — adding new presets is additive (Bolt should fetch the list, not hardcode), but renaming or removing a preset is a breaking change and will be communicated via the standard `Sunset` / `Deprecation` headers (per BOLT_BACKEND_PRD §13).

The shape of `ScoringProfile` itself is stable. Adding new fields will be additive and gated behind `kind` so older Bolt builds keep working.

---

## Appendix A — curl samples

```bash
# Catalog
curl -sH "Authorization: Bearer $KEY" "$URL/api/scoring-profiles" | jq '.presets[].name'

# Recommend, file upload
curl -sH "Authorization: Bearer $KEY" -F "file=@agent.json" \
  "$URL/api/scoring-profiles/recommend" | jq '{preset: .recommended_preset, why: .reasoning}'

# Recommend, inline JSON
curl -sH "Authorization: Bearer $KEY" \
  -F 'agent_definition_json={"name":"X","agent_role":"Customer-facing FAQ","agent_instructions":"Be helpful"}' \
  "$URL/api/scoring-profiles/recommend" | jq
```

## Appendix B — implementation pointers

For when Bolt ships the advanced editor:

- The 10 categories in canonical order live in `all_categories` of the catalog response — render sliders in that order.
- Sliders should range 0.0 to 3.0 with 0.1 increments. Most curated presets keep weights between 0.5 and 2.5.
- Thresholds are integers 0–100. Status-band thresholds must be strictly ordered (production_ready > pilot_ready > needs_improvement). Bolt should validate this at submit time.
- The `safety_threshold` hard gate is on 0.0–1.0 (judge scale), not 0–100.

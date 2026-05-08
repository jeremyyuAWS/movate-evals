# Diagnostics Tab — SSE Streaming + Regenerate UX

**Audience:** Bolt.new engineer wiring the Run Detail page's Diagnostics tab against the upgraded Agent Doctor endpoint.

**Status:** Backend complete + deployed (`mdk-eval-web--0000023`, image `:20260507-221513`). 685 backend tests passing. Frontend work has not started.

**Cross-references:**
- [BOLT_API_REFERENCE.md](BOLT_API_REFERENCE.md) §8.4 — full `GET /api/runs/{run_id}/doctor` schema (now with `?stream=true`, `?regenerate=true`, `last_generated_at`)
- [MOVATE_WORKFLOW_DEEP_DIVE_DECK.md](MOVATE_WORKFLOW_DEEP_DIVE_DECK.md) Slide 24 — the Doctor's role in the eval workflow (3-tier diagnostic)

**Updated:** 2026-05-07

---

## 1. Why this exists

Today the Diagnostics tab fires a single GET, shows a static "Running diagnostic analysis…" spinner for 5–15 seconds, then renders the report. Three problems:

1. **Static spinner = wasted UX surface.** The LLM is doing real work. We can show the user what's happening — turning a black-box wait into a transparent multi-step process — at zero cost.
2. **No regenerate affordance.** Users who want a fresh framing (after a model update, or because the previous report doesn't read well) have no way to invalidate the cache from the UI.
3. **No "when was this generated?" signal.** A cached report from 3 weeks ago looks identical to one generated 30 seconds ago. Customers who run evals in CI need to see the report's age to trust it.

This PRD wires the existing backend support (already deployed) into Bolt's Diagnostics tab.

---

## 2. The user-visible change

```
┌──────────────────────────────────────────────────────────────┐
│  Diagnostics                                                  │
│  ────────────────────────────────────────                     │
│                                                                │
│  [LLM-generated · 2 hours ago]      [↻ Regenerate]            │
│                                                                │
│  [Doctor diagnosis card here…]                                 │
│  ...                                                           │
└──────────────────────────────────────────────────────────────┘
```

While the diagnosis is loading (uncached or regenerating):

```
┌──────────────────────────────────────────────────────────────┐
│  ⟳ Loading run KPIs and failure clusters...                   │
│    ✓ Loading agent responses + judge rationales               │
│    ✓ Computing per-topic and sub-agent breakdown              │
│    ✓ Fetching recent-runs trend context                       │
│    ⟳ Generating diagnosis (querying LLM)...                   │
└──────────────────────────────────────────────────────────────┘
```

(Each phase advances to ✓ as the next phase begins. The user sees motion every ~50ms-2s.)

---

## 3. Endpoint contract (already deployed — Bolt only consumes)

| Path | Behavior |
|---|---|
| `GET /api/runs/{run_id}/doctor` | One-shot JSON. Default. Use for non-interactive callers (CI, API). |
| `GET /api/runs/{run_id}/doctor?stream=true` | **Server-Sent Events.** Use for the Bolt UI. Streams 5 progress events then a `done` event with the full report. |
| `GET /api/runs/{run_id}/doctor?regenerate=true` | Bypasses cache, fires fresh LLM call. Bolt fires this from the Regenerate button. |
| `GET /api/runs/{run_id}/doctor?stream=true&regenerate=true` | Combine — streamed regenerate for the best UX. |

**SSE event shape** (one per `data: ...\n\n`):

```ts
type DoctorProgressEvent =
  | { phase: "loading"; message: "Loading run KPIs and failure clusters..." }
  | { phase: "trace_extract"; message: "Loading agent responses + judge rationales..." }
  | { phase: "topic_breakdown"; message: "Computing per-topic and sub-agent breakdown..." }
  | { phase: "trend"; message: "Fetching recent-runs trend context..." }
  | { phase: "llm_call"; message: "Generating diagnosis (querying LLM)..." }
  | { phase: "done"; report: DoctorResponse }
  | { phase: "error"; detail: string };
```

`DoctorResponse` is the same shape as the non-streaming endpoint (see API reference §8.4) — including `last_generated_at: string | null` and `source: "llm" | "cached" | "template"`.

**Headers returned:**
- `Content-Type: text/event-stream; charset=utf-8`
- `Cache-Control: no-cache`
- `X-Accel-Buffering: no` (proxy-buffer-disabled — events flush immediately)

---

## 4. Bolt implementation

### 4.1 Replace the single fetch with a streaming reader

**Why fetch + ReadableStream and not `EventSource`:** `EventSource` doesn't support custom request headers, so we can't send the Bearer auth token. The fetch + ReadableStream pattern is the standard workaround.

```tsx
// src/pages/RunDetail.tsx (or wherever DoctorTab lives)
import { useEffect, useState, useRef } from "react";

type DoctorProgressEvent =
  | { phase: "loading" | "trace_extract" | "topic_breakdown" | "trend" | "llm_call"; message: string }
  | { phase: "done"; report: DoctorResponse }
  | { phase: "error"; detail: string };

const PHASE_ORDER = ["loading", "trace_extract", "topic_breakdown", "trend", "llm_call"] as const;
type Phase = typeof PHASE_ORDER[number];

interface DoctorState {
  status: "loading" | "ready" | "error";
  currentPhase: Phase | null;
  completedPhases: Set<Phase>;
  currentMessage: string;
  report: DoctorResponse | null;
  error: string | null;
}

function useDoctorStream(runPk: number, regenerate: boolean = false) {
  const [state, setState] = useState<DoctorState>({
    status: "loading",
    currentPhase: null,
    completedPhases: new Set(),
    currentMessage: "Starting...",
    report: null,
    error: null,
  });
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    const url = new URL(`${API_BASE}/api/runs/${runPk}/doctor`);
    url.searchParams.set("stream", "true");
    if (regenerate) url.searchParams.set("regenerate", "true");

    fetch(url.toString(), {
      headers: { Authorization: `Bearer ${API_KEY}` },
      signal: ctrl.signal,
    })
      .then(async (res) => {
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const reader = res.body!.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let lastPhase: Phase | null = null;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          // SSE messages are delimited by "\n\n"
          const events = buffer.split("\n\n");
          buffer = events.pop() ?? "";

          for (const evt of events) {
            const dataLine = evt.split("\n").find((l) => l.startsWith("data: "));
            if (!dataLine) continue;
            const msg = JSON.parse(dataLine.slice(6)) as DoctorProgressEvent;

            if (msg.phase === "done") {
              setState({
                status: "ready",
                currentPhase: null,
                completedPhases: new Set(PHASE_ORDER),
                currentMessage: "",
                report: msg.report,
                error: null,
              });
              return;
            }

            if (msg.phase === "error") {
              setState((s) => ({ ...s, status: "error", error: msg.detail }));
              return;
            }

            // Progress event — advance the phase indicator
            const newCompleted = new Set(state.completedPhases);
            if (lastPhase) newCompleted.add(lastPhase);
            lastPhase = msg.phase;
            setState({
              status: "loading",
              currentPhase: msg.phase,
              completedPhases: newCompleted,
              currentMessage: msg.message,
              report: null,
              error: null,
            });
          }
        }
      })
      .catch((err) => {
        if (err.name === "AbortError") return;
        setState((s) => ({ ...s, status: "error", error: err.message }));
      });

    return () => ctrl.abort();
  }, [runPk, regenerate]);

  return { ...state, regenerate: () => abortRef.current?.abort() };
}
```

### 4.2 Phase progression UI

```tsx
const PHASE_LABELS: Record<Phase, string> = {
  loading: "Loading run KPIs",
  trace_extract: "Loading agent responses + judge rationales",
  topic_breakdown: "Computing per-topic and sub-agent breakdown",
  trend: "Fetching recent-runs trend",
  llm_call: "Generating diagnosis",
};

function ProgressList({ currentPhase, completedPhases, currentMessage }: {
  currentPhase: Phase | null;
  completedPhases: Set<Phase>;
  currentMessage: string;
}) {
  return (
    <ul className="space-y-2 text-sm">
      {PHASE_ORDER.map((phase) => {
        const done = completedPhases.has(phase);
        const active = currentPhase === phase;
        return (
          <li key={phase} className="flex items-center gap-2">
            {done ? (
              <span className="text-green-600">✓</span>
            ) : active ? (
              <Spinner size="sm" />
            ) : (
              <span className="text-gray-300">○</span>
            )}
            <span className={active ? "font-medium" : done ? "text-gray-500" : "text-gray-400"}>
              {active ? currentMessage : PHASE_LABELS[phase]}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
```

### 4.3 Render branch in DoctorTab

```tsx
function DoctorTab({ runPk }: { runPk: number }) {
  const [regenerateKey, setRegenerateKey] = useState(0);
  const { status, currentPhase, completedPhases, currentMessage, report, error } =
    useDoctorStream(runPk, regenerateKey > 0);

  if (status === "error") {
    return <ErrorBanner message={error ?? "Diagnostic generation failed."} onRetry={() => setRegenerateKey((k) => k + 1)} />;
  }

  if (status === "loading") {
    return (
      <div className="rounded-md border bg-card p-6">
        <h3 className="mb-4 text-sm font-medium">Generating diagnosis</h3>
        <ProgressList
          currentPhase={currentPhase}
          completedPhases={completedPhases}
          currentMessage={currentMessage}
        />
      </div>
    );
  }

  // status === "ready"
  return (
    <div className="space-y-4">
      <DoctorHeader
        report={report!}
        onRegenerate={() => setRegenerateKey((k) => k + 1)}
      />
      <DoctorReport data={report!} />
    </div>
  );
}
```

### 4.4 The header strip — source chip + last_generated_at + regenerate

```tsx
function DoctorHeader({ report, onRegenerate }: { report: DoctorResponse; onRegenerate: () => void }) {
  const sourceColors = {
    llm: { bg: "#1e7e34", fg: "#fff", label: "LLM-generated" },
    cached: { bg: "#f6f2f5", fg: "#4f3144", label: "Cached" },
    template: { bg: "#FFD200", fg: "#26282b", label: "Template (LLM unavailable)" },
  };
  const src = sourceColors[report.source] ?? sourceColors.template;

  return (
    <div className="flex items-center justify-between rounded-md border bg-card px-4 py-2 text-sm">
      <div className="flex items-center gap-3">
        <span
          className="rounded-full px-2 py-0.5 text-xs font-medium"
          style={{ background: src.bg, color: src.fg }}
          title={
            report.source === "llm"
              ? "Generated by Claude Sonnet 4.6 with full run context"
              : report.source === "cached"
                ? "Served from cache; original LLM generation timestamp shown"
                : "LLM unavailable; structural diagnostic from deterministic template"
          }
        >
          {src.label}
        </span>
        {report.last_generated_at && (
          <span className="text-gray-500" title={report.last_generated_at}>
            Generated {formatRelative(report.last_generated_at)}
          </span>
        )}
      </div>
      <button
        onClick={onRegenerate}
        className="flex items-center gap-1 rounded px-2 py-1 text-xs hover:bg-gray-100"
        title="Bypass cache and ask the LLM for a fresh diagnosis. Cost: ~$0.05."
      >
        <RefreshIcon className="h-3 w-3" />
        Regenerate
      </button>
    </div>
  );
}

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  const now = Date.now();
  const diff = (now - then) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} minutes ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} hours ago`;
  return `${Math.floor(diff / 86400)} days ago`;
}
```

---

## 5. Behavior across `source` states

| `report.source` | What happened | UI treatment |
|---|---|---|
| `"llm"` | Fresh LLM call landed and was cached | Green chip "LLM-generated", timestamp now |
| `"cached"` | Hit existing cache entry; didn't fire LLM | Plum chip "Cached", timestamp = original generation time |
| `"template"` | LLM call failed or unavailable; deterministic fallback ran | Yellow chip "Template (LLM unavailable)" + small note explaining the fallback. Show `report.notes[]` (which carries `"LLM failed: <type>"`) to surface the error type. |

**Important:** the report payload is structurally identical in all three cases (3 prescriptions + specific_changes + executive summary etc.). Only the narrative quality differs. UI should NOT branch on `source` for layout — only for the chip color and the optional explanatory tooltip.

---

## 6. Cost transparency

The Regenerate button should show a tooltip:

> *"Bypass cache and ask the LLM for a fresh diagnosis. Cost: ~$0.05. Cached re-fetches are $0."*

For the engagement-level audit story, the dashboard owner can see total LLM spend in the existing portfolio cost rollup (no extra UX needed for diagnostics specifically).

---

## 7. Acceptance criteria

A reviewer should be able to verify each of these by exercising the live UI:

1. **Initial load (uncached run)**: spinner shows ✓ next to completed phases as they progress; "Generating diagnosis..." remains active for the bulk of the wait.
2. **Initial load (cached run)**: phase progression flickers through quickly (< 1s) since the LLM call returns cached. Final report renders immediately.
3. **Source chip** matches the `report.source` value: green for `llm`, plum for `cached`, yellow for `template`.
4. **`last_generated_at`** is shown as a human-readable relative timestamp ("3 minutes ago", "2 hours ago", "yesterday").
5. **Regenerate button** triggers a fresh call (visible in network tab as `?regenerate=true`); spinner returns; when the new report lands, source flips to `llm` and timestamp resets to "just now".
6. **Error event** (rare — happens when run pk doesn't exist or DB transiently fails) shows an error banner with a retry button, NOT a generic crash.
7. **Tab navigation away from Diagnostics** while a stream is in flight aborts the fetch (verify in network tab that the request shows as cancelled).
8. **Console** is free of unhandled errors during the streaming flow.

---

## 8. Out of scope

These are not in this PRD; track separately:

- **Token-level streaming inside the LLM call** — the current SSE phases stop at "Generating diagnosis"; the actual Claude tokens arrive in one batch. True token streaming would require switching `_call_anthropic` to streaming mode and forwarding chunks; nice-to-have, not necessary for v1.
- **Audience-aware framing** (`?audience=engineer|exec|compliance`) — discussed in the Workflow Deep Dive deck, not yet built backend-side.
- **Per-prescription effort estimates** — discussed in earlier doctor-improvement bundle but not yet shipped.

---

## 9. Files referenced

- [BOLT_API_REFERENCE.md](BOLT_API_REFERENCE.md) §8.4 — endpoint contract
- [BOLT_DASHBOARD_SPEC.md](BOLT_DASHBOARD_SPEC.md) §8.1 — Run Detail panel structure (Doctor lives in panel #6 today; gets the SSE upgrade per this PRD)
- [MOVATE_WORKFLOW_DEEP_DIVE_DECK.md](MOVATE_WORKFLOW_DEEP_DIVE_DECK.md) Slide 24 — Doctor's role in the workflow

---

## 10. Suggested build order

Day 1 — wiring:
1. Add the `useDoctorStream` hook to a shared location (e.g. `src/hooks/useDoctorStream.ts`).
2. Refactor `DoctorTab` in `RunDetail.tsx` to use the hook + render the three branches (loading / ready / error).
3. Build the `ProgressList` component with the 5 phases.

Day 2 — polish:
4. Build the `DoctorHeader` strip with source chip, `last_generated_at` formatter, and Regenerate button.
5. Wire `onRegenerate` to remount the hook with a state-bumped `regenerateKey`.
6. Walk through §7 acceptance criteria with someone.

Day 3 — buffer:
7. Edge-case testing: navigate away mid-stream, click Regenerate during stream, server returns 404 (delete the run first).
8. Visual polish — animation on phase transitions, smooth opacity fade between loading and ready states.

Total: ~2 days of focused work for one engineer, plus a half-day of buffer.

---

## 11. Open questions for product

- **Should Regenerate require explicit confirmation?** ("This will spend $0.05 — proceed?") I'd say no — the cost is trivial and adding a confirmation modal slows the iteration loop. But if compliance customers want a per-LLM-call audit, we'd need to add it.
- **Show the full `prompt_sha` and judge model on the Diagnostics tab?** Useful for the audit-grade story; clutters the UI for casual users. Suggest hiding behind an expandable "Audit details" section that opens to show: prompt SHA, model, generation timestamp, all `notes[]`.
- **For runs whose `report.source === "template"`**, should we automatically retry the LLM call once before showing the yellow chip? Probably no — if the LLM is genuinely down, retrying once won't help and just doubles the user's wait. Better to surface the failure quickly and let them click Regenerate themselves.

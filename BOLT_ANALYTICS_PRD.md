# Movate Agent Assurance — Analytics PRD for Bolt

**Audience:** Bolt.new (or any frontend engineer) building the analytics surface of the Movate Agent Assurance dashboard.

**Status:** Most analytical data already exists in the backend; this document maps what's there, defines what should be built next, and gives Bolt the concrete UI patterns + queries to ship a first-class analytics experience.

**Updated:** 2026-05-06.

> **Read this alongside [BOLT_BACKEND_PRD.md](BOLT_BACKEND_PRD.md).** That doc covers auth, errors, polling, observability. This one covers the analytical surface specifically.

---

## 1. What "analytics" means here

The platform produces three kinds of analytical signal:

| Layer | Question | Source |
|---|---|---|
| **Per-run** | How did this single evaluation score? | `evaluation_summary`, `scenario_aggregate`, `report.json` |
| **Cross-run** (one agent over time) | Is this agent improving / regressing / drifting? | Sequential runs of the same agent |
| **Cross-agent** (portfolio) | Which agents are stable, which need help, which platform is best? | All agents' runs in a window |

Bolt's analytics views map onto these three layers. The mistake to avoid: **don't conflate run-level with portfolio-level** — they answer different questions and are billed by very different cardinalities.

---

## 2. Data model recap (the surface this all aggregates)

```
engagement (sandisk-returns, movate-faq, ...)
   └─ agent (faq-assistant-v1, ocr-agent, ...)
        ├─ scenario_set (v1-real-2026-05-05, ...)
        │    └─ scenario (movate_company_inquiry, ...)
        │         └─ scenario_aggregate (per scenario, per run)
        │              └─ finding (failure_class, evidence, severity)
        └─ run (overall_score, status, confidence, ...)
             ├─ evaluation_summary (per run)
             └─ web_run_job (cost_usd, judges_enabled, runs_per_scenario)
```

Time fields you'll join on:
- `run.started_at` and `run.ended_at` — when the eval ran
- `web_run_job.created_at` — when the user pressed Run
- `web_run_job.cost_usd` — populated post-migration-005, NULL for older runs

---

## 3. Existing endpoints — analytics use cases

These are already shipped. Bolt should use them as primary sources. Examples below show the full response shape and what chart/UI to build.

### 3.1 `GET /api/portfolio/at-a-glance`

**The single most important analytics endpoint.** One round-trip → everything the portfolio overview needs. Replace N+1 queries with this.

**Query params:**
- `sparkline_runs` (1–100, default 10) — how many recent runs per agent for sparklines
- `days` (1–365, default 30) — window for cost rollup + leaderboard
- `leaderboard_limit` (0–25, default 5) — top failing scenarios
- `stale_after_days` (default 7) — agents not run in this many days are flagged

**Response shape:**

```ts
{
  generated_at: string;             // ISO datetime
  window_days: number;
  summary: {
    total_agents: number;
    total_engagements: number;
    status_counts: Record<string, number>;     // {production_ready: 3, pilot_ready: 7, ...}
    total_runs_in_window: number;
    total_cost_usd_in_window: number;
    runs_without_cost: number;                  // pre-migration-005 runs
  };
  agents: Array<{
    id: number; slug: string; display_name: string;
    backend: string;                            // "lyzr" | "langgraph" | ...
    engagement_id: number; engagement_slug: string; engagement_name: string;
    latest_run: {
      run_id_pk: number; run_id: string;
      started_at: string; ended_at: string | null;
      overall_score: number;
      status: string;                           // production_ready | pilot_ready | ...
      confidence: number;
      passing_scenarios: number; total_scenarios: number;
      scorecard: Record<string, number>;        // all 10 categories on /100
      judges_enabled: boolean;
      cost_usd: number | null;
    } | null;
    sparkline: number[];                        // last N overall_scores, newest first
    delta_vs_prior: number | null;              // current - prior overall
    days_since_last_run: number | null;
    stale: boolean;                             // > stale_after_days
  }>;
  engagements: Array<{
    slug: string; display_name: string;
    agent_count: number;
    mean_score: number;
    status_counts: Record<string, number>;
  }>;
  platforms: Array<{
    name: string;                               // "lyzr" | "langgraph" | ...
    agent_count: number;
    mean_score: number;
    mean_per_category: Record<string, number>;  // mean across all agents per category
  }>;
  leaderboard_preview: Array<{
    scenario_id: string;
    agents_affected: number;
    mean_pass_rate: number;
    severity_max: string;
    dominant_failure_class: string | null;
  }>;
  recency_alerts: Array<{
    agent_slug: string; agent_name: string; engagement_name: string;
    days_since: number; last_score: number;
  }>;
}
```

**UI to build with this:**

1. **KPI tile row:** total_agents, status_counts (stacked bar or donut), total_cost_usd_in_window, runs in window
2. **Per-agent grid:** card per agent showing display_name, latest_run.overall_score, sparkline, delta_vs_prior arrow, status pill, cost_usd
3. **Engagement rollup table:** for each engagement, mean score + agent count + status mix (donut chart per row)
4. **Platform comparison radar chart:** for each platform, mean_per_category — answers "is Lyzr better at grounding than LangGraph?"
5. **Leaderboard preview table:** top 5 failing scenarios, agents_affected, severity_max
6. **Recency alerts banner:** "3 agents haven't been evaluated in over 7 days"

**Cache:** 30 seconds client-side. Re-fetch on user-driven actions (run completed, agent added).

### 3.2 `GET /api/agents/{agent_id}/runs?limit=10`

Recent runs for **one** agent. Drives sparkline detail views, trend pages, the recency panel.

**Response (per row):**
```ts
{
  id: number;                       // run pk for /api/insights URLs
  run_id: string;                   // timestamped
  started_at: string; ended_at: string | null;
  methodology_version: string;
  mdk_eval_version: string;
  runs_per_scenario: number;
  judges_enabled: string[];
  overall_score: number;
  status: string;
  confidence: number;
  passing_scenarios: number; total_scenarios: number;
  scorecard: Record<string, number>;
  cost_usd: number | null;
}
```

**UI:**
- **Run history table** on the agent detail page (newest first).
- **Score trend line** — `started_at` vs `overall_score` for last N runs.
- **Per-category trend small multiples** — one line chart per of the 10 categories.
- **Cost per run column** in the table (em-dash for null).

### 3.3 `GET /api/portfolio/leaderboard?limit=25&days=90`

Cross-portfolio failing-scenario leaderboard. Groups by `scenario_id` to show systemic vs agent-specific failures.

**Response (per row):**
```ts
{
  scenario_id: string;
  agents_affected: number;
  runs_observed: number;
  mean_pass_rate: number;
  mean_score: number;
  severity_max: string;
  dominant_failure_class: string | null;
  examples: Array<{ agent_slug: string; pass_rate: number; }>;
}
```

**UI:**
- **Patterns tab** — sortable table by `mean_pass_rate` ASC. Each row links to a detail view showing which agents specifically fail this scenario.
- **Failure class chart** — horizontal bar of failure_class frequency.
- **Severity heatmap** — scenario × severity grid colored by mean_pass_rate.

### 3.4 `GET /api/portfolio/cost?days=30`

Daily / weekly cost rollup.

**UI:**
- Cost-over-time line chart (days)
- Cost per agent stacked bar
- Cost per engagement pie

### 3.5 `GET /api/runs/{job_id}` (during/after run)

Single-run detail. Already documented in BOLT_BACKEND_PRD.md §2; analytical use:

**UI:**
- **Run detail page** with scorecard radar, per-scenario table, failure clusters list, suggested fixes, judge rationales (via insights endpoint), CI bands on overall score
- **Compared to prior run** — fetch the prior run via `/api/agents/{id}/runs?limit=2` and show deltas inline

### 3.6 `GET /api/insights/{run_id_pk}/{kind}/{name}`

LLM-generated narrative for one category/KPI of a completed run. **Cached server-side** — same dimension returns the same response without re-spending tokens.

**Response:**
```ts
{
  title: string;
  score: number;
  narrative: string;                            // 2-4 sentence explanation
  top_offenders: Array<Record<string, any>>;
  suggested_fixes: string[];
  suggested_new_scenarios: Array<Record<string, any>>;
  confidence: 'low' | 'medium' | 'high';
  notes: string[];
}
```

**UI:**
- **Insight cards** on the run detail page — one per low-scoring category, pre-fetched and shown as "What this means / How to fix"
- **Suggested new scenarios** — quick-add buttons that prefill `POST /api/scenarios/propose-one` with the suggested input

### 3.7 `GET /api/metrics`

Ops counters (auth required).

**UI:**
- **System status tile** in admin / settings — jobs_in_queue, jobs_running, jobs_failed_24h, cost_usd_24h. Refresh every 10s.

---

## 4. Proposed new analytics endpoints (the gaps)

These don't exist yet. Worth shipping. Each is gated behind a "build now / build later" decision — say the word and I'll implement.

### 4.1 `GET /api/analytics/score-trend?agent_id=N&days=90&granularity=day|week`

**Why it's useful:** at-a-glance gives sparklines (last N runs); this gives a **time-series** at user-chosen granularity, with one point per day/week. Bolt charts directly without aggregating client-side. Handles "we ran 12 evals on Tuesday" by averaging.

**Response:**
```ts
{
  agent_id: number; agent_slug: string;
  granularity: "day" | "week";
  series: Array<{
    bucket_start: string;                       // ISO datetime
    runs: number;                                // how many runs in this bucket
    mean_score: number;
    median_score: number;
    overall_score_ci_lo: number;                 // bootstrap CI per bucket
    overall_score_ci_hi: number;
    pass_rate: number;
    cost_usd: number;
  }>;
}
```

**UI:** time-series line chart with confidence-band shading. Best chart on the platform — "is the agent improving over time, with what statistical confidence?"

### 4.2 `GET /api/analytics/scorecard-heatmap?engagement_slug=X&days=30`

**Why it's useful:** at-a-glance gives per-platform mean_per_category, but you can't see _per-agent × per-category_. Heatmap is the right primitive: rows = agents, columns = 10 categories, cells = mean score.

**Response:**
```ts
{
  agents: Array<{
    id: number; slug: string; display_name: string;
    scorecard: Record<string, number>;          // category -> mean score in window
    runs_observed: number;
  }>;
  category_means: Record<string, number>;       // column averages for reference
}
```

**UI:** matrix view, color-graded by score (red <60, yellow <80, green ≥80). Click a cell → insights endpoint for that agent×category.

### 4.3 `GET /api/analytics/compare-agents?agent_ids=1,2,3&days=30`

**Why it's useful:** A/B/N comparison across agents on the same dataset (or aligned scenarios). Use case: "this engagement has 4 candidate prompts deployed as 4 agents — which is winning?"

**Response:**
```ts
{
  agents: Array<{
    id: number; slug: string; display_name: string;
    overall_score: number;
    overall_score_ci_lo: number; overall_score_ci_hi: number;
    pass_rate: number; pass_rate_ci_lo: number; pass_rate_ci_hi: number;
    scorecard: Record<string, number>;
    cost_usd_total: number;
    cost_per_passing_scenario: number;
  }>;
  shared_scenarios: Array<{
    scenario_id: string;
    per_agent_pass_rate: Record<number, number>; // agent_id -> pass_rate
  }>;
  best_in_category: Record<string, number>;      // category -> agent_id
  cost_efficiency_winner: number;                 // agent_id with lowest cost/passing ratio
}
```

**UI:**
- Side-by-side scorecard table with deltas highlighted
- Cost-quality scatter plot (cost on x, score on y) — find the Pareto frontier
- Shared-scenarios diff view — for each scenario, which agent passed/failed

### 4.4 `GET /api/analytics/failure-trends?days=90&granularity=week`

**Why it's useful:** are failures clustering around specific failure_classes over time? Is a class getting worse?

**Response:**
```ts
{
  granularity: "day" | "week";
  series: Array<{
    bucket_start: string;
    by_class: Record<string, {                  // failure_class -> stats
      count: number;
      affected_scenarios: number;
      severity_max: string;
    }>;
  }>;
  classes_trending_up: string[];                 // ≥2x in last bucket vs avg of prior 4
  classes_trending_down: string[];
}
```

**UI:**
- Stacked-area chart of failure counts by class over time
- "Trending up" / "trending down" callout cards above the chart
- Click a class → drill to the leaderboard filtered to that class

### 4.5 `GET /api/analytics/scenario-history?scenario_id=X&days=180`

**Why it's useful:** "this scenario fails frequently — is it stable in failing or did it just regress?" Single-scenario timeline across all agents.

**Response:**
```ts
{
  scenario_id: string;
  description: string;
  severity: string;
  series: Array<{
    bucket_start: string;
    runs: number;
    mean_pass_rate: number;
    failed_findings: Array<{                    // sample of findings in this bucket
      failure_class: string; reason: string;
    }>;
  }>;
  per_agent: Record<string, {
    pass_rate: number;
    runs_observed: number;
  }>;
}
```

**UI:** scenario-detail drilldown reachable from the leaderboard. Pass-rate timeline + per-agent breakdown.

### 4.6 `GET /api/analytics/cost-breakdown?days=30`

**Why it's useful:** the at-a-glance gives a single `total_cost_usd_in_window` number. This breaks it down so users can answer "where is my money going?"

**Response:**
```ts
{
  total_cost_usd: number;
  by_agent: Array<{ agent_slug: string; cost_usd: number; runs: number; }>;
  by_engagement: Array<{ engagement_slug: string; cost_usd: number; }>;
  by_judge_provider: Record<string, number>;    // openai: $X, anthropic: $Y
  by_day: Array<{ bucket_start: string; cost_usd: number; }>;
  cost_per_run_p50: number;
  cost_per_run_p95: number;
  runs_without_cost: number;                     // pre-migration-005
}
```

**UI:** finance/admin view — daily line + per-agent bar + provider donut.

### 4.7 `GET /api/analytics/judge-disagreement?days=30`

**Why it's useful:** when CIs are wide and confidence is low, the cause is usually judge disagreement. This surfaces it.

**Response:**
```ts
{
  total_arbitrations: number;
  escalation_rate: number;
  by_role: Record<string, {
    arbitrations: number;
    escalation_rate: number;
    median_disagreement: number;
  }>;
  most_contentious_scenarios: Array<{
    scenario_id: string;
    runs: number;
    escalation_rate: number;
  }>;
}
```

**UI:** quality-of-evaluation page (admin/methodology). Shows where the judges struggle most — useful when defending scores in stakeholder reviews.

---

## 5. Cross-cutting concerns

### 5.1 Time ranges & pagination

Every aggregate endpoint accepts a `days` parameter (default 30, max 365). Bolt UI should expose three preset buttons (7 / 30 / 90 days) plus a custom-range picker.

For list endpoints (>100 rows expected), accept `limit` + `offset` (or `cursor` for keyset pagination on the larger ones — TBD). Currently no list endpoint returns >200 rows; pagination not yet added but earmarked for the leaderboard if portfolio reaches >500 agents.

### 5.2 Null vs zero — the same rules as elsewhere

| Field | Null means | Zero means | Render |
|---|---|---|---|
| `cost_usd` | Pre-migration-005 run | Real $0 (judges off) | `—` vs `$0.00` |
| `pass_rate` | No runs in window | All scenarios failed | `—` vs `0%` |
| `delta_vs_prior` | Only one run exists | Score unchanged | `—` vs `+0.0` |
| `confidence` | Run hasn't completed | Judges fully disagreed | `—` vs `0.00` |

### 5.3 Bucket alignment

For time-series endpoints, buckets align to UTC midnight (day) or UTC Sunday (week). Bolt should re-bucket client-side if the user picks a non-UTC display TZ for charts (the timestamps are still UTC; Bolt's `formatDateTime` from PRD §F handles display).

### 5.4 Sample-size disclaimers

Every aggregate response should be **inspectable** — Bolt should show "n=12 runs" or similar near every aggregate number. With small `n`, CIs are wide; with large `n`, they're tight. Hiding `n` is the most common analytics mistake.

### 5.5 Performance characteristics

| Endpoint | Typical latency | Caching |
|---|---|---|
| `/api/portfolio/at-a-glance` | 80–200ms | client-side 30s |
| `/api/agents/{id}/runs` | 30–80ms | none |
| `/api/portfolio/leaderboard` | 100–300ms | 60s |
| Proposed analytics endpoints | 100–500ms | 60s |
| `/api/insights/...` | 200–800ms (LLM call) — first call only; cached after | server-side cache |

If you see latencies higher than these, file an issue — likely a missing index.

### 5.6 Permissions

Every analytics endpoint requires the bearer token. There's no per-user scoping yet — every request sees the entire portfolio. When multi-tenancy lands, Bolt will need to thread a user id; we'll communicate with deprecation headers (PRD §13).

---

## 6. The three analytics views Bolt should ship first

**View 1 — Portfolio overview** (already exists; just polish).
Powered by: `/api/portfolio/at-a-glance` + `/api/portfolio/cost` + `/api/portfolio/leaderboard`.
Charts: KPI tiles, per-agent cards with sparklines, engagement rollup table, platform radar, cost over time, top failing scenarios, recency alerts.
Refresh: 30s client-side; manual refresh button.

**View 2 — Agent trends** (1 agent, time-series).
Powered by: `/api/agents/{id}/runs` (today) + `/api/analytics/score-trend` (when shipped).
Charts: overall score line with CI bands, per-category small-multiple trends, run history table with cost column, judge agreement timeline.
Drill-downs: click a run point → run detail page (existing); click a category → insights endpoint.

**View 3 — Patterns** (cross-portfolio failure analytics).
Powered by: `/api/portfolio/leaderboard` (today) + `/api/analytics/failure-trends` (when shipped) + `/api/analytics/scenario-history` (when shipped).
Charts: top failing scenarios table, failure-class trending stacked area, scenario history timeline on click.

Once these three ship, the platform looks **complete** for analytics. The deeper views (cost-breakdown, compare-agents, judge-disagreement) are admin/power-user follow-ups.

---

## 7. Concrete chart-library recommendations

| Chart | Library | Why |
|---|---|---|
| Sparklines | Recharts `<LineChart>` mini, or `react-sparkline-chart` | Tiny, fast, no axis chrome |
| Time-series with CI bands | Recharts `<ComposedChart>` (Area for band + Line for mean) | Native CI rendering |
| Heatmap | `react-d3-heatmap` or custom CSS grid | The 10-category x N-agents matrix |
| Radar chart | Recharts `<RadarChart>` | The platform per-category comparison |
| Cost-quality scatter | Recharts `<ScatterChart>` | Pareto frontier visualization |
| Sankey / failure flow | Nivo `<Sankey>` (heavier import; only on Patterns view) | When showing failure_class → scenario flow |

If you're already using a charting library for the dashboard, stick with it — don't introduce a second.

---

## 8. KQL queries Bolt could expose in admin "system health" views

These query Application Insights directly (not the backend API). Useful for ops/admin pages where Bolt embeds an iframe to AppInsights or surfaces precomputed numbers.

```kusto
// Eval throughput in the last 24h
customEvents
| where timestamp > ago(24h)
| where name in ("job.started", "job.completed", "job.failed")
| summarize count() by name, bin(timestamp, 1h)
| render timechart
```

```kusto
// Judge cache hit ratio (cost-saver visibility)
customEvents
| where timestamp > ago(7d)
| where name in ("judge.call_completed", "judge.cache_hit")
| summarize count() by name, bin(timestamp, 1d)
| evaluate pivot(name)
| extend hit_rate = todouble(judge_cache_hit) / (todouble(judge_cache_hit) + todouble(judge_call_completed))
| render timechart
```

```kusto
// Slowest endpoints
customEvents
| where timestamp > ago(7d)
| where name == "http.request_completed"
| extend duration_ms = todouble(customDimensions.duration_ms),
         path = tostring(customDimensions.path)
| summarize p50=percentile(duration_ms, 50), p95=percentile(duration_ms, 95), count() by path
| order by p95 desc
| limit 20
```

---

## 9. Worked example — what shipping View 1 looks like

Concrete Bolt-side wiring for the Portfolio Overview view:

```ts
import type { paths } from './apiTypes';
type AtAGlance = paths['/api/portfolio/at-a-glance']['get']['responses']['200']['content']['application/json'];

function PortfolioOverview() {
  const { data, isLoading } = useQuery({
    queryKey: ['at-a-glance'],
    queryFn: () => api<AtAGlance>('/api/portfolio/at-a-glance?sparkline_runs=10&days=30&leaderboard_limit=5'),
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

  if (isLoading || !data) return <Skeleton />;

  return (
    <div>
      <KpiRow summary={data.summary} />
      <RecencyAlerts alerts={data.recency_alerts} />
      <AgentGrid agents={data.agents} />          {/* sparklines, deltas, cost pills */}
      <EngagementTable engagements={data.engagements} />
      <PlatformRadar platforms={data.platforms} />
      <LeaderboardPreview rows={data.leaderboard_preview} />
    </div>
  );
}

function AgentCard({ agent }: { agent: AtAGlance['agents'][number] }) {
  const cost = agent.latest_run?.cost_usd;
  const score = agent.latest_run?.overall_score ?? 0;
  return (
    <div className="card">
      <h3>{agent.display_name}</h3>
      <ScoreWithCI score={score} />            {/* uses ci_lo/hi if present */}
      <Sparkline values={agent.sparkline} />
      <DeltaPill delta={agent.delta_vs_prior} />
      <CostPill value={cost} />                {/* '—' if null, '$X.XX' if number */}
      {agent.stale && <StaleBadge days={agent.days_since_last_run} />}
    </div>
  );
}
```

That's the whole pattern: one fetch hook, one component per data section, all driven by typed responses. Multiplied across the seven existing endpoints, the analytics surface is built.

---

## 10. Things Bolt should NEVER do in analytics views

1. **Never paginate a `/api/portfolio/at-a-glance` response client-side** — the whole portfolio is intentionally returned as one round-trip; if you find yourself filtering it in the UI loop, file for a server-side filter param.
2. **Never re-aggregate per-day server data into per-week client-side** — pass `granularity=week` to the endpoint so the bucket boundaries match what the server computes.
3. **Never display a `mean_score` without `n`** — sample-size context is what makes analytics defensible.
4. **Never poll analytics endpoints faster than every 30 seconds** — most are 30-60s cached; faster polling is wasted load.
5. **Never compute confidence intervals client-side** — the backend has Wilson + bootstrap (PRD-T-P2-2); use the `ci_lo` / `ci_hi` fields.

## 11. Things Bolt should ALWAYS do in analytics views

1. **Show the time window prominently** — every aggregate is window-dependent.
2. **Show `n` (sample size) near every aggregate**.
3. **Color-grade scores consistently**: 0–60 red, 60–80 yellow, 80–90 green, 90+ deep green.
4. **Use absolute timestamps in tooltips, relative ones in row labels** ("3 days ago" in the table; "2026-05-03 14:32 UTC" on hover).
5. **Always include the trace_id in error UIs** (per BOLT_BACKEND_PRD §4) — analytics queries hit the same observability layer.

---

## 12. Open questions (decisions owed)

- **Cohort analytics** — should we group agents by deploy cadence / change-frequency for "agents with frequent changes vs stable agents" comparisons? Useful for an org with 50+ agents; premature for ≤20.
- **Drift detection** — the at-a-glance has `delta_vs_prior` which is the simplest form. Worth adding statistical drift detection (e.g., mean shift detection over the last 10 runs)? Better as an alerting feature than a chart.
- **Public analytics exports** — should we provide a `/api/analytics/export?format=csv|parquet` so finance / data teams can download? Minimal effort to add if there's demand.
- **Real-time analytics** — currently every endpoint is read-from-Postgres. If we add SSE / WebSocket streaming for live run progress, should analytics also stream? Lean toward "no, polling at 60s is fine."

---

## 13. Versioning & change communication

Same as the main backend PRD: pin to OpenAPI at frontend build time, regenerate types when the backend version increments minor or major, watch for `Sunset` / `Deprecation` headers (RFC 8594).

When we add the proposed §4 endpoints, they'll be additive — no breaking changes. Bolt can adopt them incrementally.

---

## Appendix A — Sample queries (curl, ready to copy)

Replace `$URL` and `$KEY` with the values from Azure secrets.

```bash
# Portfolio at-a-glance — 30-day window, top 5 leaderboard
curl -sH "Authorization: Bearer $KEY" \
  "$URL/api/portfolio/at-a-glance?days=30&sparkline_runs=10&leaderboard_limit=5" | jq

# Recent runs for one agent (id=23)
curl -sH "Authorization: Bearer $KEY" \
  "$URL/api/agents/23/runs?limit=20" | jq '.[].overall_score'

# Top failing scenarios across portfolio in last 90 days
curl -sH "Authorization: Bearer $KEY" \
  "$URL/api/portfolio/leaderboard?limit=25&days=90" | jq

# Cost rollup
curl -sH "Authorization: Bearer $KEY" \
  "$URL/api/portfolio/cost?days=30" | jq

# Insights for run pk=42, category=grounding
curl -sH "Authorization: Bearer $KEY" \
  "$URL/api/insights/42/category/grounding" | jq

# Ops metrics (auth required)
curl -sH "Authorization: Bearer $KEY" \
  "$URL/api/metrics" | jq
```

---

## Appendix B — Build order if Bolt has a week

**Day 1–2:** View 1 (Portfolio overview) using existing `/api/portfolio/at-a-glance` + cost + leaderboard. KPI tiles, agent grid, recency alerts.

**Day 3:** View 2 (Agent trends) using `/api/agents/{id}/runs`. Score line, per-category small-multiples, run history table.

**Day 4:** View 3 (Patterns) using `/api/portfolio/leaderboard`. Failing scenarios table, basic drill-down to the failure clusters from each run report.

**Day 5:** Polish — empty states, error states with trace_id, cost null handling, time-window picker, refresh button.

**End of week:** Three first-class analytics views shipped. The proposed §4 endpoints (score-trend, scorecard-heatmap, compare-agents, etc.) are next-week candidates — file them as backlog and we can ship them when Bolt has bandwidth.

---

When in doubt, ship analytics with **fewer charts and more sample-size context** rather than the inverse. The platform is more credible when its numbers come with `n=` and CIs than when it has 50 chart types but no provenance.

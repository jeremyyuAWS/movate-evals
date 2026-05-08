"""Request/response Pydantic models for the FastAPI surface.

Kept in a separate module from the route handlers so the dashboard frontend
can import / generate types from this file alone (FastAPI's OpenAPI export
covers everything declared here).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ----------------------------- ingest -----------------------------


class IngestRequest(BaseModel):
    """Request body for POST /api/agent-definitions.

    The agent definition itself is sent as the uploaded file (multipart).
    These fields come from form data that accompanies it.
    """
    engagement_slug: str = Field(..., description="e.g. 'sandisk-returns'; created on first use.")
    engagement_name: str | None = Field(None, description="Display name; defaults to slug.")
    agent_slug: str = Field(..., description="e.g. 'returns-manager'; unique within engagement.")
    agent_name: str | None = Field(None, description="Display name; defaults to slug.")
    scenario_set_name: str = Field(..., description="Name for this bundle, e.g. 'v3-baseline'.")
    synthesize: bool = Field(False, description="Augment heuristic scenarios with LLM proposals.")
    triggered_by: str | None = Field(None, description="Who uploaded — captured for audit.")
    parent_agent_slug: str | None = Field(
        None,
        description=(
            "Multi-agent systems: when this agent is a sub-agent of a manager, "
            "set this to the manager's slug (within the same engagement). "
            "The platform will set agent.parent_agent_id to the resolved manager. "
            "Standalone agents and managers themselves leave this null."
        ),
    )


class ManagedAgentDetected(BaseModel):
    """Sub-agent referenced in an uploaded manager's `managed_agents` array.

    Surfaced in IngestResponse so Bolt can prompt the user to upload each
    sub-agent next, with the manager's slug pre-filled as parent_agent_slug.
    """
    backend_id: str                                  # Lyzr agent_id of the sub-agent
    display_name: str                                # cleaned-up display name from the manager's JSON
    usage_description: str | None = None             # what the manager uses it for
    suggested_slug: str                              # auto-suggested slug (e.g. "ocr-agent")
    already_uploaded: bool = False                   # true if a child agent with this backend_id exists


class IngestedScenario(BaseModel):
    id: int
    scenario_id: str
    severity: str | None
    tags: list[str] = Field(default_factory=list)
    derived_from: dict[str, Any] | None = None


class IngestResponse(BaseModel):
    scenario_set_id: int
    agent_id: int
    engagement_id: int
    source_sha256: str
    scenarios: list[IngestedScenario]
    warnings: list[str] = Field(default_factory=list)
    parent_agent_id: int | None = Field(
        None,
        description="If this agent was uploaded with parent_agent_slug, the resolved manager's ID.",
    )
    managed_agents_detected: list[ManagedAgentDetected] = Field(
        default_factory=list,
        description=(
            "If this agent's JSON contains a managed_agents array (i.e., it's a manager), "
            "the platform surfaces the referenced sub-agents here so Bolt can prompt "
            "the user to upload them next, pre-linked via parent_agent_slug."
        ),
    )


# ----------------------------- agent systems (manager + sub-agents) -----------------------------


class AgentSystemMember(BaseModel):
    """One agent in a multi-agent system rollup view."""
    id: int
    slug: str
    display_name: str
    backend: str
    role: str                                        # "manager" | "sub_agent"
    overall_score: float | None
    status: str | None                               # production_ready / pilot_ready / ...
    pass_rate: float | None
    runs_count: int                                  # lifetime runs for this agent
    last_run_at: datetime | None
    cost_usd: float | None                           # sum of run costs in window


class AgentSystemResponse(BaseModel):
    """Full rollup of a manager + all its sub-agents.

    Powers Bolt's "Returns Manager System" view — one card per agent, plus a
    composite system score that blends them with manager-weighted importance.
    """
    engagement_slug: str
    engagement_name: str
    manager: AgentSystemMember
    sub_agents: list[AgentSystemMember]
    composite_score: float                           # weighted: manager 1.5x, sub-agents 1.0x each
    composite_status: str                            # weakest of the members (worst-of)
    members_evaluated: int                           # how many members have at least 1 run
    members_total: int


class AgentMeta(BaseModel):
    """Identity columns for an agent — used as a sub-payload in cleanup responses."""
    id: int
    slug: str
    display_name: str | None = None
    backend: str | None = None
    backend_id: str | None = None


class AgentCleanupCounts(BaseModel):
    """Per-table count of rows that would (or did) cascade-delete with an agent."""
    scenario_sets: int = 0
    scenarios: int = 0
    runs: int = 0
    scenario_aggregates: int = 0
    scenario_runs: int = 0
    findings: int = 0
    failure_clusters: int = 0
    evaluation_summaries: int = 0


class AgentCleanupPreviewResponse(BaseModel):
    """Returned from GET /api/agents/{id}/cleanup-preview.

    Lets a Bolt admin UI display "this will delete N runs and M scenarios"
    before the user confirms the destructive action.
    """
    agent: AgentMeta
    would_delete: AgentCleanupCounts
    would_orphan_sub_agents: list[AgentMeta] = Field(default_factory=list)


class AgentDeleteResponse(BaseModel):
    """Returned from DELETE /api/agents/{id}.

    Echoes back what was deleted so the client has an audit log of the
    destructive action without a separate query.

    `notes` carries side-effect messages — for sandbox agents, this surfaces
    the Lyzr-side tear-down result (success, leak warning, etc.) so the
    operator has full visibility into what happened on both sides.
    """
    agent: AgentMeta
    deleted: AgentCleanupCounts
    orphaned_sub_agents: list[AgentMeta] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RunMeta(BaseModel):
    """Identity columns for a run — used in run-delete responses."""
    id: int
    run_id: str | None = None
    agent_id: int
    started_at: str | None = None


class RunCleanupCounts(BaseModel):
    """Per-table count of rows that cascade-delete with a run."""
    scenario_aggregates: int = 0
    scenario_runs: int = 0
    findings: int = 0
    failure_clusters: int = 0
    evaluation_summaries: int = 0


class RunDeleteResponse(BaseModel):
    """Returned from DELETE /api/runs/{run_pk}."""
    run: RunMeta
    deleted: RunCleanupCounts


class AgentRelinkRequest(BaseModel):
    """PATCH /api/agents/{id} body — manage parent_agent_id and recover from
    upload-time data loss (e.g. backend_id missing because the export shape
    didn't include it).

    All fields are optional. Pass only the fields you want to change.
    """
    parent_agent_slug: str | None = Field(
        None,
        description="Set to a manager's slug to link this agent as its sub-agent. Pass null to unlink.",
    )
    backend_id: str | None = Field(
        None,
        description=(
            "Override the platform-side agent ID (Lyzr `_id` for Lyzr agents). "
            "Use this to recover an agent record whose upload didn't include "
            "the ID. Empty string is rejected — pass a real ID."
        ),
    )


# ----------------------------- runs -----------------------------


class RunRequest(BaseModel):
    """POST /api/runs body."""
    agent_id: int
    scenario_set_id: int
    judges_enabled: bool = True
    runs_per_scenario: int = Field(1, ge=1, le=10)
    triggered_by: str | None = None
    only_approved: bool = Field(
        True,
        description=(
            "If True (default), evaluate only scenarios with status='approved'. "
            "Set False to include unverified scenarios as well."
        ),
    )


class RunQueuedResponse(BaseModel):
    job_id: str
    status: str
    total_scenarios: int


class RunStatusResponse(BaseModel):
    job_id: str
    status: str
    judges_enabled: bool
    runs_per_scenario: int
    triggered_by: str | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    total_scenarios: int | None
    completed_scenarios: int
    error_message: str | None
    result_run_id: str | None  # the run_id (e.g. "run_2026-..."), only set when status='done'
    overall_score: float | None
    result_status: str | None  # production_ready / pilot_ready / etc.
    cost_usd: float | None  # populated on completion via cost.estimate_run_cost; null pre-migration-005


# ----------------------------- cost preview -----------------------------


class CostPreviewResponse(BaseModel):
    """Estimated cost of a proposed run. Surfaced in the dashboard's
    'Run Evaluation' modal so users see what they're about to spend."""
    estimated_cost_usd: float
    estimated_total_judge_calls: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    breakdown_per_call_usd: float
    num_scenarios: int
    runs_per_scenario: int
    judges_enabled: bool
    notes: list[str] = Field(default_factory=list)


# ----------------------------- insights -----------------------------


class InsightResponse(BaseModel):
    """LLM-generated narrative for a single category or KPI of one run.

    Cached via the judge cache — same (run, dimension) returns the same
    response on subsequent calls without re-spending tokens.

    `top_offenders[]` carries a richer per-scenario shape than the LLM
    produces — backend mechanically merges structured data the LLM doesn't
    need to re-derive (scenario_title, severity, topic, behavior_category,
    failure_class, category_score, agent_response_excerpt, judge_rationale).
    Bolt should render `scenario_title` (human-readable) as the row label;
    `scenario_id` is the slug suitable for deep-linking to the scenario
    detail drawer.
    """
    title: str                 # the category/KPI name (e.g., "task_success")
    score: float               # 0–100 headline number
    narrative: str             # 2–4 sentence explanation
    top_offenders: list[dict[str, Any]] = Field(default_factory=list)
    suggested_fixes: list[str] = Field(default_factory=list)
    suggested_new_scenarios: list[dict[str, Any]] = Field(default_factory=list)
    confidence: str = "medium"  # low | medium | high
    notes: list[str] = Field(default_factory=list)
    # ---------- enrichments shipped 2026-05-07 ----------
    score_band: str = "watch"            # "pass" (>=80) | "watch" (60-79) | "fail" (<60)
    weight_pct: float = 0.0              # this category's % weight in the composite score
    raw_weight: float = 0.0              # raw weight (default profile)
    distribution: dict[str, int] = Field(default_factory=dict)
    # `distribution`: {"pass": N, "watch": N, "fail": N, "total": N} for the
    # category — lets the UI render "3 of 13 scenarios are dragging this score"
    # without recomputing.


# ----------------------------- preview / categories -----------------------------


class IngestPreviewRequest(BaseModel):
    """Request body for POST /api/agent-definitions/preview.

    Same shape as the persisting ingest, plus per-category mix + optional
    focus. Returns proposed scenarios + estimated cost WITHOUT writing
    anything to the DB. Lets the Test Mix Designer UI show "here's what we'd
    generate" before commitment.
    """
    engagement_slug: str | None = None        # optional — preview doesn't persist
    agent_slug: str | None = None
    scenario_set_name: str | None = None
    mix: dict[str, int] | None = None         # category -> count; defaults to DEFAULT_MIX
    focus: str | None = None                  # one-sentence guidance for the LLM
    custom_directive: str | None = None       # required if mix['custom'] > 0


class IngestPreviewResponse(BaseModel):
    """No-persist preview of an ingest. Returns proposed scenarios + cost so
    the user can refine the mix before committing.

    `counts_by_category` is the legacy 1D view (behavioral category -> count).
    When the request used the 2D topic-mix path, `counts_by_topic_category` is
    also populated with the per-cell breakdown so the UI can show coverage
    grouped either way; both fields are derived from the SAME generated
    scenarios — they just project on different axes.
    """
    source_sha256: str
    scenarios: list[dict[str, Any]]
    counts_by_category: dict[str, int]
    counts_by_topic_category: dict[str, dict[str, int]] = Field(default_factory=dict)
    estimated_cost_usd: float
    estimated_total_judge_calls: int
    warnings: list[str] = Field(default_factory=list)


class TopicResponse(BaseModel):
    """One topical category extracted from an agent definition."""
    name: str                                          # display name, e.g., "Movate Services"
    slug: str                                          # canonical, e.g., "movate_services"
    description: str
    relevance: float                                   # 0..1
    example_queries: list[str] = Field(default_factory=list)
    recommended_count: int = 0                         # default test count the Mix Designer
                                                       # should populate for this topic. Backend
                                                       # guarantees ≥ 1 for every extracted
                                                       # topic so coverage is automatic; the
                                                       # remainder of a 13-test budget is
                                                       # distributed by relevance via Hamilton's
                                                       # method. Bolt populates the per-topic
                                                       # counters from this field on first load.


class TopicExtractionResponse(BaseModel):
    """Returned from POST /api/agent-definitions/topics."""
    topics: list[TopicResponse] = Field(default_factory=list)
    source: str                                        # "llm" | "cached" | "heuristic"
    notes: list[str] = Field(default_factory=list)


class CategoryInfo(BaseModel):
    """Public-facing description of one extraction category."""
    name: str                                  # 'standard' | 'edge' | ...
    label: str
    description: str
    default_severity: str
    directive: str                             # the LLM directive — auditable
    default_count: int                         # count from DEFAULT_MIX (0 if not in default)


class ExtractionPromptsResponse(BaseModel):
    """Returned from GET /api/extraction/prompts.

    Surfaces the per-category directives the LLM extractor uses, plus the
    base system prompt all categories share. Powers:
      - the Test Mix Designer's per-category tooltip ("what is 'adversarial'?")
      - audit / risk-review compliance ("show me what you ask the LLM to do")
    """
    base_system_prompt: str
    categories: list[CategoryInfo]
    default_mix: dict[str, int]


class MixPresetResponse(BaseModel):
    """One named behavioral preset for the Test Mix Designer.

    `ratios` is the *normalised* distribution (sums to 1.0). The Mix Designer
    multiplies these against per-topic counts to derive how many scenarios
    of each behavioral category to generate per topic.
    """
    name: str                                          # 'balanced' | 'compliance_heavy' | ...
    label: str                                         # 'Balanced'
    description: str                                   # one-paragraph plain-English
    ratios: dict[str, float]                           # category -> 0..1


class MixPresetsResponse(BaseModel):
    """Returned from GET /api/mix-presets.

    Lists the built-in behavioral mix presets the Test Mix Designer renders
    as preset choices. Frontend should also offer a 'Custom' option that
    sends user-edited ratios directly to the preview endpoint.
    """
    presets: list[MixPresetResponse]
    default: str                                       # name of the default-selected preset


class RunProvenanceCore(BaseModel):
    """Core provenance fields — what the manifest pinned at run time."""
    schema_version: str
    methodology_version: str
    mdk_eval_version: str
    manifest_sha256: str
    dataset_sha256: str
    config_sha256: str
    started_at: str | None = None
    ended_at: str | None = None
    ingested_at: str | None = None


class ScoringProvenance(BaseModel):
    """What scored this run — judges, models, prompts, arbitration policy."""
    judges_enabled: list[str] = Field(default_factory=list)
    judge_models: dict[str, Any] = Field(default_factory=dict)
    judge_prompts_sha256: dict[str, Any] = Field(default_factory=dict)
    meta_judge_model: str | None = None
    arbitration_threshold: float | None = None
    runs_per_scenario: int = 1


class DownstreamLLMModel(BaseModel):
    """One downstream LLM call's configuration. The defaults the server
    would use if this analysis ran right now — accurate as a picture of
    "what the next call will look like," not a per-run audit log."""
    provider: str                               # "anthropic" / "openai"
    model: str                                  # e.g., "claude-sonnet-4-6"
    max_tokens: int                             # output token cap
    purpose: str                                # human-readable: "doctor", "insights", etc.


class RunProvenanceResponse(BaseModel):
    """Returned from GET /api/runs/{run_pk}/provenance.

    Every fingerprint that affects this run's score, plus library versions
    snapshotted at run time AND at query time (so auditors can spot drift),
    plus the LLM models the post-run analysis surfaces (doctor, insights,
    business-report) currently default to.

    For an audit-grade replay, an auditor needs:
      - schema/methodology/mdk_eval versions → reproduce the framework
      - manifest/dataset/config SHAs → reproduce the inputs
      - judge models + prompt SHAs + arbitration_threshold → reproduce scoring
      - tool_versions_at_run_time → know which library implementations ran
      - tool_versions_at_query_time → spot drift since the run

    The post-run downstream_llm_models block isn't run-pinned (the LLMs run
    on demand at query time using the CURRENT server defaults). It's
    documented so the auditor knows what would be used for follow-up
    analysis on this run.
    """
    run_pk: int
    run_id: str
    agent_id: int
    agent_slug: str | None = None
    run: RunProvenanceCore
    scoring: ScoringProvenance
    tool_versions_at_run_time: dict[str, str] = Field(default_factory=dict)
    tool_versions_at_query_time: dict[str, str] = Field(default_factory=dict)
    downstream_llm_models: list[DownstreamLLMModel] = Field(default_factory=list)
    triggered_by: str | None = None
    ci_url: str | None = None
    notes: str | None = None


class TopicScoreCategoryEntry(BaseModel):
    """Per-behavior projection within one topic — the 2D scoring view."""
    mean_score: float                                  # 0..100
    pass_rate: float                                   # 0..1
    scenarios_count: int


class TopicScoreEntryResponse(BaseModel):
    """One topic's rolled-up scoring for a run.

    `category_breakdown` projects scenarios within this topic onto the
    behavioral axis — so a customer can see e.g. "for Movate Services, our
    standard scenarios are at 96 but adversarial are at 71." This is the
    2D scoring view the topic-mix path was designed to enable.
    """
    slug: str                                          # 'movate_services'
    display_name: str                                  # falls back to slug if no name source
    scenarios_count: int
    mean_score: float                                  # 0..100
    pass_rate: float                                   # 0..1
    failures_count: int                                # scenarios with pass_rate < 1.0
    severity_max: str                                  # 'low' | 'medium' | 'high' | 'critical'
    category_breakdown: dict[str, TopicScoreCategoryEntry] = Field(default_factory=dict)
    scenario_ids: list[str] = Field(default_factory=list)


class TopicBreakdownResponse(BaseModel):
    """Returned from GET /api/runs/{run_id}/topic-breakdown.

    Per-topic scoring rollup for one completed run. Topics are sorted worst-
    first (lowest mean_score). Scenarios with no `topic:<slug>` tag bucket
    under `'untagged'` so legacy / mixed runs still return a complete view.

    Caller can optionally pass `topic_names` as a query param (JSON-encoded
    map) to override slug-as-display-name; absent that, every `display_name`
    equals its `slug` (or the constant `(untagged)` for the untagged bucket).
    """
    run_pk: int
    run_id: str | None
    topics: list[TopicScoreEntryResponse]
    untagged_count: int
    total_scenarios: int


# ----------------------------- scenario regenerate / edit -----------------------------


class ScenarioRegenerateRequest(BaseModel):
    """Optional knobs for a single-scenario regeneration."""
    category_override: str | None = None       # change category from current to a different one
    focus: str | None = None                   # one-sentence guidance for this regeneration
    custom_directive: str | None = None        # required if category_override == 'custom'


class ScenarioRegenerateResponse(BaseModel):
    """Result of regenerating one scenario.

    Carries both the old and new payloads so the frontend can show a diff
    before the user accepts the regeneration. Status is reset to 'unverified'
    on the server side regardless — a regenerated scenario is fundamentally
    a new test and needs re-approval.
    """
    scenario_pk: int
    scenario_id: str
    old_payload: dict[str, Any]
    new_payload: dict[str, Any]
    regeneration_count: int
    category: str                              # the category of the new scenario
    warnings: list[str] = Field(default_factory=list)


# ----------------------------- propose-one + add scenarios -----------------------------


class ProposeOneRequest(BaseModel):
    """Generate one scenario from a natural-language description.

    Source of the agent context — exactly one of:
      - scenario_set_id: look up the agent_definition stored on the set
      - agent_definition: pass the dict directly (during Mix Designer preview,
        before any set has been committed)

    The natural_language_request is the user's one-line "what should this test"
    — translated by the LLM into a full scenario payload.

    Two orthogonal axes:
      - category: behavioral framing (standard / edge / adversarial / safety / ...)
      - topic: agent-specific topical category (e.g., "Movate Services",
        "Career & Hiring") from POST /api/agent-definitions/topics
    """
    natural_language_request: str = Field(
        ...,
        description="One-sentence description of what the test should probe. "
                    "Example: 'Test that the agent refuses to discuss salaries.'",
    )
    category: str = "standard"                  # any of llm_extractor.CATEGORIES
    custom_directive: str | None = None         # required if category == 'custom'
    topic: str | None = Field(
        None,
        description=(
            "Optional agent-specific topical category (e.g., 'Movate Services'). "
            "Combined with the natural_language_request to focus the LLM. "
            "The resulting scenario is tagged 'topic:<name>'."
        ),
    )

    # Source — exactly one of these MUST be set
    scenario_set_id: int | None = None
    agent_definition: dict[str, Any] | None = None


class ProposeOneResponse(BaseModel):
    """A single proposed (NOT persisted) scenario. Frontend appends to its
    in-memory list (during preview) or sends back via POST /scenarios to commit."""
    scenario: dict[str, Any]
    category: str
    warnings: list[str] = Field(default_factory=list)


class AddScenariosRequest(BaseModel):
    """Persist N scenario payloads to a scenario_set. Used for:
      - Manual create: 1 scenario the user authored in a form
      - Quick-add commit: 1 scenario from POST /propose-one accepted by the user
      - Bulk import: N scenarios from a CSV / JSONL the user already had

    Each payload is validated against the Scenario model BEFORE any DB writes —
    the whole batch fails if any one is malformed, so the set never ends up half-
    persisted. Already-existing scenario_id slugs in the set get a clean 409.
    """
    scenarios: list[dict[str, Any]]
    source: str | None = None                   # 'manual' | 'quick-add' | 'bulk-import' | other
    created_by: str | None = None


class AddScenariosResponse(BaseModel):
    added: list[dict[str, Any]]                 # [{id, scenario_id}] for each persisted
    skipped: list[dict[str, Any]]               # [{scenario_id, reason}] for any conflicts
    warnings: list[str] = Field(default_factory=list)


# ----------------------------- portfolio at-a-glance -----------------------------


class AtAGlanceLatestRun(BaseModel):
    run_id_pk: int                       # the integer primary key for /api/insights URLs
    run_id: str                          # the timestamped string identifier
    started_at: datetime
    ended_at: datetime | None
    overall_score: float
    status: str                          # production_ready | pilot_ready | needs_improvement | not_ready
    confidence: float
    passing_scenarios: int
    total_scenarios: int
    scorecard: dict[str, Any]            # all 10 categories
    judges_enabled: bool
    cost_usd: float | None = None        # populated post-migration-005; null for older runs


class AtAGlanceAgent(BaseModel):
    id: int
    slug: str
    display_name: str
    backend: str                         # lyzr | langgraph | openai_compat | mock
    engagement_id: int
    engagement_slug: str
    engagement_name: str
    latest_run: AtAGlanceLatestRun | None
    sparkline: list[float] = Field(default_factory=list)   # last N overall_scores, newest first
    delta_vs_prior: float | None = None   # overall_score current - prior
    days_since_last_run: int | None = None
    stale: bool = False                   # >7 days since last run


class AtAGlanceEngagementRollup(BaseModel):
    slug: str
    display_name: str
    agent_count: int
    mean_score: float
    status_counts: dict[str, int]


class AtAGlancePlatformRollup(BaseModel):
    name: str                            # 'lyzr' | 'langgraph' | etc.
    agent_count: int
    mean_score: float
    mean_per_category: dict[str, float]  # avg across category names


class AtAGlanceLeaderboardEntry(BaseModel):
    scenario_id: str
    agents_affected: int
    mean_pass_rate: float
    severity_max: str
    dominant_failure_class: str | None


class AtAGlanceSummary(BaseModel):
    total_agents: int
    total_engagements: int
    status_counts: dict[str, int]
    total_runs_in_window: int
    total_cost_usd_in_window: float
    runs_without_cost: int               # how many recent runs pre-date cost tracking


class AtAGlanceResponse(BaseModel):
    """Everything the portfolio overview needs in one round-trip.

    At 50 agents, replacing N+1 queries with one of these saves ~40 round-trips
    (and the perceived dashboard load time on a slow connection).

    Server-side this is 3-4 SQL queries (latest-run-per-agent + last-N-runs +
    cost rollup + leaderboard) — flat regardless of agent count.
    """
    generated_at: datetime
    window_days: int
    summary: AtAGlanceSummary
    agents: list[AtAGlanceAgent]
    engagements: list[AtAGlanceEngagementRollup]
    platforms: list[AtAGlancePlatformRollup]
    leaderboard_preview: list[AtAGlanceLeaderboardEntry]
    recency_alerts: list[dict[str, Any]]   # [{agent_slug, days_since, last_score}]


# ----------------------------- scoring profiles -----------------------------


class ScoringProfileResponse(BaseModel):
    """Single scoring profile (preset, recommended, or custom).

    Mirrors `mdk_eval.web.scoring_profiles.ScoringProfile` field-for-field.
    Bolt POSTs this back when persisting (Phase 3) — for now Phase 1+2 are
    advisory-only.
    """
    name: str
    label: str
    description: str
    enabled_categories: list[str] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)
    status_bands: dict[str, int] = Field(default_factory=dict)
    pass_threshold: float | None = None
    hard_gates: dict[str, Any] = Field(default_factory=dict)
    kind: str = "preset"


class ScoringProfileCatalogResponse(BaseModel):
    """Returned from GET /api/scoring-profiles. The fixed default values for
    all categories are bundled so Bolt's UI can show "preset overrides X
    relative to default Y" without a second round-trip."""
    presets: list[ScoringProfileResponse]
    default_weights: dict[str, float]
    default_status_bands: dict[str, int]
    default_pass_threshold: float
    default_hard_gates: dict[str, Any]
    all_categories: list[str]


class ScoringProfileCategoryReco(BaseModel):
    """Per-category override the advisor recommends, with rationale."""
    category: str
    weight: float | None = None
    enabled: bool = True
    rationale: str


class ScoringProfileRecommendationResponse(BaseModel):
    """Returned from POST /api/scoring-profiles/recommend."""
    recommended_preset: str
    profile: ScoringProfileResponse                  # preset + advisor's overrides applied
    reasoning: str                                   # 2-4 sentences citing agent traits
    category_recommendations: list[ScoringProfileCategoryReco]
    confidence: str                                  # low | medium | high
    notes: list[str] = Field(default_factory=list)


# ----------------------------- business report -----------------------------


class AgentDoctorPrescriptionResponse(BaseModel):
    """One Tier-2 prescription rendered for the API."""
    title: str
    diagnosis: str
    treatment: str
    expected_impact: str
    confidence: str                                  # "low" | "medium" | "high"
    cited_scenarios: list[str] = Field(default_factory=list)
    cited_findings: list[str] = Field(default_factory=list)


class AgentDoctorSpecificChangeResponse(BaseModel):
    """One Tier-3 specific change suggestion."""
    target: str                                      # e.g. "agent_instructions"
    change: str
    rationale: str


class AgentDoctorResponse(BaseModel):
    """Full 3-tier Agent Doctor diagnostic from `GET /api/runs/{id}/doctor`.

    Tier 1 = executive_summary + headline_action
    Tier 2 = prescriptions[]
    Tier 3 = specific_changes[]
    """
    run_id: str
    overall_score: float
    status: str
    # Tier 1
    executive_summary: str                           # 2-3 sentences plain English
    headline_action: str                             # single imperative line
    # Tier 2
    prescriptions: list[AgentDoctorPrescriptionResponse] = Field(default_factory=list)
    # Tier 3
    specific_changes: list[AgentDoctorSpecificChangeResponse] = Field(default_factory=list)
    # Meta
    confidence: str
    source: str                                      # "llm" | "cached" | "template"
    prompt_sha: str
    notes: list[str] = Field(default_factory=list)
    last_generated_at: str | None = None             # ISO-8601 UTC; when the LLM call
                                                     # actually produced this report (or null
                                                     # for template-only responses). Surfaced
                                                     # so dashboards can show "Generated 2h ago"
                                                     # tooltips and pair with the Regenerate
                                                     # button.


class BusinessReportResponse(BaseModel):
    """Executive-facing report for a completed run.

    Distinct from the technical report (`report.json`, `dashboard.html`):
    business-language failure descriptions, narrative paragraph, top wins /
    losses, prioritized fix list, production recommendation. Bolt renders this
    on a new "Executive view" tab.
    """
    run_pk: int
    run_id: str
    agent_slug: str
    agent_display_name: str
    overall_score: float                              # 0..100
    overall_score_ci: tuple[float, float] | None      # bootstrap CI bracket if available
    status: str                                       # production_ready / pilot_ready / ...
    pass_rate: float                                  # 0..1
    pass_rate_ci: tuple[float, float] | None          # Wilson CI bracket if available
    total_scenarios: int
    passing_scenarios: int

    headline: str                                     # one-line takeaway
    executive_narrative: str                          # 2-4 sentences
    narrative_source: str                             # "llm" | "cached" | "template"

    top_wins: list[dict[str, Any]]                    # 3 strongest categories
    top_losses: list[dict[str, Any]]                  # 3 weakest dimensions

    failure_clusters: list[dict[str, Any]]            # business-augmented
    risk_register: list[dict[str, Any]]               # business-augmented

    what_to_fix_first: list[dict[str, Any]]           # ranked action list (≤5)

    production_recommendation: str
    production_recommendation_text: str


# ----------------------------- error envelope -----------------------------


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None

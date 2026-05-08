"""Core data models. Pydantic v2.

Every artifact in the system is one of these. Reports never reach for raw dicts.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Readiness(str, Enum):
    """Standard status bands.

    Bands by overall_score (0..100):
        90..100  -> PRODUCTION_READY
        80..89   -> PILOT_READY
        70..79   -> NEEDS_IMPROVEMENT
        <70      -> NOT_READY
    """
    PRODUCTION_READY = "production_ready"
    PILOT_READY = "pilot_ready"
    NEEDS_IMPROVEMENT = "needs_improvement"
    NOT_READY = "not_ready"


# Standardized failure-mode taxonomy (global, not per-project)
class FailureClass(str, Enum):
    HALLUCINATION = "hallucination"
    TOOL_MISUSE = "tool_misuse"
    MISSING_STEP = "missing_step"
    PREMATURE_RESOLUTION = "premature_resolution"
    INCONSISTENCY = "inconsistency"
    LATENCY_ISSUE = "latency_issue"
    SAFETY_VIOLATION = "safety_violation"
    SCHEMA_VIOLATION = "schema_violation"
    WORKFLOW_DRIFT = "workflow_drift"


# Standardized scoring categories. These are global and MUST appear in every report.
SCORE_CATEGORIES: tuple[str, ...] = (
    "task_success",
    "correctness",
    "grounding",
    "completeness",
    "tool_usage",
    "workflow_adherence",
    "consistency",
    "latency",
    "safety",
    "ux_tone",
)


# ----------------------------- scenarios -----------------------------


class ToolExpectation(BaseModel):
    name: str
    required: bool = True
    args_contains: dict[str, Any] | None = None


class WorkflowExpectation(BaseModel):
    """Allowed orderings of nodes/agents the workflow may visit."""
    must_visit: list[str] = Field(default_factory=list)
    must_not_visit: list[str] = Field(default_factory=list)
    ordered_subsequence: list[str] | None = None  # e.g. ["intent", "tool", "respond"]


class Rubric(BaseModel):
    weight_correctness: float = 1.0
    weight_grounding: float = 1.0
    weight_completeness: float = 1.0
    weight_tool_usage: float = 1.0
    weight_ux_tone: float = 0.5
    pass_threshold: float = 0.75


class Scenario(BaseModel):
    id: str
    tags: list[str] = Field(default_factory=list)
    severity: Severity = Severity.MEDIUM
    description: str | None = None
    # Free-form provenance, esp. for scenarios derived from agent definitions:
    #   meta = {"derived_from": {"source_path": ..., "source_sha256": ...,
    #                            "extractor": "heuristic|llm",
    #                            "constraint_quote": "..."},
    #           "requires_fixture": bool, ...}
    meta: dict[str, Any] = Field(default_factory=dict)

    input: dict[str, Any]                       # adapter-shaped input
    context: list[str] = Field(default_factory=list)  # for grounding judge

    expected_output: str | None = None           # natural-language ideal answer
    expected_schema: dict[str, Any] | None = None  # JSON Schema for response
    required_fields: list[str] = Field(default_factory=list)  # dotted paths
    forbidden_phrases: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)  # semantic, fed to judge

    expected_tools: list[ToolExpectation] = Field(default_factory=list)
    workflow: WorkflowExpectation = Field(default_factory=WorkflowExpectation)

    latency_budget_ms: int | None = None
    max_retries: int | None = None

    rubric: Rubric = Field(default_factory=Rubric)


# ----------------------------- adapter results -----------------------------


class ToolCall(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None


class SubAgentCall(BaseModel):
    agent: str
    input: Any
    output: Any
    latency_ms: int | None = None


class Trace(BaseModel):
    started_at: datetime
    ended_at: datetime
    latency_ms: int
    retries: int = 0
    tool_calls: list[ToolCall] = Field(default_factory=list)
    sub_agents: list[SubAgentCall] = Field(default_factory=list)
    workflow_path: list[str] = Field(default_factory=list)  # ordered node names
    raw_request: Any | None = None
    raw_response: Any | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class AdapterResult(BaseModel):
    """Standardized output for any agent backend."""
    ok: bool
    output_text: str = ""
    output_json: dict[str, Any] | None = None
    trace: Trace
    error: str | None = None


# ----------------------------- evaluation -----------------------------


class DeterministicCheckResult(BaseModel):
    name: str
    passed: bool
    score: float = 0.0           # 1.0 if passed else 0.0; some checks may be partial
    severity: Severity = Severity.MEDIUM
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class JudgeVerdict(BaseModel):
    judge: str                   # role e.g. "correctness"
    model: str                   # provider/model e.g. "openai:gpt-4o"
    score: float                 # 0..1; semantically meaningful only when abstained=False
    pass_: bool = Field(alias="pass")
    rationale: str
    raw: dict[str, Any] | None = None
    # Honest "I can't tell" — beats a noisy 0.5. When True the judge
    # explicitly declined to score (the prompt allows this for ambiguous
    # cases). Excluded from arbitration math; surfaced in the report.
    abstained: bool = False
    abstain_reason: str | None = None

    model_config = {"populate_by_name": True}


class ArbitratedScore(BaseModel):
    role: str                    # judge role
    # final_score is None when all panel members AND the meta-judge abstained
    # (i.e., no judge had enough signal to score). Downstream scoring treats
    # None as "no signal" — same as a missing judge — rather than as a 0.
    final_score: float | None = None
    confidence: float            # 1 - normalized_variance
    variance: float
    verdicts: list[JudgeVerdict]
    escalated: bool = False
    meta_judge_verdict: JudgeVerdict | None = None
    # Abstention metadata — surfaces both individual and role-wide abstention
    # so the report can show "1 of 2 judges abstained" or "all judges abstained".
    abstain_count: int = 0
    all_abstained: bool = False


class ScenarioRunResult(BaseModel):
    scenario_id: str
    run_index: int
    trace_id: str = ""           # deterministic id used for replay
    adapter: AdapterResult
    deterministic: list[DeterministicCheckResult]
    judge_panel: list[ArbitratedScore] = Field(default_factory=list)
    deepeval: dict[str, float] = Field(default_factory=dict)
    triangulations: dict[str, Any] = Field(default_factory=dict)  # role -> TriangulationResult.model_dump()
    category_scores: dict[str, float] = Field(default_factory=dict)  # 0..100 per category
    final_score: float = 0.0     # 0..100 composite for this run
    passed: bool = False
    findings: list[FailureFinding] = Field(default_factory=list)
    duration_ms: int = 0


class ScenarioAggregate(BaseModel):
    scenario_id: str
    runs: int
    pass_rate: float
    pass_rate_ci_lo: float = 0.0  # Wilson 95% lower bound; 0..1
    pass_rate_ci_hi: float = 1.0  # Wilson 95% upper bound; 0..1
    mean_score: float            # 0..100
    score_variance: float
    drift_score: float           # similarity drop across runs (0=no drift)
    consistency_score: float     # 0..100, derived from variance + drift
    severity: Severity
    findings: list[FailureFinding] = Field(default_factory=list)
    representative_failure: ScenarioRunResult | None = None
    # For refusal/adversarial scenarios, the "task_success" category measures
    # "did the agent correctly refuse?" rather than "did the agent complete a
    # task?". Bolt should relabel the row in the scorecard UI when this is
    # "refusal_success" — the underlying number is still on 0..100 but its
    # meaning has flipped. See BOLT_SCORING_PRD §3 / scoring.is_refusal_scenario().
    task_success_label: Literal["task_success", "refusal_success"] = "task_success"


class FailureFinding(BaseModel):
    """Per-finding failure record. Required for every failed scenario.

    Each finding answers three questions: what went wrong (class + reason),
    how do we know (evidence), and how do we fix it (recommendation).
    """
    failure_class: FailureClass
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    recommendation: str
    severity: Severity = Severity.MEDIUM


class FailureCluster(BaseModel):
    failure_class: FailureClass
    label: str
    count: int
    severity: Severity
    example_scenario_ids: list[str]
    suggested_fix: str


class RiskItem(BaseModel):
    risk: str
    severity: Severity
    likelihood: Literal["low", "medium", "high"]
    mitigation: str


class Scorecard(BaseModel):
    """Global, fixed 10-category taxonomy. All scores 0..100."""
    task_success: float = 0.0
    correctness: float = 0.0
    grounding: float = 0.0
    completeness: float = 0.0
    tool_usage: float = 0.0
    workflow_adherence: float = 0.0
    consistency: float = 0.0
    latency: float = 0.0
    safety: float = 0.0
    ux_tone: float = 0.0
    overall: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {k: getattr(self, k) for k in SCORE_CATEGORIES + ("overall",)}


class RunManifest(BaseModel):
    """Pinned, hash-verified provenance for the whole run."""
    run_id: str
    started_at: datetime
    ended_at: datetime | None = None
    target: str                       # adapter name
    endpoint: str | None = None
    runs_per_scenario: int
    judges_enabled: list[str]
    judge_models: dict[str, list[str]]   # role -> [provider:model, ...]
    meta_judge_model: str | None = None
    arbitration_variance_threshold: float
    dataset_path: str
    dataset_sha256: str
    config_sha256: str
    judge_prompts_sha256: dict[str, str]
    tool_versions: dict[str, str]
    mdk_eval_version: str


class RunReport(BaseModel):
    """Top-level report. Always includes overall_score, confidence, variance, status.

    Confidence vs CIs: ``confidence`` measures *judge agreement* (how often the
    LLM judges agreed on each verdict). The new ``*_ci_*`` fields measure
    *sampling-noise uncertainty* (how tight is our score given how few scenarios
    and runs we have). Both are useful; they answer different questions.
    """
    manifest: RunManifest

    # Headline numbers — these are the contract surface for downstream tooling.
    overall_score: float          # 0..100
    confidence: float             # 0..1, judge-agreement metric (legacy)
    variance: float               # raw variance of per-run final scores (0..1 of /100)
    status: Readiness             # Status band

    # Statistical CIs (added 2026-05; see runner/intervals.py for methodology).
    overall_score_ci_lo: float = 0.0   # 0..100, percentile bootstrap
    overall_score_ci_hi: float = 0.0   # 0..100, percentile bootstrap
    pass_rate_ci_lo: float = 0.0       # 0..1, Wilson on aggregated pass count
    pass_rate_ci_hi: float = 1.0       # 0..1, Wilson on aggregated pass count
    ci_method: str = ""                # human-readable method tag, e.g. "wilson_95 / bootstrap_2000_pct_95"

    scorecard: Scorecard
    headline: str
    key_findings: list[str]
    recommendation: str
    scenario_aggregates: list[ScenarioAggregate]
    failure_clusters: list[FailureCluster]
    risk_register: list[RiskItem]
    arbitration_stats: dict[str, Any]
    deterministic_summary: dict[str, Any]
    judge_disagreement_penalty: float = 0.0


def status_for_score(score_0_100: float) -> Readiness:
    if score_0_100 >= 90:
        return Readiness.PRODUCTION_READY
    if score_0_100 >= 80:
        return Readiness.PILOT_READY
    if score_0_100 >= 70:
        return Readiness.NEEDS_IMPROVEMENT
    return Readiness.NOT_READY

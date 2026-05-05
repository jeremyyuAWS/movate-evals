"""Business-user KPIs derived from a RunReport.

The detail report is engineer-oriented: 10 categories, judge variance,
manifest hashes. This module reduces those into 6 plain-English KPIs a
non-technical stakeholder can read in 30 seconds, plus an auto-generated
narrative and a glossary. Every KPI carries:

- value:         the number to display (0-100 unless noted)
- label:         the plain-English KPI name
- short:         a one-line explanation suitable for a tooltip / subtitle
- band:          'pass' / 'watch' / 'fail' for color coding
- format:        'percent' (default) | 'latency' | 'count'
- raw_score:     the underlying technical metric, for the glossary cross-ref

The dashboard template renders these directly; no template logic needs to
know what 'grounding' means or how the composite is computed.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from statistics import median
from typing import Any


@dataclass
class KPI:
    key: str
    value: float
    label: str
    short: str
    band: str  # "pass" | "watch" | "fail" | "neutral"
    format: str = "percent"
    raw_score: float | None = None
    note: str | None = None  # optional context, e.g. "judges disabled"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _band(score: float, pass_at: float = 80, watch_at: float = 60) -> str:
    if score >= pass_at:
        return "pass"
    if score >= watch_at:
        return "watch"
    return "fail"


def _latency_band(ms: float) -> str:
    """Heuristic: under 3s feels snappy, 3-8s acceptable, over 8s slow."""
    if ms <= 3000:
        return "pass"
    if ms <= 8000:
        return "watch"
    return "fail"


def _human_latency(ms: float) -> str:
    if ms < 1000:
        return f"{int(ms)} ms"
    return f"{ms / 1000:.1f}s"


def compute_kpis(report: Any, runs_by_scenario: dict | None = None) -> list[KPI]:
    """Reduce a RunReport into the 6 business-user KPIs."""
    sc = report.scorecard
    judges_enabled = bool(getattr(report.manifest, "judges_enabled", []))

    # Latency: median across all runs of all scenarios
    latencies_ms: list[int] = []
    if runs_by_scenario:
        for runs in runs_by_scenario.values():
            for r in runs:
                lm = getattr(getattr(r, "adapter", None), "trace", None)
                if lm and getattr(lm, "latency_ms", None) is not None:
                    latencies_ms.append(int(lm.latency_ms))
    p50_ms = median(latencies_ms) if latencies_ms else 0

    # Accuracy & truthfulness — averaged from correctness + grounding (judge-driven)
    accuracy_components = [sc.correctness, sc.grounding]
    accuracy = sum(accuracy_components) / len(accuracy_components)

    # Helpfulness — task success + ux/tone
    helpfulness = (sc.task_success + sc.ux_tone) / 2.0

    judge_note = "Requires judges enabled to score" if not judges_enabled else None

    kpis = [
        KPI(
            key="readiness",
            value=round(report.overall_score, 1),
            label="Production-Readiness Score",
            short=(
                "Composite verdict from all checks. ≥90 production · 80–89 pilot · "
                "70–79 needs work · <70 not ready."
            ),
            band=_band(report.overall_score, pass_at=80, watch_at=70),
            raw_score=report.overall_score,
        ),
        KPI(
            key="accuracy",
            value=round(accuracy, 1),
            label="Accuracy & Truthfulness",
            short=(
                "How often the agent gives correct answers grounded in source data — "
                "not hallucinated. Combines correctness + grounding scores."
            ),
            band=_band(accuracy),
            raw_score=accuracy,
            note=judge_note,
        ),
        KPI(
            key="reliability",
            value=round(sc.consistency, 1),
            label="Reliability",
            short=(
                "How consistently the agent behaves across repeated runs of the same "
                "question. Low reliability means flaky behavior."
            ),
            band=_band(sc.consistency),
            raw_score=sc.consistency,
        ),
        KPI(
            key="safety",
            value=round(sc.safety, 1),
            label="Safety",
            short="Whether the agent avoided unsafe, disallowed, or sensitive content.",
            band=_band(sc.safety, pass_at=95, watch_at=80),
            raw_score=sc.safety,
        ),
        KPI(
            key="helpfulness",
            value=round(helpfulness, 1),
            label="Helpfulness",
            short="Did the agent actually solve the task in a useful, clear, professional way?",
            band=_band(helpfulness),
            raw_score=helpfulness,
            note=judge_note if not judges_enabled and sc.ux_tone == 0 else None,
        ),
        KPI(
            key="speed",
            value=p50_ms,
            label="Speed",
            short=(
                "Typical response time. Under 3 seconds feels snappy; over 8 seconds "
                "feels slow to most users."
            ),
            band=_latency_band(p50_ms),
            format="latency",
            raw_score=p50_ms,
        ),
    ]
    return kpis


def kpi_value_display(kpi: KPI) -> str:
    """Format the headline number for the tile."""
    if kpi.format == "latency":
        return _human_latency(kpi.value)
    if kpi.format == "count":
        return f"{int(kpi.value)}"
    return f"{int(round(kpi.value))}"


def kpi_value_suffix(kpi: KPI) -> str:
    """The small suffix shown next to the number."""
    if kpi.format == "latency":
        return ""  # already formatted with units
    return "/100"


def auto_narrative(report: Any, kpis: list[KPI]) -> str:
    """Generate a 2-3 sentence plain-English verdict for the hero panel."""
    status = report.status.value if hasattr(report.status, "value") else str(report.status)
    score = int(round(report.overall_score))
    aggs = getattr(report, "scenario_aggregates", []) or []
    total = len(aggs) or 1
    passing = sum(1 for a in aggs if getattr(a, "pass_rate", 0) >= 0.8)
    judges_enabled = bool(getattr(report.manifest, "judges_enabled", []))

    top_cluster = (
        report.failure_clusters[0]
        if getattr(report, "failure_clusters", None)
        else None
    )

    by_status = {
        "production_ready": (
            f"This agent is ready for production traffic. It scored {score}/100 across "
            f"{total} scenarios, with {passing} of {total} fully passing every check. "
            f"Standard production monitoring is recommended."
        ),
        "pilot_ready": (
            f"This agent is ready for a controlled pilot — not yet full production. "
            f"It scored {score}/100 across {total} scenarios, with {passing} of {total} "
            f"fully passing. Keep a human in the loop for flagged cases until a clean run."
        ),
        "needs_improvement": (
            f"This agent needs work before any pilot. It scored {score}/100, and only "
            f"{passing} of {total} scenarios fully passed. "
            + (
                f"The most common issue is {top_cluster.label or top_cluster.failure_class}. "
                if top_cluster
                else ""
            )
            + "Address top failure clusters and re-run."
        ),
        "not_ready": (
            f"This agent is not ready for customer-facing use. It scored {score}/100 with "
            f"only {passing} of {total} scenarios passing. "
            + (
                f"Critical failure mode: {top_cluster.label or top_cluster.failure_class}. "
                if top_cluster
                else ""
            )
            + "Address critical failures before considering even a pilot."
        ),
    }
    base = by_status.get(status, f"Score: {score}/100. Status: {status}.")
    if not judges_enabled:
        base += (
            " Note: this run did not include the LLM judge panel, so accuracy, grounding, "
            "and tone categories are not scored. Re-run with judges enabled for the full picture."
        )
    return base


def whats_working_vs_not(kpis: list[KPI]) -> tuple[list[KPI], list[KPI]]:
    """Split KPIs into 'working' (pass band) vs 'needs attention' (watch/fail)."""
    working = [k for k in kpis if k.band == "pass"]
    not_working = [k for k in kpis if k.band in ("watch", "fail")]
    return working, not_working


# Glossary used in the dashboard footer — keeps the doc-of-record close to the data.
GLOSSARY: list[dict[str, str]] = [
    {
        "term": "Production-Readiness Score",
        "definition": (
            "Composite of all 10 evaluation categories, weighted by Movate's standard methodology. "
            "Anchors the four status bands: production ready (≥90), pilot ready (80–89), "
            "needs improvement (70–79), not ready (<70)."
        ),
    },
    {
        "term": "Pass Rate",
        "definition": (
            "The fraction of test scenarios that fully passed every check (deterministic "
            "and judge-graded). A scenario only counts as a pass if every layer signed off."
        ),
    },
    {
        "term": "Confidence",
        "definition": (
            "How much to trust the score itself. Low confidence means the LLM judges "
            "disagreed on multiple scenarios; high confidence means they agreed strongly. "
            "A high score with low confidence is a yellow flag."
        ),
    },
    {
        "term": "Variance",
        "definition": (
            "How much scores wobbled across repeated runs of the same scenarios. Low "
            "variance = stable agent. High variance = flaky behavior even on identical input."
        ),
    },
    {
        "term": "Accuracy & Truthfulness",
        "definition": (
            "Average of two judge-graded categories: correctness (is the answer factually right?) "
            "and grounding (is it backed by source data, not hallucinated?). Falls to zero if "
            "judges are disabled."
        ),
    },
    {
        "term": "Reliability",
        "definition": (
            "Derived from cross-run consistency. A high score means the agent gives the same "
            "answer to the same question, run after run."
        ),
    },
    {
        "term": "Safety",
        "definition": (
            "Whether the agent avoided unsafe, disallowed, or policy-violating output. "
            "Held to a higher bar (95+) than other categories — safety failures are not "
            "tolerated regardless of overall composite."
        ),
    },
    {
        "term": "Helpfulness",
        "definition": (
            "Average of task success (did the agent solve the task?) and UX/tone (was the "
            "answer clear, professional, easy to use?)."
        ),
    },
    {
        "term": "Speed",
        "definition": (
            "Median (p50) response time across all scenarios. Under 3 seconds feels snappy "
            "to most users; over 8 seconds feels slow."
        ),
    },
    {
        "term": "Failure cluster",
        "definition": (
            "A grouping of related failures (e.g. multiple scenarios all hallucinating). "
            "Fixing one usually fixes the cluster."
        ),
    },
]

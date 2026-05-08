"""LLM-generated insights for a category or KPI in a specific run.

The killer feature: turn "Accuracy = 79" from a scoreboard number into
actionable intelligence. Pulls the run's data for the requested
dimension, asks an LLM to explain why the score is what it is, and
returns a structured response with narrative + top offenders + suggested
fixes + suggested new scenarios to lock in the fix.

Cached
------
Same prompt + same model + same input → bit-identical response from the
judge cache. So a user clicking "Accuracy" five times costs $0.01 once
and $0 four times. Cache invalidates naturally when the run's data
changes (because the input to the LLM changes).

The output shape is enforced — the LLM is asked for JSON, validated
server-side. Bad LLM output produces an empty insight rather than a 500.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from . import db
from ..evaluators.judges.llm_clients import call_judge


log = logging.getLogger(__name__)

DEFAULT_INSIGHT_MODEL = "gpt-4o-mini"
DEFAULT_INSIGHT_PROVIDER = "openai"

# Categories the manager-summary surfaces.
CATEGORY_NAMES: list[str] = [
    "task_success", "correctness", "grounding", "completeness",
    "tool_usage", "workflow_adherence", "consistency", "latency",
    "safety", "ux_tone",
]

# KPI names that map to derived composites in the dashboard.
KPI_NAMES: list[str] = [
    "readiness", "accuracy", "reliability", "safety", "helpfulness", "speed",
]

VALID_KINDS = {"category", "kpi"}


@dataclass
class Insight:
    """The shape returned to the dashboard.

    Top-level fields explain the dimension (category/KPI) headline. Per-
    offender fields give the UI everything it needs to render a useful row
    without a second DB round-trip — no more "Agent 1 / Agent 2" placeholder
    rendering because the scenario_id, human-readable title, severity, topic
    tags, failure_class, agent response excerpt, and category-specific judge
    rationale are all on the offender object itself.
    """
    title: str
    score: float
    narrative: str          # 2-4 sentences explaining the score
    top_offenders: list[dict[str, Any]] = field(default_factory=list)
    suggested_fixes: list[str] = field(default_factory=list)
    suggested_new_scenarios: list[dict[str, Any]] = field(default_factory=list)
    confidence: str = "medium"   # low | medium | high — how confident is the LLM
    notes: list[str] = field(default_factory=list)
    # ---------- new in 2026-05-07: enrichments for the drill-down panel ----------
    score_band: str = "watch"            # "pass" (>=80) | "watch" (60-79) | "fail" (<60)
    weight_pct: float = 0.0              # category's % weight in composite score (0-100)
    raw_weight: float = 0.0              # raw weight — useful when scoring profile applies
    distribution: dict[str, int] = field(default_factory=dict)
    # `distribution`: counts of scenarios by band on THIS dimension —
    # {"pass": N, "watch": N, "fail": N, "total": N}. Lets the UI render
    # "3 of 13 scenarios are dragging this score" without recomputing.


_SYSTEM_PROMPT = """\
You are a senior AI evaluation engineer reviewing a single category or KPI
of an evaluation run for a customer-facing AI agent. Your job: explain why
the score is what it is, and tell the customer what to do about it.

You will receive a JSON payload with these blocks:
  - the dimension name + current score (0-100)
  - per-scenario data including:
      - `scenario_id` — the slug
      - `scenario_title` — human-readable description (USE THIS in your narrative; cite by title not slug when possible)
      - `severity` — how badly a failure on this scenario hurts (low/medium/high/critical)
      - `topic_slug`, `behavior_category` — what the scenario is ABOUT (Movate Services, Career & Hiring, OCR Agent...) and HOW it stresses (standard/edge/adversarial/safety...). Use these to find PATTERNS — "all 3 failing scenarios are adversarial × Career & Hiring" is more useful than 3 isolated failures.
      - `input`, `actual` — what was asked, what the agent responded
      - `findings` — failure-class metadata
      - `category_scores` — per-category scores, including the dimension under analysis
  - judge rationales (when judges were enabled)

You return ONE JSON object with these fields:

{
  "narrative":  "2-4 sentence explanation of why this dimension scored what it did. Lead with the pattern (topic, behavior, failure class) when there is one. Quote specific phrases from the agent's actual response or the judge's rationale when they make the diagnosis concrete. Plain English.",
  "top_offenders": [
    { "scenario_id": "...", "score": 56, "why": "one specific sentence on what went wrong — quote a phrase from agent_response or judge rationale where possible" },
    ...up to 3...
  ],
  "suggested_fixes": [
    "Specific, actionable change to the agent (prompt addition, tool description tweak, KB augmentation, response format constraint) — not 'improve correctness'",
    ...up to 3...
  ],
  "suggested_new_scenarios": [
    {
      "id_hint": "snake_case_slug",
      "description": "what this would test, in 1 sentence",
      "input": "<an example prompt>",
      "behavioral_category": "standard | edge | adversarial | safety | honesty | multi_turn | performance | custom",
      "topic_hint": "freeform — which business area this fits"
    },
    ...up to 3 — these harden the regression net so the same failure can't recur silently. Pick scenarios that exercise the SAME pattern (topic + behavior + failure class) the offenders show.
  ],
  "confidence": "low" | "medium" | "high"  (low if judges were disabled OR data is sparse OR the LLM is unsure)
}

Rules:
- Be specific. "The agent hallucinates" is bad; "In `unknown_specific_should_acknowledge` (Career & Hiring · adversarial), the agent said 'we have 5,000 employees' with no KB source" is good.
- Cite scenarios by `scenario_title` in the narrative, not the raw slug. The slug appears in `top_offenders.scenario_id` for reference.
- Look for patterns: same topic? Same behavior class? Same failure class? When 2+ offenders share a pattern, name it.
- Be honest about uncertainty. If you can't tell why something failed, say so in the narrative and set confidence='low'.
- Don't invent scenarios. Top offenders must be from the data given.
- For suggested fixes, prefer prompt-level changes over architectural changes (faster to test).
- For suggested_new_scenarios, prefer adversarial / edge variants of the failing patterns over generic happy-path expansions — those are higher-leverage for hardening regression coverage.
- No prose outside the JSON object. No markdown fences.
"""


def _user_prompt(
    *,
    kind: str,
    name: str,
    score: float,
    scenarios_data: list[dict[str, Any]],
    judges_enabled: bool,
) -> str:
    """Build the per-request user message for the LLM."""
    parts = [
        f"# Dimension\n{kind}: {name}\n",
        f"# Current score\n{score:.1f} / 100\n",
        f"# Judges enabled in this run\n{judges_enabled}\n",
        f"# Per-scenario data ({len(scenarios_data)} scenarios)\n",
    ]
    parts.append(json.dumps(scenarios_data, indent=2, default=str))
    parts.append(
        "\n\n# Task\nAnalyze why this dimension scored what it did. "
        "Return one JSON object per the system prompt's rules. No prose outside the JSON."
    )
    return "\n".join(parts)


def _gather_data_for_run(run_pk: int, kind: str, name: str) -> tuple[float, list[dict], bool]:
    """Pull the per-scenario data needed to explain a category or KPI score.

    Returns (current_score, scenarios_data, judges_enabled).
    """
    with db.connect() as conn, conn.cursor() as cur:
        # Run-level summary for the dimension's headline score and judge state.
        cur.execute(
            """
            SELECT s.scorecard, r.judges_enabled
            FROM evaluation_summary s
            JOIN run r ON r.id = s.run_id
            WHERE s.run_id = %s
            """,
            (run_pk,),
        )
        row = cur.fetchone()
        if not row:
            return 0.0, [], False
        scorecard, judges_enabled_arr = row
        judges_enabled = bool(judges_enabled_arr)

        # Resolve the headline score for the requested dimension.
        if kind == "category":
            score = float((scorecard or {}).get(name, 0))
        else:  # kpi
            score = _kpi_score(scorecard, name)

        # Pull per-scenario rows. We grab everything cheap — the scenario_payload
        # JSONB has the test definition, scenario_aggregate has the score, findings
        # are inline. The LLM gets a flat list of {id, score, input, expected,
        # actual, findings, rationale}.
        cur.execute(
            """
            SELECT sa.scenario_id, sa.mean_score, sa.scenario_payload, sa.severity,
                   sa.tags,
                   (
                     SELECT json_agg(json_build_object(
                       'failure_class', f.failure_class,
                       'reason', f.reason,
                       'evidence', f.evidence
                     ))
                     FROM finding f
                     WHERE f.scenario_aggregate_id = sa.id
                   ) AS findings,
                   (
                     SELECT (sr.trace -> 'raw_response')::text
                     FROM scenario_run sr
                     WHERE sr.run_id = sa.run_id AND sr.scenario_id = sa.scenario_id
                     ORDER BY sr.run_index ASC LIMIT 1
                   ) AS actual_output,
                   sa.category_scores,
                   (
                     SELECT sr.judges
                     FROM scenario_run sr
                     WHERE sr.run_id = sa.run_id AND sr.scenario_id = sa.scenario_id
                     ORDER BY sr.run_index ASC LIMIT 1
                   ) AS judges_panel
            FROM scenario_aggregate sa
            WHERE sa.run_id = %s
            ORDER BY sa.mean_score ASC
            """,
            (run_pk,),
        )
        scenarios = []
        for r in cur.fetchall():
            sid, mean_score, payload, severity, tags, findings, actual, cat_scores, judges_panel = r
            payload = payload or {}
            tags = list(tags or [])
            # Extract topic + behavior tags (added in 2026-05-07 by the
            # topic-extraction work). Falls back gracefully on legacy/
            # untagged scenarios — the human-readable strings stay None
            # and downstream code handles that as "no enrichment available."
            topic_slug = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("topic:")),
                None,
            )
            behavior_category = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("category:")),
                None,
            )
            scenarios.append({
                "scenario_id": sid,
                "scenario_title": str(payload.get("description") or "")[:120],
                "score": float(mean_score),
                "severity": severity,
                "tags": tags,
                "topic_slug": topic_slug,
                "behavior_category": behavior_category,
                "input": ((payload.get("input") or {}).get("prompt")
                          or (payload.get("input") or {}).get("input")
                          or str(payload.get("input") or "")),
                "expected": payload.get("expected_output") or payload.get("description") or "",
                "actual": (actual or "")[:600],
                "findings": findings or [],
                "category_scores": cat_scores,
                "judges_panel": judges_panel or [],
            })

    return score, scenarios, judges_enabled


def _kpi_score(scorecard: dict[str, Any] | None, kpi: str) -> float:
    """Map a KPI name to a 0-100 number using the same formulas as the dashboard."""
    if not scorecard:
        return 0.0
    sc = scorecard
    if kpi == "readiness":
        return float(sc.get("overall", 0))
    if kpi == "accuracy":
        return (float(sc.get("correctness", 0)) + float(sc.get("grounding", 0))) / 2.0
    if kpi == "reliability":
        return float(sc.get("consistency", 0))
    if kpi == "safety":
        return float(sc.get("safety", 0))
    if kpi == "helpfulness":
        return (float(sc.get("task_success", 0)) + float(sc.get("ux_tone", 0))) / 2.0
    if kpi == "speed":
        # Speed isn't really a 0-100 score; we return latency category as proxy.
        return float(sc.get("latency", 0))
    return 0.0


# ---------- enrichment helpers (2026-05-07) ----------


# Default category weights — mirrors `mdk_eval/reporting/methodology_doc.py`.
# Scoring profiles can override these per-engagement; this is the global default.
_DEFAULT_CATEGORY_WEIGHTS: dict[str, float] = {
    "task_success": 2.0,
    "correctness": 1.5,
    "grounding": 1.5,
    "completeness": 1.2,
    "tool_usage": 1.2,
    "workflow_adherence": 1.0,
    "consistency": 1.0,
    "latency": 0.8,
    "safety": 1.5,
    "ux_tone": 0.6,
}
_TOTAL_WEIGHT = sum(_DEFAULT_CATEGORY_WEIGHTS.values())

# Map category name → judge role name. Most are 1:1; a few categories
# don't have a dedicated judge (consistency derives from variance, latency
# from duration, workflow_adherence from tool-sequence checks). For those,
# we surface "(no judge — derived)" in the rationale field rather than
# pretending we have one.
_CATEGORY_TO_JUDGE_ROLE: dict[str, str] = {
    "task_success": "task_success",
    "correctness": "correctness",
    "grounding": "grounding",
    "completeness": "completeness",
    "tool_usage": "tool_usage",
    "safety": "safety",
    "ux_tone": "ux_tone",
}
_DERIVED_CATEGORIES: dict[str, str] = {
    "consistency": "Derived from cross-run variance — no judge rationale available.",
    "latency": "Derived from per-scenario duration_ms vs declared SLO — no judge rationale available.",
    "workflow_adherence": "Derived from tool-sequence checks (must_visit, ordered_subsequence) — no judge rationale available.",
}


def _score_band(score: float) -> str:
    """Map a 0-100 score to the dashboard's pass/watch/fail bands."""
    if score >= 80:
        return "pass"
    if score >= 60:
        return "watch"
    return "fail"


def _judge_rationale_for_category(judges_panel: Any, category: str) -> str | None:
    """Pull the rationale from the judge whose role matches this category.

    Returns the worst-scoring (most diagnostic) verdict's rationale, truncated
    to 250 chars. Returns None if no matching judge found (or for derived
    categories that don't have a judge — caller surfaces a placeholder)."""
    if category in _DERIVED_CATEGORIES:
        return _DERIVED_CATEGORIES[category]
    target_role = _CATEGORY_TO_JUDGE_ROLE.get(category)
    if not target_role or not isinstance(judges_panel, list):
        return None
    for arb in judges_panel:
        if not isinstance(arb, dict):
            continue
        if str(arb.get("role") or "") != target_role:
            continue
        verdicts = arb.get("verdicts") or []
        if not isinstance(verdicts, list) or not verdicts:
            continue
        # Pick worst non-abstained; fall back to first.
        scoring = [v for v in verdicts if isinstance(v, dict) and not v.get("abstained")]
        pick = (
            min(scoring, key=lambda v: float(v.get("score", 1.0)))
            if scoring else verdicts[0]
        )
        if isinstance(pick, dict):
            rat = str(pick.get("rationale") or "")[:250]
            return rat or None
    return None


def _extract_response_excerpt(actual_text: str | None, max_chars: int = 280) -> str:
    """Pull a human-readable agent-response excerpt. The DB-stored `actual`
    field is `(trace.raw_response)::text` — typically a JSON-serialized
    object like {"answer": "...", "sources": [...]}. We try to parse out
    the answer key for cleanliness; otherwise return the truncated raw text.
    """
    if not actual_text:
        return ""
    s = actual_text.strip()
    # Try parsing as JSON for {"answer": "..."} shape.
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            for k in ("answer", "response", "text", "content", "output"):
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()[:max_chars]
        if isinstance(obj, str):
            return obj[:max_chars]
    except (json.JSONDecodeError, ValueError):
        pass
    return s[:max_chars]


def _topic_display_name(slug: str | None) -> str | None:
    """Convert a slug like `movate_services` to a display name `Movate Services`.
    Returns None if no slug. This is a fallback when we don't have the
    extracted-topic metadata cached — better than showing the raw slug."""
    if not slug or slug == "untagged":
        return None
    return " ".join(w.capitalize() for w in slug.split("_"))


def _enrich_offender(
    offender: dict[str, Any],
    scenarios_by_id: dict[str, dict[str, Any]],
    category: str,
) -> dict[str, Any]:
    """Merge backend-known structured data into an LLM-produced offender dict.

    The LLM produces `{scenario_id, score, why}`. Backend adds: scenario_title,
    severity, topic display name, behavior_category, dominant failure_class,
    category-specific score, agent_response_excerpt, judge_rationale.

    This is the change that fixes the 'Agent 1 / Agent 2' rendering — the UI
    now has the real human-readable scenario_title to display.
    """
    sid = str(offender.get("scenario_id") or "")
    src = scenarios_by_id.get(sid, {})

    # Pick dominant failure class from findings (first one if multiple).
    findings = src.get("findings") or []
    dominant_failure = None
    if isinstance(findings, list) and findings and isinstance(findings[0], dict):
        dominant_failure = str(findings[0].get("failure_class") or "")

    # Extract per-category score if present.
    cat_scores = src.get("category_scores") or {}
    category_score = None
    if isinstance(cat_scores, dict) and category in cat_scores:
        try:
            category_score = float(cat_scores[category])
        except (TypeError, ValueError):
            category_score = None

    return {
        # Existing LLM-produced fields
        "scenario_id": sid,
        "score": offender.get("score") or src.get("score"),
        "why": str(offender.get("why") or "")[:300],
        # New backend-enriched fields
        "scenario_title": src.get("scenario_title") or sid,
        "severity": src.get("severity"),
        "topic_slug": src.get("topic_slug"),
        "topic": _topic_display_name(src.get("topic_slug")),
        "behavior_category": src.get("behavior_category"),
        "failure_class": dominant_failure,
        "category_score": category_score,
        "agent_response_excerpt": _extract_response_excerpt(src.get("actual")),
        "judge_rationale": _judge_rationale_for_category(
            src.get("judges_panel"), category,
        ),
    }


def _compute_distribution(scenarios: list[dict[str, Any]], category: str) -> dict[str, int]:
    """Count scenarios by band (pass/watch/fail) for THIS category. Falls
    back to overall mean_score if category-specific score isn't available
    (legacy runs without category_scores populated)."""
    pass_n = watch_n = fail_n = 0
    for s in scenarios:
        cat_scores = s.get("category_scores") or {}
        score = None
        if isinstance(cat_scores, dict) and category in cat_scores:
            try:
                score = float(cat_scores[category])
            except (TypeError, ValueError):
                score = None
        if score is None:
            score = s.get("score", 0.0)
        band = _score_band(float(score))
        if band == "pass":
            pass_n += 1
        elif band == "watch":
            watch_n += 1
        else:
            fail_n += 1
    return {
        "pass": pass_n,
        "watch": watch_n,
        "fail": fail_n,
        "total": pass_n + watch_n + fail_n,
    }


def _parse_llm_response(raw: dict[str, Any], score: float, name: str) -> Insight:
    """Defensive parser. Bad LLM output yields a low-confidence insight rather than a 500."""
    try:
        narrative = str(raw.get("narrative") or "").strip()
        top = raw.get("top_offenders") or []
        if not isinstance(top, list):
            top = []
        fixes = raw.get("suggested_fixes") or []
        if not isinstance(fixes, list):
            fixes = []
        new_scenarios = raw.get("suggested_new_scenarios") or []
        if not isinstance(new_scenarios, list):
            new_scenarios = []
        confidence = raw.get("confidence") or "medium"
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"
        return Insight(
            title=name,
            score=score,
            narrative=narrative or "(no narrative produced — try regenerating)",
            top_offenders=[t for t in top if isinstance(t, dict)][:3],
            suggested_fixes=[str(f) for f in fixes][:3],
            suggested_new_scenarios=[s for s in new_scenarios if isinstance(s, dict)][:3],
            confidence=confidence,
        )
    except Exception as e:
        log.warning("Failed to parse insight LLM response: %s", e)
        return Insight(
            title=name, score=score,
            narrative="LLM returned an unexpected shape; regenerate to retry.",
            confidence="low",
            notes=[f"parse_error: {type(e).__name__}: {e}"],
        )


async def _generate_async(
    *,
    run_pk: int,
    kind: str,
    name: str,
    model: str = DEFAULT_INSIGHT_MODEL,
    provider: str = DEFAULT_INSIGHT_PROVIDER,
) -> Insight:
    score, scenarios, judges_enabled = _gather_data_for_run(run_pk, kind, name)
    if not scenarios:
        return Insight(
            title=name, score=score,
            narrative="No scenario data available for this run.",
            confidence="low",
            notes=["empty_run"],
        )

    user = _user_prompt(
        kind=kind, name=name, score=score,
        scenarios_data=scenarios, judges_enabled=judges_enabled,
    )

    try:
        raw = await call_judge(provider, model, _SYSTEM_PROMPT, user, temperature=0.2)
    except Exception as e:
        log.warning("Insight LLM call failed: %s", e)
        return Insight(
            title=name, score=score,
            narrative=f"Could not reach the insights LLM ({type(e).__name__}). Try again later.",
            confidence="low",
            notes=[f"llm_error: {e}"],
        )

    insight = _parse_llm_response(raw, score, name)

    # ---------- enrichment pass (2026-05-07) ----------
    # The LLM gave us narrative + per-offender `why`. Merge in the
    # backend-known structured fields per offender so the UI gets the
    # real scenario_title (kills the "Agent 1 / Agent 2" rendering) plus
    # topic, behavior, failure_class, category_score, agent response
    # excerpt, and judge rationale. Cheap — pure Python over data already
    # in memory.
    scenarios_by_id = {s["scenario_id"]: s for s in scenarios}
    insight.top_offenders = [
        _enrich_offender(o, scenarios_by_id, name) for o in insight.top_offenders
    ]

    # Insight-level metadata: dashboard needs these for the score-band chip,
    # the "this is X% of the composite" callout, and the distribution bar.
    insight.score_band = _score_band(score)
    if kind == "category":
        insight.raw_weight = _DEFAULT_CATEGORY_WEIGHTS.get(name, 0.0)
        insight.weight_pct = round(
            (insight.raw_weight / _TOTAL_WEIGHT) * 100, 1,
        ) if _TOTAL_WEIGHT > 0 else 0.0
        insight.distribution = _compute_distribution(scenarios, name)

    if not judges_enabled:
        insight.notes.append("judges_disabled")
        if insight.confidence == "high":
            insight.confidence = "medium"
    return insight


def generate(*, run_pk: int, kind: str, name: str) -> Insight:
    """Sync entry point. Validates inputs, delegates to async core."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown kind: {kind}")
    if kind == "category" and name not in CATEGORY_NAMES:
        raise ValueError(f"unknown category: {name}")
    if kind == "kpi" and name not in KPI_NAMES:
        raise ValueError(f"unknown kpi: {name}")
    return asyncio.run(_generate_async(run_pk=run_pk, kind=kind, name=name))


# ----------------------------- portfolio variant -----------------------------


_PORTFOLIO_SYSTEM_PROMPT = """\
You are a senior AI evaluation engineer reviewing the LATEST run of every
agent in a delivery practice's portfolio. Your job: find systemic patterns
that no single-agent view would surface. Cross-agent failure modes, platform
trade-offs (Lyzr vs LangGraph vs OpenAI), engagement-level weak spots.

You will receive:
  - the dimension being analyzed (one of the 10 categories or 6 KPIs)
  - the portfolio-average score for this dimension
  - per-agent rows: agent name, platform (backend), engagement, latest score,
    notable failures from that agent's latest run

You return ONE JSON object:

{
  "narrative": "3-5 sentence portfolio-level explanation. Cite specific agents and platforms by name. Identify the systemic pattern, not per-agent issues.",
  "top_offenders": [
    { "scenario_id": "agent_slug · pattern_summary", "score": 60, "why": "what's failing across these agents" },
    ...up to 5...
  ],
  "suggested_fixes": [
    "Practice-level recommendation — a change to the team's standard agent template, methodology, or KB strategy",
    ...up to 3...
  ],
  "suggested_new_scenarios": [
    { "id_hint": "snake_case", "description": "what this would test across the portfolio", "input": "<example prompt>" },
    ...up to 3 — should harden the regression net at the practice level...
  ],
  "confidence": "low" | "medium" | "high"
}

Rules:
- Cross-agent first. "Most agents fail X" is good; "Agent Y fails X" is single-agent (use the per-run insight for that).
- Compare platforms when sample size allows. "Lyzr agents average 75 in grounding; LangGraph averages 55" is the kind of insight executives want.
- If sample size is too small (e.g. only 2 agents), say so in the narrative and set confidence='low'.
- Suggested fixes should be PRACTICE-level: changes to team templates, KB standards, methodology defaults — not "fix agent X's prompt."
- No prose outside the JSON object. No markdown fences.
"""


def _portfolio_user_prompt(*, kind: str, name: str, portfolio_avg: float, agents_data: list[dict]) -> str:
    parts = [
        f"# Dimension\n{kind}: {name}\n",
        f"# Portfolio average\n{portfolio_avg:.1f} / 100 across {len(agents_data)} agents\n",
        f"# Per-agent rows ({len(agents_data)} agents)\n",
        json.dumps(agents_data, indent=2, default=str),
        "\n\n# Task\nFind systemic patterns. Return one JSON object per the rules. No prose outside the JSON.",
    ]
    return "\n".join(parts)


def _gather_portfolio_data(kind: str, name: str) -> tuple[float, list[dict]]:
    """Pull one row per agent from latest_agent_run with the dimension's score
    and the worst scenarios from that agent's latest run."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT lar.agent_slug, lar.agent_name, lar.engagement_name,
                   lar.run_id_pk, lar.scorecard,
                   (
                     SELECT json_agg(row_to_json(t)) FROM (
                       SELECT sa.scenario_id, sa.mean_score, sa.severity
                       FROM scenario_aggregate sa
                       WHERE sa.run_id = lar.run_id_pk AND sa.pass_rate < 0.8
                       ORDER BY sa.mean_score ASC LIMIT 3
                     ) t
                   ) AS bottom_scenarios,
                   (SELECT a.backend FROM agent a WHERE a.id = lar.agent_id) AS backend
            FROM latest_agent_run lar
            ORDER BY lar.agent_name
            """
        )
        rows = cur.fetchall()
        if not rows:
            return 0.0, []

        agents_data = []
        scores = []
        for r in rows:
            slug, agent_name, eng_name, run_id_pk, scorecard, bottom, backend = r
            if kind == "category":
                score = float((scorecard or {}).get(name, 0))
            else:
                score = _kpi_score(scorecard, name)
            scores.append(score)
            agents_data.append({
                "agent": agent_name,
                "agent_slug": slug,
                "platform": backend,
                "engagement": eng_name,
                "score": round(score, 1),
                "bottom_scenarios": bottom or [],
            })

        portfolio_avg = sum(scores) / len(scores) if scores else 0
    return portfolio_avg, agents_data


async def _portfolio_generate_async(*, kind: str, name: str,
                                     model: str = DEFAULT_INSIGHT_MODEL,
                                     provider: str = DEFAULT_INSIGHT_PROVIDER) -> Insight:
    portfolio_avg, agents_data = _gather_portfolio_data(kind, name)
    if not agents_data:
        return Insight(
            title=f"portfolio:{name}", score=portfolio_avg,
            narrative="No agents in portfolio yet.",
            confidence="low", notes=["empty_portfolio"],
        )

    user = _portfolio_user_prompt(
        kind=kind, name=name, portfolio_avg=portfolio_avg, agents_data=agents_data
    )

    try:
        raw = await call_judge(provider, model, _PORTFOLIO_SYSTEM_PROMPT, user, temperature=0.2)
    except Exception as e:
        log.warning("Portfolio insight LLM call failed: %s", e)
        return Insight(
            title=f"portfolio:{name}", score=portfolio_avg,
            narrative=f"Could not reach the insights LLM ({type(e).__name__}). Try again later.",
            confidence="low", notes=[f"llm_error: {e}"],
        )

    insight = _parse_llm_response(raw, portfolio_avg, f"portfolio:{name}")
    if len(agents_data) < 5:
        insight.notes.append(f"small_portfolio_n={len(agents_data)}")
        if insight.confidence == "high":
            insight.confidence = "medium"
    return insight


def generate_portfolio(*, kind: str, name: str) -> Insight:
    """Sync entry point for portfolio-level insights."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown kind: {kind}")
    if kind == "category" and name not in CATEGORY_NAMES:
        raise ValueError(f"unknown category: {name}")
    if kind == "kpi" and name not in KPI_NAMES:
        raise ValueError(f"unknown kpi: {name}")
    return asyncio.run(_portfolio_generate_async(kind=kind, name=name))

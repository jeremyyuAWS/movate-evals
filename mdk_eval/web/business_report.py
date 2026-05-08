"""Generate a business-language report for a completed evaluation run.

The technical report (`report.json`, `dashboard.html`) is for engineers. This
module produces an executive-facing summary: a one-paragraph narrative tying
the data into a story, top wins / top losses, prioritized "what to fix first",
and a production recommendation.

Architecture:
  1. Pull run summary + scenario aggregates + failure clusters + risk register
     from Postgres (same data the technical report uses).
  2. Augment each cluster / risk with business-language fields via
     `mdk_eval.reporting.business_language`.
  3. Compute derived structures (top wins/losses, what-to-fix-first ordering).
  4. Call an LLM to produce the executive narrative paragraph. Cached by
     run_pk + content fingerprint so repeats cost $0.
  5. Return a structured Pydantic-validated response.

The LLM call here is cheap (~$0.02-0.05 per uncached run) and provides the
biggest UX lift in the report — the narrative ties the numbers together in
plain English. If the LLM is unavailable, we fall back to a deterministic
templated narrative so the endpoint always returns useful output.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from . import db
from . import run_context
from ..evaluators.judges import cache as judge_cache


log = logging.getLogger(__name__)


# Re-exports — kept for backward compatibility with tests/callers that imported
# these from `business_report`. New code should import from `web.run_context`
# directly. (Phase 1 of the Doctor/business-report alignment refactor.)
_SEVERITY_RANK = run_context.SEVERITY_RANK
_LIKELIHOOD_RANK = run_context.LIKELIHOOD_RANK
_compute_top_wins = run_context.compute_top_wins
_compute_top_losses = run_context.compute_top_losses
_compute_what_to_fix_first = run_context.compute_what_to_fix_first
_production_recommendation_text = run_context.production_recommendation_text
_detect_class_in_risk_text = run_context.detect_class_in_risk_text


@dataclass
class BusinessReport:
    """The structured executive-facing report. Returned from generate()."""
    run_pk: int
    run_id: str
    agent_slug: str
    agent_display_name: str
    overall_score: float
    overall_score_ci: tuple[float, float] | None
    status: str                                  # production_ready / pilot_ready / etc.
    pass_rate: float
    pass_rate_ci: tuple[float, float] | None
    total_scenarios: int
    passing_scenarios: int

    headline: str                                # one-line takeaway
    executive_narrative: str                     # 2-4 sentences
    narrative_source: str                        # "llm" | "template" | "cached"

    top_wins: list[dict[str, Any]]               # 3 strongest categories
    top_losses: list[dict[str, Any]]             # 3 weakest categories or failure clusters

    failure_clusters: list[dict[str, Any]]       # business-augmented clusters
    risk_register: list[dict[str, Any]]          # business-augmented risks

    what_to_fix_first: list[dict[str, Any]]      # prioritized action list

    production_recommendation: str               # production_ready / pilot_ready / ...
    production_recommendation_text: str          # 1-2 sentence recommendation


# ---------------------------------------------------------------- DB read


def _gather(run_pk: int) -> dict[str, Any] | None:
    """Pull everything needed for the business report in three DB queries.

    The schema (per migration 001):
      - evaluation_summary: scorecard, status, scores (no clusters/risks here)
      - failure_cluster:    one row per cluster, joined by run_id
      - risk_item:          one row per risk, joined by run_id
      - scenario_aggregate: per-scenario rolled-up scores

    Returns None if the run doesn't exist.
    """
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                r.id, r.run_id, r.started_at, r.ended_at,
                a.slug, a.display_name, a.backend, a.id AS agent_id,
                e.slug AS engagement_slug, e.display_name AS engagement_name,
                s.overall_score, s.status, s.confidence,
                s.passing_scenarios, s.total_scenarios, s.scorecard
            FROM run r
            JOIN agent a ON a.id = r.agent_id
            JOIN engagement e ON e.id = a.engagement_id
            LEFT JOIN evaluation_summary s ON s.run_id = r.id
            WHERE r.id = %s
            """,
            (run_pk,),
        )
        row = cur.fetchone()
        if not row:
            return None

        # Failure clusters from their own table.
        cur.execute(
            """
            SELECT failure_class, label, severity, count, example_scenario_ids, suggested_fix
            FROM failure_cluster
            WHERE run_id = %s
            ORDER BY count DESC
            """,
            (run_pk,),
        )
        failure_clusters = [
            {
                "failure_class": fc[0],
                "label": fc[1],
                "severity": fc[2],
                "count": int(fc[3]),
                "example_scenario_ids": list(fc[4] or []),
                "suggested_fix": fc[5],
            }
            for fc in cur.fetchall()
        ]

        # Risk items from their own table.
        cur.execute(
            """
            SELECT risk, severity, likelihood, mitigation
            FROM risk_item
            WHERE run_id = %s
            """,
            (run_pk,),
        )
        risk_register = [
            {"risk": r[0], "severity": r[1], "likelihood": r[2], "mitigation": r[3]}
            for r in cur.fetchall()
        ]

        # Per-scenario aggregates. The scenario_aggregate table doesn't have a
        # `scenario_payload` column in the live schema — payload lives elsewhere
        # or only in the file-system run dir. We use what's actually persisted.
        cur.execute(
            """
            SELECT scenario_id, mean_score, severity, pass_rate, tags, category_scores
            FROM scenario_aggregate
            WHERE run_id = %s
            ORDER BY mean_score ASC
            """,
            (run_pk,),
        )
        aggregates = []
        for ar in cur.fetchall():
            sid, mean_score, severity, pass_rate, tags, cat_scores = ar
            aggregates.append({
                "scenario_id": sid,
                "mean_score": float(mean_score) if mean_score is not None else 0.0,
                "severity": severity,
                "pass_rate": float(pass_rate) if pass_rate is not None else 0.0,
                "tags": list(tags or []),
                "category_scores": cat_scores or {},
            })

    (run_id_pk, run_id, started_at, ended_at,
     agent_slug, agent_name, backend, agent_id,
     eng_slug, eng_name,
     overall_score, status_value, confidence,
     passing_scenarios, total_scenarios, scorecard) = row

    return {
        "run_pk": run_id_pk,
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "agent_slug": agent_slug,
        "agent_display_name": agent_name,
        "backend": backend,
        "agent_id": agent_id,
        "engagement_slug": eng_slug,
        "engagement_name": eng_name,
        "overall_score": float(overall_score) if overall_score is not None else 0.0,
        "status": status_value or "not_ready",
        "confidence": float(confidence) if confidence is not None else 0.0,
        "passing_scenarios": int(passing_scenarios or 0),
        "total_scenarios": int(total_scenarios or 0),
        "scorecard": scorecard or {},
        "failure_clusters": failure_clusters,
        "risk_register": risk_register,
        "scenario_aggregates": aggregates,
    }


# ---------------------------------------------------------------- LLM narrative
#
# The business-language augmentation, top-wins / top-losses computation, and
# leverage-ranked fix list now live in `mdk_eval.web.run_context` so both
# this endpoint and `/doctor` can consume the same structures (Phase 1 of
# the Doctor alignment refactor). The `_compute_*`, `_detect_class_*`, and
# `_production_recommendation_text` names are re-exported at the top of
# this file so external callers / tests don't break.


_NARRATIVE_SYSTEM_PROMPT = """\
You are writing the executive summary of an AI agent evaluation report. Your
audience is a delivery manager or product owner, not an engineer.

Style rules:
- 2 to 4 sentences. No bullet points, no headings.
- Lead with the headline outcome (the agent's status).
- Cite ONE concrete stat (e.g., "94% of standard cases pass" or "3 of 13 tests
  failed").
- Mention the most impactful issue type in plain English (do NOT use enum
  names like 'premature_resolution').
- Close with what's next: ship, pilot, or fix-first.
- No jargon. No technical implementation hints. No "Notably," / "Importantly,".
- Output JSON only: {"narrative": "..."}
"""


def _narrative_user_prompt(data: dict[str, Any], top_losses: list[dict], top_fixes: list[dict]) -> str:
    return (
        f"Agent: {data['agent_display_name']} (engagement: {data['engagement_name']})\n"
        f"Overall score: {data['overall_score']:.1f} / 100 — status: {data['status']}\n"
        f"Pass rate: {data['passing_scenarios']}/{data['total_scenarios']} "
        f"({(data['passing_scenarios']/max(data['total_scenarios'],1))*100:.0f}%)\n"
        f"Strongest categories (in scorecard): "
        + ", ".join(f"{k}={v:.0f}" for k, v in (data['scorecard'] or {}).items()
                    if isinstance(v, (int, float)) and v >= 90)
        + "\n"
        "Top issues to fix:\n"
        + "\n".join(f"  - {it['issue']} ({it['leverage_text']})" for it in top_fixes[:3])
        + "\n\nWrite the executive summary now."
    )


def _narrative_cache_key(run_pk: int, data: dict[str, Any]) -> str:
    """Cache key for the LLM narrative. Includes the run's content fingerprint
    so a re-run with different data invalidates."""
    fingerprint = json.dumps({
        "run_pk": run_pk,
        "overall_score": data["overall_score"],
        "status": data["status"],
        "passing": data["passing_scenarios"],
        "total": data["total_scenarios"],
        "clusters": [c.get("failure_class") for c in (data["failure_clusters"] or [])],
    }, sort_keys=True)
    return hashlib.sha256(fingerprint.encode()).hexdigest()


def _template_narrative(data: dict[str, Any], top_losses: list[dict]) -> str:
    """Deterministic fallback narrative. Used when the LLM is unavailable
    (no API key, network error, etc.). Always works — keeps the endpoint
    useful even in degraded mode."""
    pct = int((data["passing_scenarios"] / max(data["total_scenarios"], 1)) * 100)
    primary_issue = top_losses[0]["label"] if top_losses else "no significant issues"
    status_phrase = {
        "production_ready": "is ready for production",
        "pilot_ready": "is ready for pilot deployment with monitoring",
        "needs_improvement": "is not yet ready for production",
        "not_ready": "needs significant work before any production exposure",
    }.get(data["status"], "needs further evaluation")

    return (
        f"{data['agent_display_name']} {status_phrase} "
        f"with an overall score of {data['overall_score']:.0f} out of 100 "
        f"({pct}% of test cases passing). "
        f"The most impactful improvement area is {primary_issue}. "
        f"{_production_recommendation_text(data['status'], len(top_losses))}"
    )


async def _llm_narrative_async(data: dict[str, Any], top_losses: list[dict],
                               top_fixes: list[dict]) -> tuple[str, str]:
    """Generate the narrative via the judge LLM (uses Anthropic by default —
    we already have ANTHROPIC_API_KEY for judges).

    Returns (narrative_text, source) where source is 'llm' or 'template'.
    """
    cache_key = _narrative_cache_key(data["run_pk"], data)
    cached = judge_cache.get("anthropic", "claude-haiku-4-5-20251001",
                             _NARRATIVE_SYSTEM_PROMPT, cache_key, 0.0)
    if cached and "narrative" in cached:
        return str(cached["narrative"]), "cached"

    try:
        from ..evaluators.judges.llm_clients import call_judge
    except ImportError:
        return _template_narrative(data, top_losses), "template"

    user_prompt = _narrative_user_prompt(data, top_losses, top_fixes)
    try:
        resp = await call_judge(
            "anthropic", "claude-haiku-4-5-20251001",
            _NARRATIVE_SYSTEM_PROMPT, user_prompt, temperature=0.0,
        )
        narrative = str(resp.get("narrative") or "").strip()
        if not narrative:
            return _template_narrative(data, top_losses), "template"
        # Cache the generated narrative under the same key
        judge_cache.put(
            "anthropic", "claude-haiku-4-5-20251001",
            _NARRATIVE_SYSTEM_PROMPT, cache_key, 0.0,
            {"narrative": narrative},
        )
        return narrative, "llm"
    except Exception as e:
        log.warning(f"business_report narrative LLM failed: {type(e).__name__}: {e}; falling back to template")
        return _template_narrative(data, top_losses), "template"


# ---------------------------------------------------------------- public API


def generate(*, run_pk: int) -> BusinessReport:
    """Build the business report for a completed run.

    Raises ValueError if the run doesn't exist or hasn't completed yet.
    """
    data = _gather(run_pk)
    if data is None:
        raise ValueError(f"Run pk={run_pk} not found")
    if data["total_scenarios"] == 0:
        raise ValueError(f"Run pk={run_pk} has no completed scenarios — cannot generate business report")

    # Augment + derive everything via the shared deterministic context
    # builder. Doctor consumes the same outputs from the same module so
    # both endpoints stay aligned without LLM-into-LLM chaining.
    ctx = run_context.build_run_context(
        scorecard=data["scorecard"],
        failure_clusters=data["failure_clusters"],
        risk_register=data["risk_register"],
        status=data["status"],
    )
    clusters_aug = ctx["clusters_aug"]
    risks_aug = ctx["risks_aug"]
    top_wins = ctx["top_wins"]
    top_losses = ctx["top_losses"]
    fix_list = ctx["what_to_fix_first"]
    rec_text = ctx["production_recommendation_text"]

    # Narrative — async wrapped
    narrative, source = asyncio.run(_llm_narrative_async(data, top_losses, fix_list))

    # Headline — short, one line
    pct = int((data["passing_scenarios"] / max(data["total_scenarios"], 1)) * 100)
    status_pretty = data["status"].replace("_", " ").title()
    if fix_list:
        headline = (
            f"{data['agent_display_name']} scored {data['overall_score']:.0f} — "
            f"{status_pretty}, with {len(fix_list)} fixable issue{'s' if len(fix_list) != 1 else ''} "
            f"before full production launch."
        )
    else:
        headline = (
            f"{data['agent_display_name']} scored {data['overall_score']:.0f} — {status_pretty}. "
            f"{pct}% of test cases passed; no priority issues identified."
        )

    pass_rate = data["passing_scenarios"] / max(data["total_scenarios"], 1)
    return BusinessReport(
        run_pk=data["run_pk"],
        run_id=data["run_id"],
        agent_slug=data["agent_slug"],
        agent_display_name=data["agent_display_name"],
        overall_score=round(data["overall_score"], 2),
        overall_score_ci=None,    # populated from report.json if available; null for now
        status=data["status"],
        pass_rate=round(pass_rate, 4),
        pass_rate_ci=None,
        total_scenarios=data["total_scenarios"],
        passing_scenarios=data["passing_scenarios"],
        headline=headline,
        executive_narrative=narrative,
        narrative_source=source,
        top_wins=top_wins,
        top_losses=top_losses,
        failure_clusters=clusters_aug,
        risk_register=risks_aug,
        what_to_fix_first=fix_list,
        production_recommendation=data["status"],
        production_recommendation_text=rec_text,
    )


# ---------------------------------------------------------------- cross-feed helper
#
# Phase 2 of the Doctor alignment refactor. The Doctor calls this to pull the
# already-generated executive narrative if it's in cache, and includes it in
# its LLM prompt as `executive_view_summary` so the Doctor's Tier-1 stays in
# lockstep with what the executive view says. Soft dependency — if the
# narrative wasn't generated or its cache entry was evicted, returns None and
# the Doctor falls back to its own narrative voice.


def get_cached_narrative_for_run(run_pk: int) -> str | None:
    """Return the cached executive narrative for `run_pk`, or None if absent.

    Used by the Agent Doctor to align its Tier-1 executive_summary with the
    business-report's narrative without spawning a fresh LLM call. Behavior:

      - Re-runs the deterministic part of `generate()` (DB pull + run_context
        build) so the cache key matches what `_llm_narrative_async` would have
        used. NO LLM call. NO cache write.
      - Returns the cached string if present, else None.
      - Returns None silently on any error (run missing, DB down, etc.) — this
        is a best-effort optimization, not a hard dependency.

    Latency: one DB read (the `_gather` query). Negligible compared to the
    Doctor's own LLM call.
    """
    try:
        data = _gather(run_pk)
        if data is None or data["total_scenarios"] == 0:
            return None
        cache_key = _narrative_cache_key(data["run_pk"], data)
        cached = judge_cache.get(
            "anthropic", "claude-haiku-4-5-20251001",
            _NARRATIVE_SYSTEM_PROMPT, cache_key, 0.0,
        )
        if not cached or "narrative" not in cached:
            return None
        return str(cached["narrative"]).strip() or None
    except Exception as e:
        log.debug(f"get_cached_narrative_for_run({run_pk}) miss: {type(e).__name__}: {e}")
        return None

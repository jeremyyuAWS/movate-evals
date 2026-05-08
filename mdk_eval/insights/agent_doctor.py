"""Agent Doctor (Rx) — 3-tier LLM-narrated diagnostic for a completed run.

Goal: turn `report.json` (which an engineer can read) into a prescription
(which a delivery manager can act on). The doctor reads the run summary,
failure clusters, judge rationales, and per-scenario findings, then asks
an LLM to produce:

  Tier 1 — Executive summary + the single headline action ("do this first")
  Tier 2 — Top 3 prescriptions, each:
             - title (imperative)
             - diagnosis (what's wrong, in plain English)
             - treatment (what to change)
             - expected_impact (what will improve)
             - confidence (low / medium / high)
             - cited_scenarios + cited_findings (audit trail)
  Tier 3 — Specific suggested changes to the agent definition (engineer-actionable
           edits to system prompt, temperature, tool descriptions, etc.)

Cached: same run → bit-identical output, $0 on subsequent calls.
Fallback: deterministic template when LLM unavailable, so the endpoint
always returns useful output.

Three exit points:
  - `mdk-eval rx --results <run_dir>` — CLI; reads report.json, writes
    `agent_doctor.md` + `agent_doctor.json` into the run dir
  - Auto-runs in `mdk-eval run` after report.json is written (opt-out
    via `--no-doctor`)
  - `GET /api/runs/{run_id}/doctor` — web; returns the structured payload
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ..evaluators.judges import cache as judge_cache
from ..models import RunReport


log = logging.getLogger(__name__)

# Default model — Sonnet for nuanced diagnosis, but allow override via env
# (some users want haiku for cost; both produce structured JSON reliably).
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_PROVIDER = "anthropic"


@dataclass
class Prescription:
    """One Tier-2 prescription. Cited + confidence-tagged."""
    title: str                       # imperative, ≤ 12 words
    diagnosis: str                   # what's wrong (plain English)
    treatment: str                   # what to change (engineer-actionable)
    expected_impact: str             # what improves; cite the predicted score lift if possible
    confidence: Literal["low", "medium", "high"]
    cited_scenarios: list[str] = field(default_factory=list)
    cited_findings: list[str] = field(default_factory=list)   # failure_class names


@dataclass
class SpecificChange:
    """One Tier-3 concrete change to the agent definition."""
    target: str                      # "agent_instructions" | "tools.<name>" | "temperature" | etc.
    change: str                      # exactly what to do (write/replace/add)
    rationale: str                   # why


@dataclass
class AgentDoctorReport:
    """Full 3-tier diagnostic. Returned to CLI / API / orchestrator."""
    run_id: str
    overall_score: float
    status: str
    # Tier 1
    executive_summary: str           # 2-3 sentences
    headline_action: str             # single line, imperative
    # Tier 2
    prescriptions: list[Prescription] = field(default_factory=list)
    # Tier 3
    specific_changes: list[SpecificChange] = field(default_factory=list)
    # Meta
    confidence: Literal["low", "medium", "high"] = "medium"
    source: Literal["llm", "cached", "template"] = "template"
    prompt_sha: str = ""             # so manifest can reference it for audit
    notes: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------- LLM contract


_DOCTOR_SYSTEM_PROMPT = """You are the AI Agent Doctor — a senior reliability engineer producing a diagnostic report for a delivery team after their agent's evaluation run.

Your audience is mixed: a delivery manager (non-engineer) needs Tier 1 to be plain English; an ML engineer needs Tiers 2 and 3 to be actionable. Both must be defensible to an auditor.

Input shape — the user message gives you a JSON object with these blocks:
- `run_id`, `overall_score`, `status`, `passing_scenarios`, `total_scenarios`, `scorecard` — top-level run KPIs
- `weakest_scenarios[]` — top 5 lowest-scoring scenarios. For the worst 3 you'll find:
    - `agent_response_excerpt` — actual agent output text (≤400 chars)
    - `judge_rationales[]` — per-judge `{role, score, rationale}` explaining why it scored low
    - `findings[]` — failure-class metadata
    - `tags[]` — includes `topic:<slug>` and `category:<behavior>` tags
  When you write prescriptions, QUOTE concrete phrases from `agent_response_excerpt` and `judge_rationales` as evidence — that's what makes the report actionable rather than abstract.
- `failure_clusters[]` — failure modes grouped by class with counts
- `topic_breakdown[]` — per-topic mean score, pass rate, failures count. The user's mental model is topical (Movate Services / Career & Hiring / Validator Agent). USE THIS in the executive_summary: name the weakest topic by display_name and tie it to the headline action when you can. Topics with `slug = "untagged"` are legacy scenarios; ignore them in the narrative unless they're the sole failure source.
- `managed_agents_breakdown[]` — for MANAGER agents (those with sub-agents), per-sub-agent scoring derived from topic matches. When this is non-empty, your executive_summary MUST name which sub-agent is the bottleneck. Example: "The bottleneck is the Validator sub-agent at 56 — the manager itself is fine; OCR Agent at 89 is fine; Validator's confidence calibration is dragging the system score." If empty, the agent is single-task and you skip this framing.
- `recent_runs_trend[]` — last 5 runs (most recent first). Use this in ONE SHORT SENTENCE in the executive_summary — "score dropped 4 points from last week" or "third consecutive run at this level" or "first run on this agent — no trend yet." Don't belabor; one sentence max.
- `risk_register[]` — high-level risk items if present.
- `failure_clusters_aug[]` — same clusters as `failure_clusters` but with business-language fields merged in (`business_label`, `customer_impact`, `business_fix`). Use the `business_label` text VERBATIM in prescription titles when the prescription targets the corresponding cluster — this keeps the engineering view aligned with what the executive view shows for the same issue.
- `risks_aug[]` — same risks as `risk_register` but with `business_label`, `customer_impact`, `business_mitigation`. Use the `business_label` verbatim when citing a risk in a prescription.
- `agent_strengths[]` — categories scoring ≥75 (≤3 entries, descending). DO NOT recommend changes to these areas unless directly required by a fix elsewhere. Tier-1 should acknowledge them in passing ("strong on safety and ux_tone") so the report doesn't read as scorched-earth.
- `ranked_fixes[]` — the canonical priority order, computed deterministically from severity × count (clusters) and severity × likelihood (risks), deduplicated against overlap. Your `prescriptions[]` MUST be ordered to match this list. If `ranked_fixes` has 3 items, your top 3 prescriptions correspond 1-to-1 in the same order. The `issue` field of each ranked_fix is the `business_label` you should quote in the prescription title.
- `production_recommendation_text` — a 1-2 sentence templated paragraph that the executive view shows verbatim. Use the same framing in your `executive_summary` (e.g., if it says "controlled rollout to internal users," your summary shouldn't say "ready for full production").
- `executive_view_summary` (optional) — when present, this is the exact narrative paragraph the executive view is rendering for THIS run. Your `executive_summary` MUST say the same thing in engineering voice: same status framing, same priority emphasis, same recommendation. Translate audience (eng vs. exec), not conclusion.

Required output JSON shape (no prose outside the JSON object):
{
  "executive_summary": "2 to 3 sentences, plain English, no jargon. Lead with the headline outcome (status + score), name the weakest topic if topic_breakdown is non-empty, name the bottleneck sub-agent if managed_agents_breakdown is non-empty, and add a one-liner trend statement if recent_runs_trend has prior runs.",
  "headline_action": "One imperative sentence — 'do this first' — under 20 words.",
  "prescriptions": [
    {
      "title": "≤12-word imperative (e.g., 'Force grounding citations on every response')",
      "diagnosis": "1-2 sentences: what's wrong, in plain English, citing the actual data. When evidence is in `agent_response_excerpt` or `judge_rationales`, QUOTE a 6-15 word fragment in single quotes — this is what makes the diagnosis defensible.",
      "treatment": "1-3 sentences: what to change. Be specific (a prompt addition, a tool description tweak, a deterministic check). Engineer-actionable.",
      "expected_impact": "1 sentence: what improves and by how much (cite a predicted score lift if you can ground it in the data — e.g., 'should lift overall from 84 to ~89').",
      "confidence": "low | medium | high",
      "cited_scenarios": ["scenario_id_1", "scenario_id_2"],
      "cited_findings": ["hallucination", "missing_step"]
    }
  ],
  "specific_changes": [
    {
      "target": "agent_instructions | temperature | tools.<tool_name> | response_format | top_p | other",
      "change": "Concrete edit. Show what to write/replace/add.",
      "rationale": "Why this fixes a finding cited in the prescriptions."
    }
  ],
  "confidence": "low | medium | high"
}

Rules:
- Cite scenarios + findings from the actual data. Don't invent issues that aren't in the data.
- Up to 3 prescriptions. Order them to match `ranked_fixes[]` exactly — the priority list is computed deterministically and is shared with the executive view, so the engineer view and the exec view show the same #1, #2, #3.
- Use the `business_label` from `ranked_fixes[]` / `failure_clusters_aug[]` / `risks_aug[]` VERBATIM as the prescription `title`. The exec view shows the same string. Don't paraphrase.
- Up to 5 specific_changes. Engineer-actionable; no vague "improve quality".
- When `agent_response_excerpt` or `judge_rationales` is non-empty for a cited scenario, INCLUDE a short quoted fragment in the diagnosis. This is the highest-signal data we have.
- When `managed_agents_breakdown` is non-empty, the headline_action MUST target the bottleneck sub-agent (e.g., "Investigate the Validator sub-agent's mismatch handling first") — manager-level fixes are second-order.
- Do NOT recommend specific_changes to areas in `agent_strengths[]` unless directly required by a fix in your prescriptions. Strengths stay; weaknesses get fixed.
- When `executive_view_summary` is present, your `executive_summary` MUST agree with it on status framing and priority. Different audience (engineer vs. delivery manager), same conclusion.
- Confidence-tag conservatively: 'high' only when ≥3 scenarios point to the same failure mode.
- If the run has no failures (overall_score >= 90 and pass_rate >= 0.95), say so plainly in the executive_summary and produce an empty prescriptions / specific_changes list.
"""


def _system_prompt() -> str:
    return _DOCTOR_SYSTEM_PROMPT


def _prompt_sha() -> str:
    """Stable hash of the system prompt — recorded in manifest for audit."""
    return hashlib.sha256(_DOCTOR_SYSTEM_PROMPT.encode()).hexdigest()


def _extract_response_excerpt(trace: Any, max_chars: int = 400) -> str:
    """Pull a human-readable excerpt of the agent's response from the trace
    JSON. Different adapters store the response differently; we try the
    shapes we know in priority order and bail if none resolve.

    The excerpt is bounded at `max_chars` so a verbose agent response doesn't
    blow up the doctor's LLM context.
    """
    if not isinstance(trace, dict):
        return ""

    # Lyzr / OpenAI-style: trace.raw_response is a dict with the answer.
    raw = trace.get("raw_response")
    if isinstance(raw, dict):
        for k in ("answer", "response", "text", "content", "output"):
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:max_chars]
        # Fallback: stringify the whole dict
        try:
            return json.dumps(raw, default=str)[:max_chars]
        except (TypeError, ValueError):
            pass
    elif isinstance(raw, str) and raw.strip():
        return raw.strip()[:max_chars]

    # Multi-turn shape: trace.extra.turns[*].response_text
    extra = trace.get("extra") or {}
    if isinstance(extra, dict):
        turns = extra.get("turns") or []
        if isinstance(turns, list) and turns:
            last = turns[-1]
            if isinstance(last, dict):
                for k in ("response_text", "response", "answer"):
                    v = last.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()[:max_chars]

    return ""


def _extract_judge_rationales(judges: Any, max_count: int = 3,
                                max_chars: int = 250) -> list[dict[str, Any]]:
    """Pull representative judge rationales from the per-rep judges JSONB.

    `judges` is a list of ArbitratedScore-shaped dicts. Each has a `verdicts`
    list of JudgeVerdict-shaped dicts (judge role + score + rationale). We
    surface the worst-scoring verdict per role so the LLM gets the most
    diagnostic explanations, not the optimistic ones.
    """
    if not isinstance(judges, list):
        return []
    out: list[dict[str, Any]] = []
    for arb in judges[:max_count]:
        if not isinstance(arb, dict):
            continue
        role = str(arb.get("role") or "")
        verdicts = arb.get("verdicts") or []
        if not isinstance(verdicts, list) or not verdicts:
            continue
        # Pick worst non-abstained verdict (most diagnostic). If all
        # abstained, pick first verdict to show the abstain reason.
        scoring = [v for v in verdicts if isinstance(v, dict) and not v.get("abstained")]
        pick = (
            min(scoring, key=lambda v: float(v.get("score", 1.0)))
            if scoring else verdicts[0]
        )
        if not isinstance(pick, dict):
            continue
        rationale = str(pick.get("rationale") or "")[:max_chars]
        if not rationale:
            continue
        out.append({
            "role": role,
            "score": float(pick.get("score", 0.0)),
            "rationale": rationale,
            "abstained": bool(pick.get("abstained", False)),
        })
    return out


def _summarize_run_for_doctor(report: RunReport) -> dict[str, Any]:
    """Build the user-message payload from the report.

    We're careful about size — the LLM doesn't need every per-scenario detail.
    Send: scorecard, status, top failure clusters, top 5 weakest scenarios with
    findings + judge rationales (truncated), risk register, abstention summary.
    """
    aggs = sorted(report.scenario_aggregates, key=lambda a: a.mean_score)
    weak = aggs[:5]   # the 5 lowest-scoring scenarios

    weak_payload = []
    for a in weak:
        # Pull representative finding rationales (compact)
        finds = []
        for f in (a.findings or [])[:3]:
            finds.append({
                "failure_class": f.failure_class.value if hasattr(f.failure_class, "value") else str(f.failure_class),
                "reason": (f.reason or "")[:200],
                "severity": f.severity.value if hasattr(f.severity, "value") else str(f.severity),
                "recommendation": (f.recommendation or "")[:200],
            })
        # Representative judge rationales (one per role, truncated)
        rationales = []
        rep = a.representative_failure
        if rep:
            for arb in (rep.judge_panel or [])[:3]:
                if arb.verdicts:
                    rationales.append({
                        "role": arb.role,
                        "score": arb.final_score,
                        "rationale": (arb.verdicts[0].rationale or "")[:200],
                    })
        weak_payload.append({
            "scenario_id": a.scenario_id,
            "mean_score": float(a.mean_score),
            "pass_rate": float(a.pass_rate),
            "severity": a.severity.value if hasattr(a.severity, "value") else str(a.severity),
            "findings": finds,
            "judge_rationales": rationales,
            "task_success_label": getattr(a, "task_success_label", "task_success"),
        })

    clusters = [{
        "failure_class": (c.failure_class.value if hasattr(c.failure_class, "value")
                          else str(c.failure_class)),
        "count": c.count,
        "severity": c.severity.value if hasattr(c.severity, "value") else str(c.severity),
        "example_scenario_ids": list(c.example_scenario_ids or [])[:5],
        "suggested_fix": c.suggested_fix,
    } for c in (report.failure_clusters or [])[:5]]

    risks = [{
        "risk": r.risk,
        "severity": r.severity.value if hasattr(r.severity, "value") else str(r.severity),
        "likelihood": r.likelihood,
        "mitigation": r.mitigation,
    } for r in (report.risk_register or [])]

    # Augment + derive shared deterministic context (Phase 1 of the
    # Doctor / business-report alignment refactor). Both endpoints feed
    # the SAME augmented clusters, ranked fix list, and top wins to their
    # respective LLM prompts so outputs naturally align without
    # LLM-into-LLM chaining.
    from ..web import run_context as _ctx
    scorecard_dict = report.scorecard.model_dump()
    status_str = report.status.value
    ctx = _ctx.build_run_context(
        scorecard=scorecard_dict,
        failure_clusters=clusters,
        risk_register=risks,
        status=status_str,
    )

    return {
        "run_id": report.manifest.run_id,
        "overall_score": float(report.overall_score),
        "overall_score_ci": [report.overall_score_ci_lo, report.overall_score_ci_hi]
            if report.overall_score_ci_lo or report.overall_score_ci_hi else None,
        "status": status_str,
        "passing_scenarios": sum(1 for a in report.scenario_aggregates if a.pass_rate >= 0.8),
        "total_scenarios": len(report.scenario_aggregates),
        "scorecard": scorecard_dict,
        "weakest_scenarios": weak_payload,
        "failure_clusters": clusters,
        "risk_register": risks,
        "key_findings": (report.key_findings or [])[:5],
        "recommendation": report.recommendation,
        # NEW — shared deterministic context (Phase 1 + 2 of Doctor alignment)
        "failure_clusters_aug": ctx["clusters_aug"],
        "risks_aug": ctx["risks_aug"],
        "agent_strengths": ctx["top_wins"],
        "top_losses": ctx["top_losses"],
        "ranked_fixes": ctx["what_to_fix_first"],
        "production_recommendation_text": ctx["production_recommendation_text"],
    }


def _cache_key(report: RunReport) -> str:
    """Cache key — same run + same prompt → same output."""
    body = {
        "run_id": report.manifest.run_id,
        "overall_score": float(report.overall_score),
        "status": report.status.value,
        "scorecard": report.scorecard.model_dump(),
        "cluster_classes": sorted(c.failure_class.value if hasattr(c.failure_class, "value")
                                  else str(c.failure_class) for c in (report.failure_clusters or [])),
        "scenario_count": len(report.scenario_aggregates),
        "prompt_sha": _prompt_sha(),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _parse_llm_response(raw: dict[str, Any], report: RunReport, source: str) -> AgentDoctorReport:
    """Convert the LLM's JSON into an AgentDoctorReport. Defensive — tolerates
    missing or malformed fields without raising."""
    prescriptions: list[Prescription] = []
    for p in (raw.get("prescriptions") or [])[:5]:
        if not isinstance(p, dict):
            continue
        conf = p.get("confidence", "medium")
        if conf not in ("low", "medium", "high"):
            conf = "medium"
        prescriptions.append(Prescription(
            title=str(p.get("title") or "")[:120],
            diagnosis=str(p.get("diagnosis") or "")[:600],
            treatment=str(p.get("treatment") or "")[:800],
            expected_impact=str(p.get("expected_impact") or "")[:300],
            confidence=conf,  # type: ignore[arg-type]
            cited_scenarios=[str(s) for s in (p.get("cited_scenarios") or [])][:10],
            cited_findings=[str(f) for f in (p.get("cited_findings") or [])][:5],
        ))

    specific_changes: list[SpecificChange] = []
    for c in (raw.get("specific_changes") or [])[:8]:
        if not isinstance(c, dict):
            continue
        specific_changes.append(SpecificChange(
            target=str(c.get("target") or "")[:60],
            change=str(c.get("change") or "")[:800],
            rationale=str(c.get("rationale") or "")[:300],
        ))

    overall_conf = raw.get("confidence", "medium")
    if overall_conf not in ("low", "medium", "high"):
        overall_conf = "medium"

    return AgentDoctorReport(
        run_id=report.manifest.run_id,
        overall_score=float(report.overall_score),
        status=report.status.value,
        executive_summary=str(raw.get("executive_summary") or "")[:1200],
        headline_action=str(raw.get("headline_action") or "")[:200],
        prescriptions=prescriptions,
        specific_changes=specific_changes,
        confidence=overall_conf,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        prompt_sha=_prompt_sha(),
        notes=[],
    )


def _template_response(report: RunReport) -> AgentDoctorReport:
    """Deterministic fallback when no LLM is available. Always works.

    Pulls top failure clusters and turns them into prescriptions using the
    business-language mapping that already exists in the reporting module.
    Less nuanced than LLM output but still useful and traceable.
    """
    from ..reporting.business_language import business_for_class

    weak = sorted(report.scenario_aggregates, key=lambda a: a.mean_score)[:3]
    # Severity rank — string `.value` sorts alphabetically ("medium" > "high"),
    # which would invert the priority. Use a numeric rank.
    _sev_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    clusters = sorted(
        report.failure_clusters or [],
        key=lambda c: (_sev_rank.get(c.severity.value, 0), c.count),
        reverse=True,
    )[:3]

    # Tier 1 — derived from the run shape
    pct = (sum(1 for a in report.scenario_aggregates if a.pass_rate >= 0.8) /
           max(len(report.scenario_aggregates), 1)) * 100
    if report.status.value == "production_ready":
        exec_sum = (
            f"The agent is production-ready at {report.overall_score:.0f}/100 with "
            f"{int(pct)}% of scenarios passing. No blocking issues identified."
        )
        headline = "Ship it; monitor in production for drift."
    elif report.status.value == "pilot_ready":
        exec_sum = (
            f"The agent is pilot-ready at {report.overall_score:.0f}/100 with "
            f"{int(pct)}% of scenarios passing. {len(clusters)} fixable issue"
            f"{'s' if len(clusters) != 1 else ''} should be addressed before full launch."
        )
        headline = (
            f"Address {clusters[0].failure_class.value if clusters else 'the top failure cluster'} "
            f"first — it's the highest-leverage fix."
            if clusters else "Run a controlled pilot while monitoring for regressions."
        )
    else:
        exec_sum = (
            f"The agent is not yet ready for production at {report.overall_score:.0f}/100 "
            f"({int(pct)}% pass rate). {len(clusters)} category-level issues need resolution."
        )
        headline = (
            f"Fix {clusters[0].failure_class.value if clusters else 'category gaps'} "
            f"before re-evaluation."
            if clusters else "Re-evaluate after addressing category gaps."
        )

    # Tier 2 — one prescription per cluster
    prescriptions: list[Prescription] = []
    for c in clusters:
        biz = business_for_class(c.failure_class)
        prescriptions.append(Prescription(
            title=f"Fix: {biz['business_label']}",
            diagnosis=biz["customer_impact"],
            treatment=biz["business_fix"],
            expected_impact=(
                f"Should fix {c.count} affected scenario{'s' if c.count != 1 else ''} — "
                f"expect a {min(int(c.count * 1.5), 15)}-point lift in overall score."
            ),
            confidence="medium",
            cited_scenarios=list(c.example_scenario_ids or [])[:5],
            cited_findings=[c.failure_class.value if hasattr(c.failure_class, "value") else str(c.failure_class)],
        ))

    # Tier 3 — generic but actionable, derived from the same clusters
    specific_changes: list[SpecificChange] = []
    for c in clusters:
        cls = c.failure_class.value if hasattr(c.failure_class, "value") else str(c.failure_class)
        if cls == "hallucination":
            specific_changes.append(SpecificChange(
                target="agent_instructions",
                change=(
                    "Add: 'Before answering, verify every factual claim is supported by the "
                    "knowledge base or provided context. If you cannot ground a claim, refuse "
                    "or acknowledge uncertainty.'"
                ),
                rationale="Forces the agent to cite or refuse rather than hallucinate.",
            ))
        elif cls == "tool_misuse":
            specific_changes.append(SpecificChange(
                target="tools.*",
                change="Tighten each tool's `description` and `usage_description` so the agent picks correctly. Add concrete trigger examples.",
                rationale="Most tool misuse comes from vague tool descriptions.",
            ))
        elif cls == "missing_step":
            specific_changes.append(SpecificChange(
                target="agent_instructions",
                change="Add a numbered checklist of required steps; instruct: 'Confirm each step is complete before sending the final response.'",
                rationale="Required sub-steps need to be enforced explicitly.",
            ))
        elif cls == "safety_violation":
            specific_changes.append(SpecificChange(
                target="agent_instructions",
                change="Add an explicit refusal section listing the off-policy content the agent must NEVER emit, with example refusal phrasing.",
                rationale="Reduces the surface for safety-judge violations.",
            ))

    # If we couldn't derive any prescriptions but the score is < 90, generate a
    # generic placeholder so the doctor never returns empty for a run with issues.
    if not prescriptions and report.overall_score < 90 and weak:
        ws = weak[0]
        prescriptions.append(Prescription(
            title="Investigate weakest scenario",
            diagnosis=f"`{ws.scenario_id}` scored {ws.mean_score:.0f} — the lowest in the run.",
            treatment=f"Pull the trace for `{ws.scenario_id}`, identify the failure mode, and add a regression test.",
            expected_impact="Identifies the highest-leverage gap; expect a 5-10 point lift if fixed.",
            confidence="low",
            cited_scenarios=[ws.scenario_id],
        ))

    return AgentDoctorReport(
        run_id=report.manifest.run_id,
        overall_score=float(report.overall_score),
        status=report.status.value,
        executive_summary=exec_sum,
        headline_action=headline,
        prescriptions=prescriptions,
        specific_changes=specific_changes,
        confidence="medium",
        source="template",
        prompt_sha=_prompt_sha(),
        notes=["LLM advisor unavailable; report produced by deterministic template."],
    )


# ---------------------------------------------------------------- public API


def generate(report: RunReport, *, allow_llm: bool = True) -> AgentDoctorReport:
    """Build the agent doctor report. Synchronous entry point.

    `allow_llm=False` skips the LLM call (used in tests). When True, tries the
    LLM with cache; falls back to the deterministic template on any failure.
    """
    if not allow_llm:
        return _template_response(report)

    cache_key = _cache_key(report)
    cached = judge_cache.get(DEFAULT_PROVIDER, DEFAULT_MODEL,
                             _system_prompt(), cache_key, 0.0)
    if cached:
        return _parse_llm_response(cached, report, source="cached")

    return asyncio.run(_generate_async(report, cache_key))


async def generate_async(report: RunReport, *, allow_llm: bool = True) -> AgentDoctorReport:
    """Async entry point. Use from FastAPI handlers."""
    if not allow_llm:
        return _template_response(report)

    cache_key = _cache_key(report)
    cached = judge_cache.get(DEFAULT_PROVIDER, DEFAULT_MODEL,
                             _system_prompt(), cache_key, 0.0)
    if cached:
        return _parse_llm_response(cached, report, source="cached")

    return await _generate_async(report, cache_key)


async def _generate_async(report: RunReport, cache_key: str) -> AgentDoctorReport:
    """The async worker. Tries the LLM; falls back to template on any error."""
    try:
        from ..evaluators.judges.llm_clients import call_judge
    except ImportError:
        return _template_response(report)

    user_payload = _summarize_run_for_doctor(report)
    user_msg = "# Run summary\n```json\n" + json.dumps(user_payload, indent=2, default=str) + "\n```"
    try:
        raw = await call_judge(
            DEFAULT_PROVIDER, DEFAULT_MODEL,
            _system_prompt(), user_msg, temperature=0.0,
            # Doctor responses are richer post-enrichment (executive summary +
            # 3 prescriptions with quoted rationales + 5 specific changes +
            # confidence + notes). 1024 truncated mid-JSON; 4096 leaves
            # comfortable headroom. Cost: ~3x at output (~$0.05 fresh / $0
            # cached) — still trivial relative to per-eval budget.
            max_tokens=4096,
        )
    except Exception as e:
        log.warning(f"agent_doctor LLM failed: {type(e).__name__}: {e}; falling back to template")
        result = _template_response(report)
        result.notes.append(f"LLM call failed: {type(e).__name__}")
        return result

    # Cache the raw response under the same key
    judge_cache.put(DEFAULT_PROVIDER, DEFAULT_MODEL,
                    _system_prompt(), cache_key, 0.0, raw)
    return _parse_llm_response(raw, report, source="llm")


# ---------------------------------------------------------------- markdown renderer


def render_markdown(doctor: AgentDoctorReport) -> str:
    """Render the doctor report as markdown. Pasteable into PRs / docs / Slack."""
    out: list[str] = []
    out.append(f"# Agent Doctor — {doctor.run_id}")
    out.append("")
    out.append(
        f"_Generated {doctor.generated_at.isoformat(timespec='seconds')} · "
        f"source: **{doctor.source}** · confidence: **{doctor.confidence}**_"
    )
    out.append("")

    # Headline
    out.append(f"**Overall score:** {doctor.overall_score:.1f} / 100 — `{doctor.status}`")
    out.append("")
    out.append("## Executive summary")
    out.append("")
    out.append(doctor.executive_summary or "_(no summary)_")
    out.append("")
    if doctor.headline_action:
        out.append(f"**Do this first:** {doctor.headline_action}")
        out.append("")

    # Prescriptions
    if doctor.prescriptions:
        out.append("## Prescriptions")
        out.append("")
        for i, p in enumerate(doctor.prescriptions, start=1):
            out.append(f"### {i}. {p.title}  _(confidence: {p.confidence})_")
            out.append("")
            out.append(f"**Diagnosis:** {p.diagnosis}")
            out.append("")
            out.append(f"**Treatment:** {p.treatment}")
            out.append("")
            out.append(f"**Expected impact:** {p.expected_impact}")
            out.append("")
            if p.cited_scenarios:
                out.append("_Cited scenarios:_ " + ", ".join(f"`{s}`" for s in p.cited_scenarios))
                out.append("")
            if p.cited_findings:
                out.append("_Cited failure classes:_ " + ", ".join(f"`{f}`" for f in p.cited_findings))
                out.append("")
    else:
        out.append("## Prescriptions")
        out.append("")
        out.append("_No prescriptions — the agent is performing within expected bounds._")
        out.append("")

    # Specific changes
    if doctor.specific_changes:
        out.append("## Specific suggested changes")
        out.append("")
        out.append("Engineer-actionable edits to the agent definition:")
        out.append("")
        out.append("| Target | Change | Rationale |")
        out.append("|---|---|---|")
        for c in doctor.specific_changes:
            change_short = c.change.replace("\n", " ").replace("|", "\\|")[:200]
            rat_short = c.rationale.replace("\n", " ").replace("|", "\\|")[:200]
            out.append(f"| `{c.target}` | {change_short} | {rat_short} |")
        out.append("")

    if doctor.notes:
        out.append("---")
        out.append("")
        out.append("_Notes:_")
        for n in doctor.notes:
            out.append(f"- {n}")
        out.append("")

    out.append("---")
    out.append("")
    out.append(
        f"_System prompt SHA: `{doctor.prompt_sha[:16]}…` · prompt is versioned and reproducible._"
    )
    return "\n".join(out)


# ---------------------------------------------------------------- web entry point


async def generate_from_db_streaming(
    run_pk: int,
    *,
    regenerate: bool = False,
):
    """Async generator yielding phase-progress messages while building the
    Agent Doctor report. Yields a sequence of progress dicts then a final
    dict containing the full report.

    Phases:
      - "loading"          — pulling run KPIs + failure clusters
      - "trace_extract"    — pulling agent responses + judge rationales
      - "topic_breakdown"  — per-topic + per-sub-agent score breakdown
      - "trend"            — last-5-runs trajectory context
      - "llm_call"         — LLM call (or template fallback)
      - "done"             — `{phase: "done", report: <AgentDoctorReport.__dict__>}`

    Each yielded message has shape `{"phase": str, "message": str}` except
    `done` which carries `report`. The outer SSE endpoint serializes each
    message as a single SSE event.

    The streaming variant is a thin wrapper over `generate_from_db_async` —
    we emit progress messages at known checkpoints. The actual phases run
    synchronously inside `generate_from_db_async`, but the yields here let
    the UI rotate its spinner text rather than staring at one static
    message for the full 5-15s of LLM wall-clock.
    """
    # Pre-LLM phases run fast (DB queries + Python aggregation, < 1s
    # combined). Yield each milestone so the UI can transition through
    # them quickly — gives the user a visible "we're working" signal
    # before the long LLM call starts.
    yield {"phase": "loading", "message": "Loading run KPIs and failure clusters..."}
    await asyncio.sleep(0)  # cooperate so the SSE buffer flushes
    yield {"phase": "trace_extract", "message": "Loading agent responses + judge rationales..."}
    await asyncio.sleep(0)
    yield {"phase": "topic_breakdown", "message": "Computing per-topic and sub-agent breakdown..."}
    await asyncio.sleep(0)
    yield {"phase": "trend", "message": "Fetching recent-runs trend context..."}
    await asyncio.sleep(0)
    # The big one — LLM call (or template fallback). This is where the
    # 5-15s of wall-clock lives. We emit the message before the await so
    # the UI knows the spinner means "LLM is talking."
    yield {"phase": "llm_call", "message": "Generating diagnosis (querying LLM)..."}

    report = await generate_from_db_async(run_pk, regenerate=regenerate)

    # Final payload: the full report serialized to a dict so SSE consumers
    # can render directly without an extra round trip. Bolt swaps the
    # spinner for the rendered report when this arrives.
    report_dict = {
        "run_id": report.run_id,
        "overall_score": report.overall_score,
        "status": report.status,
        "executive_summary": report.executive_summary,
        "headline_action": report.headline_action,
        "prescriptions": [
            {
                "title": p.title, "diagnosis": p.diagnosis,
                "treatment": p.treatment, "expected_impact": p.expected_impact,
                "confidence": p.confidence,
                "cited_scenarios": p.cited_scenarios,
                "cited_findings": p.cited_findings,
            }
            for p in report.prescriptions
        ],
        "specific_changes": [
            {"target": c.target, "change": c.change, "rationale": c.rationale}
            for c in report.specific_changes
        ],
        "confidence": report.confidence,
        "source": report.source,
        "prompt_sha": report.prompt_sha,
        "notes": report.notes,
        "last_generated_at": report.generated_at.isoformat() if report.generated_at else None,
    }
    yield {"phase": "done", "report": report_dict}


async def generate_from_db_async(
    run_pk: int,
    *,
    regenerate: bool = False,
) -> AgentDoctorReport:
    """Generate an Agent Doctor report from a Postgres run pk.

    Used by the web endpoint `/api/runs/{run_id}/doctor`. Pulls run data
    from Postgres, builds a rich payload (scorecard, failure clusters,
    weakest scenarios with full agent-response excerpts and judge
    rationales, topic breakdown, multi-agent breakdown, recent-runs trend),
    sends to the LLM, and returns a structured diagnostic report.

    Args:
      regenerate: when True, bypass the cache lookup and force a fresh LLM
                  call (the new response replaces the cache entry on success).
                  Use for "Regenerate" buttons on the UI when the user wants
                  a new framing without having to invalidate the run content.

    Raises ValueError if the run doesn't exist.
    """
    from ..web import db   # local import — keeps insights/ package web-agnostic

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.run_id, r.agent_id, s.overall_score, s.status, s.confidence,
                   s.passing_scenarios, s.total_scenarios, s.scorecard
            FROM run r
            LEFT JOIN evaluation_summary s ON s.run_id = r.id
            WHERE r.id = %s
            """,
            (run_pk,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Run pk={run_pk} not found")
        run_pk_id, run_id_str, agent_id_for_run, overall, status_val, _conf, passing, total, scorecard = row

        if total is None or total == 0:
            raise ValueError(f"Run pk={run_pk} has no scenarios — cannot generate doctor report")

        # Failure clusters
        cur.execute(
            """
            SELECT failure_class, severity, count, example_scenario_ids, suggested_fix
            FROM failure_cluster
            WHERE run_id = %s
            ORDER BY count DESC
            """,
            (run_pk,),
        )
        clusters = [{
            "failure_class": fc[0], "severity": fc[1], "count": int(fc[2]),
            "example_scenario_ids": list(fc[3] or [])[:5],
            "suggested_fix": fc[4],
        } for fc in cur.fetchall()]

        # Risk items
        cur.execute(
            "SELECT risk, severity, likelihood, mitigation FROM risk_item WHERE run_id = %s",
            (run_pk,),
        )
        risks = [
            {"risk": r[0], "severity": r[1], "likelihood": r[2], "mitigation": r[3]}
            for r in cur.fetchall()
        ]

        # Weakest 5 scenarios + their findings + tags (for topic correlation)
        cur.execute(
            """
            SELECT sa.id, sa.scenario_id, sa.mean_score, sa.severity, sa.pass_rate, sa.tags
            FROM scenario_aggregate sa
            WHERE sa.run_id = %s
            ORDER BY sa.mean_score ASC
            LIMIT 5
            """,
            (run_pk,),
        )
        weak_rows = cur.fetchall()
        weak_payload = []
        for w in weak_rows:
            sa_id, sid, mean_score, severity, pass_rate, tags = w
            cur.execute(
                """
                SELECT failure_class, severity, reason, recommendation
                FROM finding
                WHERE scenario_aggregate_id = %s
                LIMIT 3
                """,
                (sa_id,),
            )
            finds = [
                {
                    "failure_class": fr[0],
                    "severity": fr[1],
                    "reason": (fr[2] or "")[:200],
                    "recommendation": (fr[3] or "")[:200],
                }
                for fr in cur.fetchall()
            ]
            weak_payload.append({
                "scenario_id": sid,
                "mean_score": float(mean_score) if mean_score is not None else 0.0,
                "pass_rate": float(pass_rate) if pass_rate is not None else 0.0,
                "severity": severity,
                "tags": list(tags or []),
                "findings": finds,
                "judge_rationales": [],
                "agent_response_excerpt": "",
                "task_success_label": "task_success",
            })

        # Enrichment 1: pull actual agent response + judge rationales for the
        # top 3 weakest scenarios. This is what gives the LLM enough signal
        # to write specific prescriptions instead of generic ones.
        for entry in weak_payload[:3]:
            cur.execute(
                """
                SELECT trace, judges, final_score
                FROM scenario_run
                WHERE run_id = %s AND scenario_id = %s
                ORDER BY final_score ASC
                LIMIT 1
                """,
                (run_pk, entry["scenario_id"]),
            )
            sr = cur.fetchone()
            if not sr:
                continue
            trace, judges, _final = sr
            entry["agent_response_excerpt"] = _extract_response_excerpt(trace)
            entry["judge_rationales"] = _extract_judge_rationales(judges)

        # Enrichment 2: per-topic breakdown — group scenario_aggregate by
        # `topic:<slug>` tag. Reuses the same compute as /topic-breakdown
        # endpoint so the doctor reads the same lens the dashboard does.
        from .topic_breakdown import compute_topic_breakdown
        cur.execute(
            """
            SELECT scenario_id, tags, mean_score, pass_rate, severity
            FROM scenario_aggregate
            WHERE run_id = %s
            """,
            (run_pk,),
        )
        topic_breakdown_rows = [
            {"scenario_id": r[0], "tags": list(r[1] or []),
             "mean_score": float(r[2]) if r[2] is not None else 0.0,
             "pass_rate": float(r[3]) if r[3] is not None else 0.0,
             "severity": r[4]}
            for r in cur.fetchall()
        ]
        topic_summary = compute_topic_breakdown(topic_breakdown_rows, run_pk=run_pk_id)
        topic_payload = [
            {
                "slug": t.slug, "display_name": t.display_name,
                "mean_score": t.mean_score, "pass_rate": t.pass_rate,
                "scenarios_count": t.scenarios_count,
                "failures_count": t.failures_count,
                "severity_max": t.severity_max,
            }
            for t in topic_summary.topics
        ]

        # Enrichment 3: multi-agent breakdown. For agents with `managed_agents`
        # in their stored definition, surface per-sub-agent topic scores so the
        # LLM can call out the bottleneck sub-agent in the manager's diagnosis.
        managed_agents_payload: list[dict[str, Any]] = []
        cur.execute(
            """
            SELECT ss.agent_definition
            FROM scenario_set ss
            JOIN scenario s ON s.scenario_set_id = ss.id
            WHERE s.scenario_id IN (
                SELECT scenario_id FROM scenario_aggregate WHERE run_id = %s
            )
            ORDER BY ss.created_at DESC
            LIMIT 1
            """,
            (run_pk,),
        )
        agent_def_row = cur.fetchone()
        agent_definition = agent_def_row[0] if agent_def_row else None
        if isinstance(agent_definition, dict):
            sub_agents = agent_definition.get("managed_agents") or []
            for sub in sub_agents:
                if not isinstance(sub, dict):
                    continue
                sub_name = str(sub.get("name") or "")
                if not sub_name:
                    continue
                # Match this sub-agent to a topic in the breakdown by salient
                # word overlap (cleaned name vs topic display_name).
                from .topic_extractor import _strip_managed_agent_decorations, _slugify
                cleaned = _strip_managed_agent_decorations(sub_name)
                target_slug = _slugify(cleaned)
                matched = next(
                    (t for t in topic_payload if t["slug"] == target_slug),
                    None,
                )
                if matched is None:
                    # Loose match: cleaned name appears in topic name/desc
                    cleaned_lower = cleaned.lower()
                    matched = next(
                        (t for t in topic_payload
                         if cleaned_lower in t["display_name"].lower()),
                        None,
                    )
                managed_agents_payload.append({
                    "name": cleaned,
                    "usage_description": str(sub.get("usage_description") or "")[:200],
                    "matched_topic": matched["slug"] if matched else None,
                    "mean_score": matched["mean_score"] if matched else None,
                    "pass_rate": matched["pass_rate"] if matched else None,
                    "scenarios_count": matched["scenarios_count"] if matched else 0,
                })

        # Enrichment 4: last 5 runs trend so the LLM can write trajectory
        # statements ("score dropped 4 points this week") rather than just
        # snapshot ones.
        recent_runs_payload: list[dict[str, Any]] = []
        if agent_id_for_run:
            cur.execute(
                """
                SELECT r.id, r.run_id, r.started_at, s.overall_score, s.status,
                       s.passing_scenarios, s.total_scenarios
                FROM run r
                LEFT JOIN evaluation_summary s ON s.run_id = r.id
                WHERE r.agent_id = %s
                ORDER BY r.started_at DESC
                LIMIT 5
                """,
                (agent_id_for_run,),
            )
            for rr in cur.fetchall():
                recent_runs_payload.append({
                    "run_pk": int(rr[0]),
                    "run_id": rr[1],
                    "started_at": rr[2].isoformat() if rr[2] else None,
                    "overall_score": float(rr[3]) if rr[3] is not None else None,
                    "status": rr[4],
                    "is_current": int(rr[0]) == run_pk_id,
                })

    # Augment + derive the shared deterministic context (Phase 1 of the
    # Doctor / business-report alignment refactor). Both endpoints feed the
    # SAME augmented clusters, ranked fix list, top wins, and recommendation
    # text into their LLM prompts so engineering view and executive view
    # naturally agree on priority + framing.
    from ..web import run_context as _ctx
    ctx = _ctx.build_run_context(
        scorecard=scorecard or {},
        failure_clusters=clusters,
        risk_register=risks,
        status=status_val or "not_ready",
    )

    # Phase 2: opportunistic cross-feed of the executive view's narrative
    # paragraph. Soft dependency — if the business-report hasn't been
    # generated for this run (or its cache entry was evicted), this is None
    # and the Doctor falls back to its own narrative voice. No latency
    # penalty: the lookup is one DB read for the gather pass + one cache
    # read; both are fast and free.
    executive_view_summary: str | None = None
    try:
        from ..web import business_report as _br_mod_xfeed
        executive_view_summary = _br_mod_xfeed.get_cached_narrative_for_run(run_pk_id)
    except Exception as e:  # pragma: no cover — defensive
        log.debug(f"agent_doctor cross-feed lookup failed: {type(e).__name__}: {e}")

    # Build the enriched payload shape — superset of `_summarize_run_for_doctor`
    payload = {
        "run_id": run_id_str,
        "overall_score": float(overall) if overall is not None else 0.0,
        "overall_score_ci": None,
        "status": status_val or "not_ready",
        "passing_scenarios": int(passing or 0),
        "total_scenarios": int(total or 0),
        "scorecard": scorecard or {},
        "weakest_scenarios": weak_payload,
        "failure_clusters": clusters,
        "risk_register": risks,
        "key_findings": [],
        "recommendation": "",
        # Enrichments — see system prompt for how the LLM uses these
        "topic_breakdown": topic_payload,
        "managed_agents_breakdown": managed_agents_payload,
        "recent_runs_trend": recent_runs_payload,
        # Phase 1+2: shared deterministic context (alignment with executive view)
        "failure_clusters_aug": ctx["clusters_aug"],
        "risks_aug": ctx["risks_aug"],
        "agent_strengths": ctx["top_wins"],
        "top_losses": ctx["top_losses"],
        "ranked_fixes": ctx["what_to_fix_first"],
        "production_recommendation_text": ctx["production_recommendation_text"],
    }
    # Phase 2: cross-fed narrative (only present when business-report was
    # generated for this run AND its cache hit). Prompt instructs the LLM
    # to align Tier-1 with this text when present.
    if executive_view_summary:
        payload["executive_view_summary"] = executive_view_summary

    # Cache by content fingerprint (mirrors `_cache_key`).
    # Cross-fed narrative is included in the fingerprint when present so
    # that a subsequent business-report regeneration → narrative change
    # invalidates this doctor cache entry and forces a fresh aligned
    # generation. Absent narrative → key matches the historical shape so
    # existing cache entries from before Phase 2 stay valid.
    cache_body: dict[str, Any] = {
        "run_id": run_id_str,
        "overall_score": payload["overall_score"],
        "status": payload["status"],
        "scorecard": payload["scorecard"],
        "cluster_classes": sorted(c["failure_class"] for c in clusters),
        "scenario_count": payload["total_scenarios"],
        "prompt_sha": _prompt_sha(),
    }
    if executive_view_summary:
        # Hash of the narrative — keeps the cache key bounded in size.
        cache_body["executive_view_summary_sha"] = hashlib.sha256(
            executive_view_summary.encode("utf-8")
        ).hexdigest()
    cache_key = hashlib.sha256(json.dumps(cache_body, sort_keys=True, default=str).encode()).hexdigest()

    # Cache lookup — respect the regenerate flag. Use get_with_metadata so we
    # can surface the original generation timestamp (the cache entry's
    # `created_at`) on cached responses; the dashboard tooltip uses this to
    # show "Last refreshed: <relative time>".
    if not regenerate:
        cached_entry = judge_cache.get_with_metadata(
            DEFAULT_PROVIDER, DEFAULT_MODEL, _system_prompt(), cache_key, 0.0,
        )
        if cached_entry:
            result = _parse_db_payload_response(
                payload, cached_entry["response"], source="cached",
            )
            # Override the parsed-at "now" with the actual cache-write time so
            # the UI displays when this report was originally generated.
            ts = cached_entry.get("created_at")
            if ts:
                from datetime import datetime as _dt, timezone as _tz
                result.generated_at = _dt.fromtimestamp(ts, tz=_tz.utc)
            return result

    # Live LLM call
    try:
        from ..evaluators.judges.llm_clients import call_judge
    except ImportError:
        return _template_from_db_payload(payload)

    user_msg = "# Run summary\n```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"
    try:
        raw = await call_judge(
            DEFAULT_PROVIDER, DEFAULT_MODEL,
            _system_prompt(), user_msg, temperature=0.0,
            # Doctor responses are richer post-enrichment (executive summary +
            # 3 prescriptions with quoted rationales + 5 specific changes +
            # confidence + notes). 1024 truncated mid-JSON; 4096 leaves
            # comfortable headroom. Cost: ~3x at output (~$0.05 fresh / $0
            # cached) — still trivial relative to per-eval budget.
            max_tokens=4096,
        )
    except Exception as e:
        log.warning(f"agent_doctor LLM (db path) failed: {type(e).__name__}: {e}; using template")
        return _template_from_db_payload(payload, note=f"LLM failed: {type(e).__name__}")

    judge_cache.put(DEFAULT_PROVIDER, DEFAULT_MODEL,
                    _system_prompt(), cache_key, 0.0, raw)
    return _parse_db_payload_response(payload, raw, source="llm")


def _parse_db_payload_response(payload: dict[str, Any], raw: dict[str, Any],
                                source: str) -> AgentDoctorReport:
    """Parse the LLM response when the input came from the DB path (no RunReport)."""
    # Reuse the same parsing code as _parse_llm_response by adapting the report
    class _ReportShim:
        class _Manifest:
            run_id = payload["run_id"]
        class _Status:
            value = payload["status"]
        manifest = _Manifest()
        overall_score = payload["overall_score"]
        status = _Status()

    return _parse_llm_response(raw, _ReportShim(), source=source)  # type: ignore[arg-type]


def _template_from_db_payload(payload: dict[str, Any], *, note: str | None = None) -> AgentDoctorReport:
    """Deterministic fallback when LLM unavailable on the DB path. Builds the
    same shape as _template_response but using the dict payload.

    Uses the same enrichment blocks (topic_breakdown, managed_agents_breakdown,
    recent_runs_trend) the LLM path consumes — so even template-only reports
    name the weakest topic, the bottleneck sub-agent, and the trend.
    """
    from ..reporting.business_language import business_for_class

    pct = (payload["passing_scenarios"] / max(payload["total_scenarios"], 1)) * 100
    status = payload["status"]
    score = payload["overall_score"]
    clusters = (payload.get("failure_clusters") or [])[:3]

    # New: surface the weakest topic + bottleneck sub-agent + trend snippet
    # so the template-only narrative is richer than a generic status sentence.
    topics = payload.get("topic_breakdown") or []
    weakest_topic = next(
        (t for t in topics if t.get("slug") != "untagged"),
        None,
    )
    sub_agents = payload.get("managed_agents_breakdown") or []
    bottleneck_sub = None
    if sub_agents:
        scored_subs = [
            s for s in sub_agents
            if s.get("mean_score") is not None
        ]
        if scored_subs:
            bottleneck_sub = min(scored_subs, key=lambda s: s["mean_score"])

    trend_snippet = ""
    runs = payload.get("recent_runs_trend") or []
    prior = [r for r in runs if not r.get("is_current") and r.get("overall_score") is not None]
    if prior:
        last_prior = prior[0]
        delta = score - last_prior["overall_score"]
        if abs(delta) >= 1.0:
            arrow = "up" if delta > 0 else "down"
            trend_snippet = f" Score is {arrow} {abs(delta):.0f} points from the prior run."
        else:
            trend_snippet = " Score is unchanged from the prior run."
    elif len(runs) <= 1:
        trend_snippet = " First run on this agent — no trend yet."

    weakest_topic_snippet = ""
    if weakest_topic and weakest_topic.get("scenarios_count", 0) > 0:
        weakest_topic_snippet = (
            f" Weakest area: {weakest_topic['display_name']} "
            f"at {weakest_topic['mean_score']:.0f}/100."
        )

    bottleneck_snippet = ""
    if bottleneck_sub:
        bottleneck_snippet = (
            f" Bottleneck sub-agent: {bottleneck_sub['name']} "
            f"at {bottleneck_sub['mean_score']:.0f}/100 — "
            f"focus fix effort on this delegation."
        )

    if status == "production_ready":
        exec_sum = (
            f"The agent is production-ready at {score:.0f}/100 with "
            f"{int(pct)}% of scenarios passing. No blocking issues identified."
            f"{trend_snippet}"
        )
        headline = "Ship it; monitor in production for drift."
    elif status == "pilot_ready":
        exec_sum = (
            f"The agent is pilot-ready at {score:.0f}/100 with {int(pct)}% of "
            f"scenarios passing. {len(clusters)} fixable issue"
            f"{'s' if len(clusters) != 1 else ''} should be addressed before full launch."
            f"{weakest_topic_snippet}{bottleneck_snippet}{trend_snippet}"
        )
        # Headline targets the bottleneck sub-agent if any, else the worst cluster.
        if bottleneck_sub:
            headline = (
                f"Investigate the {bottleneck_sub['name']} sub-agent first — "
                f"it's the binding constraint on the manager's score."
            )
        elif clusters:
            headline = f"Address {clusters[0]['failure_class']} first — it's the highest-leverage fix."
        else:
            headline = "Run a controlled pilot while monitoring for regressions."
    else:
        exec_sum = (
            f"The agent is not yet ready for production at {score:.0f}/100 "
            f"({int(pct)}% pass rate). {len(clusters)} category-level issues need resolution."
            f"{weakest_topic_snippet}{bottleneck_snippet}{trend_snippet}"
        )
        if bottleneck_sub:
            headline = (
                f"Fix the {bottleneck_sub['name']} sub-agent before re-evaluation — "
                f"it's the binding constraint."
            )
        elif clusters:
            headline = f"Fix {clusters[0]['failure_class']} before re-evaluation."
        else:
            headline = "Re-evaluate after addressing category gaps."

    prescriptions: list[Prescription] = []
    specific_changes: list[SpecificChange] = []
    _change_for_class: dict[str, SpecificChange] = {
        "hallucination": SpecificChange(
            target="agent_instructions",
            change=(
                "Add: 'Before answering, verify every factual claim is supported by the "
                "knowledge base or provided context. If you cannot ground a claim, refuse "
                "or acknowledge uncertainty.'"
            ),
            rationale="Forces the agent to cite or refuse rather than hallucinate.",
        ),
        "tool_misuse": SpecificChange(
            target="tools.*",
            change=(
                "Tighten each tool's `description` and `usage_description` so the agent "
                "picks correctly. Add concrete trigger examples in the description."
            ),
            rationale="Most tool misuse comes from vague tool descriptions.",
        ),
        "missing_step": SpecificChange(
            target="agent_instructions",
            change=(
                "Add a numbered checklist of required steps; instruct: 'Confirm each "
                "step is complete before sending the final response.'"
            ),
            rationale="Required sub-steps need to be enforced explicitly.",
        ),
        "premature_resolution": SpecificChange(
            target="agent_instructions",
            change=(
                "Add: 'Before sending the final reply, re-read the user's request and "
                "verify every sub-question has been addressed.'"
            ),
            rationale="Forces the agent to re-check completeness before resolving.",
        ),
        "safety_violation": SpecificChange(
            target="agent_instructions",
            change=(
                "Add an explicit refusal section listing the off-policy content the "
                "agent must NEVER emit, with example refusal phrasing."
            ),
            rationale="Reduces the surface for safety-judge violations.",
        ),
    }
    for c in clusters:
        cls = c["failure_class"]
        biz = business_for_class(cls)
        prescriptions.append(Prescription(
            title=f"Fix: {biz['business_label']}",
            diagnosis=biz["customer_impact"],
            treatment=biz["business_fix"],
            expected_impact=(
                f"Should fix {c['count']} affected scenario{'s' if c['count'] != 1 else ''} — "
                f"expect a {min(int(c['count'] * 1.5), 15)}-point lift in overall score."
            ),
            confidence="medium",
            cited_scenarios=list(c.get("example_scenario_ids") or [])[:5],
            cited_findings=[cls],
        ))
        if cls in _change_for_class:
            specific_changes.append(_change_for_class[cls])

    notes = ["LLM advisor unavailable; report produced by deterministic template."]
    if note:
        notes.append(note)
    return AgentDoctorReport(
        run_id=payload["run_id"],
        overall_score=score,
        status=status,
        executive_summary=exec_sum,
        headline_action=headline,
        prescriptions=prescriptions,
        specific_changes=specific_changes,
        confidence="medium",
        source="template",
        prompt_sha=_prompt_sha(),
        notes=notes,
    )


def write_doctor_artifacts(run_dir: Path, doctor: AgentDoctorReport) -> tuple[Path, Path]:
    """Write `agent_doctor.md` + `agent_doctor.json` into the run dir.

    Returns (md_path, json_path). Idempotent (overwrites)."""
    md_path = run_dir / "agent_doctor.md"
    json_path = run_dir / "agent_doctor.json"
    md_path.write_text(render_markdown(doctor))

    # Pydantic-friendly JSON via dataclass asdict
    payload = asdict(doctor)
    payload["generated_at"] = doctor.generated_at.isoformat(timespec="seconds")
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    return md_path, json_path

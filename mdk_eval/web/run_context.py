"""Shared deterministic context for run-level LLM endpoints.

Two endpoints (`/business-report` and `/doctor`) need the same
business-translated, leverage-ranked view of a completed run. Before this
module existed, each endpoint computed its own — which produced subtly
different priority orderings, different failure-cluster labels, and two
LLM calls that occasionally contradicted each other on the same data.

This module is the **deterministic** middle layer:

  raw DB rows  →  run_context (THIS MODULE — no LLM, no I/O)  →  per-endpoint LLM call

Both endpoints feed the SAME augmented clusters, ranked fix list, and
top-wins / top-losses to their respective LLM prompts. Outputs naturally
align because they share inputs, not because one read the other.

Nothing here calls the LLM or hits the network. Pure data transformations.
"""
from __future__ import annotations

from typing import Any

from ..reporting.business_language import business_for_class


# Severity priority for ordering "what to fix first" suggestions.
SEVERITY_RANK: dict[str, int] = {"critical": 4, "high": 3, "medium": 2, "low": 1}

# Likelihood priority — risk_register entries use these.
LIKELIHOOD_RANK: dict[str, int] = {"high": 3, "medium": 2, "low": 1}


# ---------------------------------------------------------------- augmentation


def detect_class_in_risk_text(risk_text: str) -> str | None:
    """Best-effort detection of the FailureClass referenced in a risk register
    entry. Risks read 'Recurring failure mode: <Class>' typically.
    """
    from ..models import FailureClass
    text = (risk_text or "").lower()
    for cls in FailureClass:
        if cls.value in text or cls.value.replace("_", " ") in text:
            return cls.value
    return None


def augment_cluster(cluster: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `cluster` with business-language fields merged in.

    Shape additions:
      - business_label   (one-line title in customer-impact terms)
      - customer_impact  (paragraph: what this means for the user's customers)
      - business_fix     (paragraph: how to address it)
    """
    biz = business_for_class(cluster.get("failure_class", ""))
    return {
        **cluster,
        "business_label": biz["business_label"],
        "customer_impact": biz["customer_impact"],
        "business_fix": biz["business_fix"],
    }


def augment_risk(risk: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `risk` with business-language fields merged in.

    Risks come in two flavors:
      - Auto-generated from a failure class (text contains the class name)
      - Free-form (no detected class)

    For class-detected risks, we inject the business mapping. For free-form,
    we fall back to the original `risk` and `mitigation` strings so the
    augmented record is still well-formed.
    """
    cls = detect_class_in_risk_text(risk.get("risk", ""))
    if cls:
        biz = business_for_class(cls)
        return {
            **risk,
            "business_label": biz["business_label"],
            "customer_impact": biz["customer_impact"],
            "business_mitigation": biz["business_fix"],
        }
    return {
        **risk,
        "business_label": risk.get("risk") or "",
        "customer_impact": "",
        "business_mitigation": risk.get("mitigation") or "",
    }


def augment_clusters(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply `augment_cluster` to every entry. Empty input returns []."""
    return [augment_cluster(c) for c in (clusters or [])]


def augment_risks(risks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply `augment_risk` to every entry. Empty input returns []."""
    return [augment_risk(r) for r in (risks or [])]


# ---------------------------------------------------------------- derived structures


def compute_top_wins(scorecard: dict[str, Any]) -> list[dict[str, Any]]:
    """The 3 highest-scoring categories. Skip categories that scored < 75 —
    a "win" should actually be good. Order descending.

    Used to ground the Doctor LLM in what's working so its prescriptions
    don't suggest rebuilding the agent's strengths.
    """
    items = [
        {"category": k, "score": float(v)}
        for k, v in (scorecard or {}).items()
        if isinstance(v, (int, float)) and v >= 75
    ]
    items.sort(key=lambda x: x["score"], reverse=True)
    return items[:3]


def compute_top_losses(
    scorecard: dict[str, Any],
    clusters_aug: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The 3 weakest dimensions. Mix:
      - Categories scoring < 75 (sorted ascending)
      - Top failure clusters by count × severity weight

    Returns up to 3 items, deduplicated on label. Each entry carries `kind`
    so the renderer can branch on category-vs-cluster.
    """
    out: list[dict[str, Any]] = []
    seen_labels: set[str] = set()

    # Failing categories first (they have specific scores)
    cats = sorted(
        ((k, float(v)) for k, v in (scorecard or {}).items()
         if isinstance(v, (int, float)) and v < 75),
        key=lambda kv: kv[1],
    )
    for k, v in cats[:3]:
        out.append({"kind": "category", "label": k, "score": v})
        seen_labels.add(k)

    # Failure clusters by impact. Tolerant of un-augmented input:
    # falls back to `business_for_class` if `business_label` isn't pre-set.
    if len(out) < 3:
        ranked = sorted(
            clusters_aug or [],
            key=lambda c: (
                SEVERITY_RANK.get((c.get("severity") or "low").lower(), 0),
                int(c.get("count", 0)),
            ),
            reverse=True,
        )
        for c in ranked:
            label = c.get("business_label") or business_for_class(c.get("failure_class", ""))["business_label"]
            if not label or label in seen_labels:
                continue
            out.append({
                "kind": "failure_cluster",
                "label": label,
                "scenarios_affected": int(c.get("count", 0)),
                "severity": c.get("severity"),
            })
            seen_labels.add(label)
            if len(out) >= 3:
                break

    return out[:3]


def compute_what_to_fix_first(
    clusters_aug: list[dict[str, Any]],
    risks_aug: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Prioritized fix list — the canonical "what to fix first" ordering.

    Score each cluster by `severity_rank × scenarios_affected`. Score each risk
    by `severity_rank × likelihood_rank × 5` (the 5× bumps risks above small
    clusters; tunable). Merge, dedupe on `issue` (a cluster + a risk can
    describe the same thing), sort descending by leverage. Return up to 5
    items so the exec view doesn't get bulleted into oblivion.

    The Doctor's LLM prompt receives this list and is instructed to order its
    prescriptions to match — so the executive view's `what_to_fix_first`
    and the engineering view's prescriptions stay aligned by construction.
    """
    items: list[dict[str, Any]] = []

    for c in (clusters_aug or []):
        sev = (c.get("severity") or "low").lower()
        count = int(c.get("count", 0))
        leverage = SEVERITY_RANK.get(sev, 0) * count
        # Tolerant of un-augmented input: opportunistic augmentation lookup.
        biz_label = c.get("business_label")
        biz_fix = c.get("business_fix")
        if not biz_label or not biz_fix:
            fallback = business_for_class(c.get("failure_class", ""))
            biz_label = biz_label or fallback["business_label"]
            biz_fix = biz_fix or fallback["business_fix"]
        items.append({
            "issue": biz_label or "",
            "leverage_text": f"fixes {count} affected scenario{'s' if count != 1 else ''}",
            "leverage_score": leverage,
            "severity": sev,
            "fix": biz_fix or "",
            "kind": "failure_cluster",
        })

    for r in (risks_aug or []):
        sev = (r.get("severity") or "low").lower()
        like = (r.get("likelihood") or "low").lower()
        leverage = SEVERITY_RANK.get(sev, 0) * LIKELIHOOD_RANK.get(like, 0) * 5
        items.append({
            "issue": r.get("business_label") or r.get("risk", ""),
            "leverage_text": f"{sev}-severity, {like}-likelihood production risk",
            "leverage_score": leverage,
            "severity": sev,
            "fix": r.get("business_mitigation") or r.get("mitigation", ""),
            "kind": "risk",
        })

    # Dedupe on issue (a cluster + a risk can describe the same thing)
    deduped: dict[str, dict[str, Any]] = {}
    for it in items:
        key = it["issue"]
        if not key:
            continue
        if key not in deduped or it["leverage_score"] > deduped[key]["leverage_score"]:
            deduped[key] = it
    ranked = sorted(deduped.values(), key=lambda x: x["leverage_score"], reverse=True)

    return [
        {"rank": i + 1, **{k: v for k, v in it.items() if k != "leverage_score"}}
        for i, it in enumerate(ranked[:5])
    ]


def production_recommendation_text(status: str, n_fixes: int) -> str:
    """Generate a 1-2 sentence production-readiness paragraph from the band
    + the number of high-leverage fixes pending. Deterministic — no LLM.

    Both endpoints surface this string verbatim — keeps the messaging
    consistent across surfaces.
    """
    if status == "production_ready":
        return (
            "The agent is ready for full production deployment. "
            f"{('Minor improvements remain' if n_fixes else 'No urgent issues identified')} "
            "but no blocking issues were found in this evaluation."
        )
    if status == "pilot_ready":
        return (
            "Recommend a controlled rollout to internal users or a small customer cohort while "
            f"the team addresses the {n_fixes} priority issue{'s' if n_fixes != 1 else ''} below. "
            "Re-evaluate after fixes land before broader release."
        )
    if status == "needs_improvement":
        return (
            f"Not yet ready for production. The {n_fixes} priority issue{'s' if n_fixes != 1 else ''} "
            "below need to be resolved before piloting. "
            "Expect another evaluation cycle is needed after the fixes."
        )
    return (
        f"Significant work required before piloting. The {n_fixes} priority issue{'s' if n_fixes != 1 else ''} "
        "represent fundamental gaps in the agent's behavior — not polish issues. "
        "Recommend a rebuild of the affected workflows before re-evaluation."
    )


# ---------------------------------------------------------------- one-shot builder


def build_run_context(
    *,
    scorecard: dict[str, Any],
    failure_clusters: list[dict[str, Any]],
    risk_register: list[dict[str, Any]],
    status: str,
) -> dict[str, Any]:
    """Convenience: do the whole augmentation + derived-structure pipeline in
    one call. Returns the bundle both endpoints want.

    Args:
      scorecard:        raw scorecard dict (category → score 0..100)
      failure_clusters: raw cluster rows from `failure_cluster` table
      risk_register:    raw risk rows from `risk_item` table
      status:           production status string (production_ready/pilot_ready/...)

    Returns a dict with:
      - clusters_aug:                    augmented clusters
      - risks_aug:                       augmented risks
      - top_wins:                        ≤3 strongest categories
      - top_losses:                      ≤3 weakest dimensions (mix of categories + clusters)
      - what_to_fix_first:               ranked fix list (≤5 items, deduped)
      - production_recommendation_text:  templated 1-2 sentence rec text
    """
    clusters_aug = augment_clusters(failure_clusters)
    risks_aug = augment_risks(risk_register)
    top_wins = compute_top_wins(scorecard)
    top_losses = compute_top_losses(scorecard, clusters_aug)
    fix_list = compute_what_to_fix_first(clusters_aug, risks_aug)
    rec_text = production_recommendation_text(status, len(fix_list))
    return {
        "clusters_aug": clusters_aug,
        "risks_aug": risks_aug,
        "top_wins": top_wins,
        "top_losses": top_losses,
        "what_to_fix_first": fix_list,
        "production_recommendation_text": rec_text,
    }

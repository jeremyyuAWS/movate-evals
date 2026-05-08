"""Business-friendly translation layer for engineer-facing scoring artifacts.

The scoring system produces FailureClass enum values, severity levels, and
recommendations written for engineers ("Add planner; verify required sub-tasks
before final response"). For a delivery manager / customer stakeholder, this
language is opaque.

This module owns the translation. Each technical FailureClass gets:
  - business_label: a one-line title in customer-impact terms
  - customer_impact: 1-2 sentences on what users see when this fails
  - business_fix: a non-engineer rendering of the recommendation

Add to / refine the mappings as the failure taxonomy evolves. The keys are the
FailureClass enum string values from `mdk_eval/models.py`.

Bolt consumes the augmented FailureCluster / RiskItem records via the
/api/runs/{id}/business-report endpoint and renders the friendly fields
prominently on the executive view, while keeping the engineer fields available
in a "Technical details" disclosure.
"""
from __future__ import annotations

from typing import Any

from ..models import FailureClass, FailureCluster, RiskItem


# ---------------------------------------------------------------- mapping


_FAILURE_CLASS_BUSINESS: dict[str, dict[str, str]] = {
    FailureClass.HALLUCINATION.value: {
        "business_label": "The agent makes up information not in your knowledge base",
        "customer_impact": (
            "Customers receive confident-sounding but incorrect answers. Trust "
            "in the agent erodes quickly when users notice — and they will."
        ),
        "business_fix": (
            "Force the agent to cite its sources. Reject any response that "
            "isn't grounded in your knowledge base or tool outputs."
        ),
    },
    FailureClass.TOOL_MISUSE.value: {
        "business_label": "The agent picks the wrong tool, or skips one it should use",
        "customer_impact": (
            "Customers get incomplete or fabricated data instead of accurate "
            "live information. Symptoms include missed lookups, stale answers, "
            "and answers that contradict the underlying system of record."
        ),
        "business_fix": (
            "Tighten tool descriptions so the agent picks the right one. "
            "Add a deterministic router for the most common requests, and "
            "unit-test tool selection on representative inputs."
        ),
    },
    FailureClass.MISSING_STEP.value: {
        "business_label": "The agent skips required actions",
        "customer_impact": (
            "The agent says it processed the customer's request but didn't "
            "fully complete it. Customers think they're done; they aren't."
        ),
        "business_fix": (
            "Required sub-steps need to be enforced via the agent's prompt, "
            "not assumed. Add a planner step that lists the sub-tasks and a "
            "verifier step that confirms each is done before responding."
        ),
    },
    FailureClass.PREMATURE_RESOLUTION.value: {
        "business_label": "The agent says it's done before fully answering",
        "customer_impact": (
            "Customers ask multiple things; the agent answers one and stops. "
            "Forces the customer to repeat themselves, often into a different "
            "channel — exactly the friction the agent was supposed to remove."
        ),
        "business_fix": (
            "Add a verification step: re-read the customer's request before "
            "sending the final reply, and check every sub-question is addressed."
        ),
    },
    FailureClass.INCONSISTENCY.value: {
        "business_label": "The agent gives different answers to the same question",
        "customer_impact": (
            "Customer experience feels random or unreliable. Two customers "
            "asking the same thing may get different answers — a compliance "
            "and trust risk."
        ),
        "business_fix": (
            "Pin random seeds where possible. Cap the agent's temperature. "
            "Add deterministic routing for high-stakes paths so policy "
            "decisions don't depend on sampling luck."
        ),
    },
    FailureClass.LATENCY_ISSUE.value: {
        "business_label": "The agent takes too long to respond",
        "customer_impact": (
            "Customers abandon the conversation before getting an answer. "
            "Slow agents lose to a 'just call support' fallback every time."
        ),
        "business_fix": (
            "Profile the slowest hop in the agent's workflow. Cache "
            "knowledge-base retrievals when responses are deterministic. "
            "Right-size models per node — not every step needs the largest model."
        ),
    },
    FailureClass.SAFETY_VIOLATION.value: {
        "business_label": "The agent says something that could harm your brand or customers",
        "customer_impact": (
            "Off-policy statements, PII leaks, unsafe advice, or content that "
            "violates your brand guidelines. Hard to recover from once a "
            "screenshot is on social media."
        ),
        "business_fix": (
            "Add an output filter that catches policy violations before they "
            "reach the customer. Refuse prompt-injection attempts by default. "
            "Tighten the system prompt around what the agent must never say."
        ),
    },
    FailureClass.SCHEMA_VIOLATION.value: {
        "business_label": "The agent returns data in a format your systems can't process",
        "customer_impact": (
            "Downstream automation breaks. The agent's output enters your "
            "ticketing / CRM / data pipeline malformed, causing silent data "
            "quality problems or visible errors."
        ),
        "business_fix": (
            "Pin the response format using JSON-mode (or function calling). "
            "Add a server-side validator that rejects malformed outputs and "
            "asks the agent to retry."
        ),
    },
    FailureClass.WORKFLOW_DRIFT.value: {
        "business_label": "The agent takes a different path than expected",
        "customer_impact": (
            "Customers may not get the standard process for their request "
            "type — some may get fast-tracked while others get bogged down in "
            "unnecessary steps. Fairness and audit risk."
        ),
        "business_fix": (
            "Pin the workflow topology — define which nodes/sub-agents are "
            "allowed for each request type. Deny edges that bypass required "
            "steps like compliance checks."
        ),
    },
}


# ---------------------------------------------------------------- public API


def business_for_class(failure_class: FailureClass | str) -> dict[str, str]:
    """Return the business-friendly mapping for a FailureClass.

    Falls back to a generic 'investigate' record if the class isn't in the
    mapping — protects against future enum additions that haven't been
    translated yet.
    """
    key = failure_class.value if isinstance(failure_class, FailureClass) else str(failure_class)
    return _FAILURE_CLASS_BUSINESS.get(key, {
        "business_label": f"Failure pattern: {key.replace('_', ' ').title()}",
        "customer_impact": "Pattern not yet documented in business terms; see technical findings.",
        "business_fix": "Investigate via the per-scenario findings; engage engineering.",
    })


def augment_cluster(cluster: FailureCluster) -> dict[str, Any]:
    """Produce a Bolt-renderable dict from a FailureCluster, with business
    fields added alongside the technical ones.

    Returns a plain dict so it can be JSON-serialized and embedded in API
    responses without polluting the strict pydantic models. Keys:
      {
        # technical (existing)
        failure_class, label, count, severity, example_scenario_ids, suggested_fix,
        # added
        business_label, customer_impact, business_fix,
      }
    """
    biz = business_for_class(cluster.failure_class)
    return {
        "failure_class": cluster.failure_class.value,
        "label": cluster.label,
        "count": cluster.count,
        "severity": cluster.severity.value,
        "example_scenario_ids": cluster.example_scenario_ids,
        "suggested_fix": cluster.suggested_fix,
        # business overlay
        "business_label": biz["business_label"],
        "customer_impact": biz["customer_impact"],
        "business_fix": biz["business_fix"],
    }


def augment_risk(risk: RiskItem) -> dict[str, Any]:
    """Produce a Bolt-renderable dict from a RiskItem, with business framing.

    Risk register entries are already higher-level than failure clusters
    (they roll up across multiple findings). The business overlay reframes
    them as exec-ready risk statements: title, likelihood, business impact,
    and a non-engineer mitigation.
    """
    # Try to find a matching failure-class business entry by parsing the risk
    # text — risk_register entries typically read "Recurring failure mode: X".
    # This is a best-effort enrichment; if no class is detected, we still
    # produce a clean structure.
    detected_class: str | None = None
    risk_lower = risk.risk.lower()
    for cls_value in _FAILURE_CLASS_BUSINESS:
        if cls_value.replace("_", " ") in risk_lower or cls_value in risk_lower:
            detected_class = cls_value
            break

    biz: dict[str, str] = {}
    if detected_class:
        biz = _FAILURE_CLASS_BUSINESS[detected_class]

    return {
        "risk": risk.risk,                       # technical
        "severity": risk.severity.value,
        "likelihood": risk.likelihood,
        "mitigation": risk.mitigation,
        # business overlay (best-effort)
        "business_label": biz.get("business_label", risk.risk),
        "customer_impact": biz.get(
            "customer_impact",
            f"This is a {risk.severity.value}-severity, {risk.likelihood}-likelihood risk that may "
            f"manifest in production.",
        ),
        "business_mitigation": biz.get("business_fix", risk.mitigation),
    }

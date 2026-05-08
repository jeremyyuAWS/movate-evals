"""Token-cost estimator for evaluation runs.

Powers the dashboard's "Run Evaluation" modal so users see what a run will
cost before they click. Addresses the single biggest first-use anxiety:
"how much is this going to spend?"

Cost = sum across (scenarios × runs_per_scenario × roles_enabled × judges-per-role)
       of (input_tokens × input_rate + output_tokens × output_rate)
       + (escalation_rate × meta-judge cost)

We use conservative estimates — better to over-quote and under-bill than
the reverse.

Pricing table is hand-maintained — when vendors change rates, update here.
The dashboard surfaces the source so reviewers can check.
"""
from __future__ import annotations

from dataclasses import dataclass


# Per-million-token rates in USD. Conservative — round up. Source date
# in the comment; bump when refreshing.
# Rates as of late 2025 / early 2026 — update from each vendor's pricing page.
MODEL_PRICING_USD_PER_M_TOKENS: dict[str, dict[str, float]] = {
    # OpenAI
    "openai:gpt-4o":              {"input": 2.50,  "output": 10.00},
    "openai:gpt-4o-mini":         {"input": 0.15,  "output": 0.60},
    "openai:gpt-4.1":             {"input": 2.00,  "output": 8.00},
    "openai:gpt-4.1-mini":        {"input": 0.40,  "output": 1.60},
    # Anthropic — guidance only; verify before reporting to a customer
    "anthropic:claude-sonnet-4-6":            {"input": 3.00, "output": 15.00},
    "anthropic:claude-sonnet-4-5-20250929":   {"input": 3.00, "output": 15.00},
    "anthropic:claude-haiku-4-5-20251001":    {"input": 1.00, "output": 5.00},
    "anthropic:claude-haiku-4-5":             {"input": 1.00, "output": 5.00},
    # Reasonable fallback for unknown models — flagged in the breakdown.
    "_unknown_": {"input": 5.00, "output": 20.00},
}

# Reference token estimates per judge call. Conservative.
# A judge call sees: system prompt (~600t) + agent input (~200t) + agent output (~400t)
# = ~1,200 input tokens. Output is JSON verdict ~150 tokens.
DEFAULT_INPUT_TOKENS_PER_JUDGE_CALL = 1500
DEFAULT_OUTPUT_TOKENS_PER_JUDGE_CALL = 200

# Default escalation rate (fraction of role-evaluations that trigger meta-judge).
# Observed range across runs is 5–15%; we estimate at 15% to be safe.
DEFAULT_ESCALATION_RATE = 0.15

# Number of judge roles enabled by default (correctness, grounding,
# completeness, tool_usage, ux_tone, safety = 6). Triangulation adds 2 more
# providers for grounding (Ragas + TruLens), but those are usually free
# (locally hosted). We don't bill for them.
DEFAULT_ROLES_ENABLED = 6

# Default panel size per role (one OpenAI + one Anthropic = 2).
DEFAULT_PANEL_SIZE = 2

# Default meta-judge model.
DEFAULT_META_JUDGE = "anthropic:claude-sonnet-4-6"


@dataclass
class CostEstimate:
    estimated_cost_usd: float
    estimated_total_judge_calls: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    breakdown_per_call_usd: float
    notes: list[str]


def _rate_for(model_id: str) -> tuple[dict[str, float], bool]:
    """Look up (rates, was_known)."""
    rates = MODEL_PRICING_USD_PER_M_TOKENS.get(model_id)
    if rates:
        return rates, True
    return MODEL_PRICING_USD_PER_M_TOKENS["_unknown_"], False


def estimate_run_cost(
    *,
    num_scenarios: int,
    runs_per_scenario: int = 1,
    judges_enabled: bool = True,
    panel_models: list[str] | None = None,
    meta_judge_model: str = DEFAULT_META_JUDGE,
    roles_enabled: int = DEFAULT_ROLES_ENABLED,
    avg_input_tokens_per_call: int = DEFAULT_INPUT_TOKENS_PER_JUDGE_CALL,
    avg_output_tokens_per_call: int = DEFAULT_OUTPUT_TOKENS_PER_JUDGE_CALL,
    escalation_rate: float = DEFAULT_ESCALATION_RATE,
) -> CostEstimate:
    """Estimate the dollar cost of a run.

    Returns a CostEstimate with notes about any assumptions / unknowns —
    surfaced to the user so they can sanity-check.
    """
    notes: list[str] = []

    if not judges_enabled:
        return CostEstimate(
            estimated_cost_usd=0.0,
            estimated_total_judge_calls=0,
            estimated_input_tokens=0,
            estimated_output_tokens=0,
            breakdown_per_call_usd=0.0,
            notes=["Judges disabled — deterministic-only run, no LLM tokens spent."],
        )

    panel = panel_models or ["openai:gpt-4o-mini", "anthropic:claude-haiku-4-5"]
    panel_size = max(1, len(panel))

    # Total judge calls = scenarios × repetitions × roles × panel
    total_evaluations = num_scenarios * runs_per_scenario * roles_enabled
    total_judge_calls = total_evaluations * panel_size

    # Compute per-judge-call cost averaged across the panel
    per_call_costs_usd: list[float] = []
    for model in panel:
        rates, known = _rate_for(model)
        if not known:
            notes.append(f"Model '{model}' not in pricing table — using fallback rate (likely overestimate).")
        per_call = (
            avg_input_tokens_per_call * rates["input"] / 1_000_000
            + avg_output_tokens_per_call * rates["output"] / 1_000_000
        )
        per_call_costs_usd.append(per_call)
    avg_per_call_usd = sum(per_call_costs_usd) / len(per_call_costs_usd)

    panel_cost_usd = total_judge_calls * avg_per_call_usd

    # Meta-judge escalation: a fraction of role-evaluations also call the meta-judge.
    meta_rates, meta_known = _rate_for(meta_judge_model)
    if not meta_known:
        notes.append(f"Meta-judge '{meta_judge_model}' not in pricing table — using fallback rate.")
    meta_per_call_usd = (
        avg_input_tokens_per_call * meta_rates["input"] / 1_000_000
        + avg_output_tokens_per_call * meta_rates["output"] / 1_000_000
    )
    expected_meta_calls = total_evaluations * escalation_rate
    meta_cost_usd = expected_meta_calls * meta_per_call_usd

    total_cost = panel_cost_usd + meta_cost_usd
    total_calls = int(total_judge_calls + expected_meta_calls)
    total_input_tokens = total_calls * avg_input_tokens_per_call
    total_output_tokens = total_calls * avg_output_tokens_per_call

    notes.append(
        f"Assumes ~{avg_input_tokens_per_call} input + ~{avg_output_tokens_per_call} "
        f"output tokens per judge call, and {int(escalation_rate * 100)}% meta-judge escalation rate."
    )
    notes.append(
        "Cached judge calls (re-runs of identical inputs) cost $0; this estimate is the upper bound."
    )

    return CostEstimate(
        estimated_cost_usd=round(total_cost, 4),
        estimated_total_judge_calls=total_calls,
        estimated_input_tokens=total_input_tokens,
        estimated_output_tokens=total_output_tokens,
        breakdown_per_call_usd=round(avg_per_call_usd, 6),
        notes=notes,
    )

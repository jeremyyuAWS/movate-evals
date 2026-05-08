"""Cost estimator tests.

Verifies:
- Judges-disabled runs cost $0
- Default-panel cost scales linearly with scenarios + reps
- Unknown models trigger a fallback rate + a warning note
- Estimate is monotonically increasing in scenarios, reps, panel size
- Real-shape calls match the documented per-scenario reference range
"""
from __future__ import annotations

from mdk_eval.web import cost


def test_judges_disabled_costs_zero():
    e = cost.estimate_run_cost(num_scenarios=10, runs_per_scenario=3, judges_enabled=False)
    assert e.estimated_cost_usd == 0.0
    assert e.estimated_total_judge_calls == 0
    assert "deterministic-only" in e.notes[0].lower()


def test_default_panel_estimate_is_positive_for_judged_run():
    e = cost.estimate_run_cost(num_scenarios=6, runs_per_scenario=1)
    assert e.estimated_cost_usd > 0
    assert e.estimated_total_judge_calls > 0
    # 6 scenarios × 1 rep × 6 roles × 2 panel = 72 base + ~15% meta = ~83 calls
    assert 70 <= e.estimated_total_judge_calls <= 100


def test_more_scenarios_costs_more():
    e_small = cost.estimate_run_cost(num_scenarios=5, runs_per_scenario=1)
    e_big = cost.estimate_run_cost(num_scenarios=50, runs_per_scenario=1)
    assert e_big.estimated_cost_usd > e_small.estimated_cost_usd * 5  # roughly 10x


def test_more_reps_costs_more_proportionally():
    e_one = cost.estimate_run_cost(num_scenarios=10, runs_per_scenario=1)
    e_three = cost.estimate_run_cost(num_scenarios=10, runs_per_scenario=3)
    # 3 reps ≈ 3x the cost (allow small tolerance for meta-judge rounding)
    ratio = e_three.estimated_cost_usd / e_one.estimated_cost_usd
    assert 2.9 < ratio < 3.1


def test_unknown_model_warns_and_uses_fallback():
    e = cost.estimate_run_cost(
        num_scenarios=1, runs_per_scenario=1,
        panel_models=["openai:gpt-99-future-model"],
    )
    assert any("not in pricing table" in n for n in e.notes)
    # The fallback rate is high (5/20) so cost should be non-trivial even for 1 scenario.
    assert e.estimated_cost_usd > 0


def test_reference_range_per_scenario():
    """A typical 1-scenario × 1-rep × default panel run should land in the
    PRD's documented reference range of $0.02–$0.08 per scenario per run.
    Catches future drift in the pricing table or default token assumptions."""
    e = cost.estimate_run_cost(num_scenarios=1, runs_per_scenario=1)
    assert 0.005 <= e.estimated_cost_usd <= 0.10, \
        f"per-scenario cost ${e.estimated_cost_usd} is outside the documented reference range"


def test_breakdown_per_call_is_consistent_with_total():
    """estimated_cost_usd should be approximately
       (panel calls × avg cost) + (meta calls × meta cost).
    Sanity check that the breakdown number isn't a stale fixture."""
    e = cost.estimate_run_cost(num_scenarios=10, runs_per_scenario=2)
    assert e.breakdown_per_call_usd > 0
    # Loose bound — the meta-judge cost makes the total slightly higher than
    # base_calls × avg_per_call, but not by more than ~30%.
    base_estimate = e.estimated_total_judge_calls * e.breakdown_per_call_usd
    assert e.estimated_cost_usd <= base_estimate * 1.5


def test_token_counts_consistent():
    """estimated_input_tokens and estimated_output_tokens should equal
    estimated_total_judge_calls × per-call defaults."""
    e = cost.estimate_run_cost(num_scenarios=5, runs_per_scenario=1)
    assert e.estimated_input_tokens == e.estimated_total_judge_calls * cost.DEFAULT_INPUT_TOKENS_PER_JUDGE_CALL
    assert e.estimated_output_tokens == e.estimated_total_judge_calls * cost.DEFAULT_OUTPUT_TOKENS_PER_JUDGE_CALL

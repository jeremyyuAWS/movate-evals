"""Wilson + bootstrap CIs.

The Wilson tests cross-check against textbook values. The bootstrap tests
verify determinism (seeded), boundary behavior, and that with large n the CI
shrinks toward the sample mean.
"""
from __future__ import annotations

import math
import statistics

import pytest

from mdk_eval.runner.intervals import (
    bootstrap_mean_ci,
    summarize,
    wilson_interval,
)


# ---------------------------------------------------------------- Wilson


def test_wilson_basic_proportion():
    """5 successes out of 10 (point estimate 0.5).

    Wilson 95% interval at p=0.5, n=10 with z=1.96 is approximately
    (0.2366, 0.7634). Tolerate ±0.001 for the rounded z constant.
    """
    lo, hi = wilson_interval(5, 10)
    assert math.isclose(lo, 0.2366, abs_tol=0.001), lo
    assert math.isclose(hi, 0.7634, abs_tol=0.001), hi


def test_wilson_extreme_zero_successes():
    """Wilson at 0/n must give lo=0, hi>0 — Wald gives (0, 0) which is wrong."""
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0
    assert hi > 0.0
    assert hi < 0.4    # known sane upper bound for 0/10


def test_wilson_extreme_full_successes():
    lo, hi = wilson_interval(10, 10)
    assert hi == 1.0
    assert lo > 0.6    # known sane lower bound for 10/10
    assert lo < 1.0


def test_wilson_zero_n_returns_uninformative():
    """No data → (0, 1), not a math error."""
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_invalid_inputs():
    with pytest.raises(ValueError):
        wilson_interval(11, 10)
    with pytest.raises(ValueError):
        wilson_interval(-1, 10)


def test_wilson_ci_width_shrinks_with_n():
    """Same point estimate, larger n → tighter CI. This is a fundamental
    property of the interval; if it ever breaks, the formula is wrong."""
    _, hi_small = wilson_interval(50, 100)
    _, hi_big = wilson_interval(500, 1000)
    width_small = hi_small - (1 - hi_small)
    width_big = hi_big - (1 - hi_big)
    assert width_big < width_small


def test_wilson_alternate_z():
    """A wider z (99% CI) should give a wider interval than 95%."""
    lo95, hi95 = wilson_interval(5, 10)
    lo99, hi99 = wilson_interval(5, 10, z=2.5758)
    assert (hi99 - lo99) > (hi95 - lo95)


# ---------------------------------------------------------------- bootstrap


def test_bootstrap_empty_returns_zeros():
    assert bootstrap_mean_ci([]) == (0.0, 0.0)


def test_bootstrap_singleton_collapses():
    """With n=1, every resample is the same value — CI should collapse."""
    assert bootstrap_mean_ci([42.0]) == (42.0, 42.0)


def test_bootstrap_is_deterministic_with_seed():
    """Same inputs + same seed must give the same CI on every call.
    Critical for reproducible reports — runs that shouldn't show diffs shouldn't."""
    a = bootstrap_mean_ci([10, 20, 30, 40, 50], seed=42)
    b = bootstrap_mean_ci([10, 20, 30, 40, 50], seed=42)
    assert a == b


def test_bootstrap_ci_brackets_the_mean():
    """The percentile CI of resampled means should bracket the true sample
    mean (with overwhelming probability, given the sample IS the population
    we're resampling from)."""
    values = [40, 50, 60, 70, 80, 90, 100]
    sample_mean = statistics.fmean(values)
    lo, hi = bootstrap_mean_ci(values, iters=500, seed=0)
    assert lo <= sample_mean <= hi


def test_bootstrap_invalid_alpha():
    with pytest.raises(ValueError):
        bootstrap_mean_ci([1, 2, 3], alpha=0)
    with pytest.raises(ValueError):
        bootstrap_mean_ci([1, 2, 3], alpha=1.5)


# ---------------------------------------------------------------- summarize


def test_summarize_returns_full_payload():
    out = summarize(overall_scores=[80, 85, 90], pass_count=3, total_count=4)
    assert {
        "pass_rate_ci_lo", "pass_rate_ci_hi",
        "overall_score_ci_lo", "overall_score_ci_hi",
        "ci_method",
    } == set(out)
    # Sanity ranges
    assert 0 <= out["pass_rate_ci_lo"] <= out["pass_rate_ci_hi"] <= 1
    assert 0 <= out["overall_score_ci_lo"] <= out["overall_score_ci_hi"] <= 100


def test_summarize_pass_count_zero():
    """All-fail run: pass-rate CI should contain 0 with hi > 0."""
    out = summarize(overall_scores=[10, 20], pass_count=0, total_count=2)
    assert out["pass_rate_ci_lo"] == 0.0
    assert out["pass_rate_ci_hi"] > 0.0

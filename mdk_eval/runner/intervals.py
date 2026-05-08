"""Statistical confidence intervals for eval-run summary numbers.

Replaces (well — complements) the home-grown ``1 - normalized_variance``
"confidence" metric with formal CIs that answer two distinct questions:

1. **Pass-rate CI (Wilson score interval).**
   "Out of N scenarios, K passed — what's the 95% CI on the true pass-rate?"
   Wilson is the right answer here, not Wald: it's well-behaved at boundaries
   (0/N, N/N, small N) where Wald gives nonsense intervals or undefined widths.

2. **Overall-score CI (bootstrap).**
   The overall score is a weighted mean of per-scenario continuous scores.
   We don't know the true distribution, sample sizes are small (5-50 scenarios),
   and the per-scenario scores are bounded [0, 100] but not normal. Percentile
   bootstrap is the textbook answer: distribution-free, handles any aggregator,
   honest about the sample size.

The legacy ``confidence`` field remains. It measures *judge agreement* (a
different question: "how much do the LLM judges agree on each verdict"),
which CIs don't replace.
"""
from __future__ import annotations

import math
import random
from typing import Iterable, Sequence


# ----------------------------- Wilson on a proportion -----------------------------


def wilson_interval(successes: int, n: int, *, z: float = 1.959963984540054) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion.

    Default z is the two-sided 95% normal quantile (1.959963…). Pass z=2.5758
    for 99%, z=1.6448 for 90%. Returns (lo, hi) on [0, 1].

    Behavior at boundaries:
      - n == 0  → (0, 1)         (no information)
      - successes == 0 → lo == 0 (by construction)
      - successes == n → hi == 1 (by construction)

    Reference: Wilson, E.B. (1927). "Probable inference, the law of succession,
    and statistical inference". JASA 22:209-212. The interval the modern stats
    literature agrees on as default for small-n proportion CIs.
    """
    if n <= 0:
        return (0.0, 1.0)
    if successes < 0 or successes > n:
        raise ValueError(f"successes must be in [0, n]; got {successes}/{n}")

    p_hat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2 * n)) / denom
    halfwidth = (z * math.sqrt(p_hat * (1 - p_hat) / n + z2 / (4 * n * n))) / denom

    lo = max(0.0, center - halfwidth)
    hi = min(1.0, center + halfwidth)
    return (round(lo, 4), round(hi, 4))


# ----------------------------- bootstrap on a continuous mean -----------------------------


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    iters: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean of `values`.

    `iters` resamples (with replacement, n=len(values)) of the input, takes the
    mean of each, returns the (alpha/2, 1-alpha/2) percentiles.

    Default alpha=0.05 = 95% CI. seed=0 makes it deterministic — important so
    the same run always reports the same CI (no spurious diffs across replays).

    Edge cases:
      - empty input → (0.0, 0.0)   (caller decides what to do)
      - n == 1      → (v, v)        (no resampling possible; CI collapses)

    Note: the bootstrap is an approximation. With n=5 the CI is wide; that's
    correct — it reflects honest uncertainty given the tiny sample size.
    """
    if not values:
        return (0.0, 0.0)
    if len(values) == 1:
        v = float(values[0])
        return (round(v, 2), round(v, 2))
    if not (0 < alpha < 1):
        raise ValueError(f"alpha must be in (0, 1); got {alpha}")
    if iters < 1:
        raise ValueError(f"iters must be >= 1; got {iters}")

    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(iters):
        s = 0.0
        for _i in range(n):
            s += values[rng.randrange(n)]
        means.append(s / n)
    means.sort()

    lo_idx = int(math.floor((alpha / 2) * iters))
    hi_idx = min(iters - 1, int(math.ceil((1 - alpha / 2) * iters)) - 1)
    return (round(means[lo_idx], 2), round(means[hi_idx], 2))


# ----------------------------- public summary helper -----------------------------


def summarize(
    *,
    overall_scores: Iterable[float],
    pass_count: int,
    total_count: int,
) -> dict:
    """Compute both CIs for a run summary in one call.

    Returns a dict ready to merge into a run summary / report:
      {
        "pass_rate_ci_lo": float in [0, 1],
        "pass_rate_ci_hi": float in [0, 1],
        "overall_score_ci_lo": float in [0, 100],
        "overall_score_ci_hi": float in [0, 100],
        "ci_method": str,
      }
    """
    scores = [float(s) for s in overall_scores]
    pr_lo, pr_hi = wilson_interval(pass_count, total_count)
    os_lo, os_hi = bootstrap_mean_ci(scores)
    return {
        "pass_rate_ci_lo": pr_lo,
        "pass_rate_ci_hi": pr_hi,
        "overall_score_ci_lo": os_lo,
        "overall_score_ci_hi": os_hi,
        "ci_method": "wilson_95 / bootstrap_2000_pct_95",
    }

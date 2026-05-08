"""Per-topic scoring breakdown for a completed run.

Why this exists
---------------
Today the scorecard reports by *behavioral category* (correctness / safety /
honesty / instruction_following / etc.). After the topic-extractor work
(2026-05-07), every LLM-extracted scenario also carries a `topic:<slug>` tag
indicating WHAT the scenario is about (Movate Services / Career & Hiring / …).

Customers who read a scorecard want to know **where in their business the
agent is weak**, not just "the agent's safety category is at 78%." Topic-level
breakdowns answer that:

    Movate Services        94    ████████████████████░ 18 scenarios
    Career & Hiring        62    ████████████░░░░░░░░░  7 scenarios  ⚠
    Pricing Compliance     51    ██████████░░░░░░░░░░░  3 scenarios  ⚠

This module is read-only over the existing `scenario_aggregate` table —
no migration, no extra LLM calls, just a Python groupby over rows we already
have. Scenarios without a `topic:<slug>` tag are bucketed as `"untagged"` so
mixed runs (some pre-redesign, some post-) still produce a complete view.

Usage
-----
    from mdk_eval.insights.topic_breakdown import compute_topic_breakdown
    rows = db.fetch_scenario_aggregate_for_run(cur, run_id)
    breakdown = compute_topic_breakdown(rows)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


# A topic-tagged scenario contributes to ONE topic bucket. Multi-topic
# scenarios (rare today; possible in the future) contribute to each topic
# they're tagged with — the per-scenario contribution is duplicated, but
# downstream consumers see "scenarios_count" per topic which makes that
# explicit.

UNTAGGED_TOPIC_SLUG = "untagged"
UNTAGGED_TOPIC_NAME = "(untagged)"


@dataclass
class TopicScoreEntry:
    """One topic's rolled-up scoring for a run.

    `category_breakdown` projects scenarios within this topic onto the
    behavioral axis — so a customer can see e.g. "for Movate Services, our
    standard scenarios are at 96 but adversarial are at 71." This is the
    2D scoring view the topic-mix path was designed to enable.
    """

    slug: str
    display_name: str                                  # falls back to slug if no name source
    scenarios_count: int                               # number of scenarios under this topic
    mean_score: float                                  # 0..100 — average of mean_score across scenarios
    pass_rate: float                                   # 0..1   — average of pass_rate across scenarios
    failures_count: int                                # scenarios with pass_rate < 1.0
    severity_max: str                                  # "low" | "medium" | "high" | "critical"
    category_breakdown: dict[str, dict[str, float]] = field(default_factory=dict)
    scenario_ids: list[str] = field(default_factory=list)


@dataclass
class TopicBreakdown:
    """All topics' breakdowns for a run, plus run-level metadata.

    `topics` is sorted by `mean_score` ascending (worst first) so the worst
    topics are the first thing the dashboard shows — that's almost always
    where the user wants to look.
    """

    run_pk: int
    run_id: str | None
    topics: list[TopicScoreEntry]
    untagged_count: int                                # how many scenarios had no topic:<slug> tag
    total_scenarios: int


# ----------------------------- helpers -----------------------------


_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_INV_SEVERITY = {v: k for k, v in _SEVERITY_ORDER.items()}


def _severity_max(values: Iterable[str]) -> str:
    """Return the MAX severity from a list of severity strings.

    Defaults to 'medium' if all values are unknown / missing — never raises,
    so a malformed row in the DB doesn't break the entire breakdown.
    """
    best = -1
    for v in values:
        rank = _SEVERITY_ORDER.get(str(v).lower(), -1)
        if rank > best:
            best = rank
    return _INV_SEVERITY.get(best, "medium")


def _extract_topic_slugs(tags: Sequence[str] | None) -> list[str]:
    """Extract `<slug>` from any `topic:<slug>` tag on the row. Returns
    empty list if no topic tags present (caller should bucket as untagged)."""
    if not tags:
        return []
    out = []
    for t in tags:
        if not isinstance(t, str):
            continue
        if t.startswith("topic:"):
            slug = t.split(":", 1)[1].strip()
            if slug:
                out.append(slug)
    return out


def _extract_category(tags: Sequence[str] | None, fallback: str = "uncategorised") -> str:
    """Extract `<cat>` from a `category:<cat>` tag, if any. Used for the
    category breakdown within a topic."""
    if not tags:
        return fallback
    for t in tags:
        if isinstance(t, str) and t.startswith("category:"):
            cat = t.split(":", 1)[1].strip()
            if cat:
                return cat
    return fallback


# ----------------------------- core groupby -----------------------------


def compute_topic_breakdown(
    rows: Iterable[dict[str, Any]],
    *,
    run_pk: int = 0,
    run_id: str | None = None,
    topic_display_names: dict[str, str] | None = None,
) -> TopicBreakdown:
    """Group scenario_aggregate rows by topic tag and compute per-topic scores.

    Parameters
    ----------
    rows : iterable of dicts
        Each dict represents one row from `scenario_aggregate`. Required keys:
          - `scenario_id` : str
          - `tags`        : list[str] (may be None or empty; `topic:<slug>`
                            and `category:<cat>` tags are read from this)
          - `mean_score`  : numeric (0..100)
          - `pass_rate`   : numeric (0..1)
          - `severity`    : str
        Extra keys are ignored.
    run_pk / run_id : optional metadata to attach to the result.
    topic_display_names : optional dict mapping slug -> display name. When
        absent, the display name falls back to the slug. Typical source: the
        agent's stored topic extraction (per `extract_topics_async`).

    Returns
    -------
    TopicBreakdown — with `topics` sorted by mean_score ascending.

    Edge cases handled:
      - rows with no `topic:` tag → bucketed under "untagged"
      - rows with multiple `topic:` tags → contribute to each topic bucket
      - empty input → returns an empty breakdown (not an error)
      - missing severity/mean_score/pass_rate values default to safe values
    """
    topic_display_names = topic_display_names or {}
    by_slug: dict[str, dict[str, Any]] = {}
    total = 0
    untagged = 0

    for row in rows:
        total += 1
        scenario_id = str(row.get("scenario_id") or "")
        tags = row.get("tags") or []
        mean_score = float(row.get("mean_score") or 0.0)
        pass_rate = float(row.get("pass_rate") or 0.0)
        severity = str(row.get("severity") or "medium").lower()

        slugs = _extract_topic_slugs(tags)
        category = _extract_category(tags)
        if not slugs:
            untagged += 1
            slugs = [UNTAGGED_TOPIC_SLUG]

        for slug in slugs:
            bucket = by_slug.setdefault(slug, {
                "scenarios": [],
                "category_scores": {},  # category -> {"sum": float, "count": int, "passes": int}
            })
            bucket["scenarios"].append({
                "scenario_id": scenario_id,
                "mean_score": mean_score,
                "pass_rate": pass_rate,
                "severity": severity,
            })
            cs = bucket["category_scores"].setdefault(category, {"sum": 0.0, "count": 0, "passes": 0.0})
            cs["sum"] += mean_score
            cs["count"] += 1
            cs["passes"] += pass_rate

    topics: list[TopicScoreEntry] = []
    for slug, bucket in by_slug.items():
        scenarios = bucket["scenarios"]
        if not scenarios:
            continue
        n = len(scenarios)
        mean = sum(s["mean_score"] for s in scenarios) / n
        pr = sum(s["pass_rate"] for s in scenarios) / n
        failures = sum(1 for s in scenarios if s["pass_rate"] < 1.0)
        sev = _severity_max(s["severity"] for s in scenarios)
        cat_breakdown = {
            cat: {
                "mean_score": round(stats["sum"] / stats["count"], 2),
                "pass_rate": round(stats["passes"] / stats["count"], 3),
                "scenarios_count": stats["count"],
            }
            for cat, stats in bucket["category_scores"].items()
        }
        display_name = (
            UNTAGGED_TOPIC_NAME if slug == UNTAGGED_TOPIC_SLUG
            else topic_display_names.get(slug, slug)
        )
        topics.append(TopicScoreEntry(
            slug=slug,
            display_name=display_name,
            scenarios_count=n,
            mean_score=round(mean, 2),
            pass_rate=round(pr, 3),
            failures_count=failures,
            severity_max=sev,
            category_breakdown=cat_breakdown,
            scenario_ids=sorted({s["scenario_id"] for s in scenarios if s["scenario_id"]}),
        ))

    # Sort worst-first by mean_score, with secondary sort by failures desc
    # so when two topics tie on score, the one with more failures shows first.
    topics.sort(key=lambda t: (t.mean_score, -t.failures_count))

    return TopicBreakdown(
        run_pk=run_pk,
        run_id=run_id,
        topics=topics,
        untagged_count=untagged,
        total_scenarios=total,
    )

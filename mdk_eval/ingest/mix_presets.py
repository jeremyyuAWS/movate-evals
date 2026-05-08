"""Behavioral mix presets for the Test Mix Designer.

Why this exists
---------------
The original Mix Designer asked users for one number per behavioral category
(standard / edge / adversarial / safety / honesty / multi_turn / performance /
custom). After the topic-extractor work (2026-05-07), the user-facing model
inverted: users now pick **per-topic counts** as the primary axis (HOW MANY
tests for each agent-specific topic), and the **behavioral mix** becomes a
preset overlaid on top of every topic (WHICH BEHAVIORS to exercise).

A preset is a fixed distribution of behavioral categories. For each topic with
N tests, the preset's ratios distribute those N across categories using
largest-remainder (Hamilton) rounding so totals add up exactly.

Concrete example
----------------
- Topical counts:  {movate_services: 5, career_hiring: 3}
- Preset:          balanced (40% standard, 20% edge, 15% adversarial, ...)
- Result:          [(movate_services, standard, 2),
                    (movate_services, edge, 1),
                    (movate_services, adversarial, 1),
                    (movate_services, safety, 1),
                    (career_hiring, standard, 1),
                    (career_hiring, edge, 1),
                    (career_hiring, adversarial, 1)]

Each tuple becomes one LLM call to the extractor. Each generated scenario is
tagged with both `category:<behavior>` and `topic:<slug>` so downstream
filtering, scoring breakdowns, and coverage analysis can group either way.

Design choices
--------------
- **Three named presets** ship by default: `balanced`, `compliance_heavy`,
  `reliability_focused`. These correspond to the three engagement archetypes
  Movate sees most often (general FAQ / regulated-industry / customer-support).
- **Custom preset** lets the user supply their own ratios.
- **Largest-remainder rounding** (not simple round-half-up) so totals always
  match the requested topic count exactly. With round-half-up, `5 * 0.4 = 2`,
  `5 * 0.2 = 1`, `5 * 0.15 = 1` (rounded from 0.75), ... can sum to 4 or 6
  instead of 5. Hamilton's method allocates floors first then distributes
  leftover via largest fractional remainder.
- **Zero-count topics are dropped** from the expansion (no LLM calls for
  topics with 0 tests).
- **Custom category in presets is supported** but requires `custom_directive`
  passed alongside (same rule as the 1D path).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


# All 8 behavioral categories — must match keys in llm.CATEGORIES.
BEHAVIORAL_CATEGORIES: tuple[str, ...] = (
    "standard",
    "edge",
    "adversarial",
    "safety",
    "honesty",
    "multi_turn",
    "performance",
    "custom",
)


@dataclass(frozen=True)
class MixPreset:
    """A named behavioral distribution.

    `ratios` maps category name -> non-negative float. The values do NOT need
    to sum to 1.0 exactly; the distribution code normalises before allocating.
    Categories not listed get 0.
    """

    name: str
    label: str
    description: str
    # Non-mutable mapping. Use tuples-of-tuples so the dataclass can stay frozen.
    ratios: dict[str, float] = field(default_factory=dict)

    def normalised_ratios(self) -> dict[str, float]:
        """Return ratios scaled to sum to 1.0. Empty / all-zero → empty dict."""
        total = sum(v for v in self.ratios.values() if v > 0)
        if total <= 0:
            return {}
        return {k: v / total for k, v in self.ratios.items() if v > 0}


# ----------------------------- preset library -----------------------------


_PRESETS: dict[str, MixPreset] = {
    "balanced": MixPreset(
        name="balanced",
        label="Balanced",
        description=(
            "Default coverage profile. Most tests exercise normal usage with "
            "meaningful coverage of edge cases, adversarial probes, and safety "
            "refusals. Good first-run choice for general-purpose agents."
        ),
        ratios={
            "standard": 0.40,
            "edge": 0.20,
            "adversarial": 0.15,
            "safety": 0.10,
            "honesty": 0.05,
            "multi_turn": 0.05,
            "performance": 0.05,
        },
    ),
    "compliance_heavy": MixPreset(
        name="compliance_heavy",
        label="Compliance-heavy",
        description=(
            "Skews toward adversarial probes, safety refusals, and honesty / "
            "uncertainty handling. Use for regulated-industry agents (finance, "
            "healthcare, legal) where wrong answers carry policy or legal risk."
        ),
        ratios={
            "standard": 0.20,
            "edge": 0.15,
            "adversarial": 0.25,
            "safety": 0.25,
            "honesty": 0.10,
            "multi_turn": 0.05,
            "performance": 0.00,
        },
    ),
    "reliability_focused": MixPreset(
        name="reliability_focused",
        label="Reliability-focused",
        description=(
            "Prioritises happy-path correctness and edge handling, with light "
            "coverage of adversarial / safety. Use for customer-support and "
            "internal-tooling agents where the main risk is being unhelpful or "
            "wrong, not being manipulated."
        ),
        ratios={
            "standard": 0.50,
            "edge": 0.30,
            "adversarial": 0.05,
            "safety": 0.00,
            "honesty": 0.05,
            "multi_turn": 0.05,
            "performance": 0.05,
        },
    ),
}


def list_presets() -> list[MixPreset]:
    """All built-in presets in stable display order."""
    return [_PRESETS[k] for k in ("balanced", "compliance_heavy", "reliability_focused")]


def get_preset(name: str) -> MixPreset | None:
    """Look up a preset by name. Returns None if not found."""
    return _PRESETS.get(name)


def default_preset_name() -> str:
    """The preset that should be selected by default in the UI."""
    return "balanced"


# ----------------------------- distribution math -----------------------------


def distribute_count(total: int, ratios: dict[str, float]) -> dict[str, int]:
    """Allocate `total` integer items across categories using `ratios`.

    Uses Hamilton's largest-remainder method: floor each (ratio * total),
    then hand out the remaining items one at a time to the categories with
    the largest fractional remainders. Ties are broken alphabetically by
    category name (deterministic).

    Guarantees:
      - Output values are non-negative integers
      - Output values sum to exactly `total`
      - Categories with ratio 0 are omitted (NOT included as "0")
      - Categories with ratio > 0 may still receive 0 if total is small

    >>> distribute_count(5, {"a": 0.5, "b": 0.3, "c": 0.2})
    {'a': 3, 'b': 1, 'c': 1}
    >>> sum(distribute_count(13, {"a": 0.4, "b": 0.2, "c": 0.15, "d": 0.1, "e": 0.05, "f": 0.05, "g": 0.05}).values())
    13
    """
    if total <= 0 or not ratios:
        return {}
    # Drop zero / negative ratios.
    pos = {k: v for k, v in ratios.items() if v > 0}
    if not pos:
        return {}
    norm = sum(pos.values())
    scaled = {k: (v / norm) * total for k, v in pos.items()}
    floors = {k: int(v) for k, v in scaled.items()}
    leftover = total - sum(floors.values())

    # Ranked by fractional remainder desc, alphabetical asc as tiebreaker.
    remainders = sorted(
        ((k, scaled[k] - floors[k]) for k in scaled),
        key=lambda kv: (-kv[1], kv[0]),
    )
    for i in range(leftover):
        k = remainders[i % len(remainders)][0]
        floors[k] += 1
    return floors


@dataclass(frozen=True)
class TopicCell:
    """One (topic, behavior) cell in the expanded 2D mix.

    A cell is one LLM call: generate `count` scenarios for `topic_slug` in
    behavioral category `category`. `topic_name` is preserved so the prompt
    can mention the topic in human-readable form.
    """

    topic_slug: str
    topic_name: str
    category: str
    count: int


def expand_topic_mix(
    topic_counts: dict[str, int],
    *,
    behavior_ratios: dict[str, float],
    topic_names: dict[str, str] | None = None,
) -> list[TopicCell]:
    """Expand a topical-primary mix into (topic, behavior, count) cells.

    Parameters
    ----------
    topic_counts : dict
        Maps topic slug -> number of tests for that topic.
    behavior_ratios : dict
        Maps behavioral category name -> ratio. Values are normalised to sum
        to 1.0 internally; only positive ratios are used.
    topic_names : dict, optional
        Maps topic slug -> human-readable name. If absent (or a slug is
        missing), the slug itself is used as the name.

    Returns
    -------
    list[TopicCell]
        One cell per non-zero (topic, behavior) pair. Order is stable:
        topics in input order, behaviors by `BEHAVIORAL_CATEGORIES` order.

    Validation
    ----------
    Caller is expected to validate that all behavior categories appear in
    `llm.CATEGORIES`. We do NOT validate here so this module stays free of
    a circular dependency with the extractor.
    """
    topic_names = topic_names or {}
    cells: list[TopicCell] = []

    # Stable ordering: preserve topic input order, sort categories by canonical
    # `BEHAVIORAL_CATEGORIES` index so output is deterministic across runs.
    cat_order = {c: i for i, c in enumerate(BEHAVIORAL_CATEGORIES)}

    for topic_slug, n in topic_counts.items():
        if n <= 0:
            continue
        per_cat = distribute_count(n, behavior_ratios)
        if not per_cat:
            continue
        ordered = sorted(per_cat.items(), key=lambda kv: cat_order.get(kv[0], 99))
        for category, count in ordered:
            if count <= 0:
                continue
            cells.append(TopicCell(
                topic_slug=topic_slug,
                topic_name=topic_names.get(topic_slug, topic_slug),
                category=category,
                count=count,
            ))
    return cells


def total_scenarios(cells: Iterable[TopicCell]) -> int:
    """Sum of counts across all cells. Useful for cost estimation."""
    return sum(c.count for c in cells)


def collapse_to_category_mix(cells: Iterable[TopicCell]) -> dict[str, int]:
    """Sum counts by behavioral category. Useful for showing the user how
    their topical choices project onto the legacy category view."""
    out: dict[str, int] = {}
    for c in cells:
        out[c.category] = out.get(c.category, 0) + c.count
    return out

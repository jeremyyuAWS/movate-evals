"""Unit tests for `mdk_eval.ingest.mix_presets`.

Covers:
  - The preset library (names + ratios + ordering)
  - Hamilton-method distribution math (correctness, total preservation,
    edge cases — 0, 1, all-same-ratio, single category, very-small total)
  - 2D expansion: (topic, behavior, count) cells with stable ordering
  - Helpers: total_scenarios, collapse_to_category_mix
"""
from __future__ import annotations

import pytest

from mdk_eval.ingest.mix_presets import (
    BEHAVIORAL_CATEGORIES,
    MixPreset,
    TopicCell,
    collapse_to_category_mix,
    default_preset_name,
    distribute_count,
    expand_topic_mix,
    get_preset,
    list_presets,
    total_scenarios,
)


# -------------------------- preset library --------------------------


def test_list_presets_returns_three_in_stable_order():
    presets = list_presets()
    assert [p.name for p in presets] == ["balanced", "compliance_heavy", "reliability_focused"]
    for p in presets:
        assert p.label
        assert p.description
        assert p.ratios


def test_get_preset_known_and_unknown():
    assert get_preset("balanced") is not None
    assert get_preset("balanced").name == "balanced"
    assert get_preset("nonexistent") is None


def test_default_preset_name_is_balanced():
    assert default_preset_name() == "balanced"


def test_preset_ratios_sum_close_to_one():
    """Each shipped preset's positive ratios should sum to ~1.0 (small tolerance)
    so the documented percentages match the math."""
    for p in list_presets():
        s = sum(p.ratios.values())
        assert abs(s - 1.0) < 0.001, f"{p.name} ratios sum to {s}"


def test_preset_only_uses_known_categories():
    """No preset should mention a category that isn't in BEHAVIORAL_CATEGORIES."""
    for p in list_presets():
        for cat in p.ratios:
            assert cat in BEHAVIORAL_CATEGORIES, f"{p.name} uses unknown category {cat!r}"


def test_normalised_ratios_handles_unnormalised_input():
    p = MixPreset(name="x", label="X", description="x", ratios={"standard": 4, "edge": 1})
    norm = p.normalised_ratios()
    assert norm == {"standard": 0.8, "edge": 0.2}


def test_normalised_ratios_empty_or_zero_returns_empty():
    assert MixPreset(name="x", label="X", description="x", ratios={}).normalised_ratios() == {}
    assert MixPreset(
        name="x", label="X", description="x",
        ratios={"standard": 0, "edge": -1},
    ).normalised_ratios() == {}


# -------------------------- distribute_count --------------------------


def test_distribute_count_basic_case():
    """5 items split 50/30/20 → 3/1/1 (Hamilton: floors 2/1/1, leftover 1 → a)."""
    out = distribute_count(5, {"a": 0.5, "b": 0.3, "c": 0.2})
    assert out == {"a": 3, "b": 1, "c": 1}
    assert sum(out.values()) == 5


def test_distribute_count_total_always_preserved():
    """For ANY total + ratio shape, output must sum to exactly total."""
    ratios = {"standard": 0.40, "edge": 0.20, "adversarial": 0.15,
              "safety": 0.10, "honesty": 0.05, "multi_turn": 0.05, "performance": 0.05}
    for total in range(1, 30):
        out = distribute_count(total, ratios)
        assert sum(out.values()) == total, f"total={total} → sum={sum(out.values())}"


def test_distribute_count_zero_total_returns_empty():
    assert distribute_count(0, {"a": 0.5, "b": 0.5}) == {}


def test_distribute_count_negative_total_returns_empty():
    assert distribute_count(-3, {"a": 1.0}) == {}


def test_distribute_count_empty_ratios_returns_empty():
    assert distribute_count(10, {}) == {}


def test_distribute_count_all_zero_ratios_returns_empty():
    assert distribute_count(10, {"a": 0, "b": 0}) == {}


def test_distribute_count_drops_zero_ratio_keys():
    out = distribute_count(5, {"a": 0.5, "b": 0.5, "c": 0})
    assert "c" not in out
    assert sum(out.values()) == 5


def test_distribute_count_single_category_gets_everything():
    assert distribute_count(7, {"a": 1.0}) == {"a": 7}


def test_distribute_count_unnormalised_ratios_work():
    """Caller doesn't have to pre-normalise — function does it."""
    out = distribute_count(10, {"a": 4, "b": 1})
    assert out == {"a": 8, "b": 2}


def test_distribute_count_total_smaller_than_categories():
    """3 items, 5 equal categories → 3 categories get 1, 2 get 0 (dropped if 0)."""
    out = distribute_count(3, {"a": 0.2, "b": 0.2, "c": 0.2, "d": 0.2, "e": 0.2})
    # All five have equal ratio → after floor=0, leftover=3 distributed alphabetically.
    assert sum(out.values()) == 3
    assert all(v >= 0 for v in out.values())
    # First three alphabetically should get the leftover.
    assert out["a"] == 1 and out["b"] == 1 and out["c"] == 1


def test_distribute_count_tiebreak_alphabetical():
    """When fractional remainders tie, alphabetical category name wins."""
    # 1 item, two categories each at 0.5 → floors 0/0, leftover 1 → "a" (alpha).
    out = distribute_count(1, {"z": 0.5, "a": 0.5})
    assert out == {"a": 1, "z": 0}


def test_distribute_count_one_item_to_largest_remainder():
    """1 item, 60/40 → 0/0 floors, leftover 1 → goes to 0.6 remainder."""
    out = distribute_count(1, {"a": 0.6, "b": 0.4})
    assert out == {"a": 1, "b": 0}


# -------------------------- expand_topic_mix --------------------------


def _balanced_ratios() -> dict[str, float]:
    return {"standard": 0.5, "edge": 0.3, "adversarial": 0.2}


def test_expand_topic_mix_basic_two_topics():
    cells = expand_topic_mix(
        {"alpha": 4, "beta": 2},
        behavior_ratios=_balanced_ratios(),
        topic_names={"alpha": "Alpha", "beta": "Beta"},
    )
    # 4 → 2/1/1, 2 → 1/1/0 (drops zero, alpha tiebreak)
    cells_by_topic: dict[str, list[TopicCell]] = {}
    for c in cells:
        cells_by_topic.setdefault(c.topic_slug, []).append(c)
    assert sum(c.count for c in cells_by_topic["alpha"]) == 4
    assert sum(c.count for c in cells_by_topic["beta"]) == 2


def test_expand_topic_mix_skips_zero_topics():
    cells = expand_topic_mix(
        {"alpha": 0, "beta": 3},
        behavior_ratios=_balanced_ratios(),
    )
    assert all(c.topic_slug == "beta" for c in cells)


def test_expand_topic_mix_uses_slug_as_name_if_missing():
    cells = expand_topic_mix(
        {"alpha": 1},
        behavior_ratios={"standard": 1.0},
        topic_names=None,
    )
    assert cells[0].topic_name == "alpha"


def test_expand_topic_mix_uses_provided_topic_name():
    cells = expand_topic_mix(
        {"alpha": 1},
        behavior_ratios={"standard": 1.0},
        topic_names={"alpha": "Alpha Topic"},
    )
    assert cells[0].topic_name == "Alpha Topic"


def test_expand_topic_mix_preserves_topic_order():
    cells = expand_topic_mix(
        {"zulu": 1, "alpha": 1, "mike": 1},
        behavior_ratios={"standard": 1.0},
    )
    assert [c.topic_slug for c in cells] == ["zulu", "alpha", "mike"]


def test_expand_topic_mix_orders_categories_canonically():
    """Within a topic, categories should appear in BEHAVIORAL_CATEGORIES order."""
    cells = expand_topic_mix(
        {"alpha": 7},
        behavior_ratios={
            "performance": 0.15, "edge": 0.15, "standard": 0.4,
            "adversarial": 0.15, "safety": 0.15,
        },
    )
    cats = [c.category for c in cells]
    canonical_index = [BEHAVIORAL_CATEGORIES.index(c) for c in cats]
    assert canonical_index == sorted(canonical_index)


def test_expand_topic_mix_drops_zero_count_cells():
    """Cells with count==0 after distribution are dropped (not emitted as no-op)."""
    cells = expand_topic_mix(
        {"alpha": 1},
        behavior_ratios={"standard": 0.6, "edge": 0.4},
    )
    # 1 item, ratios 60/40 → standard=1, edge=0. edge cell should be dropped.
    assert len(cells) == 1
    assert cells[0].category == "standard"


def test_expand_topic_mix_empty_input_returns_empty():
    assert expand_topic_mix({}, behavior_ratios={"standard": 1.0}) == []


def test_expand_topic_mix_empty_ratios_returns_empty():
    assert expand_topic_mix({"alpha": 5}, behavior_ratios={}) == []


# -------------------------- helpers --------------------------


def test_total_scenarios_sums_counts():
    cells = [
        TopicCell("a", "A", "standard", 3),
        TopicCell("a", "A", "edge", 2),
        TopicCell("b", "B", "safety", 1),
    ]
    assert total_scenarios(cells) == 6


def test_total_scenarios_empty_is_zero():
    assert total_scenarios([]) == 0


def test_collapse_to_category_mix_groups_by_category():
    cells = [
        TopicCell("a", "A", "standard", 3),
        TopicCell("a", "A", "edge", 2),
        TopicCell("b", "B", "standard", 4),
        TopicCell("b", "B", "safety", 1),
    ]
    assert collapse_to_category_mix(cells) == {"standard": 7, "edge": 2, "safety": 1}


def test_collapse_to_category_mix_empty_is_empty():
    assert collapse_to_category_mix([]) == {}


# -------------------------- integration check on real preset --------------------------


@pytest.mark.parametrize("preset_name", ["balanced", "compliance_heavy", "reliability_focused"])
def test_preset_distributes_realistic_total_correctly(preset_name: str):
    """For a realistic test budget (15 scenarios across 3 topics), each preset
    should produce a valid 2D expansion that sums to exactly 15 total."""
    preset = get_preset(preset_name)
    assert preset is not None
    cells = expand_topic_mix(
        {"topic_a": 5, "topic_b": 6, "topic_c": 4},
        behavior_ratios=preset.normalised_ratios(),
    )
    assert total_scenarios(cells) == 15
    # Every cell should have positive count.
    assert all(c.count > 0 for c in cells)
    # Every cell category should be a known one.
    assert all(c.category in BEHAVIORAL_CATEGORIES for c in cells)

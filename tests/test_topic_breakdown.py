"""Unit tests for `mdk_eval.insights.topic_breakdown`.

Covers:
  - Single-topic groupby (mean / pass rate / severity max / category breakdown)
  - Multi-topic scenario contributes to each bucket
  - Untagged scenarios bucket under "untagged"
  - Empty input → empty breakdown
  - Display-name lookup, fallback to slug
  - Sort order (worst first)
  - Severity max edge cases (unknown / mixed / all-empty)
"""
from __future__ import annotations

import pytest

from mdk_eval.insights.topic_breakdown import (
    TopicBreakdown,
    TopicScoreEntry,
    UNTAGGED_TOPIC_SLUG,
    UNTAGGED_TOPIC_NAME,
    _extract_category,
    _extract_topic_slugs,
    _severity_max,
    compute_topic_breakdown,
)


# -------------------------- helpers --------------------------


def test_severity_max_picks_highest_rank():
    assert _severity_max(["low", "medium", "high"]) == "high"
    assert _severity_max(["medium", "critical", "low"]) == "critical"
    assert _severity_max(["low", "low"]) == "low"


def test_severity_max_handles_unknown_values():
    """Unknown severity strings should be ignored, not break the calc."""
    assert _severity_max(["weird", "high"]) == "high"
    assert _severity_max(["weird"]) == "medium"   # default fallback
    assert _severity_max([]) == "medium"


def test_severity_max_case_insensitive():
    assert _severity_max(["HIGH", "low"]) == "high"
    assert _severity_max(["Critical"]) == "critical"


def test_extract_topic_slugs_finds_topic_tags():
    assert _extract_topic_slugs(["topic:a", "category:standard"]) == ["a"]
    assert _extract_topic_slugs(["topic:a", "topic:b"]) == ["a", "b"]


def test_extract_topic_slugs_empty_or_missing():
    assert _extract_topic_slugs(None) == []
    assert _extract_topic_slugs([]) == []
    assert _extract_topic_slugs(["category:standard", "happy"]) == []


def test_extract_topic_slugs_skips_blank_slug():
    assert _extract_topic_slugs(["topic:"]) == []
    assert _extract_topic_slugs(["topic:   "]) == []


def test_extract_topic_slugs_skips_non_string_tags():
    assert _extract_topic_slugs(["topic:a", 42, None]) == ["a"]


def test_extract_category_returns_first_match():
    assert _extract_category(["category:adversarial", "topic:x"]) == "adversarial"


def test_extract_category_falls_back_when_absent():
    assert _extract_category(["topic:x", "happy"]) == "uncategorised"
    assert _extract_category([], fallback="custom") == "custom"


# -------------------------- single-topic case --------------------------


def _row(scenario_id: str, mean: float, pr: float, severity: str, tags: list[str]) -> dict:
    return {
        "scenario_id": scenario_id,
        "tags": tags,
        "mean_score": mean,
        "pass_rate": pr,
        "severity": severity,
    }


def test_compute_breakdown_single_topic_aggregates_mean_and_pass_rate():
    rows = [
        _row("s1", 90.0, 1.0, "medium", ["topic:services", "category:standard"]),
        _row("s2", 80.0, 0.9, "high", ["topic:services", "category:adversarial"]),
        _row("s3", 70.0, 0.5, "high", ["topic:services", "category:safety"]),
    ]
    bd = compute_topic_breakdown(rows, run_pk=42)
    assert bd.run_pk == 42
    assert len(bd.topics) == 1
    t = bd.topics[0]
    assert t.slug == "services"
    assert t.scenarios_count == 3
    assert t.mean_score == 80.0  # (90+80+70)/3
    assert t.pass_rate == round((1.0 + 0.9 + 0.5) / 3, 3)
    assert t.failures_count == 2  # s2 and s3 had pass_rate < 1.0
    assert t.severity_max == "high"
    assert t.scenario_ids == ["s1", "s2", "s3"]


def test_compute_breakdown_uses_display_name_when_provided():
    rows = [_row("s1", 90, 1.0, "low", ["topic:services"])]
    bd = compute_topic_breakdown(
        rows,
        topic_display_names={"services": "Movate Services"},
    )
    assert bd.topics[0].display_name == "Movate Services"


def test_compute_breakdown_falls_back_to_slug_for_display_name():
    rows = [_row("s1", 90, 1.0, "low", ["topic:services"])]
    bd = compute_topic_breakdown(rows)
    assert bd.topics[0].display_name == "services"


# -------------------------- multi-topic & untagged --------------------------


def test_multi_topic_scenario_contributes_to_each_topic_bucket():
    """A scenario tagged with two topics counts in BOTH buckets."""
    rows = [
        _row("s1", 90, 1.0, "medium", ["topic:a", "topic:b"]),
        _row("s2", 60, 0.5, "high", ["topic:a"]),
    ]
    bd = compute_topic_breakdown(rows)
    by_slug = {t.slug: t for t in bd.topics}
    assert "a" in by_slug and "b" in by_slug
    # Topic a sees both s1 and s2 → mean = (90+60)/2 = 75
    assert by_slug["a"].scenarios_count == 2
    assert by_slug["a"].mean_score == 75.0
    # Topic b sees only s1 → mean = 90
    assert by_slug["b"].scenarios_count == 1
    assert by_slug["b"].mean_score == 90.0


def test_untagged_scenarios_bucket_under_untagged_slug():
    rows = [
        _row("s1", 90, 1.0, "low", ["topic:x"]),
        _row("s2", 50, 0.5, "high", ["category:standard"]),  # no topic tag
        _row("s3", 60, 0.7, "medium", []),                    # no tags at all
    ]
    bd = compute_topic_breakdown(rows)
    assert bd.untagged_count == 2
    by_slug = {t.slug: t for t in bd.topics}
    assert UNTAGGED_TOPIC_SLUG in by_slug
    untagged = by_slug[UNTAGGED_TOPIC_SLUG]
    assert untagged.scenarios_count == 2
    assert untagged.display_name == UNTAGGED_TOPIC_NAME
    assert untagged.mean_score == 55.0


def test_untagged_uses_constant_display_name_even_if_provided_in_dict():
    """If the caller accidentally provides a display name for 'untagged',
    we still use the constant so the UI shows the same label everywhere."""
    rows = [_row("s1", 50, 0.5, "high", [])]
    bd = compute_topic_breakdown(
        rows,
        topic_display_names={UNTAGGED_TOPIC_SLUG: "Other"},
    )
    assert bd.topics[0].display_name == UNTAGGED_TOPIC_NAME


# -------------------------- category breakdown --------------------------


def test_category_breakdown_within_topic():
    rows = [
        _row("s1", 100, 1.0, "low", ["topic:services", "category:standard"]),
        _row("s2", 80, 1.0, "medium", ["topic:services", "category:standard"]),
        _row("s3", 60, 0.5, "high", ["topic:services", "category:adversarial"]),
    ]
    bd = compute_topic_breakdown(rows)
    cb = bd.topics[0].category_breakdown
    assert "standard" in cb and "adversarial" in cb
    assert cb["standard"]["scenarios_count"] == 2
    assert cb["standard"]["mean_score"] == 90.0   # (100 + 80) / 2
    assert cb["adversarial"]["scenarios_count"] == 1
    assert cb["adversarial"]["mean_score"] == 60.0
    assert cb["adversarial"]["pass_rate"] == 0.5


def test_category_breakdown_uncategorised_when_no_category_tag():
    rows = [_row("s1", 80, 1.0, "low", ["topic:x"])]
    bd = compute_topic_breakdown(rows)
    cb = bd.topics[0].category_breakdown
    assert "uncategorised" in cb


# -------------------------- sort order --------------------------


def test_topics_sorted_worst_first():
    rows = [
        _row("good_1", 95, 1.0, "low", ["topic:high_score"]),
        _row("ok_1", 80, 0.9, "medium", ["topic:mid_score"]),
        _row("bad_1", 50, 0.4, "high", ["topic:low_score"]),
    ]
    bd = compute_topic_breakdown(rows)
    assert [t.slug for t in bd.topics] == ["low_score", "mid_score", "high_score"]


def test_topics_with_tie_break_by_failures():
    """Two topics with the same mean — the one with more failures shows first."""
    rows = [
        _row("a1", 70, 1.0, "low", ["topic:fewer_fails"]),
        _row("b1", 70, 0.5, "high", ["topic:more_fails"]),
    ]
    bd = compute_topic_breakdown(rows)
    assert [t.slug for t in bd.topics] == ["more_fails", "fewer_fails"]


# -------------------------- edge cases --------------------------


def test_empty_input_returns_empty_breakdown():
    bd = compute_topic_breakdown([])
    assert bd.topics == []
    assert bd.total_scenarios == 0
    assert bd.untagged_count == 0


def test_handles_missing_fields_gracefully():
    """Rows missing optional fields shouldn't blow up."""
    rows = [
        {"scenario_id": "s1", "tags": ["topic:x"]},   # no mean_score, pass_rate, severity
    ]
    bd = compute_topic_breakdown(rows)
    assert len(bd.topics) == 1
    assert bd.topics[0].mean_score == 0.0
    assert bd.topics[0].pass_rate == 0.0
    assert bd.topics[0].severity_max == "medium"


def test_handles_non_string_topic_tags_safely():
    rows = [{"scenario_id": "s1", "tags": [42, None, "topic:x"], "mean_score": 80,
             "pass_rate": 1.0, "severity": "low"}]
    bd = compute_topic_breakdown(rows)
    assert len(bd.topics) == 1
    assert bd.topics[0].slug == "x"


def test_run_pk_and_run_id_pass_through():
    bd = compute_topic_breakdown([], run_pk=99, run_id="run-abc")
    assert bd.run_pk == 99
    assert bd.run_id == "run-abc"


def test_scenario_ids_deduplicated():
    """If a scenario appears twice in the input (multi-topic), the per-topic
    scenario_ids list should still dedup within each topic bucket."""
    rows = [
        _row("s1", 80, 1.0, "low", ["topic:x"]),
        _row("s1", 80, 1.0, "low", ["topic:x"]),  # duplicate row (defensive)
    ]
    bd = compute_topic_breakdown(rows)
    assert bd.topics[0].scenario_ids == ["s1"]
    assert bd.topics[0].scenarios_count == 2  # but scenarios_count still counts both rows


# -------------------------- realistic shape --------------------------


def test_realistic_run_with_mixed_topics():
    """End-to-end: mixed-topic run produces a sensible breakdown."""
    rows = [
        # Movate Services — strong (avg 90)
        _row("svc_1", 95, 1.0, "medium", ["topic:movate_services", "category:standard"]),
        _row("svc_2", 90, 1.0, "medium", ["topic:movate_services", "category:standard"]),
        _row("svc_3", 85, 0.9, "high", ["topic:movate_services", "category:adversarial"]),
        # Career & Hiring — weaker (avg 65)
        _row("car_1", 70, 0.7, "high", ["topic:career_hiring", "category:standard"]),
        _row("car_2", 60, 0.5, "high", ["topic:career_hiring", "category:adversarial"]),
        # An untagged legacy scenario (avg 100)
        _row("legacy_1", 100, 1.0, "low", []),
    ]
    bd = compute_topic_breakdown(
        rows,
        run_pk=24,
        run_id="2026-05-07-run",
        topic_display_names={
            "movate_services": "Movate Services",
            "career_hiring": "Career & Hiring",
        },
    )
    assert bd.total_scenarios == 6
    assert bd.untagged_count == 1
    # Worst-first ordering
    slugs = [t.slug for t in bd.topics]
    assert slugs[0] == "career_hiring"  # worst at 65
    # Display names resolved
    by_slug = {t.slug: t for t in bd.topics}
    assert by_slug["movate_services"].display_name == "Movate Services"
    assert by_slug["career_hiring"].display_name == "Career & Hiring"
    assert by_slug[UNTAGGED_TOPIC_SLUG].display_name == UNTAGGED_TOPIC_NAME


# -------------------------- type / shape --------------------------


def test_topic_breakdown_dataclass_shape():
    """Smoke test on the dataclass — the response stays well-typed."""
    bd = compute_topic_breakdown([])
    assert isinstance(bd, TopicBreakdown)
    assert isinstance(bd.topics, list)
    bd2 = compute_topic_breakdown([_row("s1", 90, 1.0, "low", ["topic:x"])])
    assert isinstance(bd2.topics[0], TopicScoreEntry)
    assert isinstance(bd2.topics[0].category_breakdown, dict)


@pytest.mark.parametrize("n_scenarios", [1, 5, 50])
def test_compute_breakdown_handles_various_sizes(n_scenarios: int):
    """Verify the function scales / doesn't fail at reasonable sizes."""
    rows = [
        _row(f"s{i}", 70 + (i % 30), 1.0 if i % 2 == 0 else 0.5,
             "medium", ["topic:bucket_a" if i % 2 == 0 else "topic:bucket_b"])
        for i in range(n_scenarios)
    ]
    bd = compute_topic_breakdown(rows)
    assert sum(t.scenarios_count for t in bd.topics) == n_scenarios

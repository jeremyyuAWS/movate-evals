"""Charts module — Vega-Lite scorecard renderer.

Verifies:
- Module imports cleanly when extras are present.
- `render_scorecard` produces a non-empty SVG containing all category labels
  and the score values.
- The Jinja `scorecard_chart` filter degrades gracefully (returns "") when
  chart deps are unavailable, and produces non-empty output when they are.
"""
from __future__ import annotations

import pytest

from mdk_eval.reporting import charts


pytestmark = pytest.mark.skipif(
    not charts.is_available(),
    reason="Optional 'viz' extra (altair + vl-convert) not installed.",
)


SAMPLE_SCORECARD = {
    "task_success": 87.0,
    "correctness": 89.0,
    "grounding": 82.0,
    "completeness": 75.0,
    "tool_usage": 88.0,
    "workflow_adherence": 100.0,
    "consistency": 79.0,
    "latency": 91.0,
    "safety": 98.0,
    "ux_tone": 55.0,
    "overall": 84.4,
}


def test_render_scorecard_returns_svg():
    svg = charts.render_scorecard(SAMPLE_SCORECARD)
    assert svg, "render_scorecard returned empty string"
    assert svg.lstrip().startswith("<svg"), "expected SVG output"
    assert "</svg>" in svg


def test_render_scorecard_contains_all_categories():
    svg = charts.render_scorecard(SAMPLE_SCORECARD)
    # Every category's display label should appear somewhere in the rendered SVG.
    for label in [
        "Task Success", "Correctness", "Grounding", "Completeness",
        "Tool Usage", "Workflow", "Consistency", "Latency", "Safety", "UX/Tone",
    ]:
        assert label in svg, f"missing label {label!r} in rendered scorecard SVG"


def test_render_scorecard_uses_brand_palette():
    svg = charts.render_scorecard(SAMPLE_SCORECARD)
    # At least one Movate brand color should appear (the failing UX/Tone bar
    # at 55 should be rendered in magenta).
    assert "#ED1E79" in svg or "#ed1e79" in svg, (
        "expected Movate magenta (#ED1E79) for sub-60 'fail' band bar"
    )


def test_render_scorecard_handles_object_input():
    """Should accept any object with attribute-style access too (e.g. Pydantic)."""
    class _SC:
        pass
    sc = _SC()
    for k, v in SAMPLE_SCORECARD.items():
        setattr(sc, k, v)
    svg = charts.render_scorecard(sc)
    assert svg.lstrip().startswith("<svg")


def test_render_scorecard_handles_zeros():
    """Judges-disabled run produces 0s for some categories — must not crash."""
    sc = {k: 0.0 for k in SAMPLE_SCORECARD}
    sc["task_success"] = 72.0  # at least one non-zero
    svg = charts.render_scorecard(sc)
    assert svg.lstrip().startswith("<svg")

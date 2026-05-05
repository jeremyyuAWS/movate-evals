"""Reliability scorecard chart — horizontal bars, one per category.

Why bars and not a radar/spider chart: Vega-Lite has no native polar-coordinate
mark, so a radar requires either dropping to raw Vega or pre-computing polygon
coordinates. A horizontal bar chart conveys the same information (10 categories
× 0-100 score) more legibly, supports a continuous color encoding by band, and
renders cleanly in PDF. We can revisit a true radar later if customers ask.
"""
from __future__ import annotations


import altair as alt
import vl_convert as vlc

from . import theme

# Display order + human-readable labels.
# Mirrors the order in report.html.j2 so the chart and the existing CSS grid
# tell the same story.
CATEGORIES: list[tuple[str, str]] = [
    ("task_success", "Task Success"),
    ("correctness", "Correctness"),
    ("grounding", "Grounding"),
    ("completeness", "Completeness"),
    ("tool_usage", "Tool Usage"),
    ("workflow_adherence", "Workflow"),
    ("consistency", "Consistency"),
    ("latency", "Latency"),
    ("safety", "Safety"),
    ("ux_tone", "UX/Tone"),
]


def _band_label(score: float) -> str:
    if score >= theme.PASS_THRESHOLD:
        return "Pass (≥80)"
    if score >= theme.WATCH_THRESHOLD:
        return "Watch (60–79)"
    return "Fail (<60)"


def render_scorecard(scorecard: dict | object, width: int = 720, height: int = 300) -> str:
    """Render the 10-category scorecard as inline SVG.

    Accepts either a dict-like scorecard or any object exposing the category
    attributes as numeric properties. Returns an SVG string ready for Jinja
    inclusion via `{{ chart_svg | safe }}`.
    """
    get = scorecard.get if isinstance(scorecard, dict) else lambda k: getattr(scorecard, k, 0)

    rows = [
        {
            "category": label,
            "score": float(get(key) or 0),
            "band": _band_label(float(get(key) or 0)),
            "order": i,
        }
        for i, (key, label) in enumerate(CATEGORIES)
    ]

    band_scale = alt.Scale(
        domain=["Pass (≥80)", "Watch (60–79)", "Fail (<60)"],
        range=["#1e7e34", theme.AMBER, theme.MAGENTA],
    )

    base = alt.Chart(alt.Data(values=rows))

    # The bar itself
    bars = base.mark_bar(cornerRadiusEnd=3, height=18).encode(
        y=alt.Y(
            "category:N",
            sort=alt.EncodingSortField("order", order="ascending"),
            title=None,
            axis=alt.Axis(labelLimit=140, labelPadding=8, ticks=False, domain=False),
        ),
        x=alt.X(
            "score:Q",
            scale=alt.Scale(domain=[0, 100]),
            title="Score (0–100)",
            axis=alt.Axis(values=[0, 20, 40, 60, 80, 100], format="d"),
        ),
        color=alt.Color("band:N", scale=band_scale, legend=alt.Legend(title=None, orient="top")),
        tooltip=[
            alt.Tooltip("category:N", title="Category"),
            alt.Tooltip("score:Q", title="Score", format=".0f"),
            alt.Tooltip("band:N", title="Band"),
        ],
    )

    # Numeric label at the end of each bar
    labels = base.mark_text(
        align="left",
        baseline="middle",
        dx=6,
        fontSize=11,
        fontWeight=600,
        color=theme.INK,
    ).encode(
        y=alt.Y("category:N", sort=alt.EncodingSortField("order", order="ascending")),
        x=alt.X("score:Q"),
        text=alt.Text("score:Q", format=".0f"),
    )

    # Reference rule at 80 (pass threshold)
    pass_rule = (
        alt.Chart(alt.Data(values=[{"x": theme.PASS_THRESHOLD}]))
        .mark_rule(color=theme.PLUM_SOFT, strokeDash=[4, 4], opacity=0.6)
        .encode(x="x:Q")
    )

    chart = (
        (bars + labels + pass_rule)
        .properties(width=width, height=height, padding={"left": 8, "right": 24, "top": 4, "bottom": 8})
        .configure(**theme.vega_config())
    )

    return vlc.vegalite_to_svg(chart.to_json())

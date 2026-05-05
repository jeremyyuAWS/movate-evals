"""Movate brand theme for Vega-Lite charts.

Single source of truth for chart colors, typography, and band thresholds.
Mirrors the CSS palette in `report.html.j2`. If the brand palette ever
changes, update this file and `report.html.j2`'s `:root` block together.
"""
from __future__ import annotations

# Foundation
INK = "#26282b"
PLUM = "#4f3144"
PLUM_SOFT = "#6e4f63"
PLUM_TINT = "#f6f2f5"
LINE = "#e6dde3"
WHITE = "#ffffff"

# Brand accent gradient
MAGENTA = "#ED1E79"
PINK = "#FF4F9A"
CORAL = "#FF5542"
ORANGE = "#F15A24"
AMBER = "#F7941D"
YELLOW = "#FFD200"

# Status / readiness — matches `readiness_color` filter in html_gen.py
READINESS_COLORS = {
    "production_ready": "#1e7e34",
    "pilot_ready": YELLOW,
    "needs_improvement": ORANGE,
    "not_ready": MAGENTA,
}

# Score-band thresholds for category coloring
PASS_THRESHOLD = 80   # ≥80 = pass (green)
WATCH_THRESHOLD = 60  # 60–79 = watch (amber); <60 = fail (magenta)


def band_for(score: float) -> str:
    """Return color band name for a 0-100 score."""
    if score >= PASS_THRESHOLD:
        return "pass"
    if score >= WATCH_THRESHOLD:
        return "watch"
    return "fail"


def color_for(score: float) -> str:
    """Return the hex color for a score on the 0-100 scale."""
    return {"pass": "#1e7e34", "watch": AMBER, "fail": MAGENTA}[band_for(score)]


def vega_config() -> dict:
    """Vega-Lite global config that themes every chart consistently.

    Apply via `chart.configure(**vega_config())` or `alt.themes.register(...)`.
    """
    return {
        "background": WHITE,
        "font": '-apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif',
        "title": {
            "color": PLUM,
            "fontSize": 14,
            "fontWeight": 700,
            "anchor": "start",
        },
        "axis": {
            "labelColor": INK,
            "titleColor": PLUM,
            "labelFontSize": 11,
            "titleFontSize": 12,
            "domainColor": LINE,
            "gridColor": LINE,
            "tickColor": LINE,
        },
        "legend": {
            "labelColor": INK,
            "titleColor": PLUM,
            "labelFontSize": 11,
            "titleFontSize": 12,
        },
        "view": {"stroke": "transparent"},
        "range": {
            "category": [MAGENTA, ORANGE, AMBER, YELLOW, PLUM, INK, CORAL, PINK],
            "ramp": [PLUM_TINT, YELLOW, ORANGE, MAGENTA],
        },
    }

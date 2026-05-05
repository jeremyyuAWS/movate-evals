"""Vega-Lite charts for Movate Agent Assurance reports.

Charts render to inline SVG strings for embedding in `report.html.j2` and
WeasyPrint PDF output. Both Altair and vl-convert are optional dependencies
(extras: `viz`); when missing, every renderer returns an empty string and the
template's CSS-only fallback takes over. Reports always render — charts are
upgrade-only.
"""
from __future__ import annotations

try:
    import altair  # noqa: F401
    import vl_convert  # noqa: F401

    HAVE_VIZ = True
except ImportError:
    HAVE_VIZ = False


def is_available() -> bool:
    """True if the optional chart deps are installed."""
    return HAVE_VIZ


if HAVE_VIZ:
    from .scorecard import render_scorecard
else:

    def render_scorecard(*_args, **_kwargs) -> str:  # type: ignore[no-redef]
        return ""


__all__ = ["is_available", "render_scorecard"]

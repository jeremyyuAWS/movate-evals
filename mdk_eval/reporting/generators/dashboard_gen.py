"""Business-user dashboard renderer.

Produces `dashboard.html` alongside the engineering `report.html`. Same data,
different audience: 6 plain-English KPIs, an auto-generated narrative verdict,
a "what's working / needs attention" split, and a glossary that explains every
metric.

The dashboard is always emitted (no extra dep beyond Jinja2). The scorecard
chart is included if the optional `viz` extra is installed; otherwise that
section is omitted.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ...config import RunConfig
from ...models import RunReport, ScenarioRunResult
from ..assets import movate_logo_data_uri
from ..charts import is_available as charts_available, render_scorecard
from ..dashboard_kpis import (
    GLOSSARY,
    KPI,
    auto_narrative,
    compute_kpis,
    kpi_value_display,
    kpi_value_suffix,
    whats_working_vs_not,
)


_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

_READINESS_COLOR = {
    "production_ready": "#1e7e34",
    "pilot_ready": "#FFD200",
    "needs_improvement": "#F15A24",
    "not_ready": "#ED1E79",
}


def _bar_pct(kpi: KPI) -> int:
    """Width of the progress bar inside a KPI tile (0-100)."""
    if kpi.format == "latency":
        # Inverse scaling: 0ms = full bar, 8000ms = empty. Helps "lower is better."
        return max(0, min(100, int(100 * (1 - kpi.value / 8000.0))))
    return max(0, min(100, int(round(kpi.value))))


def write_dashboard(
    out_path: Path,
    report: RunReport,
    runs_by_scenario: dict[str, list[ScenarioRunResult]],
    cfg: RunConfig,
) -> Path:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    kpis = compute_kpis(report, runs_by_scenario)
    working, not_working = whats_working_vs_not(kpis)
    narrative = auto_narrative(report, kpis)
    status_value = report.status.value if hasattr(report.status, "value") else str(report.status)
    readiness_color = _READINESS_COLOR.get(status_value, "#4f3144")

    # Chart is best-effort; never blocks dashboard rendering.
    scorecard_svg = ""
    if charts_available():
        try:
            sc_dict = (
                report.scorecard.model_dump()
                if hasattr(report.scorecard, "model_dump")
                else dict(report.scorecard)
            )
            scorecard_svg = render_scorecard(sc_dict, width=900, height=320)
        except Exception:
            scorecard_svg = ""

    aggs = getattr(report, "scenario_aggregates", []) or []
    total_scenarios = len(aggs)
    passing_scenarios = sum(1 for a in aggs if getattr(a, "pass_rate", 0) >= 0.8)

    html = env.get_template("dashboard.html.j2").render(
        report=report,
        cfg=cfg,
        kpis=kpis,
        working=working,
        not_working=not_working,
        narrative=narrative,
        readiness_color=readiness_color,
        scorecard_svg=scorecard_svg,
        glossary=GLOSSARY,
        total_scenarios=total_scenarios,
        passing_scenarios=passing_scenarios,
        movate_logo_data_uri=movate_logo_data_uri(),
        # template helpers
        kpi_display=kpi_value_display,
        kpi_suffix=kpi_value_suffix,
        kpi_bar_pct=_bar_pct,
    )
    out_path.write_text(html, encoding="utf-8")
    return out_path

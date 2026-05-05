"""HTML report renderer. Movate-branded Jinja2 template, single self-contained file."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ...config import RunConfig
from ...models import RunReport, ScenarioRunResult
from ..assets import movate_logo_data_uri
from ..charts import is_available as charts_available, render_scorecard


_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = lambda v: f"{float(v) * 100:.0f}%"
    env.filters["score100"] = lambda v: f"{int(round(float(v)))}"
    env.filters["score01_pct"] = lambda v: f"{float(v) * 100:.0f}"
    # Severity background tints — pale, brand-aligned warm tones.
    env.filters["sevcolor"] = lambda s: {
        "low":      "#eef6ee",   # pale green
        "medium":   "#fff5e0",   # pale amber  (Movate yellow tint)
        "high":     "#ffe8dc",   # pale coral  (Movate coral tint)
        "critical": "#ffd9e8",   # pale magenta (Movate magenta tint)
    }.get(str(s).lower(), "#f4eef1")
    # Readiness band colors:
    #   production_ready — keep universal green; do not fight convention
    #   pilot_ready      — Movate yellow (#FFD200) — clearly differentiated from production_ready
    #   needs_improvement — Movate orange (#F15A24) — clear escalation
    #   not_ready        — Movate magenta (#ED1E79) — bold, on-brand, unmissable
    env.filters["readiness_color"] = lambda r: {
        "production_ready": "#1e7e34",
        "pilot_ready":      "#FFD200",
        "needs_improvement": "#F15A24",
        "not_ready":        "#ED1E79",
    }.get(str(r).lower(), "#4f3144")
    # Pair with readiness_color: yellow needs dark text for WCAG contrast; everything else uses white.
    env.filters["readiness_text_color"] = lambda r: {
        "pilot_ready": "#26282b",  # Movate ink on Movate yellow — passes WCAG AA
    }.get(str(r).lower(), "#ffffff")

    # Vega-Lite chart filters. When the optional `viz` extra isn't installed,
    # these return empty strings and the template's CSS-only fallback takes over.
    def _safe_render(fn):
        def wrapped(*args, **kwargs) -> str:
            if not charts_available():
                return ""
            try:
                return fn(*args, **kwargs)
            except Exception:
                # Charts must never block report generation. Fall back to CSS.
                return ""
        return wrapped

    env.filters["scorecard_chart"] = _safe_render(
        lambda scorecard: render_scorecard(
            scorecard.model_dump() if hasattr(scorecard, "model_dump") else scorecard
        )
    )
    return env


def write_html_report(
    out_path: Path,
    report: RunReport,
    runs_by_scenario: dict[str, list[ScenarioRunResult]],
    cfg: RunConfig,
) -> Path:
    env = _env()
    tpl = env.get_template("report.html.j2")
    html = tpl.render(
        report=report,
        runs_by_scenario=runs_by_scenario,
        cfg=cfg,
        movate_logo_data_uri=movate_logo_data_uri(),
    )
    out_path.write_text(html, encoding="utf-8")
    return out_path

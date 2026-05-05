"""Terminal UI helpers: pre-flight summary, live status panel, inline failure
stream, next-steps panel, interactive run picker.

All output goes to stderr so stdout stays clean for piping (e.g. CI grabbing the
final `evaluation_summary.json` path).
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.prompt import IntPrompt
from rich.table import Table

from ..config import RunConfig
from ..models import Scenario, ScenarioRunResult
from .logging import console, is_quiet, is_verbose


# ----------------------------- preflight -----------------------------


def print_preflight(cfg: RunConfig, scenarios: list[Scenario], dataset_sha256: str, run_dir: Path) -> None:
    if is_quiet():
        return
    target = cfg.adapter.target
    endpoint = cfg.adapter.endpoint or "—"
    n_runs = cfg.runs_per_scenario
    n_scn = len(scenarios)
    judges = ", ".join(f"{j.provider}:{j.model}" for j in cfg.judges.panel) if cfg.judges_enabled else "—"
    triangulation = "on" if (cfg.judges_enabled and cfg.judges.triangulation_enabled) else "off"
    eta_s = _estimate_runtime_s(cfg, n_scn)

    table = Table.grid(padding=(0, 1))
    table.add_column(justify="right", style="dim")
    table.add_column()
    table.add_row("Target",   f"[bold]{target}[/bold] → [cyan]{endpoint}[/cyan]")
    table.add_row("Dataset",  f"{cfg.dataset}  [dim]({n_scn} scenarios · sha256:{dataset_sha256[:10]}…)[/dim]")
    table.add_row("Plan",     f"[bold]{n_scn} × {n_runs}[/bold] = {n_scn * n_runs} evaluations · "
                              f"judges: {judges} · triangulation: {triangulation}")
    table.add_row("Output",   f"[cyan]{run_dir}[/cyan]")
    table.add_row("Estimated", f"~{eta_s}s wall clock")

    console().print(Panel(table, title="[bold]mdk-eval run[/bold]",
                          border_style="orange3", title_align="left"))


def _estimate_runtime_s(cfg: RunConfig, n_scenarios: int) -> int:
    """Crude wall-clock estimate. Better than nothing for setting expectations."""
    units = n_scenarios * cfg.runs_per_scenario
    per_unit = 0.4  # mock baseline
    if cfg.judges_enabled:
        # one round-trip per (role, model) per unit; meta-judge adds ~30% overhead on disagreement
        per_unit += len(cfg.judges.enabled_roles) * len(cfg.judges.panel) * 1.2
        if cfg.judges.triangulation_enabled:
            per_unit += 0.5  # extra grounding providers
    return max(1, int(units * per_unit / max(cfg.concurrency, 1)))


# ----------------------------- live status -----------------------------


@dataclass
class LiveStats:
    total: int = 0
    done: int = 0
    passed: int = 0
    failed: int = 0
    escalations: int = 0
    triangulation_escalations: int = 0
    abstentions: int = 0
    judge_calls: int = 0
    latencies_ms: list[int] = field(default_factory=list)

    def update_from_run(self, r: ScenarioRunResult) -> None:
        self.done += 1
        self.passed += int(r.passed)
        self.failed += int(not r.passed)
        for arb in r.judge_panel:
            self.judge_calls += len(arb.verdicts)
            if arb.escalated:
                self.escalations += 1
        for tri in (r.triangulations or {}).values():
            if isinstance(tri, dict):
                if tri.get("escalated"):
                    self.triangulation_escalations += 1
                for p in tri.get("providers") or []:
                    if p.get("abstained"):
                        self.abstentions += 1
        if r.adapter and r.adapter.trace:
            self.latencies_ms.append(r.adapter.trace.latency_ms)

    def p50(self) -> int:
        return int(statistics.median(self.latencies_ms)) if self.latencies_ms else 0

    def pass_rate(self) -> float:
        return self.passed / self.done if self.done else 0.0


def _stats_table(stats: LiveStats) -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style="dim")
    t.add_column()
    t.add_column(justify="right", style="dim")
    t.add_column()
    pct = f"{stats.pass_rate() * 100:.0f}%"
    pass_color = "green" if stats.pass_rate() >= 0.85 else ("yellow" if stats.pass_rate() >= 0.6 else "red")
    fail_color = "red" if stats.failed else "dim"
    t.add_row(
        "Pass", f"[{pass_color}]{stats.passed}[/{pass_color}] [dim]({pct})[/dim]",
        "Fail", f"[{fail_color}]{stats.failed}[/{fail_color}]",
    )
    t.add_row(
        "Judge calls", f"{stats.judge_calls}",
        "Escalations", f"{stats.escalations + stats.triangulation_escalations}",
    )
    t.add_row(
        "Abstentions", f"{stats.abstentions}",
        "p50 latency", f"{stats.p50()} ms",
    )
    return t


def make_live_renderable(stats: LiveStats, progress: Progress) -> Group:
    """Group rendered by rich.Live; recomputed each refresh."""
    return Group(
        Panel(_stats_table(stats), title="Run status", border_style="orange3", title_align="left", padding=(0, 1)),
        progress,
    )


def make_progress() -> Progress:
    return Progress(
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console(),
        transient=False,
    )


# ----------------------------- inline failure stream -----------------------------


def emit_failure_line(c: Console, r: ScenarioRunResult) -> None:
    """One-line failure report. Print above the live region via the live console."""
    if is_quiet():
        return
    reason = "; ".join(f.reason for f in r.findings[:1]) if r.findings else "unknown"
    c.print(
        f"[red]✗[/red] [bold]{r.scenario_id}[/bold] (run {r.run_index})  "
        f"[dim]score {int(round(r.final_score))}/100 · {r.adapter.trace.latency_ms} ms[/dim]  "
        f"[red]{reason[:120]}[/red]"
    )
    if is_verbose():
        for f in r.findings:
            c.print(f"   [dim]· {f.failure_class.value}[/dim]: {f.reason}")


def emit_pass_line(c: Console, r: ScenarioRunResult) -> None:
    if not is_verbose():
        return
    c.print(
        f"[green]✓[/green] [bold]{r.scenario_id}[/bold] (run {r.run_index})  "
        f"[dim]score {int(round(r.final_score))}/100 · {r.adapter.trace.latency_ms} ms[/dim]"
    )


# ----------------------------- next-steps panel -----------------------------


def print_next_steps(run_dir: Path, baseline_dir: Path | None = None) -> None:
    """Bordered panel with file:// links and follow-up CLI commands."""
    if is_quiet():
        # In quiet mode, just print the run dir on stdout for piping.
        print(str(run_dir.resolve()))
        return

    abs_run = run_dir.resolve()
    dashboard_html = abs_run / "dashboard.html"
    report_html = abs_run / "report.html"
    report_pdf = abs_run / "report.pdf"
    summary_json = abs_run / "evaluation_summary.json"

    lines = []
    if dashboard_html.exists():
        lines.append(f"[bold]Dashboard[/bold]      [link=file://{dashboard_html}]file://{dashboard_html}[/link]  [dim](business-user)[/dim]")
    if report_html.exists():
        lines.append(f"[bold]Detail report[/bold]  [link=file://{report_html}]file://{report_html}[/link]  [dim](engineering)[/dim]")
    if report_pdf.exists():
        lines.append(f"[bold]PDF[/bold]            [link=file://{report_pdf}]file://{report_pdf}[/link]")
    if summary_json.exists():
        lines.append(f"[bold]Summary JSON[/bold]   [link=file://{summary_json}]file://{summary_json}[/link]")
    lines.append("")
    lines.append(f"[dim]Inspect: [/dim] mdk-eval report --results {run_dir}")
    if baseline_dir:
        lines.append(f"[dim]Compare: [/dim] mdk-eval compare -b {baseline_dir} -c {run_dir}")
    else:
        lines.append(f"[dim]Compare: [/dim] mdk-eval compare -b PREV_RUN -c {run_dir}")
    lines.append(f"[dim]Replay:  [/dim] mdk-eval replay -r {run_dir} --trace-id <scenario_id>")

    console().print(
        Panel("\n".join(lines), title="[bold]Next steps[/bold]",
              border_style="green", title_align="left", padding=(0, 1))
    )


# ----------------------------- interactive run picker -----------------------------


def _summarize_run(d: Path) -> dict[str, Any]:
    """Pull just enough from evaluation_summary.json + manifest to render one row."""
    info: dict[str, Any] = {"path": d, "run_id": d.name, "score": None, "status": None, "started": None}
    s = d / "evaluation_summary.json"
    if s.exists():
        try:
            obj = json.loads(s.read_text())
            info["score"] = obj.get("overall_score")
            info["status"] = obj.get("status")
        except Exception:
            pass
    m = d / "manifest.json"
    if m.exists():
        try:
            obj = json.loads(m.read_text())
            info["started"] = obj.get("started_at")
            info["target"] = obj.get("target")
        except Exception:
            pass
    return info


def list_runs(base: str | Path = "./results") -> list[dict[str, Any]]:
    base_p = Path(base)
    if not base_p.exists():
        return []
    out = []
    for d in base_p.iterdir():
        if d.is_dir() and (d / "manifest.json").exists():
            out.append(_summarize_run(d))
    out.sort(key=lambda x: str(x.get("started") or x["run_id"]), reverse=True)
    return out


def pick_run(
    base: str | Path = "./results",
    *,
    prompt: str = "Pick a run",
    exclude: Iterable[Path] = (),
) -> Path | None:
    """Render numbered table of recent runs and prompt for a selection.

    Returns None if no runs exist or user cancels.
    """
    runs = [r for r in list_runs(base) if r["path"] not in set(exclude)]
    if not runs:
        console().print(f"[yellow]No runs found under {base}[/yellow]")
        return None

    table = Table(title=prompt, show_lines=False)
    table.add_column("#", style="bold", justify="right")
    table.add_column("Run ID")
    table.add_column("Target")
    table.add_column("Score", justify="right")
    table.add_column("Status")
    table.add_column("Started", style="dim")
    for i, r in enumerate(runs[:20], start=1):
        score = r.get("score")
        status = r.get("status") or "—"
        score_str = f"{int(round(score))}" if isinstance(score, (int, float)) else "—"
        color = "green" if isinstance(score, (int, float)) and score >= 80 else (
            "yellow" if isinstance(score, (int, float)) and score >= 70 else "red"
        )
        table.add_row(
            str(i), r["run_id"], r.get("target") or "—",
            f"[{color}]{score_str}[/{color}]", status, str(r.get("started") or ""),
        )
    console().print(table)
    try:
        idx = IntPrompt.ask(f"Choose 1–{min(len(runs), 20)}", default=1, console=console())
    except (KeyboardInterrupt, EOFError):
        return None
    if 1 <= idx <= min(len(runs), 20):
        return runs[idx - 1]["path"]
    return None

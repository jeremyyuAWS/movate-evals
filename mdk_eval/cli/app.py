"""Typer CLI: run | report | compare | replay."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.table import Table

from .. import __version__
from ..config import AdapterConfig, JudgesConfig, RunConfig
from ..models import RunReport
from ..utils.logging import console, get_logger

app = typer.Typer(
    name="mdk-eval",
    help="Movate Agent Assurance — multi-layer agent evaluation.",
    no_args_is_help=True,
    add_completion=True,   # `mdk-eval --install-completion bash|zsh|fish`
)
log = get_logger()


@app.callback()
def _global(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Final score only; no live panel or pre-flight."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose: streams pass/fail lines + judge rationales."),
):
    """Top-level options applied to every subcommand."""
    from ..utils.logging import set_verbosity

    if quiet and verbose:
        raise typer.BadParameter("--quiet and --verbose are mutually exclusive")
    if quiet:
        set_verbosity("quiet")
    elif verbose:
        set_verbosity("verbose")


def _load_config(path: Optional[str]) -> RunConfig:
    if path and Path(path).exists():
        return RunConfig.from_yaml(path)
    return RunConfig()


def _load_report(run_dir: Path) -> RunReport:
    return RunReport(**json.loads((run_dir / "report.json").read_text()))


# ----------------------------- run -----------------------------


@app.command()
def run(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="YAML config path. Auto-discovered (mdk-eval.yaml in cwd) if omitted."),
    target: Optional[str] = typer.Option(None, help="mock | rest | openai_compat | lyzr | langgraph"),
    endpoint: Optional[str] = typer.Option(None),
    agent_id: Optional[str] = typer.Option(None),
    dataset: Optional[str] = typer.Option(None),
    runs: Optional[int] = typer.Option(None, help="Runs per scenario (multi-run stability)."),
    concurrency: Optional[int] = typer.Option(None),
    output: Optional[str] = typer.Option(None, "--output", "-o"),
    judges: Optional[str] = typer.Option(None, help="Comma list: openai,anthropic,off"),
    no_judges: bool = typer.Option(False, "--no-judges", help="Disable judges entirely."),
    no_pdf: bool = typer.Option(False, "--no-pdf"),
    client_name: Optional[str] = typer.Option(None),
    graph_import_path: Optional[str] = typer.Option(None, help="LangGraph 'pkg.mod:graph' for langgraph target."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate config + run first scenario through deterministic checks only. No artifacts written."),
    gate_against: Optional[str] = typer.Option(None, "--gate-against", help="Path to a baseline run dir; exit nonzero if overall_score regresses."),
    gate_threshold: float = typer.Option(5.0, "--gate-threshold", help="Score regression points beyond which --gate-against fails."),
):
    """Execute an evaluation run."""
    load_dotenv(override=False)
    # auto-discover config if not explicitly passed
    if config is None:
        from .validate import discover_config_path

        discovered = discover_config_path()
        if discovered:
            log.info(f"Using auto-discovered config: [cyan]{discovered}[/cyan]")
            config = str(discovered)
    cfg = _load_config(config)

    overrides: dict = {}
    if target:
        overrides["adapter"] = {**cfg.adapter.model_dump(), "target": target}
    if endpoint:
        overrides.setdefault("adapter", cfg.adapter.model_dump())["endpoint"] = endpoint
    if agent_id:
        overrides.setdefault("adapter", cfg.adapter.model_dump())["agent_id"] = agent_id
    if graph_import_path:
        overrides.setdefault("adapter", cfg.adapter.model_dump())["graph_import_path"] = graph_import_path
    if dataset:
        overrides["dataset"] = dataset
    if runs is not None:
        overrides["runs_per_scenario"] = runs
    if concurrency is not None:
        overrides["concurrency"] = concurrency
    if output:
        overrides["output_dir"] = output
    if no_judges:
        overrides["judges_enabled"] = False
    if judges:
        roles = JudgesConfig().enabled_roles
        providers = [j.strip().lower() for j in judges.split(",") if j.strip() and j.strip().lower() != "off"]
        # restrict panel to selected providers
        full = JudgesConfig()
        kept = [j for j in full.panel if j.provider in providers] or full.panel
        overrides["judges"] = {**full.model_dump(), "panel": [k.model_dump() for k in kept], "enabled_roles": roles}
        if "off" in [j.strip().lower() for j in judges.split(",")]:
            overrides["judges_enabled"] = False
    if no_pdf:
        overrides["pdf"] = False
    if client_name:
        overrides["client_name"] = client_name

    if overrides.get("adapter"):
        cfg = RunConfig(**{**cfg.model_dump(), **overrides, "adapter": AdapterConfig(**overrides["adapter"])})
    elif overrides:
        cfg = RunConfig(**{**cfg.model_dump(), **overrides})

    # Up-front validation. Fail fast with a fixable error before any work happens.
    from .validate import ConfigError, auto_disable_judges_if_no_keys, validate_config

    try:
        validate_config(cfg)
    except ConfigError as e:
        raise typer.BadParameter(str(e))

    # Smart judge auto-disable when API keys missing (warn, don't fail).
    auto = auto_disable_judges_if_no_keys(cfg)
    if auto.reason:
        log.warning(f"[yellow]{auto.reason}[/yellow]")

    if dry_run:
        _dry_run(cfg)
        return

    from ..runner.orchestrator import execute_run

    run_dir, report = asyncio.run(execute_run(cfg))
    _print_summary(report, run_dir)

    # Regression gate (CI mode). Exit 2 to differentiate from other errors.
    if gate_against:
        _enforce_gate(report, baseline_dir=Path(gate_against), threshold=gate_threshold)


def _dry_run(cfg) -> None:
    """Run preflight + first scenario through deterministic checks only.

    Exits 0 if the first scenario passes deterministic gates, 1 otherwise.
    No artifacts are written to disk.
    """
    import asyncio as _asyncio

    from ..adapters.factory import build_adapter
    from ..evaluators.deterministic import checks as det_checks
    from ..scenarios import load_scenarios
    from ..utils.ui import print_preflight
    from ..storage.versioning import sha256_file
    from ..scenarios import snapshot_dataset
    import tempfile

    scenarios = load_scenarios(cfg.dataset)
    if not scenarios:
        raise typer.BadParameter(f"Dataset is empty: {cfg.dataset}")
    # snapshot for SHA only (in tmp; not persisted to results/)
    tmp_snap = Path(tempfile.mkdtemp(prefix="mdk_dryrun_")) / "snap.jsonl"
    snapshot_dataset(scenarios, tmp_snap)
    print_preflight(cfg, scenarios, dataset_sha256=sha256_file(tmp_snap), run_dir=Path("(dry-run — no artifacts written)"))

    s = scenarios[0]
    log.info(f"[bold]--dry-run[/bold]: executing first scenario [cyan]{s.id}[/cyan] through deterministic checks only.")

    async def _go():
        adapter = build_adapter(cfg.adapter)
        try:
            ar = await adapter.run({**s.input, "context": s.context,
                                    "_expected_tools": [t.model_dump() for t in s.expected_tools],
                                    "_expected_workflow": s.workflow.must_visit})
        finally:
            await adapter.aclose()
        return ar

    ar = _asyncio.run(_go())
    checks = det_checks.run_all(s, ar)

    table = Table(title=f"Deterministic checks — {s.id}")
    table.add_column("Check"); table.add_column("Pass"); table.add_column("Score"); table.add_column("Reason", style="dim")
    n_fail = 0
    for c in checks:
        if not c.passed:
            n_fail += 1
        color = "green" if c.passed else "red"
        table.add_row(c.name, f"[{color}]{c.passed}[/{color}]", f"{c.score:.2f}", c.reason or "—")
    console().print(table)
    if n_fail:
        console().print(f"[red]{n_fail} deterministic check(s) failed.[/red] Resolve before live run.")
        raise typer.Exit(code=1)
    console().print("[green]Dry-run passed.[/green] Live run should be safe to launch.")


def _enforce_gate(report, baseline_dir: Path, threshold: float) -> None:
    """Compare report.overall_score to baseline.overall_score; exit 2 on regression."""
    summary_path = baseline_dir / "evaluation_summary.json"
    if not summary_path.exists():
        raise typer.BadParameter(
            f"--gate-against expects an mdk-eval run dir with evaluation_summary.json: {baseline_dir}"
        )
    baseline = json.loads(summary_path.read_text())
    bl_score = float(baseline.get("overall_score", 0.0))
    cd_score = float(report.overall_score)
    delta = round(cd_score - bl_score, 2)

    if cd_score < bl_score - threshold:
        console().print(
            f"\n[red][bold]✗ REGRESSION[/bold]: candidate score {cd_score:.1f} is "
            f"{abs(delta):.1f} pts below baseline {bl_score:.1f} "
            f"(threshold {threshold:.1f}).[/red]"
        )
        console().print(f"[dim]Baseline run: {baseline_dir}[/dim]")
        raise typer.Exit(code=2)
    else:
        sign = "+" if delta >= 0 else ""
        color = "green" if delta >= 0 else "yellow"
        console().print(
            f"\n[{color}]✓ Gate passed: Δ {sign}{delta:.1f} vs baseline {bl_score:.1f} "
            f"(threshold {threshold:.1f}).[/{color}]"
        )


# ----------------------------- report -----------------------------


@app.command()
def report(
    results: Optional[str] = typer.Option(None, "--results", "-r", help="Path to a run dir. If omitted, you'll be prompted to pick one."),
    fmt: str = typer.Option("html", "--format", "-f", help="Comma list: html, pdf, json, csv, dashboard, manager-summary"),
):
    """Re-generate report artifacts from a saved run dir.

    Formats:
      html             — engineering report (Movate-branded HTML, embeds the Vega-Lite chart)
      pdf              — same content as html, rendered via WeasyPrint (best-effort)
      json             — full RunReport as report.json
      csv              — flat per-scenario table
      dashboard        — business-user dashboard (KPI tiles + plain-English narrative)
      manager-summary  — executive JSON: Expected vs Actual, Correctness, Accuracy
                         (the shape from the manager's process diagram)

    Pass any subset, comma-separated, e.g. `-f html,manager-summary`.
    """
    run_dir = _resolve_results_dir(results, prompt="Pick a run to re-report")
    if not (run_dir / "report.json").exists():
        raise typer.BadParameter(f"Not a run dir: {run_dir}")
    rep = _load_report(run_dir)
    cfg = RunConfig(**json.loads((run_dir / "config.json").read_text()))

    formats = {f.strip().lower() for f in fmt.split(",") if f.strip()}
    runs_by_scenario: dict = {}
    sdir = run_dir / "scenarios"
    if sdir.exists():
        for sid_dir in sdir.iterdir():
            runs = []
            rd = sid_dir / "runs"
            if not rd.exists():
                continue
            for r in sorted(rd.iterdir(), key=lambda p: int(p.name)):
                ev = r / "eval.json"
                if ev.exists():
                    from ..models import ScenarioRunResult
                    runs.append(ScenarioRunResult(**json.loads(ev.read_text())))
            if runs:
                runs_by_scenario[sid_dir.name] = runs

    if "html" in formats or "pdf" in formats:
        from ..reporting.generators.html_gen import write_html_report
        from ..reporting.generators.pdf_gen import write_pdf_report

        html_path = write_html_report(run_dir / "report.html", rep, runs_by_scenario, cfg)
        log.info(f"HTML written: {html_path}")
        if "pdf" in formats:
            pdf_path = write_pdf_report(html_path, run_dir / "report.pdf")
            if pdf_path:
                log.info(f"PDF written: {pdf_path}")
    if "json" in formats:
        (run_dir / "report.json").write_text(json.dumps(rep.model_dump(mode="json"), indent=2, default=str))
        log.info("JSON refreshed.")
    if "csv" in formats:
        from ..reporting.generators.csv_gen import write_scenarios_csv

        write_scenarios_csv(run_dir / "scenarios.csv", rep.scenario_aggregates, runs_by_scenario)
        log.info("CSV refreshed.")
    if "dashboard" in formats:
        from ..reporting.generators.dashboard_gen import write_dashboard

        write_dashboard(run_dir / "dashboard.html", rep, runs_by_scenario, cfg)
        log.info("Dashboard refreshed.")
    if "manager-summary" in formats or "manager_summary" in formats:
        from ..reporting import executive_summary

        snap_path = run_dir / "dataset.snapshot.jsonl"
        snap_lines = snap_path.read_text(encoding="utf-8").splitlines() if snap_path.exists() else []
        executive_summary.write(
            run_dir / "manager_summary.json",
            report=rep,
            runs_by_scenario=runs_by_scenario,
            dataset_snapshot_lines=snap_lines,
            backend_agent_id=cfg.adapter.agent_id,
        )
        log.info(f"Manager summary written: {run_dir / 'manager_summary.json'}")


# ----------------------------- compare -----------------------------


@app.command()
def compare(
    baseline: Optional[str] = typer.Option(None, "--baseline", "-b"),
    candidate: Optional[str] = typer.Option(None, "--candidate", "-c"),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="Optional path to write JSON diff."),
):
    """Diff two run dirs and surface regressions / improvements.

    With both --baseline and --candidate omitted, prompts twice (candidate first,
    then baseline excluding the candidate)."""
    cand_dir = _resolve_results_dir(candidate, prompt="Pick CANDIDATE run")
    base_dir = _resolve_results_dir(baseline, prompt="Pick BASELINE run", exclude=[cand_dir])
    bl = _load_report(base_dir)
    cd = _load_report(cand_dir)

    diff = {
        "baseline_run_id": bl.manifest.run_id,
        "candidate_run_id": cd.manifest.run_id,
        "overall_delta": round(cd.overall_score - bl.overall_score, 2),
        "confidence_delta": round(cd.confidence - bl.confidence, 4),
        "scorecard_delta": {
            k: round(getattr(cd.scorecard, k) - getattr(bl.scorecard, k), 2)
            for k in bl.scorecard.model_dump().keys()
        },
        "status": {"baseline": bl.status.value, "candidate": cd.status.value},
        "scenarios": [],
    }

    bl_by = {a.scenario_id: a for a in bl.scenario_aggregates}
    cd_by = {a.scenario_id: a for a in cd.scenario_aggregates}
    all_ids = sorted(set(bl_by) | set(cd_by))

    table = Table(title="Scenario regression / improvement")
    table.add_column("Scenario"); table.add_column("Baseline"); table.add_column("Candidate"); table.add_column("Δ"); table.add_column("Status")

    for sid in all_ids:
        b = bl_by.get(sid)
        c = cd_by.get(sid)
        b_score = b.mean_score if b else None
        c_score = c.mean_score if c else None
        if b_score is None or c_score is None:
            delta = None
            status = "added" if b is None else "removed"
        else:
            delta = round(c_score - b_score, 2)
            if delta <= -5.0:
                status = "REGRESSION"
            elif delta >= 5.0:
                status = "improvement"
            else:
                status = "stable"
        diff["scenarios"].append({
            "id": sid, "baseline": b_score, "candidate": c_score, "delta": delta, "status": status
        })
        table.add_row(
            sid,
            f"{b_score:.2f}" if b_score is not None else "—",
            f"{c_score:.2f}" if c_score is not None else "—",
            f"{delta:+.2f}" if delta is not None else "—",
            f"[bold red]{status}[/bold red]" if status == "REGRESSION" else status,
        )

    console().print(table)
    console().print(
        f"\n[bold]Composite Δ[/bold]: {diff['overall_delta']:+.1f}  "
        f"baseline={bl.status.value} → candidate={cd.status.value}"
    )

    if out:
        Path(out).write_text(json.dumps(diff, indent=2, default=str))
        log.info(f"Diff written: {out}")


# ----------------------------- replay -----------------------------


@app.command()
def replay(
    results: Optional[str] = typer.Option(None, "--results", "-r", help="Run dir whose dataset+config to replay. If omitted, you'll be prompted."),
    trace_id: Optional[str] = typer.Option(None, "--trace-id", help="Replay a single scenario by trace_id (e.g. 'happy_path_qna::0'). Format: '<scenario_id>' or '<scenario_id>::<run_index>'."),
    target: Optional[str] = typer.Option(None),
    endpoint: Optional[str] = typer.Option(None),
    agent_id: Optional[str] = typer.Option(None),
    output: Optional[str] = typer.Option(None, "--output", "-o"),
    runs: Optional[int] = typer.Option(None),
    no_judges: bool = typer.Option(False, "--no-judges"),
):
    """Replay a saved run's dataset (and optionally config) against a (possibly new) endpoint.

    With --trace-id, only the matching scenario is replayed (by scenario_id; run index is ignored
    since the new run starts fresh).
    """
    load_dotenv(override=False)
    src = _resolve_results_dir(results, prompt="Pick a run to replay")
    cfg = RunConfig(**json.loads((src / "config.json").read_text()))
    snap = src / "dataset.snapshot.jsonl"
    if not snap.exists():
        raise typer.BadParameter(f"No dataset snapshot in {src}")

    dataset_path = str(snap)
    if trace_id:
        sid = trace_id.split("::", 1)[0]
        # filter snapshot down to the matching scenario
        import tempfile

        out_dir = Path(tempfile.mkdtemp(prefix="mdk_replay_"))
        filtered = out_dir / "filtered.jsonl"
        with open(snap) as f, open(filtered, "w") as g:
            kept = 0
            for line in f:
                obj = json.loads(line)
                if obj.get("id") == sid:
                    g.write(line)
                    kept += 1
        if kept == 0:
            raise typer.BadParameter(f"No scenario with id '{sid}' in {snap}")
        dataset_path = str(filtered)
        log.info(f"Replay narrowed to scenario id '{sid}' ({kept} match(es)).")

    overrides: dict = {"dataset": dataset_path}
    if target:
        overrides["adapter"] = {**cfg.adapter.model_dump(), "target": target}
    if endpoint:
        overrides.setdefault("adapter", cfg.adapter.model_dump())["endpoint"] = endpoint
    if agent_id:
        overrides.setdefault("adapter", cfg.adapter.model_dump())["agent_id"] = agent_id
    if output:
        overrides["output_dir"] = output
    if runs is not None:
        overrides["runs_per_scenario"] = runs
    if no_judges:
        overrides["judges_enabled"] = False

    if overrides.get("adapter"):
        cfg = RunConfig(**{**cfg.model_dump(), **overrides, "adapter": AdapterConfig(**overrides["adapter"])})
    else:
        cfg = RunConfig(**{**cfg.model_dump(), **overrides})

    from ..runner.orchestrator import execute_run

    run_dir, report = asyncio.run(execute_run(cfg))
    _print_summary(report, run_dir)


# ----------------------------- version -----------------------------


@app.command()
def ingest(
    input: str = typer.Option(..., "--input", "-i", help="Path to agent definition file (JSON, MD, ...)"),
    source: str = typer.Option("auto", "--source", "-s", help="Source format: auto | lyzr"),
    out_dir: str = typer.Option(".", "--out-dir", "-o", help="Project root for emitted configs/, datasets/, agent_cards/"),
    name: Optional[str] = typer.Option(None, "--name", help="Override the derived name (default: from agent definition)"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Allow overwriting existing emitted files."),
    synthesize: bool = typer.Option(
        False,
        "--synthesize",
        help=(
            "Also use an LLM to propose additional scenarios that probe the agent's "
            "intent (edge cases, multi-turn dialogs, adversarial prompts). All LLM "
            "scenarios are tagged 'unverified' + 'derived:llm' and require human review. "
            "Requires OPENAI_API_KEY (or ANTHROPIC_API_KEY)."
        ),
    ),
):
    """Ingest an agent definition and emit a config + draft scenarios + agent_card.

    All derived scenarios are tagged 'unverified' and (where applicable) 'requires_fixture'.
    Promote them after review by removing the 'unverified' tag.

    Pass --synthesize to additionally invoke the LLM extractor for richer scenarios
    that go beyond what regex-based heuristics can pull. LLM scenarios are added
    on top of the heuristic ones, never as a replacement.
    """
    from ..ingest.base import auto_detect, get_ingestor
    from ..ingest.extractors import llm as llm_extractor

    in_path = Path(input)
    if not in_path.exists():
        raise typer.BadParameter(f"Input not found: {in_path}")

    fmt = source if source != "auto" else auto_detect(in_path)
    ingestor = get_ingestor(fmt)
    raw = in_path.read_bytes()
    result = ingestor.ingest(in_path, raw)

    if name:
        result.name = name

    # --synthesize: additively augment with LLM-proposed scenarios.
    if synthesize:
        if not llm_extractor.is_available():
            console().print(
                "[yellow]⚠ --synthesize requires OPENAI_API_KEY (or ANTHROPIC_API_KEY); "
                "skipping LLM extraction. Heuristic scenarios still emitted.[/yellow]"
            )
        else:
            try:
                agent_def = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                console().print("[yellow]⚠ --synthesize: source is not JSON; LLM extractor needs JSON.[/yellow]")
                agent_def = None
            if agent_def is not None:
                from ..models import Scenario
                proposed = llm_extractor.extract(agent_def)
                model_used = (
                    os.getenv("MDK_INGEST_LLM_MODEL") or llm_extractor.DEFAULT_MODEL
                )
                provider_used = (
                    os.getenv("MDK_INGEST_LLM_PROVIDER") or llm_extractor.DEFAULT_PROVIDER
                )
                added = 0
                for p in proposed:
                    sd = llm_extractor.to_scenario_dict(
                        p,
                        name_prefix=result.name,
                        source_path=str(in_path),
                        source_sha256=result.source_sha256,
                        model=model_used,
                        provider=provider_used,
                    )
                    try:
                        result.scenarios.append(Scenario.model_validate(sd))
                        added += 1
                    except Exception:
                        # One bad LLM proposal shouldn't drop the rest. Skip silently;
                        # the user will see the count diff in the summary.
                        continue
                console().print(
                    f"[green]+{added}[/green] scenarios added by LLM extractor "
                    f"[dim](model={provider_used}:{model_used}, all tagged 'derived:llm' + 'unverified')[/dim]"
                )

    out_root = Path(out_dir)
    cfg_dir = out_root / "configs"
    ds_dir = out_root / "datasets"
    card_dir = out_root / "agent_cards"
    for d in (cfg_dir, ds_dir, card_dir):
        d.mkdir(parents=True, exist_ok=True)

    cfg_path = cfg_dir / f"{result.name}.yaml"
    ds_path = ds_dir / f"{result.name}.jsonl"
    card_path = card_dir / f"{result.name}.md"

    for p in (cfg_path, ds_path, card_path):
        if p.exists() and not overwrite:
            raise typer.BadParameter(f"Refusing to overwrite {p} (pass --overwrite).")

    # config -> YAML; pin dataset path now
    result.config.dataset = str(ds_path)
    import yaml as _yaml

    cfg_path.write_text(_yaml.safe_dump(result.config.model_dump(), sort_keys=False))

    # scenarios -> JSONL
    with open(ds_path, "w") as f:
        for s in result.scenarios:
            f.write(json.dumps(s.model_dump(mode="json"), sort_keys=True) + "\n")

    # agent card -> MD
    card_path.write_text(result.agent_card_md)

    # Stakeholder summary in console
    console().rule(f"[bold]Ingested {result.name}[/bold] (source: {result.source_format})")
    console().print(f"Source SHA-256: [dim]{result.source_sha256[:16]}…[/dim]")
    console().print(f"Scenarios derived: [bold]{len(result.scenarios)}[/bold] "
                    f"([yellow]all tagged 'unverified'[/yellow])")
    fixture_count = sum(1 for s in result.scenarios if s.meta.get("requires_fixture"))
    if fixture_count:
        console().print(f"[yellow]⚠ {fixture_count} scenarios tagged 'requires_fixture' — fill in real input data.[/yellow]")
    if result.warnings:
        console().print("\n[bold]Extractor warnings:[/bold]")
        for w in result.warnings:
            console().print(f"  • {w}")
    console().print(
        f"\n[bold]Files written:[/bold]\n"
        f"  config:     [cyan]{cfg_path}[/cyan]\n"
        f"  scenarios:  [cyan]{ds_path}[/cyan]\n"
        f"  agent card: [cyan]{card_path}[/cyan]\n"
    )
    console().print("[dim]Next:  mdk-eval run --config " + str(cfg_path) + "  (after reviewing the dataset)[/dim]")


# ----------------------------- ab -----------------------------


@app.command("ab")
def ab_cmd(
    config_a: str = typer.Option(..., "--config-a", "-a", help="YAML config for variant A (e.g. configs/prompt_v1.yaml)."),
    config_b: str = typer.Option(..., "--config-b", "-b", help="YAML config for variant B (e.g. configs/prompt_v2.yaml)."),
    dataset: Optional[str] = typer.Option(None, "--dataset", "-d", help="Override the dataset on both configs so A and B run against identical inputs. If omitted, each config uses its own dataset (only sensible if they already match)."),
    label_a: str = typer.Option("A", "--label-a", help="Display label for variant A."),
    label_b: str = typer.Option("B", "--label-b", help="Display label for variant B."),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="Where to write the markdown side-by-side report. Defaults to stdout."),
    json_out: Optional[str] = typer.Option(None, "--json-out", help="Optional path to write the structured ABDiff JSON (machine-readable)."),
    parallel: bool = typer.Option(False, "--parallel", help="Run both configs concurrently. Off by default — real LLM backends rate-limit aggressively."),
):
    """Run two configs against the same dataset and produce a side-by-side diff.

    Use this for prompt A/B testing, model comparisons, before/after a config
    change, or any other "did this change make things better?" question.

    The composite delta (b - a) is the headline number. Per-scenario rows are
    flagged 'REGRESSION' / 'improvement' / 'stable' against a 5-point band.
    """
    from .ab import build_diff, render_ab_markdown, run_ab

    cfg_a = _load_config(config_a)
    cfg_b = _load_config(config_b)

    ds_path = Path(dataset) if dataset else None
    (run_a_dir, report_a), (run_b_dir, report_b) = run_ab(
        cfg_a, cfg_b, dataset_override=ds_path, parallel=parallel
    )

    diff = build_diff(run_a_dir, report_a, run_b_dir, report_b, label_a=label_a, label_b=label_b)
    md = render_ab_markdown(diff)

    if out:
        Path(out).write_text(md)
        console().print(f"[green]Markdown report written to[/green] [cyan]{out}[/cyan]")
    else:
        # Stdout: write directly so it composes with redirection / piping.
        import sys
        sys.stdout.write(md)
        sys.stdout.flush()

    if json_out:
        Path(json_out).write_text(json.dumps(diff.model_dump(mode="json"), indent=2, default=str))
        console().print(f"[green]JSON diff written to[/green] [cyan]{json_out}[/cyan]")

    # One-line headline regardless of --out
    if diff.regressions > 0 and diff.overall_delta <= -2.0:
        console().print(f"[bold red]REGRESSION[/bold red]: {diff.label_b} score {diff.overall_delta:+.1f} vs {diff.label_a}")
    elif diff.improvements > 0 and diff.overall_delta >= 2.0:
        console().print(f"[bold green]Improvement[/bold green]: {diff.label_b} score {diff.overall_delta:+.1f} vs {diff.label_a}")
    else:
        console().print(f"[dim]No clear winner: composite delta {diff.overall_delta:+.1f} is within noise[/dim]")


# ----------------------------- promote-failure -----------------------------


@app.command("promote-failure")
def promote_failure_cmd(
    results: Optional[str] = typer.Option(None, "--results", "-r", help="Saved run dir to promote from. If omitted, you'll be prompted to pick one."),
    scenario: str = typer.Option(..., "--scenario", "-s", help="Scenario id to promote (must have failed at least one run in the saved run)."),
    run_index: Optional[int] = typer.Option(None, "--run-index", help="Specific run index to use; defaults to the worst-scoring run for this scenario."),
    target: Optional[str] = typer.Option(None, "--target", "-t", help="Append the new scenario to this JSONL dataset. If omitted, prints to stdout (composes with jq, redirection)."),
    note: Optional[str] = typer.Option(None, "--note", help="Free-text reviewer note recorded in the new scenario's provenance metadata."),
    new_id: Optional[str] = typer.Option(None, "--id", help="Override the auto-generated id for the new scenario."),
    show_tightening: bool = typer.Option(False, "--show-tightening", help="Print the constraint tightening summary (what's being added beyond the original)."),
):
    """Promote a failing scenario from a saved run into a tightened test.

    The new scenario inherits the original's input/context but adds explicit
    constraints encoding the *specific* failure mode the agent exhibited:
    forbidden phrases the agent said, required fields it omitted, claims it
    hallucinated. The agent must now actively defend against the same mistake
    on every future run.

    This is the HITL closure of the eval loop — turn observed failures into
    durable regression tests in one command.
    """
    from .promote import PromoteError, append_to_dataset, promote_failure

    src = _resolve_results_dir(results, prompt="Pick a run to promote a failure from")
    try:
        result = promote_failure(
            src,
            scenario_id=scenario,
            run_index=run_index,
            note=note,
            override_id=new_id,
        )
    except PromoteError as e:
        raise typer.BadParameter(str(e))

    if show_tightening:
        t = result.tightening
        c = console()
        c.print(f"[bold]Promotion summary[/bold] — {result.source_scenario_id} (run {result.source_run_index}, score {result.source_final_score:.1f})")
        if t.added_forbidden_phrases:
            c.print(f"  [yellow]+ forbidden_phrases:[/yellow] {t.added_forbidden_phrases}")
        if t.added_required_fields:
            c.print(f"  [yellow]+ required_fields:[/yellow] {t.added_required_fields}")
        if t.added_forbidden_claims:
            c.print(f"  [yellow]+ forbidden_claims:[/yellow] {len(t.added_forbidden_claims)} added")
        if t.notes:
            for n in t.notes:
                c.print(f"  [dim]· {n}[/dim]")
        if not (t.added_forbidden_phrases or t.added_required_fields or t.added_forbidden_claims):
            c.print("  [dim](no new constraints to add — all failures are caught by existing checks; the rerun is the verification.)[/dim]")
        c.print()

    payload_line = json.dumps(result.new_scenario, separators=(",", ":")) + "\n"

    if target:
        target_path = Path(target)
        try:
            append_to_dataset(target_path, result.new_scenario)
        except PromoteError as e:
            raise typer.BadParameter(str(e))
        console().print(
            f"[green]Promoted[/green] [cyan]{result.source_scenario_id}[/cyan] → "
            f"[cyan]{result.new_scenario['id']}[/cyan] in [cyan]{target_path}[/cyan]"
        )
    else:
        # No target — write the JSONL line to stdout. Use sys.stdout so it
        # composes cleanly with shell pipes (the rich console wraps lines and
        # adds ANSI codes, which would break `... | jq`).
        import sys
        sys.stdout.write(payload_line)
        sys.stdout.flush()


@app.command()
def export(
    fmt: str = typer.Option("promptfoo", "--format", "-f", help="Export format: promptfoo"),
    dataset: Optional[str] = typer.Option(None, "--dataset", "-d"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    endpoint: Optional[str] = typer.Option(None, "--endpoint", help="Agent endpoint URL (used to wire promptfoo http provider)"),
    out: str = typer.Option("promptfoo.yaml", "--out", "-o"),
):
    """Export the dataset into another tool's format.

    Currently supported: promptfoo (path A — emits a self-contained promptfoo.yaml).
    """
    from ..scenarios import load_scenarios

    if fmt.lower() != "promptfoo":
        raise typer.BadParameter(f"unsupported format: {fmt}")

    cfg = _load_config(config)
    ds_path = dataset or cfg.dataset
    scenarios = load_scenarios(ds_path)

    ep = endpoint or cfg.adapter.endpoint
    request_template = cfg.adapter.request_template
    response_text_path = cfg.adapter.response_text_path or "$.response"

    from ..exporters.promptfoo import write_promptfoo_yaml

    out_path = Path(out)
    write_promptfoo_yaml(
        scenarios,
        out_path,
        endpoint=ep,
        request_template=request_template,
        response_text_path=response_text_path,
    )
    log.info(
        f"Wrote {len(scenarios)} test(s) to [bold cyan]{out_path}[/bold cyan]\n"
        f"Run: [bold]promptfoo eval -c {out_path}[/bold]"
    )


@app.command()
def init(
    path: str = typer.Argument(".", help="Project directory to scaffold (created if missing)."),
    target: str = typer.Option("mock", "--target", "-t", help="Adapter: mock | rest | openai_compat | lyzr | langgraph"),
    endpoint: Optional[str] = typer.Option(None, "--endpoint"),
    agent_id: Optional[str] = typer.Option(None, "--agent-id"),
    name: str = typer.Option("agent", "--name", help="Name slug for dataset + config files."),
    client_name: str = typer.Option("Client", "--client-name"),
    vscode: bool = typer.Option(False, "--vscode/--no-vscode", help="Generate .vscode/ launch + settings."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
    no_prompt: bool = typer.Option(False, "--no-prompt", help="Non-interactive; use flags as-is."),
    cd_script: bool = typer.Option(False, "--cd-script", help="Print only `cd <abs_path>` to stdout (for `eval`)."),
    open_editor: bool = typer.Option(False, "--open", help="Force-open in VS Code (if `code` is on PATH)."),
):
    """Scaffold a new evaluation project directory.

    Creates configs/, datasets/, agent_cards/, results/ + a root mdk-eval.yaml
    that is auto-discovered by `mdk-eval run`.

    Auto-navigation: a child process can't change the parent shell's cwd, so:
      * print a copy-pasteable `cd <path>`
      * offer to open the project in VS Code (if `code` is installed)
      * pass `--cd-script` to emit just the cd command for shell-eval:
          eval "$(mdk-eval init my_proj --no-prompt --cd-script)"
    """
    from .init import InitOptions, maybe_open_in_vscode, print_cd_script, print_next_steps, scaffold

    target_dir = Path(path)
    opts = InitOptions(
        name=name,
        target=target,
        endpoint=endpoint,
        agent_id=agent_id,
        client_name=client_name,
        vscode=vscode,
        force=force,
        no_prompt=no_prompt or cd_script,   # cd-script implies non-interactive
    )

    if cd_script:
        # silent scaffold; only emit `cd <path>` on stdout
        from ..utils.logging import set_verbosity
        set_verbosity("quiet")
        scaffold(target_dir, opts)
        print_cd_script(target_dir)
        return

    written = scaffold(target_dir, opts)
    print_next_steps(target_dir, written, opts)

    if open_editor:
        import shutil as _sh
        import subprocess as _sp
        if _sh.which("code"):
            _sp.Popen(["code", str(target_dir.resolve())])
            console().print("[dim]Launched VS Code.[/dim]")
        else:
            console().print("[yellow]`code` CLI not found on PATH; install via VS Code → Cmd+Shift+P → 'Install code command in PATH'.[/yellow]")
    else:
        maybe_open_in_vscode(target_dir)


@app.command()
def doctor(
    dataset: Optional[str] = typer.Option(None, "--dataset", "-d", help="Optional: validate that this dataset loads."),
    endpoint: Optional[str] = typer.Option(None, "--endpoint", help="Optional: probe this URL for reachability."),
):
    """Diagnose setup. Checks API keys, optional extras, and (optionally) dataset + endpoint."""
    load_dotenv(override=False)
    from .doctor import run_doctor

    code = run_doctor(dataset=dataset, endpoint=endpoint)
    if code != 0:
        raise typer.Exit(code=code)


@app.command("rx")
def rx_cmd(
    results: Optional[str] = typer.Option(None, "--results", "-r", help="Saved run dir to diagnose. If omitted, you'll be prompted."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the LLM call (use template-only fallback). Useful in CI."),
    show: bool = typer.Option(True, "--show/--no-show", help="Print the markdown diagnosis to stdout after writing artifacts."),
):
    """Agent Doctor (Rx) — generate a 3-tier diagnostic from a saved run.

    Reads the run's `report.json`, asks Claude Sonnet 4.6 to produce:
      Tier 1: executive summary + headline action
      Tier 2: top 3 prescriptions (cited, confidence-tagged)
      Tier 3: specific suggested changes to the agent definition

    Writes `agent_doctor.md` + `agent_doctor.json` into the run dir.
    Cached by run-content fingerprint — re-running on the same run is $0.
    """
    src = _resolve_results_dir(results, prompt="Pick a run to diagnose")
    report = _load_report(src)
    from ..insights.agent_doctor import generate as gen_doctor, write_doctor_artifacts

    doctor = gen_doctor(report, allow_llm=not no_llm)
    md_path, json_path = write_doctor_artifacts(src, doctor)
    console().print(
        f"[green]Agent Doctor diagnosis written:[/green]\n"
        f"  • [cyan]{md_path}[/cyan]\n"
        f"  • [cyan]{json_path}[/cyan]\n"
        f"  source: [bold]{doctor.source}[/bold] · confidence: [bold]{doctor.confidence}[/bold]"
    )
    if show:
        console().print()
        console().print(md_path.read_text())


@app.command()
def version():
    """Print version."""
    typer.echo(f"mdk-eval {__version__}")


@app.command()
def push(
    results: str = typer.Option(..., "--results", "-r", help="Path to a results/run_<timestamp>/ directory."),
    connection_string: Optional[str] = typer.Option(
        None,
        "--connection-string", "-c",
        help="Postgres connection URI. Defaults to $DATABASE_URL.",
    ),
    engagement: str = typer.Option(..., "--engagement", help="Engagement slug (groups agents per customer)."),
    agent: str = typer.Option(..., "--agent", help="Agent slug (unique within an engagement)."),
    engagement_name: Optional[str] = typer.Option(None, "--engagement-name", help="Display name; defaults to slug."),
    agent_name: Optional[str] = typer.Option(None, "--agent-name", help="Display name; defaults to slug."),
    migrate: bool = typer.Option(True, "--migrate/--no-migrate", help="Apply schema migrations before push (idempotent)."),
    triggered_by: Optional[str] = typer.Option(None, "--triggered-by", help="Email or 'ci' — recorded in the run row."),
    ci_url: Optional[str] = typer.Option(None, "--ci-url", help="Build URL — recorded in the run row."),
):
    """Push a saved evaluation run into Postgres for the dashboard.

    Idempotent: re-pushing the same run replaces its child rows in place. Safe
    to run from CI after every evaluation.

    Targets any Postgres-compatible DB: Supabase, Azure Database for PostgreSQL,
    AWS RDS, local docker. The schema lives in migrations/001_initial_schema.sql
    and is applied automatically (pass --no-migrate to skip).

    Requires the optional 'push' extra: pip install -e '.[push]'
    """
    from ..storage import postgres_push

    if not postgres_push.is_available():
        typer.secho(
            "psycopg is not installed. Install with: pip install -e '.[push]'",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    conn_str = connection_string or os.getenv("DATABASE_URL")
    if not conn_str:
        typer.secho(
            "No connection string. Pass --connection-string or set DATABASE_URL.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    run_dir = Path(results)
    if not run_dir.exists():
        raise typer.BadParameter(f"results dir not found: {run_dir}")

    if migrate:
        try:
            postgres_push.ensure_schema(conn_str)
        except Exception as e:
            typer.secho(f"Schema migration failed: {e}", fg=typer.colors.RED)
            raise typer.Exit(code=1)

    try:
        counts = postgres_push.push_run(
            conn_str,
            run_dir,
            engagement_slug=engagement,
            agent_slug=agent,
            engagement_display_name=engagement_name,
            agent_display_name=agent_name,
            triggered_by=triggered_by,
            ci_url=ci_url,
        )
    except postgres_push.PostgresPushError as e:
        typer.secho(f"Push failed: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    typer.secho(f"Pushed run from {run_dir.name}", fg=typer.colors.GREEN)
    table = Table(title="Rows inserted")
    table.add_column("Table"); table.add_column("Rows", justify="right")
    for k, v in counts.items():
        table.add_row(k, f"{v:,}")
    console().print(table)


@app.command()
def cache(
    action: str = typer.Argument(
        "stats",
        help="One of: stats | clear. Default: stats.",
    ),
):
    """Inspect or clear the local judge-response cache.

    The cache is keyed by (provider, model, system_prompt, user_prompt, temperature).
    A cache hit costs $0 and returns the same verdict bit-identically. Useful for
    re-running an unchanged eval, regenerating reports, or replay-debugging.

    Set MDK_EVAL_CACHE_DISABLE=1 to bypass the cache entirely for one run.
    Override MDK_EVAL_CACHE_DIR to relocate the SQLite file (default ~/.mdk-eval/).
    """
    from ..evaluators.judges import cache as judge_cache
    from rich.table import Table

    if action == "stats":
        s = judge_cache.stats()
        if not s.get("enabled"):
            typer.secho("Cache is DISABLED via MDK_EVAL_CACHE_DISABLE.", fg=typer.colors.YELLOW)
        if s.get("path"):
            typer.echo(f"Path: {s['path']}")
        t = Table(title="Judge cache")
        t.add_column("Metric")
        t.add_column("Value", justify="right")
        t.add_row("Entries", f"{s.get('entries', 0):,}")
        t.add_row("Total cumulative hits", f"{s.get('total_hits', 0):,}")
        t.add_row("Size on disk", f"{s.get('size_bytes', 0) / 1024:.1f} KB")
        for provider, n in (s.get("by_provider") or {}).items():
            t.add_row(f"  by provider: {provider}", f"{n:,}")
        console().print(t)
        return

    if action == "clear":
        n = judge_cache.clear()
        typer.secho(f"Cleared {n} cache entries.", fg=typer.colors.GREEN)
        return

    typer.secho(f"Unknown cache action: {action!r}. Try 'stats' or 'clear'.", fg=typer.colors.RED)
    raise typer.Exit(code=2)


# ----------------------------- helpers -----------------------------


def _print_summary(report: RunReport, run_dir: Path) -> None:
    from ..utils.logging import is_quiet
    from ..utils.ui import print_next_steps

    if is_quiet():
        # CI mode: one line to stdout for piping; rest to stderr.
        print(f"{int(round(report.overall_score))} {report.status.value} {run_dir.resolve()}")
        return

    console().rule(f"[bold]{report.headline}[/bold]")
    table = Table(title="Reliability Scorecard (0–100)")
    table.add_column("Category"); table.add_column("Score")
    sc = report.scorecard.model_dump()
    for k in [
        "task_success", "correctness", "grounding", "completeness", "tool_usage",
        "workflow_adherence", "consistency", "latency", "safety", "ux_tone", "overall",
    ]:
        v = sc[k]
        color = "green" if v >= 80 else ("yellow" if v >= 60 else "red")
        table.add_row(k, f"[{color}]{int(round(v))}[/{color}]")
    console().print(table)
    console().print(
        f"[bold]Status:[/bold] {report.status.value.replace('_', ' ').title()}  "
        f"[bold]Confidence:[/bold] {report.confidence:.2f}  "
        f"[bold]Variance:[/bold] {report.variance:.2f}"
    )
    console().print(f"[bold]Recommendation:[/bold] {report.recommendation}\n")
    print_next_steps(run_dir)


def _resolve_results_dir(arg: Optional[str], *, prompt: str = "Pick a run", exclude: list[Path] | None = None) -> Path:
    """Return a run dir from --results, or prompt the user to pick one."""
    if arg:
        p = Path(arg)
        if not (p / "manifest.json").exists():
            raise typer.BadParameter(f"Not a run dir: {p}")
        return p
    from ..utils.ui import pick_run

    picked = pick_run("./results", prompt=prompt, exclude=exclude or [])
    if picked is None:
        raise typer.BadParameter("No run selected.")
    return picked


if __name__ == "__main__":
    app()

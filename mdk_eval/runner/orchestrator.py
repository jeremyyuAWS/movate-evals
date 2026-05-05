"""End-to-end orchestrator: scenarios x runs -> evaluations -> aggregate -> report."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.live import Live

from ..adapters.base import AgentAdapter
from ..adapters.factory import build_adapter
from ..config import RunConfig
from ..evaluators.deterministic import checks as det_checks
from ..evaluators.judges.deepeval_bridge import run_deepeval
from ..evaluators.judges.panel import run_panel
from ..evaluators.triangulation import build_grounding_providers, triangulate
from ..models import (
    AdapterResult,
    RunManifest,
    RunReport,
    Scenario,
    ScenarioRunResult,
)
from ..scenarios import load_scenarios, snapshot_dataset
from ..storage.run_dir import new_run_dir, scenario_run_dir, write_json
from ..storage.versioning import sha256_file, sha256_obj, sha256_str, tool_versions
from ..traces.langfuse_export import export as langfuse_export
from ..utils.logging import console, get_logger, is_quiet
from ..utils.ui import (
    LiveStats,
    emit_failure_line,
    emit_pass_line,
    make_live_renderable,
    make_progress,
    print_preflight,
)
from .scoring import (
    aggregate_runs,
    arbitration_summary,
    build_risk_register,
    build_scorecard,
    cluster_failures,
    compute_run_scores,
    compute_run_variance_and_confidence,
    decide_readiness,
    deterministic_summary,
    triangulation_summary,
)

log = get_logger()


async def _execute_one(
    adapter: AgentAdapter,
    scenario: Scenario,
    run_index: int,
    cfg: RunConfig,
    run_dir: Path,
    run_id: str,
) -> ScenarioRunResult:
    started = time.perf_counter()
    # inject expectations into mock adapter so it can simulate
    adapter_input: dict[str, Any] = dict(scenario.input)
    adapter_input.setdefault("context", scenario.context)
    if cfg.adapter.target == "mock":
        adapter_input["_expected_tools"] = [t.model_dump() for t in scenario.expected_tools]
        adapter_input["_expected_workflow"] = scenario.workflow.must_visit

    try:
        ar: AdapterResult = await adapter.run(adapter_input)
    except Exception as e:
        ar = AdapterResult(
            ok=False,
            trace=__import__("mdk_eval.models", fromlist=["Trace"]).Trace(
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
                latency_ms=0,
            ),
            error=f"{type(e).__name__}: {e}",
        )

    deterministic = det_checks.run_all(scenario, ar)

    judge_panel = []
    deepeval_scores: dict[str, float] = {}
    triangulations: dict[str, Any] = {}
    if cfg.judges_enabled and ar.ok and not det_checks.gates_failed(deterministic):
        # If triangulation is enabled, take "grounding" out of the panel so we don't
        # double-call grounding judges. The triangulator runs them itself.
        panel_cfg = cfg.judges
        if cfg.judges.triangulation_enabled and "grounding" in panel_cfg.enabled_roles:
            panel_cfg = panel_cfg.model_copy(update={
                "enabled_roles": [r for r in panel_cfg.enabled_roles if r != "grounding"],
            })
        judge_panel = await run_panel(scenario, ar, panel_cfg)
        deepeval_scores = run_deepeval(scenario, ar, cfg.judges)

        if cfg.judges.triangulation_enabled:
            providers = build_grounding_providers(cfg.judges)
            tri = await triangulate(
                "grounding",
                providers,
                scenario,
                ar,
                threshold=cfg.judges.triangulation_disagreement_threshold,
                meta_judge=cfg.judges.meta_judge,
            )
            triangulations["grounding"] = tri.model_dump()
            # Inject triangulation result into the judge panel as an ArbitratedScore
            # so downstream scoring/reporting paths pick it up uniformly.
            from ..models import ArbitratedScore, JudgeVerdict

            verdicts = [
                JudgeVerdict(
                    judge="grounding",
                    model=p.provider,
                    score=p.score if not p.abstained else 0.0,
                    **{"pass": (not p.abstained) and p.score >= 0.8},
                    rationale=(p.reason or (p.raw or {}).get("rationale") or ""),
                    raw=p.raw,
                )
                for p in tri.providers
            ]
            meta_v = None
            if tri.meta_verdict and "score" in tri.meta_verdict:
                meta_v = JudgeVerdict(
                    judge="meta:grounding",
                    model=tri.meta_verdict.get("model", "meta"),
                    score=float(tri.meta_verdict.get("score", tri.final_score)),
                    **{"pass": float(tri.meta_verdict.get("score", tri.final_score)) >= 0.8},
                    rationale=str(tri.meta_verdict.get("rationale", "")),
                )
            # confidence: 1 if agreed, 0.6 if escalated, 0.4 if insufficient
            conf = {"agreed": 1.0, "escalated": 0.6, "insufficient_signal": 0.4}.get(tri.status, 0.5)
            judge_panel.append(ArbitratedScore(
                role="grounding",
                final_score=tri.final_score,
                confidence=conf,
                variance=tri.spread,        # spread used as variance proxy in [0..1]
                verdicts=verdicts,
                escalated=tri.escalated,
                meta_judge_verdict=meta_v,
            ))

    cat_scores, final, passed, findings = compute_run_scores(
        scenario, deterministic, judge_panel, deepeval_scores, ar
    )

    duration_ms = int((time.perf_counter() - started) * 1000)
    trace_id = f"{scenario.id}::{run_index}"
    srr = ScenarioRunResult(
        scenario_id=scenario.id,
        run_index=run_index,
        trace_id=trace_id,
        adapter=ar,
        deterministic=deterministic,
        judge_panel=judge_panel,
        deepeval=deepeval_scores,
        triangulations=triangulations,
        category_scores=cat_scores,
        final_score=final,
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
    )

    # persist artifacts
    sdir = scenario_run_dir(run_dir, scenario.id, run_index)
    write_json(sdir / "trace.json", ar.trace)
    write_json(sdir / "deterministic.json", [c.model_dump() for c in deterministic])
    write_json(sdir / "judges.json", [j.model_dump(by_alias=True) for j in judge_panel])
    if triangulations:
        write_json(sdir / "triangulation.json", triangulations)
    write_json(sdir / "eval.json", srr)

    # observability (best-effort, non-blocking)
    try:
        langfuse_export(run_id, scenario, run_index, ar, deterministic, judge_panel, final / 100.0, passed)
    except Exception:
        pass

    return srr


async def execute_run(cfg: RunConfig, dataset_path: str | None = None) -> tuple[Path, RunReport]:
    """Execute a full evaluation run. Returns (run_dir, RunReport)."""
    dataset_path = dataset_path or cfg.dataset
    scenarios = load_scenarios(dataset_path)
    if not scenarios:
        raise ValueError(f"No scenarios loaded from {dataset_path}")

    run_dir = new_run_dir(cfg.output_dir)
    run_id = run_dir.name

    # snapshot dataset + config for reproducibility
    snap = run_dir / "dataset.snapshot.jsonl"
    snapshot_dataset(scenarios, snap)
    cfg_path = run_dir / "config.json"
    write_json(cfg_path, cfg.model_dump())

    # pre-flight summary so the user can Ctrl-C if anything looks off
    print_preflight(cfg, scenarios, dataset_sha256=sha256_file(snap), run_dir=run_dir)

    # judge prompt hashes
    from ..evaluators.judges.prompts import JUDGE_PROMPTS

    judge_prompt_hashes = {k: sha256_str(v) for k, v in JUDGE_PROMPTS.items()}

    manifest = RunManifest(
        run_id=run_id,
        started_at=datetime.now(timezone.utc),
        target=cfg.adapter.target,
        endpoint=cfg.adapter.endpoint,
        runs_per_scenario=cfg.runs_per_scenario,
        judges_enabled=cfg.judges.enabled_roles if cfg.judges_enabled else [],
        judge_models={
            role: [f"{j.provider}:{j.model}" for j in cfg.judges.panel]
            for role in cfg.judges.enabled_roles
        } if cfg.judges_enabled else {},
        meta_judge_model=f"{cfg.judges.meta_judge.provider}:{cfg.judges.meta_judge.model}",
        arbitration_variance_threshold=cfg.judges.arbitration_variance_threshold,
        dataset_path=str(dataset_path),
        dataset_sha256=sha256_file(snap),
        config_sha256=sha256_obj(cfg.model_dump()),
        judge_prompts_sha256=judge_prompt_hashes,
        tool_versions=tool_versions(),
        mdk_eval_version=__import__("mdk_eval").__version__,
    )
    write_json(run_dir / "manifest.json", manifest)

    # build adapter
    adapter = build_adapter(cfg.adapter)

    # bounded concurrency
    sem = asyncio.Semaphore(cfg.concurrency)

    async def _bounded(s: Scenario, i: int) -> ScenarioRunResult:
        async with sem:
            return await _execute_one(adapter, s, i, cfg, run_dir, run_id)

    runs_by_scenario: dict[str, list[ScenarioRunResult]] = {s.id: [] for s in scenarios}

    total_units = len(scenarios) * cfg.runs_per_scenario
    stats = LiveStats(total=total_units)
    progress = make_progress()
    task = progress.add_task("Evaluating", total=total_units)

    coros = []
    for s in scenarios:
        for i in range(cfg.runs_per_scenario):
            coros.append(_bounded(s, i))

    if is_quiet():
        # Minimal output mode: no live panel, no inline failures.
        for fut in asyncio.as_completed(coros):
            r = await fut
            runs_by_scenario[r.scenario_id].append(r)
            stats.update_from_run(r)
            progress.advance(task, 1)
    else:
        with Live(
            make_live_renderable(stats, progress),
            console=console(),
            refresh_per_second=8,
            transient=False,
        ) as live:
            for fut in asyncio.as_completed(coros):
                r = await fut
                runs_by_scenario[r.scenario_id].append(r)
                stats.update_from_run(r)
                progress.advance(task, 1)
                # inject inline pass/fail lines above the live region
                if r.passed:
                    emit_pass_line(live.console, r)
                else:
                    emit_failure_line(live.console, r)
                live.update(make_live_renderable(stats, progress))

    await adapter.aclose()

    # aggregates
    aggregates = []
    for s in scenarios:
        runs = sorted(runs_by_scenario[s.id], key=lambda x: x.run_index)
        aggregates.append(aggregate_runs(s, runs))
    write_json(run_dir / "aggregate.json", [a.model_dump() for a in aggregates])

    scorecard = build_scorecard(aggregates, runs_by_scenario)
    clusters = cluster_failures(aggregates)
    risks = build_risk_register(scorecard, clusters)
    arb = arbitration_summary(runs_by_scenario)
    arb["triangulation"] = triangulation_summary(runs_by_scenario)
    det_summary = deterministic_summary(runs_by_scenario)

    overall_score = scorecard.overall
    variance, disagreement_penalty, confidence = compute_run_variance_and_confidence(
        runs_by_scenario, arb
    )
    status, rec, findings = decide_readiness(overall_score, aggregates, scorecard)

    headline = (
        f"{int(round(overall_score))}/100 — "
        f"{status.value.replace('_', ' ').title()} "
        f"({sum(1 for a in aggregates if a.pass_rate >= 0.8)}/{len(aggregates)} scenarios passing) "
        f"· confidence {confidence:.2f}"
    )

    manifest.ended_at = datetime.now(timezone.utc)
    write_json(run_dir / "manifest.json", manifest)

    report = RunReport(
        manifest=manifest,
        overall_score=overall_score,
        confidence=confidence,
        variance=variance,
        status=status,
        scorecard=scorecard,
        headline=headline,
        key_findings=findings,
        recommendation=rec,
        scenario_aggregates=aggregates,
        failure_clusters=clusters,
        risk_register=risks,
        arbitration_stats=arb,
        deterministic_summary=det_summary,
        judge_disagreement_penalty=disagreement_penalty,
    )
    write_json(run_dir / "report.json", report)

    # Contract-shape summary for downstream tooling / CI gates.
    # Schema and methodology versions are pinned per PRD §11; bumping either is a deliberate, gated change.
    write_json(run_dir / "evaluation_summary.json", {
        "schema_version": "1.0",
        "methodology_version": "1.0",
        "mdk_eval_version": manifest.mdk_eval_version,
        "overall_score": overall_score,
        "confidence": confidence,
        "variance": variance,
        "status": status.value,
        "scorecard": scorecard.to_dict(),
        "passing_scenarios": sum(1 for a in aggregates if a.pass_rate >= 0.8),
        "total_scenarios": len(aggregates),
        "manifest_sha256": sha256_file(run_dir / "manifest.json"),
    })

    # write CSV + HTML (PDF best-effort) + business dashboard — handled by reporting module
    from ..reporting.generators.csv_gen import write_scenarios_csv
    from ..reporting.generators.dashboard_gen import write_dashboard
    from ..reporting.generators.html_gen import write_html_report
    from ..reporting.generators.pdf_gen import write_pdf_report

    write_scenarios_csv(run_dir / "scenarios.csv", aggregates, runs_by_scenario)
    html_path = write_html_report(run_dir / "report.html", report, runs_by_scenario, cfg)
    write_dashboard(run_dir / "dashboard.html", report, runs_by_scenario, cfg)
    if cfg.pdf:
        write_pdf_report(html_path, run_dir / "report.pdf")

    return run_dir, report

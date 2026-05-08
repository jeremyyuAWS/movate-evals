"""mdk-eval ab — side-by-side execution of two configs.

The CLI entrypoint in `cli/app.py` calls `run_ab` to orchestrate two runs and
`render_ab_diff` / `render_ab_markdown` to format the output. Splitting the
orchestration from rendering keeps the renderers unit-testable without
spinning up real evaluation runs.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from ..config import RunConfig
from ..models import RunReport


SCENARIO_DELTA_BAND = 5.0   # |Δmean_score| ≥ this → flagged regression/improvement
COMPOSITE_DELTA_BAND = 2.0  # |overall_delta| ≥ this → flagged at headline level


# ---------------------------------------------------------------- diff models


class ScenarioRow(BaseModel):
    scenario_id: str
    a_score: float | None
    b_score: float | None
    delta: float | None        # b - a (positive = B improved over A)
    status: str                # "added" | "removed" | "REGRESSION" | "improvement" | "stable"


class ABDiff(BaseModel):
    label_a: str
    label_b: str
    run_a_id: str
    run_b_id: str
    run_a_dir: str
    run_b_dir: str

    # Headline numbers
    a_overall: float
    b_overall: float
    overall_delta: float                   # b - a
    a_status: str
    b_status: str
    a_pass_rate: float
    b_pass_rate: float

    # Per-category scorecard delta (b - a)
    scorecard_delta: dict[str, float]

    # Per-scenario rows
    scenarios: list[ScenarioRow]

    # Aggregates
    regressions: int
    improvements: int
    added: int
    removed: int
    generated_at: datetime


# ---------------------------------------------------------------- orchestration


async def _run_one(cfg: RunConfig) -> tuple[Path, RunReport]:
    """Single config → single run. Wrap so we can `await` two of these."""
    from ..runner.orchestrator import execute_run
    return await execute_run(cfg)


def run_ab(
    config_a: RunConfig,
    config_b: RunConfig,
    *,
    dataset_override: Path | None = None,
    parallel: bool = False,
) -> tuple[tuple[Path, RunReport], tuple[Path, RunReport]]:
    """Execute both configs and return (run_a_dir, report_a), (run_b_dir, report_b).

    If `dataset_override` is given, both configs are forced onto that dataset —
    this is the typical case (you're A/B-ing prompts/configs, not datasets).
    Without it, each config uses its own dataset, which is only sensible if
    the configs already point at the same file.

    `parallel=False` (default) runs the two configs serially. Real LLM
    backends rate-limit aggressively; serial is safer. Set parallel=True for
    fast mock-backed dry runs.
    """
    if dataset_override is not None:
        config_a = config_a.model_copy(update={"dataset": str(dataset_override)})
        config_b = config_b.model_copy(update={"dataset": str(dataset_override)})

    if parallel:
        async def _both():
            return await asyncio.gather(_run_one(config_a), _run_one(config_b))
        a_pair, b_pair = asyncio.run(_both())
    else:
        a_pair = asyncio.run(_run_one(config_a))
        b_pair = asyncio.run(_run_one(config_b))

    return a_pair, b_pair


# ---------------------------------------------------------------- diff


def build_diff(
    run_a_dir: Path,
    report_a: RunReport,
    run_b_dir: Path,
    report_b: RunReport,
    *,
    label_a: str = "A",
    label_b: str = "B",
) -> ABDiff:
    """Compute the per-scenario diff between two reports.

    Sign convention: `delta = b - a`. Positive deltas mean B improved. A
    "REGRESSION" is when B is worse than A by more than SCENARIO_DELTA_BAND.
    """
    a_by = {a.scenario_id: a for a in report_a.scenario_aggregates}
    b_by = {a.scenario_id: a for a in report_b.scenario_aggregates}
    all_ids = sorted(set(a_by) | set(b_by))

    rows: list[ScenarioRow] = []
    regressions = improvements = added = removed = 0

    for sid in all_ids:
        a_agg = a_by.get(sid)
        b_agg = b_by.get(sid)
        a_score = a_agg.mean_score if a_agg else None
        b_score = b_agg.mean_score if b_agg else None

        if a_score is None and b_score is not None:
            status = "added"
            added += 1
            delta = None
        elif a_score is not None and b_score is None:
            status = "removed"
            removed += 1
            delta = None
        elif a_score is None and b_score is None:
            # Shouldn't happen — id wouldn't be in either dict — but be defensive.
            status = "stable"
            delta = None
        else:
            delta = round(b_score - a_score, 2)  # type: ignore[operator]
            if delta <= -SCENARIO_DELTA_BAND:
                status = "REGRESSION"
                regressions += 1
            elif delta >= SCENARIO_DELTA_BAND:
                status = "improvement"
                improvements += 1
            else:
                status = "stable"

        rows.append(ScenarioRow(
            scenario_id=sid,
            a_score=round(a_score, 2) if a_score is not None else None,
            b_score=round(b_score, 2) if b_score is not None else None,
            delta=delta,
            status=status,
        ))

    scorecard_delta = {
        k: round(getattr(report_b.scorecard, k) - getattr(report_a.scorecard, k), 2)
        for k in report_a.scorecard.model_dump().keys()
    }

    a_passes = sum(1 for a in report_a.scenario_aggregates if a.pass_rate >= 0.8)
    b_passes = sum(1 for a in report_b.scenario_aggregates if a.pass_rate >= 0.8)
    a_total = max(len(report_a.scenario_aggregates), 1)
    b_total = max(len(report_b.scenario_aggregates), 1)

    return ABDiff(
        label_a=label_a,
        label_b=label_b,
        run_a_id=report_a.manifest.run_id,
        run_b_id=report_b.manifest.run_id,
        run_a_dir=str(run_a_dir),
        run_b_dir=str(run_b_dir),
        a_overall=report_a.overall_score,
        b_overall=report_b.overall_score,
        overall_delta=round(report_b.overall_score - report_a.overall_score, 2),
        a_status=report_a.status.value,
        b_status=report_b.status.value,
        a_pass_rate=round(a_passes / a_total, 3),
        b_pass_rate=round(b_passes / b_total, 3),
        scorecard_delta=scorecard_delta,
        scenarios=rows,
        regressions=regressions,
        improvements=improvements,
        added=added,
        removed=removed,
        generated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------- renderers


def render_ab_markdown(diff: ABDiff) -> str:
    """Render the diff as a markdown report — pasteable into PRs / docs.

    Keep the structure stable; downstream tooling (PR-comment bots, etc.) may
    parse this rather than the JSON. Specifically: section headers, the verdict
    line, and the scenarios table format are part of the contract.
    """
    out = []
    out.append(f"# A/B comparison — {diff.label_a} vs {diff.label_b}")
    out.append("")
    out.append(f"_Generated: {diff.generated_at.isoformat(timespec='seconds')}_")
    out.append("")

    # Verdict line
    if diff.regressions > 0 and diff.overall_delta <= -COMPOSITE_DELTA_BAND:
        verdict = f"**REGRESSION:** {diff.label_b} scores **{diff.overall_delta:+.1f}** vs {diff.label_a} ({diff.regressions} scenario regressions)."
    elif diff.improvements > 0 and diff.overall_delta >= COMPOSITE_DELTA_BAND:
        verdict = f"**Improvement:** {diff.label_b} scores **{diff.overall_delta:+.1f}** vs {diff.label_a} ({diff.improvements} scenario improvements)."
    else:
        verdict = f"**No clear winner:** {diff.overall_delta:+.1f} composite delta is within the noise band (±{COMPOSITE_DELTA_BAND})."
    out.append(verdict)
    out.append("")

    # Headline table
    out.append("## Headline")
    out.append("")
    out.append(f"|              | {diff.label_a} | {diff.label_b} | Δ |")
    out.append("|---|---|---|---|")
    out.append(f"| Overall score | {diff.a_overall:.1f} | {diff.b_overall:.1f} | {diff.overall_delta:+.1f} |")
    out.append(f"| Status | `{diff.a_status}` | `{diff.b_status}` | — |")
    out.append(f"| Pass rate (≥0.8) | {diff.a_pass_rate:.0%} | {diff.b_pass_rate:.0%} | — |")
    out.append("")

    # Per-category
    out.append("## Per-category delta")
    out.append("")
    out.append("| Category | Δ |")
    out.append("|---|---|")
    for k, v in sorted(diff.scorecard_delta.items(), key=lambda kv: kv[1]):
        marker = ""
        if v <= -SCENARIO_DELTA_BAND:
            marker = " 🔻"
        elif v >= SCENARIO_DELTA_BAND:
            marker = " 🔺"
        out.append(f"| {k} | {v:+.2f}{marker} |")
    out.append("")

    # Per-scenario
    out.append("## Per-scenario")
    out.append("")
    out.append(f"| Scenario | {diff.label_a} | {diff.label_b} | Δ | Status |")
    out.append("|---|---|---|---|---|")
    for r in diff.scenarios:
        a = f"{r.a_score:.1f}" if r.a_score is not None else "—"
        b = f"{r.b_score:.1f}" if r.b_score is not None else "—"
        d = f"{r.delta:+.1f}" if r.delta is not None else "—"
        status_md = f"**{r.status}**" if r.status == "REGRESSION" else r.status
        out.append(f"| `{r.scenario_id}` | {a} | {b} | {d} | {status_md} |")
    out.append("")

    # Provenance
    out.append("## Run provenance")
    out.append("")
    out.append(f"- {diff.label_a}: `{diff.run_a_id}` → `{diff.run_a_dir}`")
    out.append(f"- {diff.label_b}: `{diff.run_b_id}` → `{diff.run_b_dir}`")
    out.append("")

    return "\n".join(out)

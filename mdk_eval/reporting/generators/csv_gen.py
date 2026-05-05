"""Scenario-level CSV writer."""
from __future__ import annotations

import csv
from pathlib import Path

from ...models import ScenarioAggregate, ScenarioRunResult


def write_scenarios_csv(
    path: Path,
    aggregates: list[ScenarioAggregate],
    runs_by_scenario: dict[str, list[ScenarioRunResult]],
) -> Path:
    cols = [
        "scenario_id",
        "severity",
        "runs",
        "pass_rate",
        "mean_score",
        "score_variance",
        "consistency_score",
        "drift_score",
        "failure_classes",
        "mean_latency_ms",
        "min_latency_ms",
        "max_latency_ms",
        "mean_retries",
    ]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for a in aggregates:
            runs = runs_by_scenario.get(a.scenario_id, [])
            lats = [r.adapter.trace.latency_ms for r in runs]
            rets = [r.adapter.trace.retries for r in runs]
            classes = sorted({f.failure_class.value for f in a.findings})
            w.writerow([
                a.scenario_id,
                a.severity,
                a.runs,
                a.pass_rate,
                a.mean_score,
                a.score_variance,
                a.consistency_score,
                a.drift_score,
                "|".join(classes),
                round(sum(lats) / max(len(lats), 1)) if lats else 0,
                min(lats) if lats else 0,
                max(lats) if lats else 0,
                round(sum(rets) / max(len(rets), 1), 2) if rets else 0,
            ])
    return path

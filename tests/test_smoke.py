"""End-to-end smoke test using the mock adapter. No network. No judges.

Verifies:
- run produces expected files (manifest, scorecard, html, csv, scenario detail, evaluation_summary)
- 10-category scorecard is present and bounded 0..100
- top-level overall_score / confidence / variance / status are produced
- deterministic gates correctly fail on engineered traps
- run is fully reproducible via versioning hashes
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from mdk_eval.config import AdapterConfig, RunConfig
from mdk_eval.models import SCORE_CATEGORIES, Readiness, FailureClass
from mdk_eval.runner.orchestrator import execute_run


@pytest.fixture
def cfg() -> RunConfig:
    out = tempfile.mkdtemp(prefix="mdk_eval_test_", dir="/tmp")
    return RunConfig(
        adapter=AdapterConfig(target="mock"),
        judges_enabled=False,
        pdf=False,
        runs_per_scenario=2,
        output_dir=out,
        dataset=str(Path(__file__).resolve().parents[1] / "datasets" / "sample.jsonl"),
    )


def test_e2e_mock_run(cfg: RunConfig) -> None:
    run_dir, report = asyncio.run(execute_run(cfg))

    # core artifacts
    for fname in (
        "manifest.json",
        "report.json",
        "report.html",
        "scenarios.csv",
        "aggregate.json",
        "dataset.snapshot.jsonl",
        "evaluation_summary.json",
    ):
        assert (run_dir / fname).exists(), f"missing {fname}"

    # one folder per scenario, one trace per run
    sdir = run_dir / "scenarios"
    counts = []
    for child in sdir.iterdir():
        runs_dir = child / "runs"
        assert runs_dir.is_dir()
        counts.append(len(list(runs_dir.iterdir())))
    assert all(c == cfg.runs_per_scenario for c in counts)

    # top-level numbers conform to contract
    assert 0.0 <= report.overall_score <= 100.0
    assert 0.0 <= report.confidence <= 1.0
    assert report.variance >= 0.0
    assert report.status in set(Readiness)

    # 10-category scorecard, each 0..100
    sc = report.scorecard.model_dump()
    for k in SCORE_CATEGORIES:
        assert k in sc, f"missing category {k}"
        assert 0.0 <= sc[k] <= 100.0

    # evaluation_summary.json conforms to the contract shape
    summary = json.loads((run_dir / "evaluation_summary.json").read_text())
    for k in ("overall_score", "confidence", "variance", "status", "scorecard"):
        assert k in summary
    assert set(summary["scorecard"].keys()) >= set(SCORE_CATEGORIES)

    # engineered traps must produce failures
    sids = {a.scenario_id: a for a in report.scenario_aggregates}
    assert sids["hallucination_trap"].pass_rate < 1.0
    assert sids["tool_required_lookup"].pass_rate < 1.0
    assert sids["schema_strict"].pass_rate < 1.0
    # latency_strict on a HIGH/CRITICAL would be a hard gate; here it's MEDIUM,
    # so the budget breach surfaces as a finding but may not fail the scenario.
    lat_findings = [f.failure_class for f in sids["latency_strict"].findings]
    assert FailureClass.LATENCY_ISSUE in lat_findings

    # versioning fields are populated
    m = report.manifest
    assert len(m.dataset_sha256) == 64
    assert len(m.config_sha256) == 64
    assert m.judge_prompts_sha256

    # readiness should not be PRODUCTION on a dataset full of traps
    assert report.status in {Readiness.NOT_READY, Readiness.NEEDS_IMPROVEMENT, Readiness.PILOT_READY}


def test_versioning_idempotency() -> None:
    from mdk_eval.scenarios import load_scenarios, snapshot_dataset
    from mdk_eval.storage.versioning import sha256_file, sha256_obj

    ds = Path(__file__).resolve().parents[1] / "datasets" / "sample.jsonl"
    out_a = Path("/tmp/_mdk_a.jsonl")
    out_b = Path("/tmp/_mdk_b.jsonl")
    snapshot_dataset(load_scenarios(ds), out_a)
    snapshot_dataset(load_scenarios(ds), out_b)
    assert sha256_file(out_a) == sha256_file(out_b)

    cfg = RunConfig(adapter=AdapterConfig(target="mock"))
    assert sha256_obj(cfg.model_dump()) == sha256_obj(cfg.model_dump())


def test_workflow_subsequence_check() -> None:
    from mdk_eval.evaluators.deterministic.checks import _is_subsequence

    assert _is_subsequence(["a", "b"], ["x", "a", "y", "b", "z"])
    assert not _is_subsequence(["b", "a"], ["x", "a", "y", "b", "z"])


def test_status_bands() -> None:
    from mdk_eval.models import status_for_score

    assert status_for_score(95.0) == Readiness.PRODUCTION_READY
    assert status_for_score(85.0) == Readiness.PILOT_READY
    assert status_for_score(75.0) == Readiness.NEEDS_IMPROVEMENT
    assert status_for_score(50.0) == Readiness.NOT_READY

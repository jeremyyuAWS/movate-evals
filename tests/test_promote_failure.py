"""mdk-eval promote-failure — HITL loop closure tests.

Covers:
- Happy path: failure produces a tightened scenario with new constraints
- Idempotency: failure whose constraints were already on the original is a no-op
- Worst-run picking when --run-index is omitted
- Provenance shape (note, captured_at, failure_classes, source ids)
- Error paths: passed scenario, missing scenario, missing run dir, target collision
- append_to_dataset creates parents, deduplicates by id, tolerates corrupt lines
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from mdk_eval.cli.promote import (
    PromoteError,
    append_to_dataset,
    promote_failure,
)


# ---------------------------------------------------------------- fixtures


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _make_run_dir(
    scenario: dict,
    eval_results: list[dict],   # one per run-index 0..N-1
    *,
    extra_scenarios: list[dict] | None = None,
) -> Path:
    """Build a minimal run dir matching what execute_run() emits.

    Layout:
      <root>/
        manifest.json
        dataset.snapshot.jsonl  (the snapshot containing `scenario` plus extras)
        scenarios/<scenario_id>/runs/<idx>/eval.json
    """
    root = Path(tempfile.mkdtemp(prefix="mdk_promote_", dir="/tmp"))
    (root / "manifest.json").write_text(json.dumps({"run_id": "test", "started_at": datetime.now().isoformat()}))

    rows = [scenario] + (extra_scenarios or [])
    _write_jsonl(root / "dataset.snapshot.jsonl", rows)

    sid = scenario["id"]
    for idx, ev in enumerate(eval_results):
        d = root / "scenarios" / sid / "runs" / str(idx)
        d.mkdir(parents=True, exist_ok=True)
        (d / "eval.json").write_text(json.dumps(ev))
    return root


def _minimal_scenario(**overrides) -> dict:
    """A bare scenario that's accepted by the Scenario model."""
    base = {
        "id": "halluc_trap",
        "tags": ["grounding"],
        "severity": "high",
        "input": {"prompt": "Who is the CEO?"},
        "context": ["The CEO is not listed in the provided documents."],
        "forbidden_phrases": [],
        "forbidden_claims": [],
        "required_fields": [],
        "expected_tools": [],
        "workflow": {"must_visit": [], "must_not_visit": [], "ordered_subsequence": None},
    }
    base.update(overrides)
    return base


def _eval_with_findings(score: float, findings: list[dict], output: str = "agent output") -> dict:
    return {
        "scenario_id": "halluc_trap",
        "run_index": 0,
        "trace_id": "halluc_trap::0",
        "adapter": {"ok": True, "output_text": output, "output_json": {"answer": output}},
        "deterministic": [],
        "judge_panel": [],
        "category_scores": {},
        "final_score": score,
        "passed": score >= 75 and not findings,
        "findings": findings,
        "duration_ms": 100,
    }


# ---------------------------------------------------------------- happy path


def test_safety_violation_adds_forbidden_phrases():
    """Agent said a forbidden phrase that wasn't on the original scenario yet —
    the new scenario should now ban that phrase explicitly."""
    scen = _minimal_scenario(forbidden_phrases=["preexisting_ban"])
    findings = [{
        "failure_class": "safety_violation",
        "reason": "hit forbidden phrase(s): ['definitively true']",
        "evidence": {"hits": ["definitively true"]},
        "severity": "high",
        "recommendation": "...",
    }]
    run_dir = _make_run_dir(scen, [_eval_with_findings(40.0, findings)])
    result = promote_failure(run_dir, "halluc_trap")

    new = result.new_scenario
    assert "preexisting_ban" in new["forbidden_phrases"]      # original preserved
    assert "definitively true" in new["forbidden_phrases"]   # new constraint added
    assert result.tightening.added_forbidden_phrases == ["definitively true"]
    assert "derived:from_failure" in new["tags"]
    assert new["id"] == "halluc_trap__from_failure_0"


def test_missing_step_adds_required_fields():
    scen = _minimal_scenario(required_fields=["answer"])
    findings = [{
        "failure_class": "missing_step",
        "reason": "missing: ['sources', 'confidence']",
        "evidence": {"missing": ["sources", "confidence"]},
        "severity": "high",
        "recommendation": "...",
    }]
    run_dir = _make_run_dir(scen, [_eval_with_findings(50.0, findings)])
    result = promote_failure(run_dir, "halluc_trap")

    new = result.new_scenario
    assert set(new["required_fields"]) == {"answer", "sources", "confidence"}
    assert set(result.tightening.added_required_fields) == {"sources", "confidence"}


def test_hallucination_captures_output_excerpt_as_forbidden_claim():
    scen = _minimal_scenario()
    findings = [{
        "failure_class": "hallucination",
        "reason": "...",
        "evidence": {},
        "severity": "high",
        "recommendation": "...",
    }]
    eval_obj = _eval_with_findings(30.0, findings, output="The CEO is Jane Smith, confirmed yesterday.")
    run_dir = _make_run_dir(scen, [eval_obj])
    result = promote_failure(run_dir, "halluc_trap")

    assert len(result.new_scenario["forbidden_claims"]) == 1
    claim = result.new_scenario["forbidden_claims"][0]
    assert "Jane Smith" in claim
    assert claim.startswith("Repeating the prior hallucination")


# ---------------------------------------------------------------- idempotency


def test_already_caught_constraints_are_not_duplicated():
    """When the original scenario's constraints already caught the failure,
    promotion is a no-op for that constraint type — but the new scenario is
    still produced (with provenance) for the regression-suite copy."""
    scen = _minimal_scenario(
        forbidden_phrases=["definitively true"],   # already banned
        required_fields=["answer", "sources"],     # already required
    )
    findings = [
        {"failure_class": "safety_violation", "reason": "...", "evidence": {"hits": ["definitively true"]}, "severity": "high", "recommendation": "..."},
        {"failure_class": "missing_step", "reason": "...", "evidence": {"missing": ["sources"]}, "severity": "high", "recommendation": "..."},
    ]
    run_dir = _make_run_dir(scen, [_eval_with_findings(56.1, findings)])
    result = promote_failure(run_dir, "halluc_trap")

    # No additions — idempotent
    assert result.tightening.added_forbidden_phrases == []
    assert result.tightening.added_required_fields == []
    # Original still single-ban'd
    assert result.new_scenario["forbidden_phrases"].count("definitively true") == 1
    assert result.new_scenario["required_fields"].count("sources") == 1


# ---------------------------------------------------------------- worst-run picking


def test_worst_run_is_picked_when_run_index_omitted():
    scen = _minimal_scenario()
    findings = [{"failure_class": "safety_violation", "reason": "...", "evidence": {"hits": ["bad"]}, "severity": "high", "recommendation": "..."}]
    runs = [
        _eval_with_findings(80.0, []),                   # passed
        _eval_with_findings(20.0, findings),             # WORST — should be picked
        _eval_with_findings(60.0, findings),
    ]
    run_dir = _make_run_dir(scen, runs)
    result = promote_failure(run_dir, "halluc_trap")
    assert result.source_run_index == 1
    assert result.source_final_score == 20.0


def test_explicit_run_index_overrides_worst_picking():
    scen = _minimal_scenario()
    findings = [{"failure_class": "safety_violation", "reason": "...", "evidence": {"hits": ["bad"]}, "severity": "high", "recommendation": "..."}]
    runs = [
        _eval_with_findings(20.0, findings),
        _eval_with_findings(60.0, findings),
    ]
    run_dir = _make_run_dir(scen, runs)
    result = promote_failure(run_dir, "halluc_trap", run_index=1)
    assert result.source_run_index == 1
    assert result.source_final_score == 60.0


# ---------------------------------------------------------------- provenance


def test_provenance_metadata_is_complete():
    scen = _minimal_scenario()
    findings = [
        {"failure_class": "safety_violation", "reason": "...", "evidence": {"hits": ["bad"]}, "severity": "high", "recommendation": "..."},
        {"failure_class": "missing_step", "reason": "...", "evidence": {"missing": ["x"]}, "severity": "high", "recommendation": "..."},
    ]
    run_dir = _make_run_dir(scen, [_eval_with_findings(40.0, findings)])
    result = promote_failure(run_dir, "halluc_trap", note="customer reported this in prod")

    derived = result.new_scenario["meta"]["derived_from"]
    assert derived["source"] == "promote-failure"
    assert derived["from_scenario_id"] == "halluc_trap"
    assert derived["from_run_index"] == 0
    assert derived["final_score"] == 40.0
    assert set(derived["failure_classes"]) == {"safety_violation", "missing_step"}
    assert derived["note"] == "customer reported this in prod"
    assert "captured_at" in derived
    assert derived["from_run_dir"] == str(run_dir)


def test_override_id_used_when_provided():
    scen = _minimal_scenario()
    findings = [{"failure_class": "safety_violation", "reason": "...", "evidence": {"hits": ["bad"]}, "severity": "high", "recommendation": "..."}]
    run_dir = _make_run_dir(scen, [_eval_with_findings(40.0, findings)])
    result = promote_failure(run_dir, "halluc_trap", override_id="my_regression_test")
    assert result.new_scenario["id"] == "my_regression_test"


# ---------------------------------------------------------------- error paths


def test_passed_scenario_with_no_findings_refuses_promotion():
    scen = _minimal_scenario()
    run_dir = _make_run_dir(scen, [_eval_with_findings(95.0, [])])
    with pytest.raises(PromoteError, match="passed with no findings"):
        promote_failure(run_dir, "halluc_trap")


def test_missing_scenario_id_lists_available():
    scen = _minimal_scenario(id="present")
    findings = [{"failure_class": "safety_violation", "reason": "...", "evidence": {"hits": ["bad"]}, "severity": "high", "recommendation": "..."}]
    eval_obj = {**_eval_with_findings(40.0, findings), "scenario_id": "present"}
    # write run files for 'present' scenario
    root = Path(tempfile.mkdtemp(prefix="mdk_promote_", dir="/tmp"))
    (root / "manifest.json").write_text("{}")
    _write_jsonl(root / "dataset.snapshot.jsonl", [scen])
    d = root / "scenarios" / "present" / "runs" / "0"
    d.mkdir(parents=True)
    (d / "eval.json").write_text(json.dumps(eval_obj))

    with pytest.raises(PromoteError, match="not found.*Available: present"):
        promote_failure(root, "absent")


def test_not_a_run_directory():
    empty = Path(tempfile.mkdtemp(prefix="mdk_empty_", dir="/tmp"))
    with pytest.raises(PromoteError, match="Not a run directory"):
        promote_failure(empty, "anything")


def test_explicit_run_index_that_doesnt_exist():
    scen = _minimal_scenario()
    findings = [{"failure_class": "safety_violation", "reason": "...", "evidence": {"hits": ["bad"]}, "severity": "high", "recommendation": "..."}]
    run_dir = _make_run_dir(scen, [_eval_with_findings(40.0, findings)])
    with pytest.raises(PromoteError, match="No eval data for run-index 7"):
        promote_failure(run_dir, "halluc_trap", run_index=7)


# ---------------------------------------------------------------- append_to_dataset


def test_append_creates_parent_dir_and_writes_jsonl():
    target = Path(tempfile.mkdtemp(prefix="mdk_promote_", dir="/tmp")) / "nested" / "out.jsonl"
    scen = _minimal_scenario(id="new_thing")
    append_to_dataset(target, scen)
    rows = [json.loads(line) for line in target.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["id"] == "new_thing"


def test_append_detects_id_collision():
    target = Path(tempfile.mkdtemp(prefix="mdk_promote_", dir="/tmp")) / "out.jsonl"
    scen = _minimal_scenario(id="collide")
    append_to_dataset(target, scen)
    with pytest.raises(PromoteError, match="already exists"):
        append_to_dataset(target, _minimal_scenario(id="collide"))


def test_append_tolerates_corrupt_existing_lines():
    target = Path(tempfile.mkdtemp(prefix="mdk_promote_", dir="/tmp")) / "out.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Corrupt line + valid line. We want append to NOT crash on the corrupt
    # one but still detect collision against the valid one.
    target.write_text("not valid json\n" + json.dumps({"id": "valid_existing"}) + "\n")
    # New id — should append cleanly
    append_to_dataset(target, _minimal_scenario(id="brand_new"))
    # Collision id — should error
    with pytest.raises(PromoteError, match="already exists"):
        append_to_dataset(target, _minimal_scenario(id="valid_existing"))

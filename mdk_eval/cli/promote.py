"""Promote a failing scenario from a saved run into a tightened test in a dataset.

This module is the heart of `mdk-eval promote-failure` — the HITL loop closure
the whiteboard centers on. A delivery engineer reviewing a failed run can pick
the failed scenario and promote it into a regression dataset; the new scenario
is *tightened* against the specific failure mode (forbidden phrases the agent
said, required fields it omitted, claims it hallucinated) so the agent can
never silently regress to that failure.

The CLI wrapper in `cli/app.py` keeps Typer / I/O concerns out of here so the
core logic is unit-testable without spinning up the CLI machinery.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..models import Scenario


class PromoteError(ValueError):
    """Raised on any pre-write validation failure (scenario not found, didn't
    actually fail, target id collision, etc.). Carries a human-readable
    message; the CLI prints it and exits non-zero."""


class TighteningSummary(BaseModel):
    """Per-bucket diff between the original scenario and the promoted one.

    The CLI prints this when `--show-tightening` is passed so the human
    reviewer sees exactly what the new scenario will lock in before they
    decide to commit it to a regression dataset.
    """
    added_forbidden_phrases: list[str] = []
    added_forbidden_claims: list[str] = []
    added_required_fields: list[str] = []
    notes: list[str] = []   # human-readable explanations of judgement calls


class PromoteResult(BaseModel):
    new_scenario: dict[str, Any]      # the full new Scenario as JSON-ready dict
    tightening: TighteningSummary
    source_run_dir: str
    source_scenario_id: str
    source_run_index: int
    source_final_score: float
    source_failure_classes: list[str]


# ---------------------------------------------------------------- helpers


def _load_dataset_snapshot(run_dir: Path) -> list[dict[str, Any]]:
    """Read dataset.snapshot.jsonl back into a list of scenario dicts."""
    snap = run_dir / "dataset.snapshot.jsonl"
    if not snap.exists():
        raise PromoteError(f"No dataset.snapshot.jsonl in {run_dir} — can't recover the original scenario.")
    out = []
    with open(snap) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _list_run_indices(scenario_dir: Path) -> list[int]:
    """All run-index subdirs containing eval.json, sorted ascending."""
    runs_dir = scenario_dir / "runs"
    if not runs_dir.exists():
        return []
    out = []
    for child in runs_dir.iterdir():
        if child.is_dir() and (child / "eval.json").exists():
            try:
                out.append(int(child.name))
            except ValueError:
                continue
    return sorted(out)


def _pick_worst_run(scenario_dir: Path) -> tuple[int, dict[str, Any]]:
    """Return the run-index whose eval.json has the lowest final_score, plus
    that eval. Ties: lowest run-index wins (deterministic)."""
    indices = _list_run_indices(scenario_dir)
    if not indices:
        raise PromoteError(f"No run results found under {scenario_dir / 'runs'}")
    best_idx, best_eval, best_score = None, None, None
    for idx in indices:
        ev = json.loads((scenario_dir / "runs" / str(idx) / "eval.json").read_text())
        score = float(ev.get("final_score", 0.0))
        if best_score is None or score < best_score:
            best_idx, best_eval, best_score = idx, ev, score
    return best_idx, best_eval  # type: ignore[return-value]


# ---------------------------------------------------------------- tightening


# Failure-class → tightening rule. Each rule reads the finding's `evidence`
# block and returns lists to extend on the new scenario.
def _tighten(
    original: dict[str, Any],
    eval_obj: dict[str, Any],
) -> tuple[dict[str, Any], TighteningSummary]:
    """Build a tightened copy of `original` based on the failures in `eval_obj`.

    Returns (new_scenario_dict, tightening_summary). Idempotent: tightening
    constraints already present on the original are NOT duplicated.
    """
    new = json.loads(json.dumps(original))  # deep copy via JSON roundtrip
    summary = TighteningSummary()

    findings = eval_obj.get("findings", []) or []
    adapter = eval_obj.get("adapter", {}) or {}
    output_text = (adapter.get("output_text") or "")[:500]

    # Existing constraints (lowercased / set-like for dedup)
    existing_phrases = {p.lower() for p in original.get("forbidden_phrases", [])}
    existing_claims_lc = {c.lower() for c in original.get("forbidden_claims", [])}
    existing_required = set(original.get("required_fields", []))

    for finding in findings:
        cls = finding.get("failure_class", "")
        evidence = finding.get("evidence") or {}

        if cls == "safety_violation":
            # The forbidden_phrases check produced 'hits' — make sure those
            # phrases stay banned even if the agent's other constraints loosen.
            for hit in evidence.get("hits", []) or []:
                if hit.lower() not in existing_phrases:
                    new.setdefault("forbidden_phrases", []).append(hit)
                    summary.added_forbidden_phrases.append(hit)
                    existing_phrases.add(hit.lower())

        elif cls == "missing_step":
            # Required-field check found omissions — promote them to required.
            for missing in evidence.get("missing", []) or []:
                if missing not in existing_required:
                    new.setdefault("required_fields", []).append(missing)
                    summary.added_required_fields.append(missing)
                    existing_required.add(missing)

        elif cls == "hallucination":
            # Hallucinated claim — capture a short evidence excerpt as a
            # forbidden_claim. Judges check forbidden_claims semantically, so
            # a paraphrase of the wrong answer is more useful than a string
            # match. Use the agent's own output as the 'claim to forbid'.
            excerpt = output_text.strip()
            if excerpt:
                claim = f"Repeating the prior hallucination: {excerpt[:200]}"
                if claim.lower() not in existing_claims_lc:
                    new.setdefault("forbidden_claims", []).append(claim)
                    summary.added_forbidden_claims.append(claim)
                    existing_claims_lc.add(claim.lower())

        # Other classes (tool_failure, latency, etc.) tighten less cleanly —
        # the original scenario's hard checks already encode them and re-running
        # is the right verification. Note this so the reviewer understands why
        # we didn't add anything new.
        elif cls in {"tool_failure", "wrong_tool", "latency_breach", "schema_violation"}:
            summary.notes.append(
                f"Failure class '{cls}' is enforced by existing scenario checks; "
                f"no new constraint added (the rerun catches it)."
            )

    return new, summary


# ---------------------------------------------------------------- public API


def promote_failure(
    run_dir: Path,
    scenario_id: str,
    *,
    run_index: int | None = None,
    note: str | None = None,
    override_id: str | None = None,
) -> PromoteResult:
    """Build a promoted scenario dict from a saved run.

    Parameters
    ----------
    run_dir : Path
        A saved run directory (must contain manifest.json + dataset.snapshot.jsonl
        + scenarios/<id>/runs/<idx>/eval.json).
    scenario_id : str
        Which scenario in the run to promote.
    run_index : int | None
        Specific run index to use; defaults to the worst-scoring run.
    note : str | None
        Free-text note attached to meta.derived_from.note (audit trail).
    override_id : str | None
        Override the generated new-scenario id. Useful for stable test fixtures.

    Returns
    -------
    PromoteResult — call sites then choose where to write (file / stdout).

    Raises
    ------
    PromoteError on validation failure. The caller is expected to surface the
    error message verbatim.
    """
    if not (run_dir / "manifest.json").exists():
        raise PromoteError(f"Not a run directory (no manifest.json): {run_dir}")

    # Locate the original scenario by id in the snapshot.
    snapshot = _load_dataset_snapshot(run_dir)
    original = next((s for s in snapshot if s.get("id") == scenario_id), None)
    if original is None:
        ids = sorted({s.get("id") for s in snapshot if s.get("id")})
        raise PromoteError(
            f"Scenario id '{scenario_id}' not found in {run_dir}. "
            f"Available: {', '.join(ids) or '(none)'}"
        )

    scen_dir = run_dir / "scenarios" / scenario_id
    if not scen_dir.exists():
        raise PromoteError(f"No per-scenario eval data at {scen_dir}")

    # Resolve which run to read.
    if run_index is None:
        idx, eval_obj = _pick_worst_run(scen_dir)
    else:
        eval_path = scen_dir / "runs" / str(run_index) / "eval.json"
        if not eval_path.exists():
            raise PromoteError(f"No eval data for run-index {run_index} at {eval_path}")
        idx, eval_obj = run_index, json.loads(eval_path.read_text())

    # Refuse to promote a passing run — the user almost certainly didn't mean
    # to. Better to fail loudly than silently emit a useless tightening.
    if eval_obj.get("passed", False) and not (eval_obj.get("findings") or []):
        raise PromoteError(
            f"Scenario '{scenario_id}' run {idx} passed with no findings — "
            f"there's nothing to tighten against. If you really want to copy "
            f"the scenario verbatim into another dataset, use a plain `cp` or "
            f"jq filter on dataset.snapshot.jsonl instead."
        )

    new_scenario, tightening = _tighten(original, eval_obj)

    # New id: deterministic suffix so promoting the same failure twice produces
    # the same id (caller will hit a target-collision check rather than silently
    # add duplicate tests).
    if override_id:
        new_scenario["id"] = override_id
    else:
        new_scenario["id"] = f"{scenario_id}__from_failure_{idx}"

    # Tags
    tags = list(new_scenario.get("tags") or [])
    if "derived:from_failure" not in tags:
        tags.append("derived:from_failure")
    new_scenario["tags"] = tags

    # Provenance — full enough to retrace exactly which run+index this came from.
    meta = dict(new_scenario.get("meta") or {})
    derived = dict(meta.get("derived_from") or {})
    derived.update({
        "source": "promote-failure",
        "from_run_dir": str(run_dir),
        "from_scenario_id": scenario_id,
        "from_run_index": idx,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "final_score": float(eval_obj.get("final_score", 0.0)),
        "failure_classes": [
            f.get("failure_class") for f in (eval_obj.get("findings") or []) if f.get("failure_class")
        ],
    })
    if note:
        derived["note"] = note
    meta["derived_from"] = derived
    new_scenario["meta"] = meta

    # Validate via Pydantic — protects against any tightening that produced a
    # malformed scenario. Round-trip ensures the dict matches what loaders expect.
    Scenario(**new_scenario)

    return PromoteResult(
        new_scenario=new_scenario,
        tightening=tightening,
        source_run_dir=str(run_dir),
        source_scenario_id=scenario_id,
        source_run_index=idx,
        source_final_score=float(eval_obj.get("final_score", 0.0)),
        source_failure_classes=derived["failure_classes"],
    )


def append_to_dataset(target: Path, scenario: dict[str, Any]) -> None:
    """Append a scenario JSONL line to `target`. Creates the file (and parent
    dirs) if missing. Detects id collision against existing rows."""
    if target.exists():
        existing_ids = set()
        with open(target) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        existing_ids.add(json.loads(line).get("id"))
                    except json.JSONDecodeError:
                        # Tolerate a corrupt line in the dataset rather than crash —
                        # the user can fix it independently. We still warn via raise
                        # if the new id collides with anything we DID parse.
                        continue
        if scenario["id"] in existing_ids:
            raise PromoteError(
                f"Scenario id '{scenario['id']}' already exists in {target}. "
                f"Pass --id <slug> to use a different id."
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a") as f:
        f.write(json.dumps(scenario) + "\n")

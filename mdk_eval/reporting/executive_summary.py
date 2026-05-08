"""Executive-summary JSON shape for the manager's diagram.

The manager's vision (per the process diagram they shared) is a single JSON
output with three things:

  1. **Expected vs Actual** per test case — what the agent should have said
     vs what it actually said, and whether they match.
  2. **Correctness** — a single 0–1 number that says "how often is the agent
     factually right."
  3. **Accuracy** — a single 0–1 number that combines correctness with
     groundedness ("is the agent's claim backed by source data?").

This module derives that shape from the existing RunReport + per-scenario
artifacts. No new evaluation logic — it's a different *projection* of data
the orchestrator already produced. Same data, different audience.

Field shape
-----------
{
  "schema_version": "1.0",
  "generated_at": "<ISO8601>",
  "agent": {
    "id": "<backend agent_id or run target>",
    "definition_sha256": "<sha of the source agent.json if known>",
    "backend": "lyzr" | "mock" | ...
  },
  "summary": {
    "test_cases_evaluated": 6,
    "test_cases_passed": 4,
    "test_cases_failed": 2,
    "correctness": 0.84,            # 0–1
    "accuracy": 0.79,               # 0–1
    "judges_enabled": true
  },
  "expected_vs_actual": [
    {
      "test_case_id": "happy_path_qna",
      "input": "What is the maximum file upload size for the customer portal?",
      "expected": "25 MB per file. Allowed formats include PDF, DOCX, PNG, JPG.",
      "actual":   "Per the available context: The Customer Portal accepts file uploads up to 25 MB...",
      "match": true,
      "match_score": 0.92,         # 0–1, derived from per-scenario final_score / 100
      "match_reason": "All checks passed.",
      "severity": "medium",
      "latency_ms": 340
    }
  ],
  "provenance": {
    "schema_version_eval": "1.0",   # from evaluation_summary.json
    "methodology_version": "1.0",
    "mdk_eval_version": "0.1.0",
    "manifest_sha256": "...",
    "dataset_sha256": "..."
  }
}

Notes on field derivation
-------------------------
- `expected`: the scenario's `expected_output` field if set, else a synthesized
  string from `description` + `required_fields` (so every test case has
  *some* statement of intent, even if loose). If we can't synthesize, the
  field is the literal string "(no expected output declared in scenario)".
- `actual`: the agent's output_text from the representative run.
- `match`: scenario.passed (the boolean across all check layers).
- `match_score`: scenario.mean_score / 100, so it's directly comparable to
  the manager's 0–1 "Correctness" / "Accuracy" framing.
- `match_reason`: the top finding's reason if the scenario failed; "All
  checks passed." if it didn't.
- `correctness`: scorecard.correctness / 100. Falls to 0 if judges off.
- `accuracy`: (scorecard.correctness + scorecard.grounding) / 200, which is
  the "factually right AND grounded" composite the manager wants.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "1.0"


def _expected_for(scenario_payload: dict[str, Any]) -> str:
    """Best-effort 'expected' field. Tries multiple sources before giving up."""
    eo = scenario_payload.get("expected_output")
    if eo:
        return str(eo)
    parts: list[str] = []
    desc = scenario_payload.get("description")
    if desc:
        parts.append(str(desc))
    required = scenario_payload.get("required_fields") or []
    if required:
        parts.append(f"Output must contain fields: {', '.join(required)}")
    forbidden = scenario_payload.get("forbidden_phrases") or []
    if forbidden:
        parts.append(f"Output must NOT contain: {', '.join(forbidden)}")
    if parts:
        return " · ".join(parts)
    return "(no expected output declared in scenario)"


def _input_for(scenario_payload: dict[str, Any]) -> str:
    """Surface the user-visible prompt or first-turn message."""
    inp = scenario_payload.get("input") or {}
    if isinstance(inp, dict):
        if "turns" in inp and isinstance(inp["turns"], list) and inp["turns"]:
            # Multi-turn: show all turns joined; the manager's UI can wrap.
            return " | ".join(str(t) for t in inp["turns"])
        return str(inp.get("prompt") or inp.get("input") or inp)
    return str(inp)


def _match_reason(passed: bool, findings: list[dict[str, Any]] | None) -> str:
    if passed:
        return "All checks passed."
    if not findings:
        return "Failed checks but no specific finding recorded."
    top = findings[0]
    cls = top.get("failure_class") or "unknown"
    reason = top.get("reason") or top.get("recommendation") or ""
    if reason:
        return f"{cls}: {reason}"
    return f"Failure class: {cls}"


def _scenario_payload_lookup(dataset_snapshot_lines: list[str]) -> dict[str, dict[str, Any]]:
    """Build a {scenario_id -> payload} map from the dataset snapshot JSONL."""
    import json as _json
    out: dict[str, dict[str, Any]] = {}
    for line in dataset_snapshot_lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        sid = payload.get("id")
        if sid:
            out[sid] = payload
    return out


def build(
    *,
    report: Any,
    runs_by_scenario: dict[str, list[Any]],
    dataset_snapshot_lines: list[str] | None = None,
    backend_agent_id: str | None = None,
    agent_definition_sha256: str | None = None,
) -> dict[str, Any]:
    """Produce the executive-summary dict from a RunReport + per-scenario data.

    `dataset_snapshot_lines` is the contents of the run's `dataset.snapshot.jsonl`
    split into lines — the only source of truth for a scenario's `expected_output`,
    `description`, `forbidden_phrases`, etc. (the RunReport doesn't carry them).
    """
    payloads = _scenario_payload_lookup(dataset_snapshot_lines or [])

    manifest = report.manifest
    sc = report.scorecard

    # Correctness + Accuracy (0–1 scale, matches the manager's diagram)
    correctness = float(getattr(sc, "correctness", 0)) / 100.0
    grounding = float(getattr(sc, "grounding", 0)) / 100.0
    accuracy = round((correctness + grounding) / 2.0, 4)
    correctness = round(correctness, 4)

    # Per-scenario rows
    rows: list[dict[str, Any]] = []
    aggregates = getattr(report, "scenario_aggregates", []) or []
    for agg in aggregates:
        sid = agg.scenario_id if hasattr(agg, "scenario_id") else agg["scenario_id"]
        runs = runs_by_scenario.get(sid) or []
        # Pick the representative run — the failed one if any, else the first.
        rep = None
        for r in runs:
            if not getattr(r, "passed", True):
                rep = r
                break
        if rep is None and runs:
            rep = runs[0]

        actual = ""
        latency_ms: int | None = None
        if rep is not None:
            adapter = getattr(rep, "adapter", None)
            if adapter is not None:
                actual = getattr(adapter, "output_text", "") or ""
                trace = getattr(adapter, "trace", None)
                if trace is not None:
                    latency_ms = getattr(trace, "latency_ms", None)

        payload = payloads.get(sid, {})
        passed = bool(getattr(agg, "pass_rate", 0) >= 0.8)
        findings = getattr(agg, "findings", None) or []
        # Findings on the aggregate are Pydantic models; convert to dicts for inspection.
        findings_dicts = [
            (f.model_dump(mode="json") if hasattr(f, "model_dump") else dict(f))
            for f in findings
        ]
        mean_score = float(getattr(agg, "mean_score", 0))

        rows.append({
            "test_case_id": sid,
            "input": _input_for(payload),
            "expected": _expected_for(payload),
            "actual": actual,
            "match": passed,
            "match_score": round(mean_score / 100.0, 4),
            "match_reason": _match_reason(passed, findings_dicts),
            "severity": str(payload.get("severity") or "medium"),
            "latency_ms": latency_ms,
        })

    passing = sum(1 for r in rows if r["match"])
    failing = len(rows) - passing

    judges_enabled = bool(getattr(manifest, "judges_enabled", []))

    # Provenance — pulled directly from manifest fields. Falls back gracefully
    # when older runs don't have the v1.0+ fields.
    provenance = {
        "schema_version_eval": "1.0",
        "methodology_version": "1.0",
        "mdk_eval_version": getattr(manifest, "mdk_eval_version", "unknown"),
        "manifest_sha256": getattr(manifest, "manifest_sha256", None),
        "dataset_sha256": getattr(manifest, "dataset_sha256", None),
        "config_sha256": getattr(manifest, "config_sha256", None),
        "judge_models": getattr(manifest, "judge_models", {}) or {},
        "runs_per_scenario": getattr(manifest, "runs_per_scenario", 1),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "agent": {
            "id": backend_agent_id or getattr(manifest, "endpoint", None) or getattr(manifest, "target", "unknown"),
            "definition_sha256": agent_definition_sha256,
            "backend": getattr(manifest, "target", "unknown"),
        },
        "summary": {
            "test_cases_evaluated": len(rows),
            "test_cases_passed": passing,
            "test_cases_failed": failing,
            "correctness": correctness,
            "accuracy": accuracy,
            "judges_enabled": judges_enabled,
        },
        "expected_vs_actual": rows,
        "provenance": provenance,
    }


def write(out_path, **kwargs) -> None:
    """Convenience: build() then dump as pretty JSON."""
    import json as _json
    from pathlib import Path

    data = build(**kwargs)
    Path(out_path).write_text(_json.dumps(data, indent=2, default=str), encoding="utf-8")

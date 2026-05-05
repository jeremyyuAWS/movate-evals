"""Deterministic, non-LLM checks. These run first and gate downstream evaluation.

Every check returns a DeterministicCheckResult so the aggregator/report can render
them uniformly.
"""
from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator

from ...models import (
    AdapterResult,
    DeterministicCheckResult,
    Scenario,
    Severity,
)
from ...utils.jsonpath import get_path


# ----------------------------- individual checks -----------------------------


def check_adapter_ok(scenario: Scenario, result: AdapterResult) -> DeterministicCheckResult:
    return DeterministicCheckResult(
        name="adapter_ok",
        passed=result.ok,
        score=1.0 if result.ok else 0.0,
        severity=Severity.CRITICAL,
        reason=None if result.ok else (result.error or "adapter call failed"),
    )


def check_schema(scenario: Scenario, result: AdapterResult) -> DeterministicCheckResult:
    if not scenario.expected_schema:
        return DeterministicCheckResult(
            name="schema", passed=True, score=1.0, severity=Severity.LOW, reason="no schema configured"
        )
    payload = result.output_json if result.output_json is not None else {"text": result.output_text}
    validator = Draft202012Validator(scenario.expected_schema)
    errs = sorted(validator.iter_errors(payload), key=lambda e: e.path)
    if not errs:
        return DeterministicCheckResult(
            name="schema", passed=True, score=1.0, severity=Severity.HIGH
        )
    return DeterministicCheckResult(
        name="schema",
        passed=False,
        score=0.0,
        severity=Severity.HIGH,
        reason=f"{len(errs)} schema violation(s)",
        details={"errors": [{"path": list(e.path), "msg": e.message} for e in errs[:8]]},
    )


def check_required_fields(scenario: Scenario, result: AdapterResult) -> DeterministicCheckResult:
    if not scenario.required_fields:
        return DeterministicCheckResult(
            name="required_fields", passed=True, score=1.0, severity=Severity.LOW, reason="none required"
        )
    payload = result.output_json or {"text": result.output_text}
    missing: list[str] = []
    for field in scenario.required_fields:
        if get_path(payload, field, default=None) in (None, "", [], {}):
            missing.append(field)
    passed = not missing
    return DeterministicCheckResult(
        name="required_fields",
        passed=passed,
        score=1.0 - len(missing) / max(len(scenario.required_fields), 1),
        severity=Severity.HIGH,
        reason=None if passed else f"missing: {missing}",
        details={"missing": missing},
    )


def check_forbidden_phrases(scenario: Scenario, result: AdapterResult) -> DeterministicCheckResult:
    if not scenario.forbidden_phrases:
        return DeterministicCheckResult(
            name="forbidden_phrases", passed=True, score=1.0, severity=Severity.LOW, reason="none defined"
        )
    text = (result.output_text or "").lower()
    hits = [p for p in scenario.forbidden_phrases if p.lower() in text]
    return DeterministicCheckResult(
        name="forbidden_phrases",
        passed=not hits,
        score=0.0 if hits else 1.0,
        severity=Severity.HIGH,
        reason=None if not hits else f"hit forbidden phrase(s): {hits}",
        details={"hits": hits},
    )


def check_tool_usage(scenario: Scenario, result: AdapterResult) -> DeterministicCheckResult:
    if not scenario.expected_tools:
        return DeterministicCheckResult(
            name="tool_usage", passed=True, score=1.0, severity=Severity.LOW, reason="no tool expectations"
        )
    called = {tc.name: tc for tc in result.trace.tool_calls}
    misses: list[str] = []
    arg_misses: list[dict[str, Any]] = []
    for spec in scenario.expected_tools:
        if spec.required and spec.name not in called:
            misses.append(spec.name)
            continue
        if spec.args_contains and spec.name in called:
            args = called[spec.name].args or {}
            for k, v in spec.args_contains.items():
                if str(args.get(k, "")) != str(v):
                    arg_misses.append({"tool": spec.name, "arg": k, "expected": v, "got": args.get(k)})
    total = len(scenario.expected_tools) or 1
    score = 1.0 - (len(misses) + 0.25 * len(arg_misses)) / total
    score = max(0.0, score)
    passed = not misses and not arg_misses
    return DeterministicCheckResult(
        name="tool_usage",
        passed=passed,
        score=score,
        severity=Severity.HIGH,
        reason=None if passed else f"missing tools={misses}, arg mismatches={len(arg_misses)}",
        details={"missing": misses, "arg_mismatches": arg_misses, "called": list(called.keys())},
    )


def check_workflow(scenario: Scenario, result: AdapterResult) -> DeterministicCheckResult:
    expect = scenario.workflow
    visited = result.trace.workflow_path
    visited_set = {_node_base(n) for n in visited}
    missed = [n for n in expect.must_visit if n not in visited_set]
    forbidden = [n for n in expect.must_not_visit if n in visited_set]
    order_ok = True
    if expect.ordered_subsequence:
        order_ok = _is_subsequence(expect.ordered_subsequence, [_node_base(n) for n in visited])

    passed = not missed and not forbidden and order_ok
    reason = []
    if missed:
        reason.append(f"did not visit {missed}")
    if forbidden:
        reason.append(f"visited forbidden {forbidden}")
    if not order_ok:
        reason.append(f"ordered subsequence {expect.ordered_subsequence} not satisfied")

    return DeterministicCheckResult(
        name="workflow_adherence",
        passed=passed,
        score=1.0 if passed else 0.0,
        severity=Severity.MEDIUM,
        reason="; ".join(reason) or None,
        details={"visited": visited},
    )


def check_latency(scenario: Scenario, result: AdapterResult) -> DeterministicCheckResult:
    budget = scenario.latency_budget_ms
    actual = result.trace.latency_ms
    if budget is None:
        return DeterministicCheckResult(
            name="latency", passed=True, score=1.0, severity=Severity.LOW,
            reason="no budget", details={"latency_ms": actual},
        )
    passed = actual <= budget
    over = max(0, actual - budget)
    score = 1.0 if passed else max(0.0, 1.0 - over / max(budget, 1))
    return DeterministicCheckResult(
        name="latency",
        passed=passed,
        score=score,
        severity=Severity.MEDIUM,
        reason=None if passed else f"latency {actual}ms over budget {budget}ms (+{over}ms)",
        details={"latency_ms": actual, "budget_ms": budget},
    )


def check_retries(scenario: Scenario, result: AdapterResult) -> DeterministicCheckResult:
    cap = scenario.max_retries
    actual = result.trace.retries
    if cap is None:
        return DeterministicCheckResult(
            name="retries", passed=True, score=1.0, severity=Severity.LOW,
            reason="no cap", details={"retries": actual},
        )
    passed = actual <= cap
    return DeterministicCheckResult(
        name="retries",
        passed=passed,
        score=1.0 if passed else 0.0,
        severity=Severity.MEDIUM,
        reason=None if passed else f"retries {actual} > cap {cap}",
        details={"retries": actual, "cap": cap},
    )


# ----------------------------- runner -----------------------------


def run_all(scenario: Scenario, result: AdapterResult) -> list[DeterministicCheckResult]:
    return [
        check_adapter_ok(scenario, result),
        check_schema(scenario, result),
        check_required_fields(scenario, result),
        check_forbidden_phrases(scenario, result),
        check_tool_usage(scenario, result),
        check_workflow(scenario, result),
        check_latency(scenario, result),
        check_retries(scenario, result),
    ]


def gates_failed(checks: list[DeterministicCheckResult]) -> bool:
    """True iff a CRITICAL check failed (e.g. adapter call itself)."""
    return any((not c.passed) and c.severity == Severity.CRITICAL for c in checks)


# ----------------------------- helpers -----------------------------


_NODE_PREFIX = re.compile(r"^(tool|agent|node):")


def _node_base(n: str) -> str:
    return _NODE_PREFIX.sub("", n)


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(any(item == h for h in it) for item in needle)

"""Optional DeepEval bridge. If deepeval isn't installed, returns empty dict.

We treat DeepEval as a complementary metric source (G-Eval, task completion,
hallucination, answer relevance), not the primary judge — our multi-role panel is
authoritative.
"""
from __future__ import annotations

from typing import Any

from ...config import JudgesConfig
from ...models import AdapterResult, Scenario


def _has_deepeval() -> bool:
    try:
        import deepeval  # noqa: F401
        return True
    except Exception:
        return False


def run_deepeval(scenario: Scenario, result: AdapterResult, cfg: JudgesConfig) -> dict[str, float]:
    """Returns {metric_name: score in [0,1]}. Best-effort; never raises."""
    if not _has_deepeval() or not result.ok:
        return {}
    try:
        from deepeval.test_case import LLMTestCase  # type: ignore
        from deepeval.metrics import (  # type: ignore
            GEval,
            HallucinationMetric,
            AnswerRelevancyMetric,
            TaskCompletionMetric,
        )
        from deepeval.test_case import LLMTestCaseParams  # type: ignore
    except Exception:
        return {}

    out: dict[str, float] = {}
    user_input = scenario.input.get("prompt") or scenario.input.get("input") or str(scenario.input)
    tc_kwargs: dict[str, Any] = {
        "input": str(user_input),
        "actual_output": result.output_text,
    }
    if scenario.expected_output:
        tc_kwargs["expected_output"] = scenario.expected_output
    if scenario.context:
        tc_kwargs["context"] = scenario.context
        tc_kwargs["retrieval_context"] = scenario.context

    case = LLMTestCase(**tc_kwargs)

    metrics: dict[str, Any] = {}
    if "g_eval" in cfg.deepeval_metrics:
        metrics["g_eval"] = GEval(
            name="OverallQuality",
            criteria="The response is correct, relevant, and grounded in the provided context.",
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT, LLMTestCaseParams.CONTEXT],
        )
    if "hallucination" in cfg.deepeval_metrics and scenario.context:
        metrics["hallucination"] = HallucinationMetric()
    if "answer_relevance" in cfg.deepeval_metrics:
        metrics["answer_relevance"] = AnswerRelevancyMetric()
    if "task_completion" in cfg.deepeval_metrics:
        try:
            metrics["task_completion"] = TaskCompletionMetric()
        except Exception:
            pass

    for name, metric in metrics.items():
        try:
            metric.measure(case)
            score = float(getattr(metric, "score", 0.0) or 0.0)
            # DeepEval HallucinationMetric: lower = less hallucination -> invert to a goodness score
            if name == "hallucination":
                score = 1.0 - score
            out[name] = max(0.0, min(1.0, score))
        except Exception:
            continue
    return out

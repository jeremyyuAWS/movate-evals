"""Ragas grounding provider — faithfulness metric.

Faithfulness = fraction of claims in the response that are supported by the
retrieval context. Abstains when context is empty or the package is missing.
"""
from __future__ import annotations

import time

from ...models import AdapterResult, Scenario
from .base import ProviderScore


def _has_ragas() -> bool:
    try:
        import ragas  # noqa: F401
        return True
    except Exception:
        return False


class RagasFaithfulnessProvider:
    role = "grounding"
    name = "ragas.faithfulness"

    def supports(self, scenario: Scenario, result: AdapterResult) -> bool:
        return _has_ragas() and result.ok and bool(result.output_text) and bool(scenario.context)

    async def score(self, scenario: Scenario, result: AdapterResult) -> ProviderScore:
        if not self.supports(scenario, result):
            reason = (
                "ragas not installed" if not _has_ragas()
                else "no retrieval context provided"
                if not scenario.context
                else "adapter result not ok or empty output"
            )
            return ProviderScore(
                provider=self.name, role=self.role,
                abstained=True, reason=reason,
            )

        t0 = time.perf_counter()
        try:
            from ragas import evaluate  # type: ignore
            from ragas.metrics import faithfulness  # type: ignore
            from datasets import Dataset  # type: ignore

            user_input = (
                scenario.input.get("prompt") or scenario.input.get("input")
                or str(scenario.input)
            )
            ds = Dataset.from_dict({
                "question": [str(user_input)],
                "answer": [result.output_text],
                "contexts": [list(scenario.context)],
            })
            res = evaluate(ds, metrics=[faithfulness])
            try:
                df = res.to_pandas()
                raw_score = float(df["faithfulness"].iloc[0])
            except Exception:
                raw_score = float(res["faithfulness"]) if isinstance(res, dict) else 0.0
            score = max(0.0, min(1.0, raw_score))
            return ProviderScore(
                provider=self.name, role=self.role,
                score=score,
                raw={"faithfulness": raw_score},
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
        except Exception as e:
            return ProviderScore(
                provider=self.name, role=self.role,
                abstained=True, reason=f"ragas_error: {type(e).__name__}: {e}",
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )

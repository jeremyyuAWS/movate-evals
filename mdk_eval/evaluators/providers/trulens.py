"""TruLens grounding provider — Groundedness feedback function.

TruLens groundedness scores how well each statement in the response is supported
by the supplied context, with sentence-level attribution. Abstains when context
is empty or the package is missing.
"""
from __future__ import annotations

import os
import time

from ...models import AdapterResult, Scenario
from .base import ProviderScore


def _has_trulens() -> bool:
    try:
        # the public API has changed across versions; probe a couple of paths
        try:
            from trulens.providers.openai import OpenAI  # type: ignore  # noqa: F401
        except Exception:
            from trulens_eval.feedback.provider import OpenAI  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


class TruLensGroundednessProvider:
    role = "grounding"
    name = "trulens.groundedness"

    def __init__(self, model: str = "gpt-4o-mini") -> None:
        self.model = model

    def supports(self, scenario: Scenario, result: AdapterResult) -> bool:
        if not result.ok or not result.output_text or not scenario.context:
            return False
        if not _has_trulens():
            return False
        if not os.getenv("OPENAI_API_KEY"):
            return False
        return True

    async def score(self, scenario: Scenario, result: AdapterResult) -> ProviderScore:
        if not self.supports(scenario, result):
            reason = (
                "trulens not installed" if not _has_trulens()
                else "no retrieval context"
                if not scenario.context
                else "OPENAI_API_KEY not set" if not os.getenv("OPENAI_API_KEY")
                else "adapter result not ok or empty output"
            )
            return ProviderScore(
                provider=self.name, role=self.role,
                abstained=True, reason=reason,
            )

        t0 = time.perf_counter()
        try:
            try:
                from trulens.providers.openai import OpenAI  # type: ignore
            except Exception:
                from trulens_eval.feedback.provider import OpenAI  # type: ignore
            provider = OpenAI(model_engine=self.model)
            joined_context = "\n\n".join(scenario.context)
            score, meta = provider.groundedness_measure_with_cot_reasons(
                joined_context, result.output_text
            )
            score = max(0.0, min(1.0, float(score)))
            return ProviderScore(
                provider=self.name, role=self.role,
                score=score,
                raw={"score": score, "reasons": meta},
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
        except Exception as e:
            return ProviderScore(
                provider=self.name, role=self.role,
                abstained=True, reason=f"trulens_error: {type(e).__name__}: {e}",
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )

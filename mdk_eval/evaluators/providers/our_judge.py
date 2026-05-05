"""Our own grounding judge wrapped as a MetricProvider.

Reuses the existing prompt + multi-model setup so triangulation includes the same
signal you'd get from the panel — but cleanly comparable to Ragas / TruLens.
"""
from __future__ import annotations

import time

from ...config import JudgesConfig
from ...models import AdapterResult, Scenario
from ..judges.llm_clients import LLMClientError, call_judge
from ..judges.prompts import JUDGE_PROMPTS, render_user
from .base import ProviderScore


def _payload(scenario: Scenario, result: AdapterResult) -> dict:
    return {
        "user_input": scenario.input,
        "context": scenario.context,
        "expected_output": scenario.expected_output,
        "agent_response_text": result.output_text,
        "agent_response_json": result.output_json,
        "forbidden_claims": scenario.forbidden_claims,
    }


class OurGroundingProvider:
    """Wraps a single (provider, model) call to our grounding judge prompt."""
    role = "grounding"

    def __init__(self, provider: str, model: str, temperature: float = 0.0) -> None:
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.name = f"our_judges.grounding[{provider}:{model}]"

    def supports(self, scenario: Scenario, result: AdapterResult) -> bool:
        return result.ok and bool(result.output_text)

    async def score(self, scenario: Scenario, result: AdapterResult) -> ProviderScore:
        if not self.supports(scenario, result):
            return ProviderScore(
                provider=self.name, role=self.role,
                abstained=True, reason="adapter result not ok or empty output",
            )
        t0 = time.perf_counter()
        try:
            raw = await call_judge(
                self.provider, self.model,
                JUDGE_PROMPTS["grounding"],
                render_user("grounding", _payload(scenario, result)),
                self.temperature,
            )
            score = float(raw.get("score", 0.0))
            score = max(0.0, min(1.0, score))
            return ProviderScore(
                provider=self.name, role=self.role,
                score=score, raw=raw,
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
        except LLMClientError as e:
            return ProviderScore(
                provider=self.name, role=self.role,
                abstained=True, reason=f"judge_error: {e}",
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )


def build_our_grounding_providers(cfg: JudgesConfig) -> list[OurGroundingProvider]:
    return [OurGroundingProvider(j.provider, j.model, j.temperature) for j in cfg.panel]

"""MetricProvider interface.

Every external eval tool (DeepEval, Ragas, TruLens, garak, our own judges) plugs in
through this Protocol. The orchestrator treats them uniformly; the triangulator
fans out across providers that report the same role.

Providers MUST be allowed to abstain (returning ProviderScore(abstained=True, ...))
when they lack the inputs needed to score reliably (e.g. Ragas without retrieval
context). Abstention is a first-class outcome, not a failure.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from ...models import AdapterResult, Scenario


class ProviderScore(BaseModel):
    """One provider's score for one (scenario, run, role)."""
    provider: str               # e.g. "ragas.faithfulness", "our_judges.grounding"
    role: str                   # one of the 10 standardized categories
    score: float = 0.0          # 0..1, ignored if abstained
    abstained: bool = False
    reason: str | None = None   # explanation, esp. for abstentions
    raw: dict[str, Any] | None = None
    latency_ms: int = 0


@runtime_checkable
class MetricProvider(Protocol):
    """Tool-agnostic scoring provider."""
    name: str
    role: str

    def supports(self, scenario: Scenario, result: AdapterResult) -> bool:
        """Quick capability check (e.g., 'do I have context to ground against')."""
        ...

    async def score(self, scenario: Scenario, result: AdapterResult) -> ProviderScore:
        ...


class TriangulationResult(BaseModel):
    """Outcome of running multiple providers for the same role."""
    role: str
    providers: list[ProviderScore]
    active_providers: int       # providers that returned a score (excluding abstentions)
    mean: float                 # mean of active scores (0..1)
    spread: float               # max - min of active scores (0..1); 0 if <2 active
    threshold: float            # disagreement threshold used
    escalated: bool             # was meta-judge invoked
    meta_verdict: dict[str, Any] | None = None  # raw meta-judge dict (judge=meta:role, model, score, rationale)
    final_score: float          # 0..1 — the chosen authoritative score
    status: str                 # "agreed" | "escalated" | "insufficient_signal"

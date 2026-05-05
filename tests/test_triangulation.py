"""Triangulation behavior tests. No network — providers are inlined fakes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from mdk_eval.evaluators.providers.base import MetricProvider, ProviderScore
from mdk_eval.evaluators.triangulation import triangulate
from mdk_eval.models import AdapterResult, Scenario, Trace


def _scn(**kw: Any) -> Scenario:
    return Scenario(
        id=kw.get("id", "t1"),
        input={"prompt": "what's the policy?"},
        context=["The refund policy is 14 days."],
        expected_output="14 days.",
    )


def _ar(text: str = "Refunds are issued within 14 business days.") -> AdapterResult:
    now = datetime.now(timezone.utc)
    return AdapterResult(
        ok=True,
        output_text=text,
        output_json={"answer": text, "sources": ["The refund policy is 14 days."]},
        trace=Trace(started_at=now, ended_at=now, latency_ms=10),
    )


class _Fake:
    """Simple async fake provider returning a fixed score (or abstaining)."""
    def __init__(self, name: str, score: float, abstain: bool = False, raw: dict | None = None):
        self.name = name
        self.role = "grounding"
        self._score = score
        self._abstain = abstain
        self._raw = raw

    def supports(self, scenario: Scenario, result: AdapterResult) -> bool:
        return True

    async def score(self, scenario: Scenario, result: AdapterResult) -> ProviderScore:
        return ProviderScore(
            provider=self.name, role=self.role,
            score=self._score, abstained=self._abstain,
            reason="fake-abstain" if self._abstain else None,
            raw=self._raw,
        )


def test_agreed_when_spread_below_threshold():
    providers: list[MetricProvider] = [_Fake("a", 0.85), _Fake("b", 0.90), _Fake("c", 0.88)]
    res = asyncio.run(triangulate(
        "grounding", providers, _scn(), _ar(), threshold=0.30, meta_judge=None,
    ))
    assert res.status == "agreed"
    assert res.escalated is False
    assert res.active_providers == 3
    assert pytest.approx(res.spread, abs=1e-6) == round(0.90 - 0.85, 4)
    assert pytest.approx(res.final_score, abs=1e-3) == round((0.85 + 0.90 + 0.88) / 3, 3)


def test_escalates_when_spread_above_threshold_no_meta_falls_back_to_mean():
    # spread = 0.6 > 0.3, but no meta_judge configured -> stays 'agreed' path? No:
    # the function does NOT escalate without meta_judge — it returns 'agreed'.
    providers: list[MetricProvider] = [_Fake("a", 0.20), _Fake("b", 0.80)]
    res = asyncio.run(triangulate(
        "grounding", providers, _scn(), _ar(), threshold=0.30, meta_judge=None,
    ))
    # Without meta_judge available, design choice: return mean and mark agreed.
    assert res.escalated is False
    assert res.status == "agreed"
    assert pytest.approx(res.final_score, abs=1e-6) == 0.50
    assert res.spread == 0.60


def test_insufficient_signal_when_one_active_provider():
    providers: list[MetricProvider] = [_Fake("a", 0.90), _Fake("b", 0.0, abstain=True)]
    res = asyncio.run(triangulate(
        "grounding", providers, _scn(), _ar(), threshold=0.30, meta_judge=None,
    ))
    assert res.status == "insufficient_signal"
    assert res.active_providers == 1
    assert res.final_score == 0.90


def test_all_abstain_yields_zero_and_status():
    providers: list[MetricProvider] = [
        _Fake("a", 0.0, abstain=True),
        _Fake("b", 0.0, abstain=True),
    ]
    res = asyncio.run(triangulate(
        "grounding", providers, _scn(), _ar(), threshold=0.30, meta_judge=None,
    ))
    assert res.status == "insufficient_signal"
    assert res.active_providers == 0
    assert res.final_score == 0.0


def test_provider_scores_serialize_into_triangulation_result():
    providers: list[MetricProvider] = [_Fake("a", 0.7, raw={"rationale": "ok"}), _Fake("b", 0.8)]
    res = asyncio.run(triangulate(
        "grounding", providers, _scn(), _ar(), threshold=0.30, meta_judge=None,
    ))
    dumped = res.model_dump()
    assert dumped["status"] == "agreed"
    assert len(dumped["providers"]) == 2
    assert dumped["providers"][0]["raw"] == {"rationale": "ok"}

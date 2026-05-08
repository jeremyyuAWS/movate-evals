"""datasets/adversarial.jsonl — reference adversarial pack guard tests.

The pack is content, not code, but we lock these invariants down so a future
edit can't silently:
- introduce a malformed scenario the loader rejects
- lose adversarial-tag coverage of an attack family
- drop the per-scenario severity below what makes sense for an adversarial test
- forget to populate forbidden_phrases / forbidden_claims (what actually
  encodes the attack signature)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mdk_eval.scenarios import load_scenarios

PACK = Path(__file__).parent.parent / "datasets" / "adversarial.jsonl"


# Attack families we expect the pack to cover. If a family is removed, we
# explicitly want a test failure — not silent regression of coverage.
EXPECTED_FAMILIES = {
    "prompt_injection",
    "indirect_injection",
    "pii",
    "jailbreak",
    "tool_hijacking",
    "off_topic",
    "role_confusion",
    "refusal_bypass",
    "multi_turn",
    "output_manipulation",
}


@pytest.fixture(scope="module")
def scenarios():
    return load_scenarios(str(PACK))


def test_pack_size_in_expected_range(scenarios):
    """Backlog promised ~30; assert a sane band so a clear-out is loud."""
    assert 25 <= len(scenarios) <= 60, f"unexpected pack size: {len(scenarios)}"


def test_every_attack_family_represented(scenarios):
    family_tags: set[str] = set()
    for s in scenarios:
        family_tags.update(s.tags)
    missing = EXPECTED_FAMILIES - family_tags
    assert not missing, f"adversarial pack missing attack families: {sorted(missing)}"


def test_every_scenario_tagged_adversarial(scenarios):
    for s in scenarios:
        assert "adversarial" in s.tags, f"{s.id} missing 'adversarial' tag"
        assert "derived:reference_pack" in s.tags, f"{s.id} missing 'derived:reference_pack' tag"


def test_every_scenario_has_attack_signature(scenarios):
    """Every adversarial scenario must encode the attack — either by phrase
    blocklist (string match) or by claim blocklist (semantic). If both are
    empty, the scenario is just a vibes check, which doesn't count."""
    weak = []
    for s in scenarios:
        if not s.forbidden_phrases and not s.forbidden_claims:
            weak.append(s.id)
    assert not weak, f"scenarios with no forbidden_phrases AND no forbidden_claims: {weak}"


def test_no_scenario_below_medium_severity(scenarios):
    """Adversarial tests are high-stakes by definition; a 'low' would be wrong."""
    low = [s.id for s in scenarios if s.severity.value == "low"]
    assert not low, f"adversarial scenarios with low severity (escalate them): {low}"


def test_critical_count_is_meaningful(scenarios):
    """At least a few should be critical — pack has bite when it includes
    PII exfil + indirect injection class."""
    crit = sum(1 for s in scenarios if s.severity.value == "critical")
    assert crit >= 4, f"expected >=4 critical-severity scenarios; got {crit}"


def test_ids_are_unique(scenarios):
    ids = [s.id for s in scenarios]
    assert len(ids) == len(set(ids)), f"duplicate ids in pack: {set(i for i in ids if ids.count(i) > 1)}"


def test_ids_follow_naming_convention(scenarios):
    """All ids start with 'adv_' for easy grep/filter."""
    bad = [s.id for s in scenarios if not s.id.startswith("adv_")]
    assert not bad, f"ids not starting with 'adv_': {bad}"

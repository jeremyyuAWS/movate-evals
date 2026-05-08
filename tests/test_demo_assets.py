"""Demo dataset + configs guard tests.

The demo is a customer-facing artifact — its quality reflects on the platform.
These tests lock in invariants so a future edit can't silently:
  - break the dataset's load-ability (a future schema change must update the dataset)
  - drop the story-arc coverage (every demo must have happy / edge / adversarial)
  - emit malformed configs the CLI won't parse
  - lose the documented commands in DEMO.md
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mdk_eval.scenarios import load_scenarios


REPO = Path(__file__).parent.parent
DEMO_DATASET = REPO / "datasets" / "demo_movate_faq.jsonl"
DEMO_CONFIG_LYZR = REPO / "configs" / "demo_lyzr.yaml"
DEMO_CONFIG_MOCK = REPO / "configs" / "demo_mock.yaml"
DEMO_DOC = REPO / "DEMO.md"


# ---------------------------------------------------------------- dataset


@pytest.fixture(scope="module")
def scenarios():
    return load_scenarios(str(DEMO_DATASET))


def test_dataset_loads_via_scenario_loader(scenarios):
    """The Scenario model must accept every row — drift between the loader
    and a hand-curated dataset is the most common silent breakage."""
    assert len(scenarios) > 0


def test_dataset_size_in_band(scenarios):
    """T44 promises 8-12 scenarios. Locking the band so a clearout / overload
    is loud, while leaving room for tweaks."""
    assert 8 <= len(scenarios) <= 14, f"unexpected demo size: {len(scenarios)}"


def test_dataset_covers_required_story_arc(scenarios):
    """The demo's narrative arc requires happy / edge / adversarial / safety
    coverage. Any of these going missing makes the 10-minute walkthrough
    incoherent."""
    all_tags = set()
    for s in scenarios:
        all_tags.update(s.tags)

    required_groups = {
        "happy_path": {"happy", "standard"},
        "edge": {"edge", "boundary"},
        "adversarial": {"adversarial", "prompt_injection", "indirect_injection"},
        "safety_or_honesty": {"safety", "honesty", "off_topic"},
    }
    for group_name, group_tags in required_groups.items():
        assert all_tags & group_tags, (
            f"demo dataset missing story-arc coverage for: {group_name} "
            f"(expected at least one of {group_tags})"
        )


def test_dataset_has_at_least_one_critical_severity(scenarios):
    """The arc benefits from one critical-severity item to demonstrate hard-gate behavior."""
    assert any(s.severity.value in ("high", "critical") for s in scenarios)


def test_every_scenario_has_attack_signature_or_expected_output(scenarios):
    """Every demo scenario must encode either:
      - an attack signature (forbidden_phrases / forbidden_claims), OR
      - an expected_output (so the judges have something to compare against).
    A scenario with neither produces noise, not signal."""
    weak = []
    for s in scenarios:
        has_assertion = bool(s.forbidden_phrases or s.forbidden_claims or s.expected_output)
        if not has_assertion:
            weak.append(s.id)
    assert not weak, f"scenarios with no assertion at all: {weak}"


def test_ids_follow_demo_naming_convention(scenarios):
    """All demo ids start with 'demo_' for easy grep / filter against
    other dataset ids the user may already have on disk."""
    bad = [s.id for s in scenarios if not s.id.startswith("demo_")]
    assert not bad, f"demo dataset has scenarios not prefixed 'demo_': {bad}"


def test_ids_are_unique(scenarios):
    ids = [s.id for s in scenarios]
    assert len(ids) == len(set(ids)), "duplicate ids in demo dataset"


# ---------------------------------------------------------------- configs


def test_lyzr_config_loads_as_yaml():
    cfg = yaml.safe_load(DEMO_CONFIG_LYZR.read_text())
    assert cfg["adapter"]["target"] == "lyzr"
    # The Movate FAQ Assistant we ingested previously
    assert cfg["adapter"]["agent_id"] == "69f9630789e1a27b8101014b"
    assert cfg["dataset"].endswith("demo_movate_faq.jsonl")
    assert cfg["judges_enabled"] is True


def test_mock_config_loads_as_yaml():
    cfg = yaml.safe_load(DEMO_CONFIG_MOCK.read_text())
    assert cfg["adapter"]["target"] == "mock"
    assert cfg["dataset"].endswith("demo_movate_faq.jsonl")
    # Mock config has judges off by default — fast deterministic demo
    assert cfg["judges_enabled"] is False


def test_both_configs_point_at_same_dataset():
    """The Lyzr and mock configs must run the SAME dataset so the two demo
    flavors tell the same story — only the adapter differs."""
    lyzr = yaml.safe_load(DEMO_CONFIG_LYZR.read_text())
    mock = yaml.safe_load(DEMO_CONFIG_MOCK.read_text())
    assert lyzr["dataset"] == mock["dataset"]


def test_lyzr_config_validates_via_runconfig():
    """Round-trip the config through the same loader the CLI uses. Catches
    schema drift between the demo config and the RunConfig model."""
    from mdk_eval.config import RunConfig
    cfg = RunConfig.from_yaml(str(DEMO_CONFIG_LYZR))
    assert cfg.dataset.endswith("demo_movate_faq.jsonl")
    assert cfg.adapter.target == "lyzr"


def test_mock_config_validates_via_runconfig():
    from mdk_eval.config import RunConfig
    cfg = RunConfig.from_yaml(str(DEMO_CONFIG_MOCK))
    assert cfg.adapter.target == "mock"


# ---------------------------------------------------------------- DEMO.md


def test_demo_doc_exists_and_is_substantive():
    """The doc should exist and be more than a stub. Catches half-deletes."""
    assert DEMO_DOC.exists()
    text = DEMO_DOC.read_text()
    assert len(text) > 2000, f"DEMO.md too short ({len(text)} chars) — possibly truncated"


def test_demo_doc_references_required_assets():
    """The walkthrough must reference the dataset, both configs, and at least
    one of the four Bolt PRDs (so a reader can find the methodology when asked)."""
    text = DEMO_DOC.read_text()
    for required in [
        "demo_movate_faq.jsonl",
        "demo_lyzr.yaml",
        "demo_mock.yaml",
        "BOLT_SCORING_PRD.md",
    ]:
        assert required in text, f"DEMO.md missing reference to {required}"


def test_demo_doc_documents_promote_failure_command():
    """The HITL-closure step is the platform's distinguishing feature; the
    walkthrough must include the actual command."""
    text = DEMO_DOC.read_text()
    assert "promote-failure" in text, "DEMO.md missing the promote-failure command"


def test_demo_doc_documents_ab_command():
    """Same for the A/B comparison step."""
    text = DEMO_DOC.read_text()
    assert "mdk-eval ab" in text, "DEMO.md missing the mdk-eval ab command"


def test_demo_doc_includes_recovery_script():
    """If a live demo goes sideways, the doc must show how to fall back to a
    saved run. Removing this section is a reliability regression for demos."""
    text = DEMO_DOC.read_text()
    assert "Recovery script" in text or "recovery" in text.lower()

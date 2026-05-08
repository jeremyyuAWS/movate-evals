"""Lyzr ingest tests.

Asserts that the heuristic extractor pulls the right facts from the user's
real-world Lyzr export, and that derived scenarios carry provenance + gating tags.
"""
from __future__ import annotations

from pathlib import Path


from mdk_eval.ingest.base import auto_detect, get_ingestor
from mdk_eval.ingest.extractors.heuristic import extract


FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "lyzr_brief_ingestion_agent.json"


def test_autodetect_lyzr():
    assert auto_detect(FIXTURE) == "lyzr"


def test_inputs_only_pulls_declared_input_fields():
    import json
    obj = json.loads(FIXTURE.read_text())
    spec = extract(obj["agent_instructions"])
    inputs = {f.value for f in spec.inputs}
    assert inputs == {"workflow_id", "sap_brief_id"}, f"unexpected inputs: {inputs}"


def test_extractor_pulls_tools_and_sequence():
    raw = FIXTURE.read_text()
    import json
    obj = json.loads(raw)
    spec = extract(obj["agent_instructions"])
    tool_names = {f.value for f in spec.tools}
    assert {"fetch_sap_brief", "assess_brief_completeness", "apply_budget_gate", "escalate"}.issubset(tool_names)
    seq = [f.value for f in spec.tool_sequence]
    assert seq[:3] == ["fetch_sap_brief", "assess_brief_completeness", "apply_budget_gate"]


def test_extractor_pulls_threshold_and_slo():
    raw = FIXTURE.read_text()
    import json
    obj = json.loads(raw)
    spec = extract(obj["agent_instructions"])
    assert spec.slo_latency_ms is not None
    assert spec.slo_latency_ms.value == 2000   # "p95 latency ≤ 2s"
    amounts = {f.value["amount"] for f in spec.thresholds}
    assert 50_000 in amounts


def test_extractor_pulls_default_rule_for_region():
    import json
    obj = json.loads(FIXTURE.read_text())
    spec = extract(obj["agent_instructions"])
    fields = [d.value["field"] for d in spec.default_rules]
    assert "region" in fields


def test_extractor_pulls_forbidden_fields():
    import json
    obj = json.loads(FIXTURE.read_text())
    spec = extract(obj["agent_instructions"])
    forbidden = {f.value for f in spec.forbidden_fields}
    # at least the named enrichment fields should be flagged
    assert {"customer_segment", "sla_tier", "economic_feasibility"}.issubset(forbidden)


def test_extractor_pulls_output_schema_keys():
    import json
    obj = json.loads(FIXTURE.read_text())
    spec = extract(obj["agent_instructions"])
    keys = {f.value for f in spec.output_keys}
    # the inline schema lists these
    assert {"brief_id", "customer_id", "category", "region", "budget", "completeness", "gate_decision"}.issubset(keys)


def test_lyzr_ingest_produces_complete_result():
    ing = get_ingestor("lyzr")
    raw = FIXTURE.read_bytes()
    result = ing.ingest(FIXTURE, raw)
    assert result.source_format == "lyzr"
    assert result.name.startswith("brief_ingestion")
    # config wired to lyzr adapter with the agent_id from the JSON
    assert result.config.adapter.target == "lyzr"
    assert result.config.adapter.agent_id == "69ef90f1d56224d9f94c61cc"
    # at least the core categories generated
    cats = {tag.split(":", 1)[1] for s in result.scenarios for tag in s.tags if tag.startswith("derived:")}
    expected = {"happy", "schema", "tool_sequence", "edge_threshold", "edge_threshold_above",
                "default_rule", "forbidden", "failure", "slo"}
    assert expected.issubset(cats), f"missing categories: {expected - cats}"


def test_heuristic_scenarios_carry_behavioral_category_tag():
    """Every heuristic scenario should have a `category:<behavior>` tag in
    addition to its `derived:<kind>` tag, so the frontend can render a
    consistent category-pill across heuristic + LLM scenarios."""
    ing = get_ingestor("lyzr")
    raw = FIXTURE.read_bytes()
    result = ing.ingest(FIXTURE, raw)

    # Map of derived:<kind> → expected category:<behavior>
    expected_mapping = {
        "happy":                "standard",
        "schema":               "standard",
        "tool_sequence":        "standard",
        "edge_threshold":       "edge",
        "edge_threshold_above": "edge",
        "default_rule":         "edge",
        "forbidden":            "safety",
        "failure":              "edge",
        "slo":                  "performance",
    }
    valid_categories = {
        "standard", "edge", "adversarial", "safety",
        "honesty", "multi_turn", "performance", "custom",
    }

    for s in result.scenarios:
        derived_tag = next((t for t in s.tags if t.startswith("derived:")), None)
        category_tag = next((t for t in s.tags if t.startswith("category:")), None)
        assert derived_tag is not None, f"{s.id}: must carry derived:<kind>"
        assert category_tag is not None, f"{s.id}: must carry category:<behavior>"
        kind = derived_tag.split(":", 1)[1]
        cat = category_tag.split(":", 1)[1]
        assert cat in valid_categories, f"{s.id}: unknown category {cat!r}"
        assert cat == expected_mapping.get(kind, cat), (
            f"{s.id}: kind={kind!r} should map to {expected_mapping[kind]!r}, got {cat!r}"
        )


def test_all_scenarios_marked_unverified_with_provenance():
    ing = get_ingestor("lyzr")
    raw = FIXTURE.read_bytes()
    result = ing.ingest(FIXTURE, raw)
    for s in result.scenarios:
        assert "unverified" in s.tags, f"{s.id}: must be tagged 'unverified'"
        assert "derived_from" in s.meta, f"{s.id}: must carry provenance"
        prov = s.meta["derived_from"]
        assert prov["source_sha256"]
        assert prov["extractor"] == "heuristic"
        assert prov["constraint_quote"]


def test_agent_card_has_required_sections():
    ing = get_ingestor("lyzr")
    raw = FIXTURE.read_bytes()
    result = ing.ingest(FIXTURE, raw)
    md = result.agent_card_md
    for section in ("Agent Card —", "Mandate", "Declared interface", "Derived test suite",
                    "Review checklist", "Provenance", "fetch_sap_brief"):
        assert section in md, f"agent card missing: {section}"

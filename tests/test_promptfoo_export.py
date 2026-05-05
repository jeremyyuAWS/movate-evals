"""PromptFoo export tests."""
from __future__ import annotations

from pathlib import Path

import yaml

from mdk_eval.exporters.promptfoo import build_promptfoo_config, write_promptfoo_yaml
from mdk_eval.scenarios import load_scenarios


def _ds() -> Path:
    return Path(__file__).resolve().parents[1] / "datasets" / "sample.jsonl"


def test_build_config_has_required_top_level_keys():
    cfg = build_promptfoo_config(load_scenarios(_ds()), endpoint=None)
    for k in ("description", "prompts", "providers", "tests"):
        assert k in cfg
    assert isinstance(cfg["tests"], list) and len(cfg["tests"]) >= 1


def test_each_test_has_prompt_and_metadata():
    cfg = build_promptfoo_config(load_scenarios(_ds()), endpoint=None)
    for t in cfg["tests"]:
        assert "vars" in t and "prompt" in t["vars"]
        assert "metadata" in t and "id" in t["metadata"]


def test_forbidden_phrase_emits_negative_assertion():
    cfg = build_promptfoo_config(load_scenarios(_ds()), endpoint=None)
    by_id = {t["metadata"]["id"]: t for t in cfg["tests"]}
    halluc = by_id["hallucination_trap"]
    types = [a["type"] for a in halluc.get("assert", [])]
    assert "not-contains" in types


def test_required_fields_emit_javascript_assertions():
    cfg = build_promptfoo_config(load_scenarios(_ds()), endpoint=None)
    by_id = {t["metadata"]["id"]: t for t in cfg["tests"]}
    h = by_id["happy_path_qna"]
    types = [a["type"] for a in h.get("assert", [])]
    assert "is-json" in types
    # required_fields is ['answer', 'sources'] in the dataset
    assert types.count("javascript") >= 2


def test_writes_valid_yaml(tmp_path: Path):
    # tmp_path inside /tmp avoids the mount cleanup quirk
    import tempfile
    target = Path(tempfile.mkdtemp(prefix="pf_", dir="/tmp")) / "promptfoo.yaml"
    write_promptfoo_yaml(load_scenarios(_ds()), target, endpoint="https://api.example.com/agent/invoke")
    loaded = yaml.safe_load(target.read_text())
    assert loaded["providers"][0]["id"] == "http"
    assert loaded["providers"][0]["config"]["url"].startswith("https://")

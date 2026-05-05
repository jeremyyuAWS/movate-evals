"""mdk-eval init scaffold tests."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml

from mdk_eval.cli.init import InitOptions, scaffold
from mdk_eval.cli.validate import discover_config_path
from mdk_eval.scenarios import load_scenarios


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="mdk_init_", dir="/tmp"))


def test_scaffold_creates_expected_tree():
    target = _tmp() / "my_proj"
    scaffold(target, InitOptions(no_prompt=True, vscode=True))
    for sub in ("configs", "datasets", "agent_cards", "results", ".vscode"):
        assert (target / sub).is_dir(), f"missing dir: {sub}"
    for f in ("mdk-eval.yaml", ".env.example", ".gitignore", "README.md"):
        assert (target / f).is_file(), f"missing file: {f}"
    assert (target / ".vscode" / "launch.json").is_file()
    assert (target / ".vscode" / "settings.json").is_file()


def test_root_config_is_valid_yaml_and_loadable():
    target = _tmp() / "my_proj"
    scaffold(target, InitOptions(no_prompt=True))
    cfg = yaml.safe_load((target / "mdk-eval.yaml").read_text())
    # required top-level keys
    for k in ("adapter", "judges", "dataset", "client_name", "runs_per_scenario"):
        assert k in cfg
    assert cfg["dataset"].endswith(".jsonl")


def test_starter_dataset_loads_via_scenario_loader():
    target = _tmp() / "my_proj"
    scaffold(target, InitOptions(no_prompt=True))
    ds = target / "datasets" / "agent.jsonl"
    scenarios = load_scenarios(ds)
    assert len(scenarios) >= 1
    ids = {s.id for s in scenarios}
    assert "starter_safety_check" in ids


def test_root_config_is_auto_discoverable():
    target = _tmp() / "my_proj"
    scaffold(target, InitOptions(no_prompt=True))
    found = discover_config_path(target)
    assert found is not None
    assert found.name == "mdk-eval.yaml"


def test_idempotent_without_force_does_not_overwrite():
    target = _tmp() / "my_proj"
    scaffold(target, InitOptions(no_prompt=True))
    cfg_path = target / "mdk-eval.yaml"
    cfg_path.write_text("# user-edited content")
    scaffold(target, InitOptions(no_prompt=True))   # no --force
    assert cfg_path.read_text().startswith("# user-edited"), "init clobbered an existing file without --force"


def test_force_overwrites():
    target = _tmp() / "my_proj"
    scaffold(target, InitOptions(no_prompt=True))
    (target / "mdk-eval.yaml").write_text("# user-edited content")
    scaffold(target, InitOptions(no_prompt=True, force=True))
    assert "user-edited" not in (target / "mdk-eval.yaml").read_text()


def test_lyzr_target_wires_agent_id_into_config():
    target = _tmp() / "my_proj"
    scaffold(target, InitOptions(no_prompt=True, target="lyzr", agent_id="abc123"))
    cfg = yaml.safe_load((target / "mdk-eval.yaml").read_text())
    assert cfg["adapter"]["target"] == "lyzr"
    assert cfg["adapter"]["agent_id"] == "abc123"
    assert cfg["adapter"]["api_key_env"] == "LYZR_API_KEY"


def test_vscode_launch_json_is_valid_json():
    target = _tmp() / "my_proj"
    scaffold(target, InitOptions(no_prompt=True, vscode=True))
    launch = json.loads((target / ".vscode" / "launch.json").read_text())
    assert launch["version"] == "0.2.0"
    names = [c["name"] for c in launch["configurations"]]
    assert any("doctor" in n for n in names)
    assert any("dry-run" in n for n in names)

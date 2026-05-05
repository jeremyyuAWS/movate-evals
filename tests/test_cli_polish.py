"""Tier-1 CLI polish: validation, judge auto-disable, config discovery."""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from mdk_eval.cli.validate import (
    ConfigError,
    auto_disable_judges_if_no_keys,
    discover_config_path,
    validate_config,
)
from mdk_eval.config import AdapterConfig, JudgeModelConfig, JudgesConfig, RunConfig


# ----------------------------- validation -----------------------------


def _cfg(target: str = "mock", **adapter_overrides) -> RunConfig:
    a = AdapterConfig(target=target, **adapter_overrides)
    return RunConfig(adapter=a, dataset="datasets/sample.jsonl")


def test_lyzr_without_agent_id_is_rejected():
    with pytest.raises(ConfigError, match="agent_id"):
        validate_config(_cfg("lyzr"))


def test_rest_without_endpoint_is_rejected():
    with pytest.raises(ConfigError, match="endpoint"):
        validate_config(_cfg("rest"))


def test_openai_compat_without_endpoint_is_rejected():
    with pytest.raises(ConfigError, match="endpoint"):
        validate_config(_cfg("openai_compat"))


def test_langgraph_without_import_path_is_rejected():
    with pytest.raises(ConfigError, match="graph import path"):
        validate_config(_cfg("langgraph"))


def test_dataset_must_exist():
    cfg = RunConfig(adapter=AdapterConfig(target="mock"), dataset="/nonexistent/path.jsonl")
    with pytest.raises(ConfigError, match="Dataset not found"):
        validate_config(cfg)


def test_runs_per_scenario_must_be_positive():
    cfg = _cfg(); cfg.runs_per_scenario = 0
    with pytest.raises(ConfigError, match="runs_per_scenario"):
        validate_config(cfg)


def test_valid_mock_config_passes():
    validate_config(_cfg())


# ----------------------------- judge auto-disable -----------------------------


def _jc(*providers: str) -> JudgesConfig:
    return JudgesConfig(panel=[JudgeModelConfig(provider=p, model=f"{p}-x") for p in providers])


def test_auto_disable_when_no_keys():
    cfg = _cfg(); cfg.judges = _jc("openai", "anthropic"); cfg.judges_enabled = True
    with mock.patch.dict(os.environ, {}, clear=True):
        result = auto_disable_judges_if_no_keys(cfg)
    assert result.disabled is True
    assert cfg.judges_enabled is False
    assert "OPENAI_API_KEY" in result.reason or "ANTHROPIC_API_KEY" in result.reason


def test_drops_only_missing_provider():
    cfg = _cfg(); cfg.judges = _jc("openai", "anthropic"); cfg.judges_enabled = True
    with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "x"}, clear=True):
        result = auto_disable_judges_if_no_keys(cfg)
    assert result.disabled is False
    assert cfg.judges_enabled is True
    assert any("anthropic" in d for d in result.panel_dropped)
    assert all(j.provider == "openai" for j in cfg.judges.panel)


def test_no_change_when_judges_disabled():
    cfg = _cfg(); cfg.judges_enabled = False
    with mock.patch.dict(os.environ, {}, clear=True):
        result = auto_disable_judges_if_no_keys(cfg)
    assert result.disabled is False
    assert result.panel_dropped == []


# ----------------------------- config auto-discovery -----------------------------


def test_discover_finds_first_match(tmp_path: Path, monkeypatch):
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="mdk_disc_", dir="/tmp"))
    (base / ".mdk-eval.yaml").write_text("dataset: foo")
    (base / "mdk-eval.yaml").write_text("dataset: bar")  # higher priority
    found = discover_config_path(base)
    assert found is not None
    assert found.name == "mdk-eval.yaml"


def test_discover_returns_none_when_absent():
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="mdk_disc2_", dir="/tmp"))
    assert discover_config_path(base) is None

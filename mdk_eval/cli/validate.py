"""Config validation + smart judge auto-disable.

Runs BEFORE the orchestrator starts so users get clear, actionable errors
instead of opaque adapter / API errors mid-flight.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import RunConfig


class ConfigError(ValueError):
    """Raised on invalid run configuration. CLI maps this to typer.BadParameter."""


@dataclass
class JudgeAutoDisableResult:
    disabled: bool
    panel_dropped: list[str]   # provider names dropped because their key was missing
    reason: str | None


def validate_config(cfg: RunConfig) -> None:
    """Hard-fail validation. Raise ConfigError with a concrete fix."""
    a = cfg.adapter

    # adapter-specific required fields
    if a.target == "lyzr" and not a.agent_id:
        raise ConfigError(
            "Lyzr adapter requires an agent_id. "
            "Pass --agent-id <id> or set adapter.agent_id in your config YAML."
        )
    if a.target == "rest" and not a.endpoint:
        raise ConfigError(
            "REST adapter requires an endpoint URL. "
            "Pass --endpoint <url> or set adapter.endpoint in your config YAML."
        )
    if a.target == "openai_compat" and not a.endpoint:
        raise ConfigError(
            "openai_compat adapter requires an endpoint URL. "
            "Pass --endpoint <url> or set adapter.endpoint in your config YAML."
        )
    if a.target == "langgraph" and not a.graph_import_path:
        raise ConfigError(
            "LangGraph adapter requires a graph import path (e.g. 'pkg.mod:graph'). "
            "Pass --graph-import-path or set adapter.graph_import_path in your config."
        )

    # dataset must exist
    if not cfg.dataset:
        raise ConfigError("No dataset configured. Pass --dataset <path> or set 'dataset' in config.")
    if not Path(cfg.dataset).exists():
        raise ConfigError(
            f"Dataset not found: {cfg.dataset}\n"
            "Hint: relative paths resolve from your current working directory."
        )

    # multi-run sanity
    if cfg.runs_per_scenario < 1:
        raise ConfigError(f"runs_per_scenario must be >= 1, got {cfg.runs_per_scenario}.")
    if cfg.concurrency < 1:
        raise ConfigError(f"concurrency must be >= 1, got {cfg.concurrency}.")


def auto_disable_judges_if_no_keys(cfg: RunConfig) -> JudgeAutoDisableResult:
    """Drop panel models whose API key is unset; fully disable if none usable.

    Returns mutation summary so the CLI can warn the user before the run starts.
    """
    if not cfg.judges_enabled:
        return JudgeAutoDisableResult(disabled=False, panel_dropped=[], reason=None)

    key_for = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
    missing_providers = {p for p, env in key_for.items() if not os.getenv(env)}

    dropped: list[str] = []
    kept: list = []
    for j in cfg.judges.panel:
        if j.provider.lower() in missing_providers:
            dropped.append(f"{j.provider}:{j.model}")
        else:
            kept.append(j)

    if not kept:
        cfg.judges_enabled = False
        return JudgeAutoDisableResult(
            disabled=True,
            panel_dropped=dropped,
            reason=(
                "No judge API keys found in environment "
                "(set OPENAI_API_KEY and/or ANTHROPIC_API_KEY, "
                "or run `mdk-eval doctor` for diagnostics). "
                "Continuing with deterministic checks only."
            ),
        )

    if dropped:
        cfg.judges.panel = kept
        # also drop the meta-judge if its provider is unavailable; fall back to mean
        if cfg.judges.meta_judge.provider.lower() in missing_providers:
            # keep it configured but the panel arbitration code already handles meta failure gracefully
            pass
        return JudgeAutoDisableResult(
            disabled=False,
            panel_dropped=dropped,
            reason=f"Dropped panel models with missing API keys: {dropped}",
        )

    return JudgeAutoDisableResult(disabled=False, panel_dropped=[], reason=None)


def discover_config_path(start: Path | None = None) -> Path | None:
    """Look for an mdk-eval config in cwd. Returns first match or None."""
    base = start or Path.cwd()
    for name in ("mdk-eval.yaml", ".mdk-eval.yaml", "mdk_eval.yaml", "mdk-eval.yml"):
        p = base / name
        if p.exists():
            return p
    return None

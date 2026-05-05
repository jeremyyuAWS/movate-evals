"""Run configuration. Loaded from YAML or CLI flags. CLI flags win."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class JudgeModelConfig(BaseModel):
    provider: str          # "openai" | "anthropic"
    model: str             # e.g. "gpt-4o", "claude-sonnet-4-6"
    temperature: float = 0.0


class JudgesConfig(BaseModel):
    enabled_roles: list[str] = Field(
        default_factory=lambda: [
            "correctness",
            "grounding",
            "completeness",
            "tool_usage",
            "ux_tone",
            "safety",
        ]
    )
    panel: list[JudgeModelConfig] = Field(
        default_factory=lambda: [
            JudgeModelConfig(provider="openai", model="gpt-4o-mini"),
            JudgeModelConfig(provider="anthropic", model="claude-haiku-4-5-20251001"),
        ]
    )
    meta_judge: JudgeModelConfig = Field(
        default_factory=lambda: JudgeModelConfig(provider="anthropic", model="claude-sonnet-4-6")
    )
    arbitration_variance_threshold: float = 0.04   # variance of [0..1] scores
    deepeval_metrics: list[str] = Field(
        default_factory=lambda: ["g_eval", "task_completion", "hallucination", "answer_relevance"]
    )
    # Triangulation: run multiple grounding providers in parallel; meta-judge breaks
    # ties when spread (max-min on 0..1) exceeds the threshold.
    triangulation_enabled: bool = True
    triangulation_disagreement_threshold: float = 0.30


class AdapterConfig(BaseModel):
    target: str = "mock"                     # mock | rest | openai_compat | lyzr | langgraph
    endpoint: str | None = None
    agent_id: str | None = None
    api_key_env: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    request_template: dict[str, Any] | None = None  # JSON body template; {{input}} substitution
    response_text_path: str | None = "$.response"   # JSONPath-ish dotted notation
    response_tools_path: str | None = None
    response_workflow_path: str | None = None
    timeout_s: float = 60.0
    max_retries: int = 2
    # langgraph
    graph_import_path: str | None = None     # "my_pkg.graphs:my_graph"


class RunConfig(BaseModel):
    adapter: AdapterConfig = Field(default_factory=AdapterConfig)
    judges: JudgesConfig = Field(default_factory=JudgesConfig)
    judges_enabled: bool = True
    runs_per_scenario: int = 1
    concurrency: int = 4
    output_dir: str = "./results"
    dataset: str = "datasets/sample.jsonl"
    langfuse_enabled: bool = False
    pdf: bool = True
    client_name: str = "Client"
    client_logo_path: str | None = None
    movate_logo_path: str | None = None
    confidentiality_footer: str = "Confidential — prepared by Movate Agent Assurance."

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RunConfig":
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)

    def merged_with_overrides(self, **overrides: Any) -> "RunConfig":
        data = self.model_dump()
        for k, v in overrides.items():
            if v is None:
                continue
            if "." in k:
                head, tail = k.split(".", 1)
                data.setdefault(head, {})
                data[head][tail] = v
            else:
                data[k] = v
        return RunConfig(**data)

"""Lyzr agent JSON ingestor.

Reads a Lyzr `/agents/{id}` export and produces:
  - a wired RunConfig (LyzrAdapter, agent_id, env-key reference)
  - a list of Scenarios derived heuristically from `agent_instructions`,
    each tagged 'unverified' + 'derived:<category>' and carrying provenance
  - an agent_card.md
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from ..config import AdapterConfig, JudgesConfig, RunConfig
from ..models import Scenario, Severity, ToolExpectation, WorkflowExpectation
from .agent_card import build_agent_card
from .base import IngestionResult, safe_json_loads
from .extractors.heuristic import ExtractedAgentSpec, ExtractedFact, extract


_NAME_SAFE = re.compile(r"[^a-zA-Z0-9]+")


def _safe_name(s: str) -> str:
    s = _NAME_SAFE.sub("_", s.strip().lower()).strip("_")
    return s[:60] or "agent"


class LyzrIngestor:
    name = "lyzr"

    def detect(self, path: Path, raw: bytes) -> bool:
        obj = safe_json_loads(raw)
        if not isinstance(obj, dict):
            return False
        # signature: agent_role + agent_instructions + features w/ TOOL_CALLING
        return all(k in obj for k in ("agent_instructions", "agent_role", "model")) and "name" in obj

    def ingest(self, path: Path, raw: bytes) -> IngestionResult:
        obj = safe_json_loads(raw)
        if not isinstance(obj, dict):
            raise ValueError(f"{path}: not valid JSON")

        sha256 = hashlib.sha256(raw).hexdigest()
        raw_name = obj.get("name") or obj.get("agent_role") or "agent"
        # Lyzr names like "brief-ingestion-agent [proj] v1.00" -> take the leading slug
        leading = raw_name.split("[")[0].strip()
        name = _safe_name(leading)

        instructions = obj.get("agent_instructions") or ""
        spec = extract(instructions)

        cfg = self._build_config(obj, name)
        scenarios = self._build_scenarios(spec, name, source_path=str(path), source_sha256=sha256)
        warnings = self._collect_warnings(obj, spec)

        card = build_agent_card(
            agent_obj=obj,
            spec=spec,
            scenarios=scenarios,
            source_path=str(path),
            source_sha256=sha256,
        )

        return IngestionResult(
            name=name,
            source_format=self.name,
            source_path=str(path),
            source_sha256=sha256,
            config=cfg,
            scenarios=scenarios,
            agent_card_md=card,
            warnings=warnings,
        )

    # ----------------------------- config -----------------------------

    def _build_config(self, obj: dict[str, Any], name: str) -> RunConfig:
        agent_id = obj.get("_id") or obj.get("id") or ""
        return RunConfig(
            adapter=AdapterConfig(
                target="lyzr",
                agent_id=agent_id,
                api_key_env="LYZR_API_KEY",
            ),
            judges=JudgesConfig(),
            judges_enabled=True,
            runs_per_scenario=3,
            output_dir="./results",
            dataset=f"datasets/{name}.jsonl",
            client_name=obj.get("name", "Client").split("[")[0].strip(),
        )

    # ----------------------------- scenarios -----------------------------

    def _build_scenarios(
        self, spec: ExtractedAgentSpec, name: str, *, source_path: str, source_sha256: str,
    ) -> list[Scenario]:
        scenarios: list[Scenario] = []

        def _meta(category: str, quote: str, requires_fixture: bool = False) -> dict[str, Any]:
            return {
                "derived_from": {
                    "source_path": source_path,
                    "source_sha256": source_sha256,
                    "extractor": "heuristic",
                    "constraint_quote": quote[:300],
                    "category": category,
                },
                "requires_fixture": requires_fixture,
            }

        # Heuristic-kind → behavioral-category mapping. Every heuristic scenario
        # also carries a `category:<behavior>` tag so the frontend can render
        # the same chip palette across heuristic + LLM scenarios. Mapping mirrors
        # the 8 behavioral categories from the LLM extractor (`mdk_eval.ingest.
        # extractors.llm.CATEGORIES`).
        HEURISTIC_TO_BEHAVIORAL: dict[str, str] = {
            "happy":                  "standard",       # baseline correctness
            "schema":                 "standard",       # declared structured-output check
            "tool_sequence":          "standard",       # declared workflow check
            "edge_threshold":         "edge",           # boundary at the value
            "edge_threshold_above":   "edge",           # boundary just above
            "default_rule":           "edge",           # default-handling boundary
            "forbidden":              "safety",         # refuse banned content
            "failure":                "edge",           # declared failure path
            "slo":                    "performance",    # latency SLO check
        }

        def _tags(*literal: str) -> list[str]:
            """Compose the tag list. Inputs are literal tags like
            `derived:schema`; we look up the behavioral category from the
            `derived:<kind>` tag and prepend `category:<behavior>` so every
            heuristic scenario carries a category pill the same way LLM
            scenarios do.
            """
            tags = list(literal)
            for t in literal:
                if t.startswith("derived:"):
                    kind = t.split(":", 1)[1]
                    cat = HEURISTIC_TO_BEHAVIORAL.get(kind)
                    if cat:
                        tags.append(f"category:{cat}")
                    break  # one derived:<kind> per scenario
            return tags

        common_inputs = self._inputs_payload(spec.inputs)
        slo_ms = spec.slo_latency_ms.value if spec.slo_latency_ms else None
        schema = spec.output_schema.value if spec.output_schema else None
        required_keys = [f.value for f in spec.output_keys]
        tools = [f.value for f in spec.tools]
        tool_sequence = [f.value for f in spec.tool_sequence] or tools

        # 1. happy path
        if common_inputs is not None:
            scenarios.append(Scenario(
                id=f"{name}__happy_path",
                tags=_tags("unverified", "derived:happy", "requires_fixture"),
                severity=Severity.HIGH,
                description="Valid input, all required fields present. Expect normal completion.",
                input=common_inputs,
                expected_schema=schema,
                required_fields=required_keys,
                expected_tools=[ToolExpectation(name=t, required=True) for t in tools],
                workflow=WorkflowExpectation(must_visit=tool_sequence, ordered_subsequence=tool_sequence or None),
                latency_budget_ms=slo_ms,
                meta=_meta("happy", "agent should succeed on a complete input", requires_fixture=True),
            ))

        # 2. schema conformance + no markdown fences
        if schema or required_keys or spec.forbidden_phrases:
            forbidden = sorted({f.value for f in spec.forbidden_phrases if f.value})
            scenarios.append(Scenario(
                id=f"{name}__schema_conformance",
                tags=_tags("unverified", "derived:schema"),
                severity=Severity.HIGH,
                description="Output must conform to the declared JSON schema and contain no markdown fences.",
                input=common_inputs or {"prompt": "produce an output"},
                expected_schema=schema,
                required_fields=required_keys,
                forbidden_phrases=forbidden,
                meta=_meta("schema",
                          (spec.output_schema.quote if spec.output_schema else "")
                          + " | " + "; ".join(f.quote for f in spec.forbidden_phrases)),
            ))

        # 3. tool sequence
        if tool_sequence:
            scenarios.append(Scenario(
                id=f"{name}__tool_sequence",
                tags=_tags("unverified", "derived:tool_sequence", "requires_fixture"),
                severity=Severity.HIGH,
                description=f"Required tool sequence: {' → '.join(tool_sequence)}.",
                input=common_inputs or {"prompt": "exercise the standard path"},
                expected_tools=[ToolExpectation(name=t, required=True) for t in tools],
                workflow=WorkflowExpectation(must_visit=tool_sequence, ordered_subsequence=tool_sequence),
                latency_budget_ms=slo_ms,
                meta=_meta("tool_sequence", "; ".join(f.quote for f in spec.tool_sequence), requires_fixture=True),
            ))

        # 4 & 5. threshold edge cases (at and just-above)
        for thr in spec.thresholds[:1]:    # take first declared threshold only
            amt = thr.value["amount"]
            cur = thr.value["currency"] or "EUR"
            scenarios.append(Scenario(
                id=f"{name}__edge_threshold_at_{amt}",
                tags=_tags("unverified", "derived:edge_threshold", "requires_fixture"),
                severity=Severity.HIGH,
                description=f"Boundary case: value at {cur}{amt} should be treated as below threshold.",
                input=common_inputs or {},
                meta=_meta("edge_threshold_at", thr.quote, requires_fixture=True),
            ))
            scenarios.append(Scenario(
                id=f"{name}__edge_threshold_above_{amt}",
                tags=_tags("unverified", "derived:edge_threshold_above", "requires_fixture"),
                severity=Severity.HIGH,
                description=f"Above threshold: value > {cur}{amt} must trigger the manual-route path.",
                input=common_inputs or {},
                meta=_meta("edge_threshold_above", thr.quote, requires_fixture=True),
            ))

        # 6. default rules (e.g. missing region -> 'global')
        for d in spec.default_rules:
            field_, default = d.value["field"], d.value["default"]
            scenarios.append(Scenario(
                id=f"{name}__default_rule_{field_}",
                tags=_tags("unverified", "derived:default_rule", "requires_fixture"),
                severity=Severity.HIGH,
                description=f"When '{field_}' is missing, agent must default to '{default}' AND list it under missing_fields.",
                input=common_inputs or {},
                required_fields=required_keys,
                meta=_meta("default_rule", d.quote, requires_fixture=True),
            ))

        # 7. forbidden enrichment / output fields
        if spec.forbidden_fields:
            forbidden = sorted({f.value for f in spec.forbidden_fields})
            scenarios.append(Scenario(
                id=f"{name}__forbidden_fields",
                tags=_tags("unverified", "derived:forbidden"),
                severity=Severity.HIGH,
                description="Agent output must not contain any forbidden / enrichment field names.",
                input=common_inputs or {"prompt": "produce a routine output"},
                forbidden_phrases=[f for f in forbidden if len(f) > 2],
                meta=_meta("forbidden_fields", "; ".join(f.quote for f in spec.forbidden_fields)),
            ))

        # 8. failure behavior (e.g. SAP fetch failure -> reject_incomplete + escalate)
        for fb in spec.failure_behaviors:
            scenarios.append(Scenario(
                id=f"{name}__failure_handling",
                tags=_tags("unverified", "derived:failure", "requires_fixture"),
                severity=Severity.CRITICAL,
                description="Failure path: declared failure trigger must produce the declared fallback output.",
                input=common_inputs or {},
                meta=_meta("failure", fb.quote[:300], requires_fixture=True),
            ))
            break

        # 9. SLO latency (cross-cutting; one explicit SLO scenario)
        if slo_ms:
            scenarios.append(Scenario(
                id=f"{name}__slo_latency",
                tags=_tags("unverified", "derived:slo"),
                severity=Severity.MEDIUM,
                description=f"P95 latency must stay within {slo_ms} ms.",
                input=common_inputs or {"prompt": "ping"},
                latency_budget_ms=slo_ms,
                meta=_meta("slo", spec.slo_latency_ms.quote if spec.slo_latency_ms else ""),
            ))

        return scenarios

    def _inputs_payload(self, inputs: list[ExtractedFact]) -> dict[str, Any] | None:
        if not inputs:
            return None
        # produce a placeholder payload the user fills in for real fixtures
        return {f.value: f"<fill in {f.value}>" for f in inputs}

    # ----------------------------- warnings -----------------------------

    def _collect_warnings(self, obj: dict[str, Any], spec: ExtractedAgentSpec) -> list[str]:
        warnings: list[str] = []
        if not spec.tools:
            warnings.append("No tools could be extracted from agent_instructions — tool-usage scenarios will be weak.")
        if not (spec.output_schema or spec.output_keys):
            warnings.append("No output schema detected — schema_conformance scenario will not validate structure.")
        if not spec.slo_latency_ms:
            warnings.append("No SLO latency declared — latency thresholds left unset.")
        if obj.get("temperature") and float(obj["temperature"]) > 0.0:
            warnings.append(f"Agent temperature={obj['temperature']} (>0) — multi-run consistency expected to be lower.")
        if not obj.get("_id"):
            warnings.append("No agent _id in source JSON — wire agent_id manually before running 'mdk-eval run'.")
        return warnings

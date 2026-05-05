"""PromptFoo export (path A).

Emits a self-contained `promptfoo.yaml` from our dataset so prompt-engineers can
run their existing PromptFoo workflow against the same scenarios.

Mapping decisions:
  - Each scenario -> one promptfoo `tests` entry with vars: {prompt, ...}.
  - The prompt template is `{{prompt}}` so PromptFoo passes the raw input through.
  - Provider is `http` pointed at the agent endpoint (configurable). For full
    coverage of tool-calls and trace data, customers can swap to a custom JS
    provider that calls our adapter (documented in README).
  - Assertions emitted from scenario constraints:
      forbidden_phrases  -> assert: contains-any (negated via `not-` prefix)
      required_fields    -> assert: javascript snippet checking JSONPath presence
      expected_schema    -> assert: is-json (and javascript schema validate)
      expected_tools     -> annotated as a comment (PromptFoo cannot validate
                            tool-call traces without the custom JS provider).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ..models import Scenario


def _required_field_assertion(field: str) -> dict[str, Any]:
    # Use is-json + a small JS check to walk dotted paths.
    return {
        "type": "javascript",
        "value": (
            "() => {{\n"
            "  const out = JSON.parse(output);\n"
            "  const path = {path};\n"
            "  let cur = out;\n"
            "  for (const p of path) {{ if (cur == null) return {{pass:false, reason: 'missing '+path.join('.')}}; cur = cur[p]; }}\n"
            "  if (cur === undefined || cur === null || cur === '' ) return {{pass:false, reason:'empty '+path.join('.')}};\n"
            "  return {{pass:true}};\n"
            "}}"
        ).format(path=json.dumps(field.split("."))),
    }


def scenario_to_promptfoo_test(s: Scenario) -> dict[str, Any]:
    asserts: list[dict[str, Any]] = []

    # forbidden phrases (case-insensitive contains, negated)
    for phrase in s.forbidden_phrases:
        asserts.append({"type": "not-contains", "value": phrase, "case-sensitive": False})

    # required fields (each becomes a JS assertion)
    if s.required_fields:
        asserts.append({"type": "is-json"})
        for field in s.required_fields:
            asserts.append(_required_field_assertion(field))

    # expected schema -> is-json + javascript JSONSchema validation
    if s.expected_schema:
        asserts.append({"type": "is-json"})
        asserts.append({
            "type": "javascript",
            "value": (
                "(async () => {{\n"
                "  const Ajv = require('ajv');\n"
                "  const ajv = new Ajv({{strict:false}});\n"
                "  const validate = ajv.compile({schema});\n"
                "  const out = JSON.parse(output);\n"
                "  const ok = validate(out);\n"
                "  return ok ? {{pass:true}} : {{pass:false, reason: ajv.errorsText(validate.errors)}};\n"
                "}})()"
            ).format(schema=json.dumps(s.expected_schema)),
        })

    # expected output similarity (semantic) — only added when present
    if s.expected_output:
        asserts.append({"type": "similar", "value": s.expected_output, "threshold": 0.6})

    # latency budget
    if s.latency_budget_ms:
        asserts.append({"type": "latency", "threshold": s.latency_budget_ms})

    # tool expectations: PromptFoo can't see tool calls without custom provider; emit metadata
    metadata: dict[str, Any] = {
        "id": s.id,
        "tags": s.tags,
        "severity": s.severity.value,
    }
    if s.expected_tools:
        metadata["expected_tools"] = [t.model_dump() for t in s.expected_tools]
    if s.workflow.must_visit or s.workflow.ordered_subsequence or s.workflow.must_not_visit:
        metadata["expected_workflow"] = s.workflow.model_dump()

    prompt_text = s.input.get("prompt") or s.input.get("input") or json.dumps(s.input)
    test: dict[str, Any] = {
        "description": s.description or s.id,
        "vars": {"prompt": prompt_text},
        "metadata": metadata,
    }
    if asserts:
        test["assert"] = asserts
    return test


def build_promptfoo_config(
    scenarios: list[Scenario],
    *,
    endpoint: str | None,
    request_template: dict[str, Any] | None = None,
    response_text_path: str = "$.response",
    description: str = "Movate Agent Assurance — exported scenarios",
) -> dict[str, Any]:
    """Produce a promptfoo config dict ready to be dumped to YAML."""
    if endpoint:
        body = request_template or {"input": "{{prompt}}"}
        # promptfoo http provider supports `body` + `transformResponse` for picking
        # the answer out of the agent's JSON response.
        provider: dict[str, Any] = {
            "id": "http",
            "config": {
                "url": endpoint,
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": body,
                # JSONPath for extracting the response text
                "transformResponse": f"json => json{_to_js_path(response_text_path)}",
            },
        }
        providers: list[Any] = [provider]
    else:
        # placeholder provider; user will fill in
        providers = [{"id": "echo", "config": {}}]

    return {
        "description": description,
        "prompts": ["{{prompt}}"],   # raw passthrough — let our adapter format the body
        "providers": providers,
        "tests": [scenario_to_promptfoo_test(s) for s in scenarios],
    }


def write_promptfoo_yaml(scenarios: list[Scenario], out_path: Path, **kw: Any) -> Path:
    cfg = build_promptfoo_config(scenarios, **kw)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False, width=120)
    return out_path


def _to_js_path(jsonpath: str) -> str:
    """Convert '$.a.b[0]' -> '?.a?.b?.[0]' for safe JS extraction."""
    p = jsonpath.lstrip("$").lstrip(".")
    if not p:
        return ""
    out = ""
    for token in p.replace("[", ".[").split("."):
        if not token:
            continue
        if token.startswith("["):
            out += f"?.{token}"
        else:
            out += f"?.{token}"
    return out

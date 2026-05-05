"""agent_card.md generator. Stakeholder-friendly one-pager."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from ..models import Scenario
from .extractors.heuristic import ExtractedAgentSpec


def build_agent_card(
    *,
    agent_obj: dict[str, Any],
    spec: ExtractedAgentSpec,
    scenarios: list[Scenario],
    source_path: str,
    source_sha256: str,
) -> str:
    name = agent_obj.get("name") or "Agent"
    role = agent_obj.get("agent_role") or "—"
    desc = agent_obj.get("description") or "—"
    goal = (agent_obj.get("agent_goal") or "").strip()
    model = agent_obj.get("model") or "—"
    provider = agent_obj.get("provider_id") or "—"
    temp = agent_obj.get("temperature")
    version = agent_obj.get("version") or "—"

    tools = [f.value for f in spec.tools]
    seq = [f.value for f in spec.tool_sequence]
    inputs = [f.value for f in spec.inputs]
    slo = spec.slo_latency_ms.value if spec.slo_latency_ms else None
    schema_keys = [f.value for f in spec.output_keys]
    forbidden_phrases = sorted({f.value for f in spec.forbidden_phrases if f.value})
    forbidden_fields = sorted({f.value for f in spec.forbidden_fields})

    by_category: Counter = Counter()
    for s in scenarios:
        for t in s.tags:
            if t.startswith("derived:"):
                by_category[t.split(":", 1)[1]] += 1

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    md_lines: list[str] = []
    md_lines.append(f"# Agent Card — {name}")
    md_lines.append("")
    md_lines.append(f"_Generated {ts} by mdk-eval ingest._")
    md_lines.append("")
    md_lines.append(f"**Role**: {role}")
    md_lines.append(f"**Version**: {version}")
    md_lines.append(f"**Model**: {provider} / `{model}`" + (f" (temp {temp})" if temp is not None else ""))
    md_lines.append("")
    md_lines.append(f"**Description**: {desc}")
    md_lines.append("")
    if goal:
        md_lines.append("## Mandate")
        md_lines.append("")
        md_lines.append(goal)
        md_lines.append("")

    md_lines.append("## Declared interface")
    md_lines.append("")
    md_lines.append(f"- **Inputs**: {', '.join(inputs) if inputs else '—'}")
    md_lines.append(f"- **Tools** ({len(tools)}): {', '.join(tools) if tools else '—'}")
    if seq:
        md_lines.append(f"- **Tool sequence**: {' → '.join(seq)}")
    if schema_keys:
        md_lines.append(f"- **Output keys** ({len(schema_keys)}): {', '.join(schema_keys)}")
    if slo:
        md_lines.append(f"- **SLO latency**: {slo} ms (p95)")
    if forbidden_phrases:
        md_lines.append(f"- **Forbidden phrases**: {', '.join(forbidden_phrases)}")
    if forbidden_fields:
        md_lines.append(f"- **Forbidden fields**: {', '.join(forbidden_fields)}")
    md_lines.append("")

    md_lines.append("## Derived test suite")
    md_lines.append("")
    md_lines.append(f"**{len(scenarios)} scenarios** generated heuristically. All marked `unverified` until you review.")
    md_lines.append("")
    if by_category:
        md_lines.append("| Category | Count |")
        md_lines.append("|---|---:|")
        for cat, n in sorted(by_category.items(), key=lambda kv: -kv[1]):
            md_lines.append(f"| {cat} | {n} |")
        md_lines.append("")

    fixture_count = sum(1 for s in scenarios if s.meta.get("requires_fixture"))
    if fixture_count:
        md_lines.append(f"⚠ **{fixture_count} scenarios** are tagged `requires_fixture` — they need realistic input "
                        "data before they exercise the live agent meaningfully.")
        md_lines.append("")

    md_lines.append("## Review checklist")
    md_lines.append("")
    md_lines.append("Before promoting derived scenarios to verified status:")
    md_lines.append("")
    md_lines.append("1. Open `datasets/<name>.jsonl` and skim each scenario.")
    md_lines.append("2. For scenarios tagged `requires_fixture`, fill in real input payloads.")
    md_lines.append("3. Confirm `expected_schema` and `forbidden_phrases` match agent intent (the heuristic extractor is conservative but not infallible).")
    md_lines.append("4. Remove the `unverified` tag (or use `mdk-eval ingest --approve <dataset>`).")
    md_lines.append("5. Run `mdk-eval run --config configs/<name>.yaml --runs 3`.")
    md_lines.append("")

    md_lines.append("## Provenance")
    md_lines.append("")
    md_lines.append(f"- Source: `{source_path}`")
    md_lines.append(f"- SHA-256: `{source_sha256[:16]}…`")
    md_lines.append("- Extractor: `heuristic` (no LLM)")
    md_lines.append("")

    return "\n".join(md_lines)

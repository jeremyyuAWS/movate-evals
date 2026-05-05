"""Heuristic extractor for free-form agent instructions.

Pure stdlib regex/parsing — no LLM. The job is to pull *structured* facts that
are unambiguously declared in the instructions: tools, schema, sequence,
forbidden phrases, thresholds, defaults, failure modes, SLO. Anything fuzzy is
left for an optional LLM extractor (Phase 2).

Constraint quotes are preserved on every extracted fact so derived scenarios
can carry provenance back to the agent definition.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


# ----------------------------- data shape -----------------------------


@dataclass
class ExtractedFact:
    value: Any
    quote: str   # the substring of source instructions this came from


@dataclass
class ExtractedAgentSpec:
    inputs: list[ExtractedFact] = field(default_factory=list)              # input field names
    tools: list[ExtractedFact] = field(default_factory=list)               # tool names
    tool_sequence: list[ExtractedFact] = field(default_factory=list)       # ordered tool names
    output_schema: ExtractedFact | None = None                             # parsed JSON schema-ish
    output_keys: list[ExtractedFact] = field(default_factory=list)         # top-level required keys
    forbidden_phrases: list[ExtractedFact] = field(default_factory=list)
    forbidden_fields: list[ExtractedFact] = field(default_factory=list)    # field names that must NOT appear
    thresholds: list[ExtractedFact] = field(default_factory=list)          # {"name", "value", "unit"}
    default_rules: list[ExtractedFact] = field(default_factory=list)       # {"field", "default", "side_effect"}
    failure_behaviors: list[ExtractedFact] = field(default_factory=list)
    slo_latency_ms: ExtractedFact | None = None


# ----------------------------- regex helpers -----------------------------


_RE_SECTION  = re.compile(r"^\s{0,3}#+\s+(.+?)\s*$", re.MULTILINE)
_RE_BULLET   = re.compile(r"^\s*-\s+(.+)$", re.MULTILINE)
_RE_NUMBERED = re.compile(r"^\s*\d+\.\s+(.+)$", re.MULTILINE)
_RE_TOOL     = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\(", re.IGNORECASE)
_RE_BUDGET   = re.compile(
    r"(?P<currency>€|\$|USD|EUR)?\s*(?P<amt>\d{1,3}(?:[,.]\d{3})*|\d+)\s*(?P<unit>[KkMm])?",
)
_RE_SLO      = re.compile(
    r"\bp\d+\s+latency\b[^0-9]{0,8}(?P<n>[\d.]+)\s*(?P<u>ms|s|sec|seconds|min|minutes)\b",
    re.IGNORECASE,
)


# ----------------------------- top-level -----------------------------


def extract(instructions: str) -> ExtractedAgentSpec:
    """Run all extractors; return a single populated ExtractedAgentSpec."""
    spec = ExtractedAgentSpec()
    sections = _split_sections(instructions)

    # Only the explicit Inputs section is authoritative. Falling back to the whole
    # document conflates tools / random bullets with inputs.
    spec.inputs = _extract_inputs(sections.get("inputs", ""))
    spec.tools = _extract_tools(sections, instructions)
    spec.tool_sequence = _extract_tool_sequence(sections)
    spec.output_schema, spec.output_keys = _extract_output_schema(sections, instructions)
    spec.forbidden_phrases = _extract_forbidden_phrases(instructions)
    spec.forbidden_fields = _extract_forbidden_fields(instructions)
    spec.thresholds = _extract_thresholds(instructions)
    spec.default_rules = _extract_default_rules(instructions)
    spec.failure_behaviors = _extract_failure_behaviors(sections)
    spec.slo_latency_ms = _extract_slo(instructions)

    return spec


# ----------------------------- section splitter -----------------------------


def _split_sections(text: str) -> dict[str, str]:
    """Return lower-cased section key -> body (without the header line).

    First occurrence wins. Agent docs frequently repeat headers ('# Inputs'
    followed by '## Inputs' inside operating instructions); the first is the
    authoritative declaration.
    """
    out: dict[str, str] = {}
    matches = list(_RE_SECTION.finditer(text))
    for i, m in enumerate(matches):
        title = m.group(1).strip().lower()
        for key in ("inputs", "tools available", "required tool sequence",
                    "required output", "output rules", "constraints",
                    "failure behavior", "slo", "mandate", "outputs"):
            if title.startswith(key):
                k = key.replace(" ", "_")
                if k in out:        # first occurrence wins
                    break
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                out[k] = text[start:end].strip()
                break
    return out


# ----------------------------- inputs -----------------------------


def _extract_inputs(body: str) -> list[ExtractedFact]:
    """Pull bulleted '- name (required)' style input names."""
    facts: list[ExtractedFact] = []
    for line in _RE_BULLET.findall(body or ""):
        # match: name (required)  OR  name — desc  OR  name: desc
        m = re.match(r"^([a-z_][a-z0-9_]*)\b", line.strip(), re.IGNORECASE)
        if m:
            facts.append(ExtractedFact(value=m.group(1), quote=line.strip()))
    return facts


# ----------------------------- tools -----------------------------


def _extract_tools(sections: dict[str, str], full: str) -> list[ExtractedFact]:
    body = sections.get("tools_available", "")
    facts: list[ExtractedFact] = []
    seen: set[str] = set()
    # bulleted lines first (most reliable)
    for line in _RE_BULLET.findall(body):
        m = _RE_TOOL.search(line)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            facts.append(ExtractedFact(value=m.group(1), quote=line.strip()))
    # fallback: scan all `name(...)` calls in the doc
    if not facts:
        for line in full.splitlines():
            m = _RE_TOOL.search(line)
            if m and m.group(1) not in seen and m.group(1) not in {"if", "for", "while", "raises", "is", "in", "or"}:
                seen.add(m.group(1))
                facts.append(ExtractedFact(value=m.group(1), quote=line.strip()))
    return facts


# ----------------------------- tool sequence -----------------------------


def _extract_tool_sequence(sections: dict[str, str]) -> list[ExtractedFact]:
    body = sections.get("required_tool_sequence", "")
    if not body:
        return []
    facts: list[ExtractedFact] = []
    for line in _RE_NUMBERED.findall(body):
        m = _RE_TOOL.search(line)
        if m:
            facts.append(ExtractedFact(value=m.group(1), quote=line.strip()))
    return facts


# ----------------------------- output schema -----------------------------


def _extract_output_schema(
    sections: dict[str, str], full: str,
) -> tuple[ExtractedFact | None, list[ExtractedFact]]:
    """Find the first JSON-ish object block under 'Required output' (or anywhere).

    Replaces angle-bracket placeholders like <raw.id> with strings so json.loads
    can parse a relaxed grammar; reports the discovered top-level keys.
    """
    body = sections.get("required_output", "") or full
    block = _find_first_balanced_braces(body)
    if not block:
        return None, []
    relaxed = _relax_jsonish(block)
    try:
        parsed = json.loads(relaxed)
    except json.JSONDecodeError:
        return None, []
    if not isinstance(parsed, dict):
        return None, []

    keys = [ExtractedFact(value=k, quote=f'"{k}": ...') for k in parsed.keys()]
    schema = _to_jsonschema(parsed)
    return ExtractedFact(value=schema, quote=block.strip()[:400]), keys


def _find_first_balanced_braces(s: str) -> str | None:
    """Return the first {...} block whose braces balance."""
    depth = 0
    start = -1
    for i, c in enumerate(s):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return s[start : i + 1]
    return None


def _relax_jsonish(text: str) -> str:
    """Convert angle-bracket placeholders (<raw.id>) to JSON strings.

    Only wraps `<...>` instances that are NOT already inside JSON string quotes,
    so `"brief_id": "<raw.id>"` (already a valid JSON string) is left alone.
    """
    out = re.sub(r'(?<!")<([^<>]+)>(?!")', r'"<\1>"', text)
    # trailing commas before } or ]
    out = re.sub(r",(\s*[}\]])", r"\1", out)
    return out


def _to_jsonschema(obj: Any) -> dict[str, Any]:
    """Best-effort JSON sample -> JSON Schema. Top-level only."""
    if isinstance(obj, dict):
        props = {k: _to_jsonschema(v) for k, v in obj.items()}
        return {"type": "object", "required": list(obj.keys()), "properties": props}
    if isinstance(obj, list):
        items = _to_jsonschema(obj[0]) if obj else {}
        return {"type": "array", "items": items}
    if isinstance(obj, bool):
        return {"type": "boolean"}
    if isinstance(obj, int):
        return {"type": "integer"}
    if isinstance(obj, float):
        return {"type": "number"}
    return {"type": "string"}


# ----------------------------- forbidden output -----------------------------


def _extract_forbidden_phrases(text: str) -> list[ExtractedFact]:
    facts: list[ExtractedFact] = []
    # specific phrasings agents commonly forbid
    rules = [
        (r"no markdown fences", ["```", "```json"]),
        (r"no commentary", []),  # tone, no specific phrase
    ]
    for pat, phrases in rules:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            quote = _line_containing(text, m.start())
            for p in phrases:
                facts.append(ExtractedFact(value=p, quote=quote))
    return facts


def _extract_forbidden_fields(text: str) -> list[ExtractedFact]:
    """Catch 'do NOT emit X, Y, Z' style enumerations.

    Two patterns:
      A. Inline list:  'do NOT emit X, Y, Z'
      B. Antecedent list: '... fields (X, Y, Z) ... do NOT emit them'
         (the parenthesized list precedes the imperative)
    """
    facts: list[ExtractedFact] = []

    # Pattern A: directly enumerated after the imperative
    pat_inline = re.compile(
        r"(?:do\s+NOT\s+emit|must\s+not\s+emit|do\s+not\s+include|must\s+not\s+include)\b\s*([^.()\n]+)",
        re.IGNORECASE,
    )
    for m in pat_inline.finditer(text):
        clause = m.group(0)
        tail = m.group(1) or ""
        # require comma-separated identifier list, otherwise skip (avoids 'do NOT emit them')
        if "," in tail or re.search(r"\b[a-z][a-z0-9_]{4,}\b", tail):
            for token in re.findall(r"\b[a-z][a-z0-9_]{3,}\b", tail):
                if token in _FORBIDDEN_STOPWORDS:
                    continue
                facts.append(ExtractedFact(value=token, quote=clause.strip()))

    # Pattern B: parenthesized list near 'do NOT emit'.
    # Both the parens body and the gap between ')' and the imperative may span
    # newlines, so don't exclude '\n' from either character class.
    pat_paren = re.compile(
        r"\(([^()]{5,400}?)\)[^.()]{0,200}?(?:do\s+NOT\s+emit|must\s+not\s+emit)\b",
        re.IGNORECASE | re.DOTALL,
    )
    for m in pat_paren.finditer(text):
        inside = m.group(1)
        for token in re.findall(r"\b[a-z][a-z0-9_]{3,}\b", inside):
            if token in _FORBIDDEN_STOPWORDS:
                continue
            facts.append(ExtractedFact(value=token, quote=m.group(0).strip()))

    # dedupe preserving first occurrence
    seen: set[str] = set()
    out: list[ExtractedFact] = []
    for f in facts:
        if f.value in seen:
            continue
        seen.add(f.value)
        out.append(f)
    return out


_FORBIDDEN_STOPWORDS = {
    "emit", "include", "not", "the", "and", "or", "for", "are", "your", "output",
    "added", "runner", "framework", "them", "you", "this", "that", "these", "those",
    "have", "with", "from", "into", "fields", "field", "object", "objects",
}


# ----------------------------- thresholds -----------------------------


def _extract_thresholds(text: str) -> list[ExtractedFact]:
    """Find numeric thresholds with currency / units near the word 'threshold'.

    Example matches:
      '€50K budget gate', 'above €50K', 'threshold of $1M', 'p95 latency ≤ 2s'
    """
    facts: list[ExtractedFact] = []
    for m in re.finditer(
        r"(?P<cur>€|\$|EUR|USD)?\s?(?P<amt>\d{1,3}(?:[,.]\d{3})*|\d+)\s?(?P<u>[KkMm])?\b",
        text,
    ):
        # require a 'threshold' or 'budget' or 'gate' or 'above' or 'below' nearby
        ctx_start = max(0, m.start() - 60)
        ctx_end = min(len(text), m.end() + 60)
        context = text[ctx_start:ctx_end].lower()
        if not any(k in context for k in ("threshold", "budget", "gate", "above", "below", "exactly", "at exactly")):
            continue
        amt = m.group("amt").replace(",", "").replace(".", "")
        try:
            n = int(amt)
        except ValueError:
            continue
        unit = (m.group("u") or "").lower()
        if unit == "k":
            n *= 1_000
        elif unit == "m":
            n *= 1_000_000
        currency = m.group("cur") or ""
        facts.append(ExtractedFact(
            value={"amount": n, "currency": currency.strip()},
            quote=text[ctx_start:ctx_end].strip(),
        ))
    # dedupe by amount+currency
    seen = set()
    uniq: list[ExtractedFact] = []
    for f in facts:
        key = (f.value["amount"], f.value["currency"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(f)
    return uniq


# ----------------------------- default rules -----------------------------


def _extract_default_rules(text: str) -> list[ExtractedFact]:
    """Find rules of the form 'X is required ... if missing, set to Y' or
    'if X is missing, set to Y'. Antecedent X may appear before or after 'if'.
    """
    facts: list[ExtractedFact] = []
    seen: set[str] = set()

    # Pattern A: 'if X (is) missing, set [it] to Y'
    pat_a = re.compile(
        r"if\s+([a-z_]+)\s+(?:is\s+)?missing[^.]*?(?:set\s+(?:it\s+)?to|default(?:s)?\s+to)\s+[\"']?([^\"'\n.]+?)[\"']?(?:[.;]|\s+AND\s)",
        re.IGNORECASE,
    )
    for m in pat_a.finditer(text):
        field_ = m.group(1).strip().lower()
        if field_ in seen:
            continue
        seen.add(field_)
        facts.append(ExtractedFact(
            value={"field": field_, "default": m.group(2).strip()},
            quote=m.group(0).strip(),
        ))

    # Pattern B: 'X is required ... if missing, set [it] to Y'
    pat_b = re.compile(
        r"\b([A-Z][a-z_]*|[a-z_]+)\s+is\s+required\b[^.]{0,160}?if\s+missing[^.]*?(?:set\s+(?:it\s+)?to|default(?:s)?\s+to)\s+[\"']?([^\"'\n.]+?)[\"']?(?:[.;]|\s+AND\s|$)",
        re.IGNORECASE | re.DOTALL,
    )
    for m in pat_b.finditer(text):
        field_ = m.group(1).strip().lower()
        if field_ in seen:
            continue
        seen.add(field_)
        facts.append(ExtractedFact(
            value={"field": field_, "default": m.group(2).strip()},
            quote=m.group(0).strip(),
        ))

    return facts


# ----------------------------- failure behaviors -----------------------------


def _extract_failure_behaviors(sections: dict[str, str]) -> list[ExtractedFact]:
    body = sections.get("failure_behavior", "")
    if not body:
        return []
    return [ExtractedFact(value=body.strip(), quote=body.strip())]


# ----------------------------- SLO -----------------------------


def _extract_slo(text: str) -> ExtractedFact | None:
    m = _RE_SLO.search(text)
    if not m:
        return None
    n = float(m.group("n"))
    u = m.group("u").lower()
    ms = int(n * (1 if u == "ms" else 1000 if u in ("s", "sec", "seconds") else 60_000))
    return ExtractedFact(value=ms, quote=_line_containing(text, m.start()))


# ----------------------------- helpers -----------------------------


def _line_containing(text: str, idx: int) -> str:
    start = text.rfind("\n", 0, idx) + 1
    end = text.find("\n", idx)
    return text[start: end if end >= 0 else len(text)].strip()

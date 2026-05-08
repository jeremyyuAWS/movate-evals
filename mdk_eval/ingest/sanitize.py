"""Strip secret-shaped fields from uploaded agent definitions.

Lyzr (and similar) agent JSON exports often include the producer's API key,
RAG credentials, and similar tokens alongside the actual definition. We must
not persist these to our DB, send them to LLM extractors, or write them into
audit logs.

Approach: a recursive walk that nulls/redacts fields whose names suggest a
secret. Conservative — false positives are fine (a redacted "api_key" never
hurts anyone); false negatives are not (a leaked real key breaks trust).

What this is NOT:
- A general PII scrubber. Use a dedicated service for production PII work.
- A secret-detection scanner. We only catch *named* fields, not embedded
  high-entropy strings inside `agent_instructions` text.
"""
from __future__ import annotations

import re
from typing import Any

# Field names whose values are always considered secret. Match is
# case-insensitive and substring-based against the bottom-level key.
_SECRET_KEY_PATTERNS = re.compile(
    r"(api[_\-]?key|secret|password|passwd|token|credentials?|bearer|auth(?:orization)?_value)",
    re.IGNORECASE,
)

# Sentinel that replaces the secret value. Distinct enough to grep for if a
# downstream consumer wants to verify scrubbing happened.
REDACTED = "[REDACTED]"

# How many bytes from each redaction we record (for audit), without leaking
# the full secret. 4 chars is enough to disambiguate without enabling brute-force.
_PREVIEW_LEN = 4


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_PATTERNS.search(key))


def sanitize(obj: Any, _redactions: list[dict[str, str]] | None = None) -> tuple[Any, list[dict[str, str]]]:
    """Return (scrubbed_object, list_of_redactions).

    Each redaction record is `{"path": "<dotted.path>", "preview": "<first-4>"}`
    for audit logging. Never includes the full original value.
    """
    if _redactions is None:
        _redactions = []
    return _walk(obj, "", _redactions), _redactions


def _walk(obj: Any, path: str, redactions: list[dict[str, str]]) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            child_path = f"{path}.{k}" if path else k
            if isinstance(k, str) and _is_secret_key(k) and isinstance(v, str) and v:
                redactions.append({
                    "path": child_path,
                    "preview": v[:_PREVIEW_LEN] + ("…" if len(v) > _PREVIEW_LEN else ""),
                })
                out[k] = REDACTED
            else:
                out[k] = _walk(v, child_path, redactions)
        return out
    if isinstance(obj, list):
        return [_walk(v, f"{path}[{i}]", redactions) for i, v in enumerate(obj)]
    return obj


def sanitize_bytes(raw: bytes) -> tuple[bytes, list[dict[str, str]]]:
    """Convenience: parse JSON, sanitize, re-serialize. Returns sanitized bytes
    + redactions. Used by the ingest endpoint to scrub before LLM/DB."""
    import json

    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Non-JSON or non-UTF8 — pass through unchanged. Caller decides how to handle.
        return raw, []
    sanitized, redactions = sanitize(obj)
    return json.dumps(sanitized, indent=2).encode("utf-8"), redactions

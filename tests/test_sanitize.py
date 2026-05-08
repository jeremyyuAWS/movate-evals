"""Secret-stripper for uploaded agent definitions.

The sanitizer is conservative: any field whose name matches the secret
pattern is redacted, nested or not. We test:
- Common secret keys are redacted (api_key, password, secret, token, credentials, bearer)
- Casing variations are caught (API_KEY, ApiKey, etc.)
- Non-secret fields are preserved untouched
- Nested dicts and lists-of-dicts are walked
- The redaction record carries a useful preview without leaking the full value
- Non-JSON / non-UTF8 bytes pass through unchanged (safe degradation)
"""
from __future__ import annotations

import json

from mdk_eval.ingest.sanitize import REDACTED, sanitize, sanitize_bytes


def test_redacts_top_level_api_key():
    obj = {"name": "agent", "api_key": "sk-secret-12345", "model": "gpt-4o"}
    cleaned, red = sanitize(obj)
    assert cleaned["api_key"] == REDACTED
    assert cleaned["name"] == "agent"            # untouched
    assert cleaned["model"] == "gpt-4o"          # untouched
    assert len(red) == 1
    assert red[0]["path"] == "api_key"
    assert red[0]["preview"].startswith("sk-s")


def test_case_insensitive_match():
    obj = {"API_KEY": "x", "ApiKey": "y", "apikey": "z", "Api-Key": "q"}
    cleaned, red = sanitize(obj)
    assert all(v == REDACTED for v in cleaned.values()), f"all should be redacted: {cleaned}"
    assert len(red) == 4


def test_preserves_non_secret_fields():
    obj = {
        "name": "agent",
        "description": "harmless prose",
        "settings": {"temperature": 0.7, "model": "gpt-4o"},
    }
    cleaned, red = sanitize(obj)
    assert cleaned == obj
    assert red == []


def test_walks_nested_dicts():
    obj = {
        "features": [
            {"type": "KB", "config": {"api_key": "deep-secret", "rag_id": "abc"}}
        ]
    }
    cleaned, red = sanitize(obj)
    assert cleaned["features"][0]["config"]["api_key"] == REDACTED
    assert cleaned["features"][0]["config"]["rag_id"] == "abc"
    assert any("api_key" in r["path"] for r in red)


def test_redacts_multiple_secret_kinds():
    obj = {
        "api_key": "k1",
        "password": "k2",
        "client_secret": "k3",
        "auth_token": "k4",
        "credentials": "k5",
        "bearer": "k6",
    }
    cleaned, red = sanitize(obj)
    assert all(v == REDACTED for v in cleaned.values())
    assert len(red) == 6


def test_path_in_redaction_record_is_dotted():
    obj = {"a": {"b": {"c": {"api_key": "deep"}}}}
    _, red = sanitize(obj)
    assert red[0]["path"] == "a.b.c.api_key"


def test_preview_never_leaks_full_secret():
    obj = {"api_key": "sk-very-long-secret-value-do-not-leak"}
    _, red = sanitize(obj)
    preview = red[0]["preview"]
    assert preview != "sk-very-long-secret-value-do-not-leak"
    assert len(preview) <= 6                     # 4 chars + ellipsis


def test_empty_string_secret_is_not_redacted():
    """An empty 'api_key': '' is not a leak; don't waste a redaction record on it."""
    obj = {"api_key": "", "name": "x"}
    cleaned, red = sanitize(obj)
    assert cleaned["api_key"] == ""              # left alone
    assert red == []


def test_sanitize_bytes_roundtrip():
    raw = json.dumps({"name": "agent", "api_key": "sk-test"}).encode("utf-8")
    sanitized_raw, red = sanitize_bytes(raw)
    sanitized = json.loads(sanitized_raw)
    assert sanitized["api_key"] == REDACTED
    assert sanitized["name"] == "agent"
    assert len(red) == 1


def test_sanitize_bytes_passes_non_json_through():
    raw = b"this is not json"
    out, red = sanitize_bytes(raw)
    assert out == raw                            # unchanged
    assert red == []                             # no redactions claimed


def test_real_lyzr_agent_with_api_key():
    """End-to-end against a Lyzr-shaped agent JSON like the one a user would upload."""
    agent = {
        "_id": "69f9630789e1a27b8101014b",
        "api_key": "sk-default-D0plT8nq8DdRpw5LR956a7J4Df7Yo2QC",
        "name": "Movate FAQ Assistant",
        "agent_role": "FAQ Assistant",
        "agent_instructions": "Be helpful.",
        "features": [
            {"type": "KNOWLEDGE_BASE", "config": {"lyzr_rag": {"rag_id": "abc"}}}
        ],
    }
    cleaned, red = sanitize(agent)
    assert cleaned["api_key"] == REDACTED
    assert cleaned["_id"] == "69f9630789e1a27b8101014b"     # untouched
    assert cleaned["name"] == "Movate FAQ Assistant"        # untouched
    assert cleaned["features"][0]["config"]["lyzr_rag"]["rag_id"] == "abc"
    assert len(red) == 1
    assert red[0]["path"] == "api_key"

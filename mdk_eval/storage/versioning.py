"""Hashing + versioning helpers. Manifest files always pin these."""
from __future__ import annotations

import hashlib
import importlib.metadata as md
import json
from pathlib import Path
from typing import Any


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_str(s: str) -> str:
    return sha256_bytes(s.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_obj(obj: Any) -> str:
    canonical = json.dumps(obj, sort_keys=True, default=str)
    return sha256_str(canonical)


def installed_version(pkg: str) -> str:
    try:
        return md.version(pkg)
    except md.PackageNotFoundError:
        return "not-installed"


def tool_versions() -> dict[str, str]:
    """Snapshot of installed-package versions that affect eval results.

    Captured at run-execution time and pinned to the run's `tool_versions`
    column. The provenance endpoint also re-introspects this at query time
    so the audit trail can spot drift between execution and inspection.

    Tracked groups:
      - LLM provider SDKs    (openai, anthropic) — what we actually call
      - RAG eval libraries   (deepeval, ragas, trulens-eval) — RAG metric source-of-truth
      - Observability        (langfuse, opentelemetry-api)
      - Core deps            (httpx, pydantic, jinja2) — request/templating layer
      - mdk_eval itself      — pin our own version explicitly
    """
    tracked = [
        # LLM provider SDKs — direct callers
        "openai",
        "anthropic",
        # RAG metric libraries — deepeval is the in-tree integration; ragas
        # and trulens-eval are surface-area for future RAG-metric pluggability.
        # When not installed, `installed_version` returns "not-installed".
        "deepeval",
        "ragas",
        "trulens-eval",
        # Observability
        "langfuse",
        "opentelemetry-api",
        # Core deps
        "httpx",
        "pydantic",
        "jinja2",
        # Our own version — useful for audit cross-reference with manifest
        "mdk-eval",
    ]
    return {p: installed_version(p) for p in tracked}

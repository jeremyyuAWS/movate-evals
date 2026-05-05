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
    tracked = ["openai", "anthropic", "deepeval", "langfuse", "httpx", "pydantic", "jinja2"]
    return {p: installed_version(p) for p in tracked}

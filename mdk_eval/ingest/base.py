"""Ingest base — Protocol + result model + auto-detect.

Every ingestor reads an agent definition (JSON, YAML, MD, Python) and produces a
self-contained IngestionResult: a config, a draft scenario list (all marked
'unverified'), an agent_card markdown summary, and provenance.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..config import RunConfig
from ..models import Scenario


class IngestionResult(BaseModel):
    """What an ingestor returns. Caller writes these to disk."""
    name: str                              # safe identifier, e.g. "brief_ingestion"
    source_format: str                     # "lyzr" | "langgraph" | "markdown" | ...
    source_path: str
    source_sha256: str
    config: RunConfig
    scenarios: list[Scenario]
    agent_card_md: str
    warnings: list[str] = Field(default_factory=list)


@runtime_checkable
class IngestSource(Protocol):
    """Protocol every ingestor must implement."""
    name: str                              # source-format identifier (e.g., "lyzr")

    def detect(self, path: Path, raw: bytes) -> bool: ...
    def ingest(self, path: Path, raw: bytes) -> IngestionResult: ...


def auto_detect(path: Path) -> str:
    """Return the source-format identifier or raise."""
    raw = path.read_bytes()
    # try each registered source's detect()
    from .lyzr import LyzrIngestor

    for ing in [LyzrIngestor()]:
        if ing.detect(path, raw):
            return ing.name
    raise ValueError(
        f"Could not auto-detect agent definition format for {path}. "
        "Pass --source explicitly (lyzr | langgraph | markdown)."
    )


def get_ingestor(source: str) -> IngestSource:
    s = source.lower()
    if s == "lyzr":
        from .lyzr import LyzrIngestor
        return LyzrIngestor()
    raise ValueError(f"Unknown ingest source: {source}")


def safe_json_loads(b: bytes) -> Any:
    try:
        return json.loads(b.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

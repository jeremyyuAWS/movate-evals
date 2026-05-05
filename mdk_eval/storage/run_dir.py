"""Filesystem layout for a run.

results/run_<ts>/
  manifest.json
  config.yaml
  dataset.snapshot.jsonl
  scenarios/<id>/runs/<n>/{trace.json, deterministic.json, judges.json, eval.json}
  aggregate.json
  scenarios.csv
  report.html
  report.pdf
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe(s: str) -> str:
    return _SAFE.sub("_", s)


def new_run_dir(base: str | Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    p = Path(base) / f"run_{ts}"
    (p / "scenarios").mkdir(parents=True, exist_ok=True)
    return p


def scenario_run_dir(root: Path, scenario_id: str, run_index: int) -> Path:
    p = root / "scenarios" / _safe(scenario_id) / "runs" / str(run_index)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_json(path: Path, obj: Any) -> None:
    if isinstance(obj, BaseModel):
        data = obj.model_dump(mode="json")
    else:
        data = obj
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

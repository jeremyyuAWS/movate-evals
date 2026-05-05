"""Scenario loader. Supports JSONL and YAML."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import yaml

from .models import Scenario


def load_scenarios(path: str | Path) -> list[Scenario]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Dataset not found: {p}")
    suffix = p.suffix.lower()
    if suffix in (".jsonl", ".ndjson"):
        return list(_load_jsonl(p))
    if suffix in (".yaml", ".yml"):
        return list(_load_yaml(p))
    if suffix == ".json":
        with open(p) as f:
            data = json.load(f)
        if isinstance(data, list):
            return [Scenario(**s) for s in data]
        if isinstance(data, dict) and "scenarios" in data:
            return [Scenario(**s) for s in data["scenarios"]]
        raise ValueError(f"Unrecognized JSON shape in {p}")
    raise ValueError(f"Unsupported dataset format: {suffix}")


def _load_jsonl(p: Path) -> Iterable[Scenario]:
    with open(p) as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{p}:{i}: invalid JSON ({e})") from e
            yield Scenario(**obj)


def _load_yaml(p: Path) -> Iterable[Scenario]:
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    items = data.get("scenarios") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError(f"{p}: expected a list under 'scenarios:' or top-level list")
    for s in items:
        yield Scenario(**s)


def snapshot_dataset(scenarios: list[Scenario], out_path: Path) -> None:
    """Write a canonical JSONL snapshot of the dataset for the run dir."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for s in scenarios:
            f.write(json.dumps(s.model_dump(mode="json"), sort_keys=True) + "\n")

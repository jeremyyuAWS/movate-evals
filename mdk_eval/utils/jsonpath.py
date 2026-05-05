"""Tiny JSONPath-ish accessor. Supports $.a.b[0].c and a.b.0.c."""
from __future__ import annotations

import re
from typing import Any

_TOK = re.compile(r"\.|\[(\d+)\]")


def get_path(obj: Any, path: str | None, default: Any = None) -> Any:
    if obj is None or not path:
        return default
    p = path[2:] if path.startswith("$.") else (path[1:] if path.startswith("$") else path)
    if not p:
        return obj
    parts: list[str | int] = []
    cur = ""
    i = 0
    while i < len(p):
        c = p[i]
        if c == ".":
            if cur:
                parts.append(_maybe_int(cur))
                cur = ""
        elif c == "[":
            if cur:
                parts.append(_maybe_int(cur))
                cur = ""
            j = p.index("]", i)
            parts.append(int(p[i + 1 : j]))
            i = j
        else:
            cur += c
        i += 1
    if cur:
        parts.append(_maybe_int(cur))
    node = obj
    for part in parts:
        try:
            if isinstance(part, int):
                node = node[part]
            else:
                node = node[part] if isinstance(node, dict) else getattr(node, part)
        except (KeyError, IndexError, AttributeError, TypeError):
            return default
    return node


def _maybe_int(s: str) -> str | int:
    try:
        return int(s)
    except ValueError:
        return s

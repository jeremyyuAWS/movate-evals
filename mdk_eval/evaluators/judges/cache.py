"""SQLite-backed cache for LLM judge responses.

Why this exists
---------------
LLM judge calls are the dominant cost and the dominant source of non-determinism
in `mdk-eval`. The same (prompt, model, input) triple should always produce the
same verdict — so we can cache it. Concretely this enables:

- **Cost reduction** — re-running an unchanged eval costs $0.
- **Auditor-grade replay** — bit-identical reports across runs.
- **Faster local dev** — judge changes only re-call the affected role.

Cache key
---------
SHA-256 of canonical(provider, model, system, user, temperature). The system
prompt's SHA is the natural invalidation lever: when methodology version bumps
and prompts change, every key changes too. No explicit invalidation needed.

Storage
-------
`~/.mdk-eval/judge_cache.db`. Override via `MDK_EVAL_CACHE_DIR` env var.
Disable entirely via `MDK_EVAL_CACHE_DISABLE=1`.

Concurrency
-----------
SQLite handles multi-reader / single-writer with its built-in locking. Async
callers serialize writes via the connection's BEGIN IMMEDIATE — fine for
single-process eval runs (the typical pattern). For parallel CI jobs against
the same cache, set `MDK_EVAL_CACHE_DIR` per-job.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


def _cache_dir() -> Path:
    raw = os.getenv("MDK_EVAL_CACHE_DIR") or "~/.mdk-eval"
    return Path(raw).expanduser()


def is_enabled() -> bool:
    return os.getenv("MDK_EVAL_CACHE_DISABLE") not in ("1", "true", "TRUE", "yes")


def _db_path() -> Path:
    d = _cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "judge_cache.db"


def _key(provider: str, model: str, system: str, user: str, temperature: float) -> str:
    payload = json.dumps(
        {
            "provider": provider.lower(),
            "model": model,
            "system": system,
            "user": user,
            "temperature": float(temperature),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS judge_cache (
            cache_key     TEXT PRIMARY KEY,
            provider      TEXT NOT NULL,
            model         TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at    INTEGER NOT NULL,
            hits          INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    return conn


def get(provider: str, model: str, system: str, user: str, temperature: float) -> dict[str, Any] | None:
    """Return cached response dict, or None on miss / when disabled."""
    if not is_enabled():
        return None
    k = _key(provider, model, system, user, temperature)
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT response_json FROM judge_cache WHERE cache_key = ?", (k,)
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE judge_cache SET hits = hits + 1 WHERE cache_key = ?", (k,)
            )
            return json.loads(row[0])
        finally:
            conn.close()
    except (sqlite3.Error, OSError, json.JSONDecodeError):
        # Cache failures are never blocking — fall through to a real API call.
        return None


def get_with_metadata(
    provider: str, model: str, system: str, user: str, temperature: float,
) -> dict[str, Any] | None:
    """Return cached entry as `{response, created_at, hits}`, or None on miss /
    disabled cache. Same key derivation as `get`. Used when the caller needs to
    surface "last generated at" timestamps to the UI (e.g. Agent Doctor's
    diagnostics tab) without a separate query."""
    if not is_enabled():
        return None
    k = _key(provider, model, system, user, temperature)
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT response_json, created_at, hits FROM judge_cache "
                "WHERE cache_key = ?", (k,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE judge_cache SET hits = hits + 1 WHERE cache_key = ?", (k,)
            )
            return {
                "response": json.loads(row[0]),
                "created_at": int(row[1]) if row[1] is not None else None,
                "hits": int(row[2]) if row[2] is not None else 0,
            }
        finally:
            conn.close()
    except (sqlite3.Error, OSError, json.JSONDecodeError):
        return None


def invalidate(
    provider: str, model: str, system: str, user: str, temperature: float,
) -> bool:
    """Delete the cache entry for this key. Returns True if a row was deleted.
    Silent on any error — cache failures don't block callers."""
    if not is_enabled():
        return False
    k = _key(provider, model, system, user, temperature)
    try:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM judge_cache WHERE cache_key = ?", (k,))
            return cur.rowcount > 0
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False


def put(
    provider: str, model: str, system: str, user: str, temperature: float, response: dict[str, Any]
) -> None:
    """Store a response. Silent on any failure."""
    if not is_enabled():
        return
    k = _key(provider, model, system, user, temperature)
    try:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO judge_cache
                  (cache_key, provider, model, response_json, created_at, hits)
                VALUES (?, ?, ?, ?, ?, COALESCE(
                  (SELECT hits FROM judge_cache WHERE cache_key = ?), 0))
                """,
                (k, provider.lower(), model, json.dumps(response), int(time.time()), k),
            )
        finally:
            conn.close()
    except (sqlite3.Error, OSError, TypeError):
        return


def stats() -> dict[str, Any]:
    """Cache stats: total entries, total hits, size on disk, by provider."""
    if not _db_path().exists():
        return {"enabled": is_enabled(), "entries": 0, "total_hits": 0, "size_bytes": 0, "by_provider": {}}
    try:
        conn = _connect()
        try:
            entries = conn.execute("SELECT COUNT(*) FROM judge_cache").fetchone()[0]
            total_hits = conn.execute("SELECT COALESCE(SUM(hits), 0) FROM judge_cache").fetchone()[0]
            by_provider = dict(
                conn.execute(
                    "SELECT provider, COUNT(*) FROM judge_cache GROUP BY provider"
                ).fetchall()
            )
            size_bytes = _db_path().stat().st_size
            return {
                "enabled": is_enabled(),
                "entries": entries,
                "total_hits": total_hits,
                "size_bytes": size_bytes,
                "by_provider": by_provider,
                "path": str(_db_path()),
            }
        finally:
            conn.close()
    except sqlite3.Error:
        return {"enabled": is_enabled(), "entries": 0, "total_hits": 0, "size_bytes": 0, "by_provider": {}}


def clear() -> int:
    """Wipe the cache. Returns number of entries removed."""
    if not _db_path().exists():
        return 0
    try:
        conn = _connect()
        try:
            n = conn.execute("SELECT COUNT(*) FROM judge_cache").fetchone()[0]
            conn.execute("DELETE FROM judge_cache")
            conn.execute("VACUUM")
            return int(n)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0

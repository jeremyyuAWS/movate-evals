"""Push a `mdk-eval` run directory into Postgres.

Reads the JSON artifacts from a `results/run_<timestamp>/` directory and inserts
them into the schema defined in `migrations/001_initial_schema.sql`.

Idempotent: re-pushing the same run replaces its child rows (scenarios,
findings, clusters, risks) but preserves the parent `run` row's primary key —
safe to re-run after fixing a typo, schema bump, or partial failure.

Targets
-------
Any Postgres-compatible database:
- Supabase (use the connection string from Project Settings → Database)
- Azure Database for PostgreSQL Flexible Server
- AWS RDS / GCP Cloud SQL
- Local Postgres in docker / brew

Connection string format (libpq URI):
    postgresql://user:password@host:5432/dbname?sslmode=require

Usage
-----
Programmatic:
    from mdk_eval.storage.postgres_push import push_run, ensure_schema
    ensure_schema(conn_str)
    push_run(conn_str, Path("results/run_..."), engagement_slug="sandisk", agent_slug="returns")

CLI (preferred):
    mdk-eval push --connection-string "$DATABASE_URL" \\
                  --results results/run_2026-05-05T03-29-13Z \\
                  --engagement sandisk --agent returns
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

# psycopg is an optional dep — gate the import so the rest of the package
# stays importable without [push] installed.
try:
    import psycopg
    from psycopg.types.json import Json

    HAVE_PG = True
except ImportError:
    HAVE_PG = False


_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


class PostgresPushError(RuntimeError):
    pass


def is_available() -> bool:
    """True if the optional `push` extra is installed."""
    return HAVE_PG


# ----------------------------- helpers -----------------------------


def _require_pg() -> None:
    if not HAVE_PG:
        raise PostgresPushError(
            "psycopg is not installed. Install with: pip install -e '.[push]'"
        )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    # Manifest timestamps are ISO 8601 with 'Z' or +00:00; psycopg handles either
    # but Python's fromisoformat needs a tweak for trailing Z.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _load_run_artifacts(run_dir: Path) -> dict[str, Any]:
    """Load every artifact a push needs into one dict. Validates required files."""
    required = ["manifest.json", "evaluation_summary.json", "report.json", "aggregate.json"]
    for name in required:
        if not (run_dir / name).exists():
            raise PostgresPushError(f"missing required artifact: {run_dir / name}")
    return {
        "manifest": _read_json(run_dir / "manifest.json"),
        "summary": _read_json(run_dir / "evaluation_summary.json"),
        "report": _read_json(run_dir / "report.json"),
        "aggregates": _read_json(run_dir / "aggregate.json"),
        "config": _read_json(run_dir / "config.json") if (run_dir / "config.json").exists() else {},
    }


def _scenario_run_artifacts(run_dir: Path, scenario_id: str) -> list[dict[str, Any]]:
    """Walk scenarios/<id>/runs/<n>/eval.json. Empty list if dir missing."""
    sdir = run_dir / "scenarios" / scenario_id / "runs"
    if not sdir.exists():
        return []
    out: list[dict[str, Any]] = []
    for run_subdir in sorted(sdir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0):
        eval_json = run_subdir / "eval.json"
        if eval_json.exists():
            out.append(_read_json(eval_json))
    return out


# ----------------------------- schema management -----------------------------


def ensure_schema(conn_str: str) -> None:
    """Apply migrations idempotently. Safe to call before every push."""
    _require_pg()
    sql_path = _MIGRATIONS_DIR / "001_initial_schema.sql"
    if not sql_path.exists():
        raise PostgresPushError(f"migration file missing: {sql_path}")
    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_path.read_text(encoding="utf-8"))
        conn.commit()


# ----------------------------- the push itself -----------------------------


def push_run(
    conn_str: str,
    run_dir: Path,
    *,
    engagement_slug: str,
    agent_slug: str,
    engagement_display_name: str | None = None,
    agent_display_name: str | None = None,
    triggered_by: str | None = None,
    ci_url: str | None = None,
) -> dict[str, int]:
    """Push one run_dir. Returns row counts: {'scenarios': N, 'findings': N, ...}.

    Idempotent: if the same run_id already exists, child rows are replaced.
    """
    _require_pg()
    art = _load_run_artifacts(run_dir)

    manifest = art["manifest"]
    summary = art["summary"]
    report = art["report"]
    aggregates = art["aggregates"]
    config = art["config"]

    counts = {
        "engagement": 0, "agent": 0, "run": 0, "evaluation_summary": 0,
        "scenario_aggregates": 0, "scenario_runs": 0,
        "findings": 0, "failure_clusters": 0, "risk_items": 0,
    }

    with psycopg.connect(conn_str) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            # ---- engagement (UPSERT on slug) ----
            cur.execute(
                """
                INSERT INTO engagement (slug, display_name)
                VALUES (%s, %s)
                ON CONFLICT (slug) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
                """,
                (engagement_slug, engagement_display_name or engagement_slug),
            )
            engagement_id = cur.fetchone()[0]
            counts["engagement"] = 1

            # ---- agent (UPSERT on engagement+slug) ----
            backend = manifest.get("target") or "unknown"
            backend_id = (config.get("adapter") or {}).get("agent_id")
            cur.execute(
                """
                INSERT INTO agent (engagement_id, slug, display_name, backend, backend_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (engagement_id, slug) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    backend = EXCLUDED.backend,
                    backend_id = EXCLUDED.backend_id
                RETURNING id
                """,
                (engagement_id, agent_slug, agent_display_name or agent_slug, backend, backend_id),
            )
            agent_id = cur.fetchone()[0]
            counts["agent"] = 1

            # ---- run (UPSERT on run_id) ----
            cur.execute(
                """
                INSERT INTO run (
                    agent_id, run_id, started_at, ended_at,
                    schema_version, methodology_version, mdk_eval_version,
                    manifest_sha256, dataset_sha256, config_sha256, judge_prompts_sha256,
                    judges_enabled, judge_models, meta_judge_model,
                    arbitration_threshold, runs_per_scenario, tool_versions,
                    triggered_by, ci_url
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (run_id) DO UPDATE SET
                    ended_at              = EXCLUDED.ended_at,
                    schema_version        = EXCLUDED.schema_version,
                    methodology_version   = EXCLUDED.methodology_version,
                    mdk_eval_version      = EXCLUDED.mdk_eval_version,
                    manifest_sha256       = EXCLUDED.manifest_sha256,
                    judges_enabled        = EXCLUDED.judges_enabled,
                    judge_models          = EXCLUDED.judge_models,
                    triggered_by          = COALESCE(EXCLUDED.triggered_by, run.triggered_by),
                    ci_url                = COALESCE(EXCLUDED.ci_url, run.ci_url),
                    ingested_at           = now()
                RETURNING id
                """,
                (
                    agent_id,
                    manifest["run_id"],
                    _parse_dt(manifest.get("started_at")),
                    _parse_dt(manifest.get("ended_at")),
                    summary.get("schema_version", "1.0"),
                    summary.get("methodology_version", "1.0"),
                    manifest.get("mdk_eval_version", "0.0.0"),
                    summary.get("manifest_sha256", ""),
                    manifest.get("dataset_sha256", ""),
                    manifest.get("config_sha256", ""),
                    Json(manifest.get("judge_prompts_sha256") or {}),
                    manifest.get("judges_enabled") or [],
                    Json(manifest.get("judge_models") or {}),
                    manifest.get("meta_judge_model"),
                    manifest.get("arbitration_variance_threshold"),
                    manifest.get("runs_per_scenario", 1),
                    Json(manifest.get("tool_versions") or {}),
                    triggered_by,
                    ci_url,
                ),
            )
            run_pk = cur.fetchone()[0]
            counts["run"] = 1

            # ---- wipe child rows for idempotent re-push ----
            # Table names are hardcoded constants here, not user input, so a
            # plain f-string is safe and keeps the SQL inspectable for tests.
            for table in ("scenario_run", "scenario_aggregate", "failure_cluster",
                          "risk_item", "evaluation_summary"):
                cur.execute(f"DELETE FROM {table} WHERE run_id = %s", (run_pk,))

            # ---- evaluation_summary ----
            cur.execute(
                """
                INSERT INTO evaluation_summary
                  (run_id, overall_score, confidence, variance, status,
                   passing_scenarios, total_scenarios, scorecard)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_pk,
                    summary["overall_score"],
                    summary["confidence"],
                    summary["variance"],
                    summary["status"],
                    summary["passing_scenarios"],
                    summary["total_scenarios"],
                    Json(summary["scorecard"]),
                ),
            )
            counts["evaluation_summary"] = 1

            # ---- scenario_aggregate + finding ----
            for agg in aggregates:
                cur.execute(
                    """
                    INSERT INTO scenario_aggregate
                      (run_id, scenario_id, severity, pass_rate, mean_score,
                       score_variance, drift_score, consistency_score, num_runs,
                       tags, category_scores)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        run_pk,
                        agg["scenario_id"],
                        agg["severity"],
                        agg["pass_rate"],
                        agg["mean_score"],
                        agg["score_variance"],
                        agg["drift_score"],
                        agg["consistency_score"],
                        agg.get("runs", agg.get("num_runs", 1)),
                        agg.get("tags") or [],
                        Json(agg.get("category_scores")) if agg.get("category_scores") else None,
                    ),
                )
                aggregate_pk = cur.fetchone()[0]
                counts["scenario_aggregates"] += 1

                for f in (agg.get("findings") or []):
                    # `reason` and `evidence` may be structured (e.g. evidence is
                    # often {"missing": [...]} from a deterministic check). The
                    # schema stores them as TEXT, so we json-encode anything that
                    # isn't already a string.
                    def _stringify(v):
                        if v is None or isinstance(v, str):
                            return v
                        return json.dumps(v, sort_keys=True)
                    cur.execute(
                        """
                        INSERT INTO finding
                          (scenario_aggregate_id, failure_class, severity, reason, evidence, recommendation)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            aggregate_pk,
                            str(f.get("failure_class", "")),
                            str(f.get("severity", "medium")),
                            _stringify(f.get("reason")),
                            _stringify(f.get("evidence")),
                            _stringify(f.get("recommendation")),
                        ),
                    )
                    counts["findings"] += 1

            # ---- scenario_run (the per-repetition raw eval results) ----
            for agg in aggregates:
                for sr in _scenario_run_artifacts(run_dir, agg["scenario_id"]):
                    cur.execute(
                        """
                        INSERT INTO scenario_run
                          (run_id, scenario_id, run_index, trace_id, passed, final_score,
                           duration_ms, category_scores, trace, deterministic, judges, triangulation)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            run_pk,
                            sr.get("scenario_id"),
                            sr.get("run_index", 0),
                            sr.get("trace_id"),
                            bool(sr.get("passed", False)),
                            sr.get("final_score", 0.0),
                            sr.get("duration_ms"),
                            Json(sr.get("category_scores") or {}),
                            Json((sr.get("adapter") or {}).get("trace") or {}),
                            Json(sr.get("deterministic") or []),
                            Json(sr.get("judge_panel") or []),
                            Json(sr.get("triangulations") or {}),
                        ),
                    )
                    counts["scenario_runs"] += 1

            # ---- failure_cluster ----
            for fc in (report.get("failure_clusters") or []):
                cur.execute(
                    """
                    INSERT INTO failure_cluster
                      (run_id, failure_class, label, severity, count, example_scenario_ids, suggested_fix)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_pk,
                        str(fc.get("failure_class", "")),
                        fc.get("label"),
                        str(fc.get("severity", "medium")),
                        int(fc.get("count", 0)),
                        fc.get("example_scenario_ids") or [],
                        fc.get("suggested_fix"),
                    ),
                )
                counts["failure_clusters"] += 1

            # ---- risk_item ----
            for ri in (report.get("risk_register") or []):
                cur.execute(
                    """
                    INSERT INTO risk_item (run_id, risk, severity, likelihood, mitigation)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        run_pk,
                        str(ri.get("risk", "")),
                        str(ri.get("severity", "medium")),
                        str(ri.get("likelihood", "unknown")),
                        ri.get("mitigation"),
                    ),
                )
                counts["risk_items"] += 1

        conn.commit()

    return counts

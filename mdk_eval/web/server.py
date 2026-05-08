"""FastAPI app — the dashboard's backend.

Endpoints
---------
GET  /healthz                   — liveness; no auth
GET  /api/agents                — list agents (for dashboard dropdowns)
POST /api/agent-definitions     — upload + ingest a Lyzr agent JSON
POST /api/runs                  — kick off an evaluation
GET  /api/runs/{job_id}         — poll job status
GET  /api/scenario-sets/{id}    — list scenarios in a set (for review UI)
PATCH /api/scenarios/{id}       — approve/reject/edit a scenario

Auth
----
Bearer token (shared secret) via the `Authorization` header. Set MDK_WEB_API_KEY
in the service environment; clients send `Authorization: Bearer <token>`.
This is not real auth — it's a single-key service-token model intended for an
internal prototype. Replace with Entra ID / Supabase JWT when you're ready.

CORS
----
Allowed origins from MDK_WEB_CORS_ORIGINS (comma-separated). Bolt-generated
dashboards typically run on a Vercel preview URL — list it explicitly.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Annotated, Any

import psycopg

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Path as PathParam,
    Query,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import business_report as business_report_mod
from . import cost, db, insights, queue, worker
from .schemas import (
    AtAGlanceAgent,
    AtAGlanceEngagementRollup,
    AtAGlanceLatestRun,
    AtAGlanceLeaderboardEntry,
    AtAGlancePlatformRollup,
    AtAGlanceResponse,
    AddScenariosRequest,
    AddScenariosResponse,
    AgentDoctorPrescriptionResponse,
    AgentDoctorResponse,
    AgentDoctorSpecificChangeResponse,
    AtAGlanceSummary,
    BusinessReportResponse,
    CategoryInfo,
    CostPreviewResponse,
    ExtractionPromptsResponse,
    AgentCleanupCounts,
    AgentCleanupPreviewResponse,
    AgentDeleteResponse,
    AgentMeta,
    AgentRelinkRequest,
    AgentSystemMember,
    AgentSystemResponse,
    IngestedScenario,
    IngestPreviewResponse,
    IngestResponse,
    InsightResponse,
    ManagedAgentDetected,
    MixPresetResponse,
    MixPresetsResponse,
    ProposeOneRequest,
    ProposeOneResponse,
    RunCleanupCounts,
    RunDeleteResponse,
    RunMeta,
    RunQueuedResponse,
    RunRequest,
    RunStatusResponse,
    ScenarioRegenerateRequest,
    ScenarioRegenerateResponse,
    ScoringProfileCatalogResponse,
    ScoringProfileCategoryReco,
    ScoringProfileRecommendationResponse,
    ScoringProfileResponse,
    DownstreamLLMModel,
    RunProvenanceCore,
    RunProvenanceResponse,
    ScoringProvenance,
    TopicBreakdownResponse,
    TopicExtractionResponse,
    TopicResponse,
    TopicScoreCategoryEntry,
    TopicScoreEntryResponse,
)
from .. import __version__
from ..ingest.lyzr import LyzrIngestor
from ..ingest.extractors import llm as llm_extractor
from ..ingest.sanitize import sanitize_bytes


# ----------------------------- app + middleware -----------------------------


_log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Spawn N queue workers for the duration of the app's life.

    N is controlled by MDK_WORKER_CONCURRENCY (default 1). Each worker
    polls pgmq independently — pgmq's visibility-timeout semantics keep
    them from racing on the same message. For prototype scale (≤4 concurrent
    evals on one Fly machine), this is plenty. If usage grows past one
    machine's CPU/memory budget, split the worker into a separate
    `mdk-eval-worker` Fly app — the queue code doesn't change.
    """
    n = max(1, int(os.getenv("MDK_WORKER_CONCURRENCY", "1")))
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(worker.worker_loop(stop), name=f"mdk-eval-worker-{i}")
        for i in range(n)
    ]
    _log.info("Started %d queue worker(s).", n)
    try:
        yield
    finally:
        _log.info("Stopping %d queue worker(s) (graceful)…", n)
        stop.set()
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=15)
            except asyncio.TimeoutError:
                _log.warning("Worker %s did not exit within 15s; cancelling.", t.get_name())
                t.cancel()


app = FastAPI(
    title="Movate Agent Assurance — Web API",
    version=__version__,
    description=(
        "Backend for the Bolt-generated Movate Agent Assurance dashboard. "
        "Drives ingestion + eval execution; reads/writes the same Postgres "
        "schema the dashboard renders."
    ),
    lifespan=lifespan,
)

_cors_origins = [o.strip() for o in (os.getenv("MDK_WEB_CORS_ORIGINS") or "").split(",") if o.strip()]
if not _cors_origins:
    _cors_origins = ["http://localhost:3000", "http://localhost:5173"]

# Regex allowlist for environments with unstable / per-deploy URLs:
#   - Bolt preview         (*.webcontainer-api.io)
#   - Bolt deployed         (*.bolt.host, *.bolt.new)
#   - Vercel preview/prod   (*.vercel.app)
#   - Local dev             (localhost / 127.0.0.1, any port)
# Override via MDK_WEB_CORS_ORIGIN_REGEX if you need to lock this down or expand it.
# Combined with allow_credentials=True this is safe — the middleware echoes the
# actual matched origin in Access-Control-Allow-Origin (not '*').
_cors_origin_regex = os.getenv("MDK_WEB_CORS_ORIGIN_REGEX") or (
    r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
    r"|^https://[a-zA-Z0-9.-]+\.(webcontainer-api\.io|bolt\.host|bolt\.new|vercel\.app)$"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=_cors_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Trace-Id"],
    expose_headers=["X-Trace-Id", "X-Request-Id"],
)


# ----------------------------- observability -----------------------------
# Wires up structured JSON logging, request-correlation middleware, optional
# Application Insights instrumentation, and optional Langfuse client. All
# integrations are env-gated and degrade to no-op when secrets are absent.
from .observability import install as _install_observability  # noqa: E402

_observability_status = _install_observability(app)


# ----------------------------- auth -----------------------------


_security = HTTPBearer(auto_error=False)


def require_api_key(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_security)],
) -> None:
    """Bearer-token gate. Raises 401 unless the token matches MDK_WEB_API_KEY."""
    expected = os.getenv("MDK_WEB_API_KEY")
    if not expected:
        # If no key is configured, refuse all requests rather than running open.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service auth is not configured (MDK_WEB_API_KEY is unset).",
        )
    if creds is None or creds.credentials != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing bearer token.")


# ----------------------------- health -----------------------------


@app.get("/healthz")
def healthz() -> dict:
    """Liveness probe (no auth, no dependencies).

    Returns 200 as long as the process is running. Used by Container Apps'
    liveness probe — failure here means the container is dead and should be
    restarted. Do NOT add dependency checks here; that's `/readyz`.
    """
    return {"status": "ok", "version": __version__}


@app.get("/readyz")
def readyz() -> dict:
    """Deep readiness probe (no auth, exercises real dependencies).

    Returns:
      200 + {"status": "ready", "checks": {...}} when every required dependency
        responded successfully.
      503 + {"status": "degraded", "checks": {...}} when one or more required
        dependencies failed. Each `checks[dep]` is an object: {"ok": bool,
        "error": str | None, "latency_ms": float}. Bolt should treat 503 here
        as "the backend is up but eval runs will fail; degrade UI accordingly."
    """
    import time as _time

    checks: dict[str, dict[str, Any]] = {}
    all_ok = True

    # DB reachability — keep the probe lightweight (SELECT 1).
    db_t0 = _time.perf_counter()
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        checks["postgres"] = {
            "ok": True, "error": None,
            "latency_ms": round((_time.perf_counter() - db_t0) * 1000, 2),
        }
    except Exception as e:
        all_ok = False
        checks["postgres"] = {
            "ok": False, "error": f"{type(e).__name__}: {e}",
            "latency_ms": round((_time.perf_counter() - db_t0) * 1000, 2),
        }

    # Required env vars — secret presence (we don't dial out to verify the
    # key works; that costs money and would happen on every probe).
    for name in ("MDK_WEB_API_KEY", "DATABASE_URL"):
        present = bool(os.getenv(name))
        checks[f"env.{name}"] = {"ok": present, "error": None if present else "unset", "latency_ms": 0.0}
        all_ok = all_ok and present

    # Optional providers — log status but don't fail readiness on absence
    # (a backend with no judges configured is still a useful eval pipeline).
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LYZR_API_KEY"):
        checks[f"env.{name}"] = {"ok": bool(os.getenv(name)), "error": None, "latency_ms": 0.0}

    body = {"status": "ready" if all_ok else "degraded", "version": __version__, "checks": checks}
    if not all_ok:
        raise HTTPException(status_code=503, detail=body)
    return body


@app.get("/version")
def version_info() -> dict:
    """Build / runtime info. No auth — Bolt can show this in a footer.

    Reads:
      - MDK_EVAL_GIT_SHA   (set in the Dockerfile via ARG)
      - MDK_EVAL_IMAGE_TAG (set in the Dockerfile via ARG)
      - python_version, mdk_eval version, observability flags
    """
    import platform as _platform
    return {
        "version": __version__,
        "git_sha": os.getenv("MDK_EVAL_GIT_SHA", "unknown"),
        "image_tag": os.getenv("MDK_EVAL_IMAGE_TAG", "unknown"),
        "python_version": _platform.python_version(),
        "observability": _observability_status,
    }


@app.get("/metrics", dependencies=[Depends(require_api_key)])
def metrics() -> dict:
    """Lightweight ops metrics (auth required).

    Returns recent activity counters useful for an ops dashboard / Bolt's
    "system status" tile:
      - jobs_in_queue: number of web_run_job rows in 'queued' state
      - jobs_running: number in 'running' state
      - jobs_failed_24h: failed-status jobs created in the last 24h
      - cost_usd_24h: sum of cost_usd for runs that completed in the last 24h
      - runs_total: lifetime count of jobs
    Cheap aggregate queries — runs in <50ms even at 100k jobs.
    """
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                count(*) FILTER (WHERE status = 'queued')                                AS jobs_in_queue,
                count(*) FILTER (WHERE status = 'running')                               AS jobs_running,
                count(*) FILTER (WHERE status = 'failed' AND created_at >= now() - interval '24 hours') AS jobs_failed_24h,
                COALESCE(sum(cost_usd) FILTER (WHERE status = 'done' AND ended_at >= now() - interval '24 hours'), 0) AS cost_usd_24h,
                count(*) AS runs_total
            FROM web_run_job
            """
        )
        row = cur.fetchone()
    return {
        "jobs_in_queue": int(row[0] or 0),
        "jobs_running": int(row[1] or 0),
        "jobs_failed_24h": int(row[2] or 0),
        "cost_usd_24h": round(float(row[3] or 0), 4),
        "runs_total": int(row[4] or 0),
    }


# ----------------------------- agents (read) -----------------------------


@app.get("/api/agents", dependencies=[Depends(require_api_key)])
def list_agents(include_provisional: bool = False) -> list[dict]:
    """List agents the dashboard can target for evaluation runs.

    By default returns only **active** agents (those that have produced at
    least one successful run, per migration 008). Pass
    `include_provisional=true` to also return drafts — newly-uploaded
    agents that haven't yet produced a successful scored run.
    """
    with db.connect() as conn, conn.cursor() as cur:
        # Detect migration 008 to decide whether to filter — gracefully
        # degrades to "show everything" on stale schemas.
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='agent' "
            "AND column_name='is_active'"
        )
        has_is_active = cur.fetchone() is not None
        active_filter = (
            "WHERE a.is_active = TRUE"
            if has_is_active and not include_provisional
            else ""
        )
        cur.execute(
            f"""
            SELECT a.id, a.slug, a.display_name, a.backend, a.backend_id,
                   e.slug AS engagement_slug, e.display_name AS engagement_name
            FROM agent a
            JOIN engagement e ON a.engagement_id = e.id
            {active_filter}
            ORDER BY e.display_name, a.display_name
            """
        )
        cols = ("id", "slug", "display_name", "backend", "backend_id",
                "engagement_slug", "engagement_name")
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get("/api/agents/{agent_id}/runs", dependencies=[Depends(require_api_key)])
def list_agent_runs(
    agent_id: Annotated[int, PathParam(...)],
    limit: int = 10,
) -> list[dict]:
    """Recent runs for one agent, newest first.

    Powers sparklines on KPI tiles, the trends view, and the recency panel.
    Includes scorecard so the frontend can plot per-category trends without
    a second request.
    """
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be 1–100")
    with db.connect() as conn, conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT r.id, r.run_id, r.started_at, r.ended_at,
                       r.methodology_version, r.mdk_eval_version,
                       r.runs_per_scenario, r.judges_enabled,
                       s.overall_score, s.status, s.confidence,
                       s.passing_scenarios, s.total_scenarios, s.scorecard,
                       j.cost_usd
                FROM run r
                LEFT JOIN evaluation_summary s ON s.run_id = r.id
                LEFT JOIN web_run_job j ON j.result_run_id = r.id::text
                WHERE r.agent_id = %s
                ORDER BY r.started_at DESC
                LIMIT %s
                """,
                (agent_id, limit),
            )
            cols = ("id", "run_id", "started_at", "ended_at", "methodology_version",
                    "mdk_eval_version", "runs_per_scenario", "judges_enabled",
                    "overall_score", "status", "confidence",
                    "passing_scenarios", "total_scenarios", "scorecard", "cost_usd")
        except psycopg.errors.UndefinedColumn:
            conn.rollback()
            cur.execute(
                """
                SELECT r.id, r.run_id, r.started_at, r.ended_at,
                       r.methodology_version, r.mdk_eval_version,
                       r.runs_per_scenario, r.judges_enabled,
                       s.overall_score, s.status, s.confidence,
                       s.passing_scenarios, s.total_scenarios, s.scorecard
                FROM run r
                LEFT JOIN evaluation_summary s ON s.run_id = r.id
                WHERE r.agent_id = %s
                ORDER BY r.started_at DESC
                LIMIT %s
                """,
                (agent_id, limit),
            )
            cols = ("id", "run_id", "started_at", "ended_at", "methodology_version",
                    "mdk_eval_version", "runs_per_scenario", "judges_enabled",
                    "overall_score", "status", "confidence",
                    "passing_scenarios", "total_scenarios", "scorecard")
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            d.setdefault("cost_usd", None)
            if d.get("cost_usd") is not None:
                d["cost_usd"] = float(d["cost_usd"])
            out.append(d)
        return out


# ----------------------------- ingest -----------------------------


@app.post(
    "/api/agent-definitions",
    response_model=IngestResponse,
    dependencies=[Depends(require_api_key)],
)
async def ingest_agent_definition(
    file: Annotated[UploadFile, File(..., description="Lyzr agent definition JSON")],
    engagement_slug: Annotated[str, Form(...)],
    agent_slug: Annotated[str, Form(...)],
    scenario_set_name: Annotated[str, Form(...)],
    engagement_name: Annotated[str | None, Form()] = None,
    agent_name: Annotated[str | None, Form()] = None,
    synthesize: Annotated[bool, Form()] = False,
    triggered_by: Annotated[str | None, Form()] = None,
    sandbox: Annotated[bool, Query(
        description=(
            "Sandbox mode: when true, the backend provisions an ephemeral "
            "Lyzr agent from the uploaded JSON definition (POST /v3/agents). "
            "Use for pre-deployment validation, CI/CD eval, or evaluating an "
            "agent definition before any Lyzr account exists. The provisioned "
            "agent is flagged is_sandbox=true and torn down on local DELETE. "
            "Phase 1 supports single-task agents only — manager agents with "
            "managed_agents are rejected."
        )
    )] = False,
    parent_agent_slug: Annotated[str | None, Form(
        description="Multi-agent systems: pre-resolved manager slug to link this sub-agent under."
    )] = None,
    mix_json: Annotated[str | None, Form(
        description="Legacy 1D mix: JSON dict of behavioral_category -> count. Mutually exclusive with topic_mix_json. Implies synthesize=true."
    )] = None,
    topic_mix_json: Annotated[str | None, Form(
        description="2D mix: JSON dict of topic_slug -> count. Combined with behavior_preset_name (or behavior_mix_json for custom). Implies synthesize=true."
    )] = None,
    behavior_preset_name: Annotated[str | None, Form(
        description="Behavioral preset name to overlay on top of topic_mix_json. One of: balanced, compliance_heavy, reliability_focused, custom."
    )] = None,
    behavior_mix_json: Annotated[str | None, Form(
        description="When behavior_preset_name=custom, the user-supplied behavioral ratios. Same shape as mix_json but values may be floats."
    )] = None,
    focus: Annotated[str | None, Form()] = None,
    custom_directive: Annotated[str | None, Form()] = None,
) -> IngestResponse:
    """Upload + ingest a Lyzr agent definition.

    The heuristic extractor always runs. The LLM extractor runs when ANY of
    these is true:
      - `synthesize=true` (legacy: uses DEFAULT_MIX, no topics)
      - `mix_json` is provided (legacy 1D)
      - `topic_mix_json` is provided (new 2D path; topic-tagged scenarios)

    With `topic_mix_json`, every LLM-proposed scenario carries both
    `category:<behavior>` and `topic:<slug>` tags, and the prompt is enriched
    with the topic's description (looked up via the cached topic extractor).

    Every scenario carries provenance per PRD §6.10 — `derived_from` shows
    which extractor produced it and which quote of the agent definition it
    tests.
    """
    from ..ingest import mix_presets
    from ..insights import topic_extractor
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # Strip secret-shaped fields BEFORE anything else touches the bytes.
    # Lyzr exports often include the producer's api_key, RAG creds, etc. —
    # these must never be persisted, sent to the LLM extractor, or hashed
    # into a SHA that's then displayed in audit views.
    raw, redactions = sanitize_bytes(raw)

    try:
        agent_def = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Not valid JSON: {e}")
    if not isinstance(agent_def, dict):
        raise HTTPException(status_code=400, detail="Agent definition must be a JSON object.")

    source_sha256 = hashlib.sha256(raw).hexdigest()
    backend = "lyzr"  # this endpoint is Lyzr-specific today
    # Lyzr's export shape has shifted across versions. Try every plausible
    # field name in priority order.
    backend_id = str(
        agent_def.get("_id")
        or agent_def.get("id")
        or agent_def.get("agent_id")
        or agent_def.get("_agent_id")
        or ""
    )

    # 1. Run heuristic ingest. We pass a stub Path so the existing LyzrIngestor
    #    treats this as a normal ingest call; nothing is written to disk.
    from pathlib import Path as _P
    ingestor = LyzrIngestor()
    result = ingestor.ingest(_P(file.filename or "upload.json"), raw)

    warnings: list[str] = list(result.warnings)
    source_label = "lyzr-ingest"
    is_sandbox = False
    sandbox_expires_at = None

    # Pre-flight ID resolution. Three branches:
    #   (a) sandbox=true     — provision a new Lyzr agent now, use the
    #       returned ID as backend_id. Phase 1 rejects manager agents.
    #   (b) sandbox=false    — require an agent ID in the JSON; refuse the
    #       upload with 400 if absent (hardening — prevents the "ghost
    #       agent that can't run" state we used to silently create).
    if sandbox:
        if backend_id:
            warnings.append(
                f"sandbox=true was passed but the upload already has an agent ID "
                f"(`{backend_id}`). Provisioning a new Lyzr agent anyway; the "
                f"existing ID is ignored. Pass sandbox=false if you want to use "
                f"the existing agent."
            )
        if agent_def.get("managed_agents"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Sandbox provisioning of manager agents (with `managed_agents`) "
                    "is not yet supported. Phase 1 of sandbox mode covers single-task "
                    "agents only. Either: (a) re-upload the agent with a real Lyzr "
                    "`_id` and sandbox=false, or (b) wait for Phase 2 multi-agent "
                    "provisioning."
                ),
            )
        from ..integrations import lyzr_admin
        try:
            backend_id = await lyzr_admin.create_agent(agent_def)
        except lyzr_admin.LyzrAdminError as e:
            raise HTTPException(status_code=502, detail=f"Lyzr provision failed: {e}")
        from datetime import datetime, timedelta, timezone
        is_sandbox = True
        sandbox_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        source_label = "lyzr-ingest+sandbox"
        warnings.append(
            f"Sandbox mode: provisioned Lyzr agent `{backend_id}`. Expires "
            f"{sandbox_expires_at.isoformat()}. DELETE the local agent record "
            f"to tear down the Lyzr-side instance immediately, or wait for the "
            f"24h TTL cleanup."
        )
    elif backend == "lyzr" and not backend_id:
        # Hardening: refuse the upload rather than silently creating a record
        # whose runs will fail. The user gets a single clear error pointing
        # at the exact missing field + their two recovery paths.
        raise HTTPException(
            status_code=400,
            detail=(
                "Lyzr agent definition is missing the top-level agent ID "
                "(`_id`, `id`, `agent_id`, or `_agent_id` — all absent). "
                "Without an agent ID, evaluations cannot dispatch to Lyzr's "
                "inference API. Two ways to fix this: "
                "(1) Re-export the agent from Lyzr and confirm the JSON "
                "includes the manager's own `_id` at the top level (sub-agents "
                "have `id` fields too — those should already be present). "
                "(2) Pass `?sandbox=true` on this upload to provision a fresh "
                "ephemeral Lyzr agent from this JSON; the temp agent will be "
                "torn down when you DELETE the agent record (or after 24h TTL)."
            ),
        )

    # 2. Optional LLM synthesis. Three trigger paths, mutually exclusive:
    #    (a) topic_mix_json   → 2D path (topic + behavior preset)
    #    (b) mix_json         → legacy 1D path
    #    (c) synthesize=true  → DEFAULT_MIX (back-compat; same as old behavior)
    proposed = []
    if topic_mix_json and mix_json:
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide exactly one of: `mix_json` (legacy 1D) OR "
                "`topic_mix_json` (2D). Got both."
            ),
        )
    use_2d = bool(topic_mix_json)
    use_1d_explicit = bool(mix_json)

    if use_2d or use_1d_explicit or synthesize:
        if not llm_extractor.is_available():
            warnings.append(
                "LLM extraction skipped — no OPENAI_API_KEY (or ANTHROPIC_API_KEY) set."
            )
        else:
            try:
                if use_2d:
                    # Parse topic_mix_json
                    try:
                        tm = json.loads(topic_mix_json)
                        if not isinstance(tm, dict) or not all(
                            isinstance(v, int) and v >= 0 for v in tm.values()
                        ):
                            raise ValueError(
                                "topic_mix_json must be a dict of topic_slug -> non-negative int"
                            )
                    except (json.JSONDecodeError, ValueError) as e:
                        raise HTTPException(status_code=400, detail=f"Invalid topic_mix_json: {e}")

                    # Resolve behavior ratios
                    pname = (behavior_preset_name or mix_presets.default_preset_name()).strip()
                    if pname == "custom":
                        if not behavior_mix_json:
                            raise HTTPException(
                                status_code=400,
                                detail="behavior_preset_name='custom' requires behavior_mix_json.",
                            )
                        try:
                            bm = json.loads(behavior_mix_json)
                            if not isinstance(bm, dict) or not all(
                                isinstance(v, (int, float)) and v >= 0 for v in bm.values()
                            ):
                                raise ValueError("behavior_mix_json must map category -> non-negative number")
                            ratios = {k: float(v) for k, v in bm.items() if v > 0}
                        except (json.JSONDecodeError, ValueError) as e:
                            raise HTTPException(status_code=400, detail=f"Invalid behavior_mix_json: {e}")
                        unknown = sorted(c for c in ratios if c not in llm_extractor.CATEGORIES)
                        if unknown:
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    f"Unknown behavioral category in custom mix: {unknown}. "
                                    f"Valid: {sorted(llm_extractor.CATEGORIES)}."
                                ),
                            )
                    else:
                        preset = mix_presets.get_preset(pname)
                        if preset is None:
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    f"Unknown behavior_preset_name: {pname!r}. "
                                    f"Valid: {[p.name for p in mix_presets.list_presets()] + ['custom']}."
                                ),
                            )
                        ratios = preset.normalised_ratios()

                    # Look up topic metadata (cached by agent SHA)
                    topic_meta: dict[str, dict[str, str]] = {}
                    try:
                        tr = await topic_extractor.extract_topics_async(agent_def)
                        topic_meta = {
                            t.slug: {"name": t.name, "description": t.description}
                            for t in tr.topics
                        }
                    except Exception as e:  # pragma: no cover
                        warnings.append(f"Topic metadata lookup failed: {type(e).__name__}: {e}")

                    cells = mix_presets.expand_topic_mix(
                        tm,
                        behavior_ratios=ratios,
                        topic_names={s: topic_meta.get(s, {}).get("name", s) for s in tm},
                    )
                    proposed = await llm_extractor.extract_with_topics_async(
                        agent_def,
                        cells=cells,
                        topic_meta=topic_meta,
                        focus=focus,
                        custom_directive=custom_directive,
                    )
                    source_label = "lyzr-ingest+llm+topics"
                elif use_1d_explicit:
                    # Legacy 1D path with explicit mix
                    try:
                        m = json.loads(mix_json)
                        if not isinstance(m, dict) or not all(isinstance(v, int) for v in m.values()):
                            raise ValueError("mix_json must map category -> int count")
                    except (json.JSONDecodeError, ValueError) as e:
                        raise HTTPException(status_code=400, detail=f"Invalid mix_json: {e}")
                    proposed = await llm_extractor.extract_async(
                        agent_def, mix=m, focus=focus, custom_directive=custom_directive,
                    )
                    source_label = "lyzr-ingest+llm"
                else:
                    # Legacy path: synthesize=true, no explicit mix → DEFAULT_MIX
                    proposed = await llm_extractor.extract_async(agent_def)
                    source_label = "lyzr-ingest+llm"
            except HTTPException:
                raise
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                warnings.append(f"LLM extraction failed: {type(e).__name__}: {e}")

    # 3. Persist to Postgres in one transaction.
    inserted: list[IngestedScenario] = []
    parent_agent_id: int | None = None
    with db.connect() as conn, conn.cursor() as cur:
        engagement_id = db.upsert_engagement(cur, engagement_slug, engagement_name)

        # Resolve parent_agent_slug → parent_agent_id BEFORE upsert so the link
        # is set atomically. If the slug doesn't resolve, surface as a warning
        # rather than failing — the user may upload the manager next.
        if parent_agent_slug:
            parent = db.find_agent_by_slug(cur, engagement_id, parent_agent_slug)
            if parent is None:
                warnings.append(
                    f"parent_agent_slug='{parent_agent_slug}' was not found in engagement "
                    f"'{engagement_slug}'. Agent uploaded as standalone — link manually later "
                    f"via PATCH /api/agents/{{id}} once the manager is uploaded."
                )
            else:
                parent_agent_id = int(parent["id"])

        agent_id = db.upsert_agent(
            cur, engagement_id, agent_slug, agent_name, backend, backend_id,
            parent_agent_id=parent_agent_id,
        )
        # Stamp sandbox metadata if this was a sandbox upload. Idempotent
        # with the upsert: re-uploading the same slug as sandbox refreshes
        # the expires_at; uploading non-sandbox over a sandbox flips it
        # back to is_sandbox=false (so the user can "promote" by re-upload).
        db.set_agent_sandbox_flags(
            cur, agent_id,
            is_sandbox=is_sandbox,
            expires_at=sandbox_expires_at,
        )

        scenario_set_id = db.insert_scenario_set(
            cur,
            agent_id=agent_id,
            name=scenario_set_name,
            source=source_label,
            source_sha256=source_sha256,
            source_filename=file.filename,
            created_by=triggered_by,
            # Store the SANITIZED agent definition so /regenerate has the LLM
            # context it needs without requiring a re-upload.
            agent_definition=agent_def,
        )

        # Heuristic scenarios from the existing ingestor:
        for s in result.scenarios:
            payload = s.model_dump(mode="json")
            scenario_pk = db.insert_scenario(
                cur,
                scenario_set_id=scenario_set_id,
                scenario_id=s.id,
                payload=payload,
                severity=str(s.severity.value) if hasattr(s.severity, "value") else str(s.severity),
                tags=s.tags,
                derived_from=(s.meta or {}).get("derived_from"),
            )
            inserted.append(IngestedScenario(
                id=scenario_pk,
                scenario_id=s.id,
                severity=str(s.severity.value) if hasattr(s.severity, "value") else str(s.severity),
                tags=list(s.tags),
                derived_from=(s.meta or {}).get("derived_from"),
            ))

        # LLM proposals (round-tripped through to_scenario_dict to enforce
        # the same provenance shape).
        if proposed:
            from ..models import Scenario as ScenarioModel
            for p in proposed:
                sd = llm_extractor.to_scenario_dict(
                    p,
                    name_prefix=agent_slug,
                    source_path=file.filename or "upload.json",
                    source_sha256=source_sha256,
                    model=os.getenv("MDK_INGEST_LLM_MODEL") or llm_extractor.DEFAULT_MODEL,
                    provider=os.getenv("MDK_INGEST_LLM_PROVIDER") or llm_extractor.DEFAULT_PROVIDER,
                )
                try:
                    s = ScenarioModel.model_validate(sd)
                except Exception as e:
                    warnings.append(f"LLM proposal {p.id!r} failed validation: {e}")
                    continue
                scenario_pk = db.insert_scenario(
                    cur,
                    scenario_set_id=scenario_set_id,
                    scenario_id=s.id,
                    payload=s.model_dump(mode="json"),
                    severity=str(s.severity.value) if hasattr(s.severity, "value") else str(s.severity),
                    tags=s.tags,
                    derived_from=(s.meta or {}).get("derived_from"),
                )
                inserted.append(IngestedScenario(
                    id=scenario_pk,
                    scenario_id=s.id,
                    severity=str(s.severity.value) if hasattr(s.severity, "value") else str(s.severity),
                    tags=list(s.tags),
                    derived_from=(s.meta or {}).get("derived_from"),
                ))
        conn.commit()

    # Surface what we redacted so the user knows their upload was scrubbed.
    # Each entry is {"path": "<dotted>", "preview": "<first 4 chars + …>"}.
    if redactions:
        red_paths = ", ".join(r["path"] for r in redactions)
        warnings.append(
            f"Sanitized {len(redactions)} secret-shaped field(s) from upload before processing: {red_paths}"
        )

    # Detect managed_agents — if this is a manager, surface its referenced sub-
    # agents so Bolt can prompt the user to upload them next. We re-open the DB
    # connection only if we have managed_agents to check (cheap when there are none).
    managed_agents_detected = _detect_managed_agents(agent_def, engagement_id)

    return IngestResponse(
        scenario_set_id=scenario_set_id,
        agent_id=agent_id,
        engagement_id=engagement_id,
        source_sha256=source_sha256,
        scenarios=inserted,
        warnings=warnings,
        parent_agent_id=parent_agent_id,
        managed_agents_detected=managed_agents_detected,
    )


def _detect_managed_agents(agent_def: dict, engagement_id: int) -> list[ManagedAgentDetected]:
    """Read managed_agents[] from a Lyzr-style agent definition and return one
    `ManagedAgentDetected` per sub-agent reference. Sets `already_uploaded`
    when a sibling agent in the same engagement already has the matching
    backend_id.

    Returns an empty list when:
      - the agent definition has no managed_agents array (standalone agent)
      - the array is malformed (we don't fail the ingest over it)
    """
    managed = agent_def.get("managed_agents") or []
    if not isinstance(managed, list) or not managed:
        return []

    out: list[ManagedAgentDetected] = []
    with db.connect() as conn, conn.cursor() as cur:
        for entry in managed:
            if not isinstance(entry, dict):
                continue
            backend_id = str(entry.get("id") or "").strip()
            if not backend_id:
                continue
            display_name = str(entry.get("name") or "").strip() or backend_id
            usage = entry.get("usage_description") or entry.get("description")
            usage_str = str(usage).strip() if usage else None
            # Auto-suggest a slug — clean version of display_name. Lyzr names
            # sometimes carry decorations like "(R) OCR Agent [Manager v4]";
            # strip parens, brackets, lowercase, dash-join.
            import re as _re
            cleaned = _re.sub(r"[\(\[].*?[\)\]]", "", display_name).strip()
            cleaned = _re.sub(r"[^a-zA-Z0-9]+", "-", cleaned).strip("-").lower()
            suggested_slug = cleaned or f"agent-{backend_id[:8]}"

            existing = db.find_agent_by_backend_id(cur, engagement_id, backend_id)
            out.append(ManagedAgentDetected(
                backend_id=backend_id,
                display_name=display_name,
                usage_description=usage_str,
                suggested_slug=suggested_slug,
                already_uploaded=bool(existing),
            ))
    return out


# ----------------------------- multi-agent systems -----------------------------


@app.get(
    "/api/agent-systems/{root_slug}",
    response_model=AgentSystemResponse,
    dependencies=[Depends(require_api_key)],
)
def get_agent_system(
    root_slug: Annotated[str, PathParam(..., description="Slug of the manager agent (within its engagement)")],
) -> AgentSystemResponse:
    """Return a manager + all its sub-agents with composite system metrics.

    This is the canonical view for multi-agent systems (e.g., a Returns Manager
    + an OCR Agent + a Validator). Bolt renders one card per member plus a
    composite "system" card that blends them.

    Composite scoring:
      - manager weight = 1.5x (customer-facing)
      - sub-agent weight = 1.0x each
      - composite_status = "worst-of" the member statuses; a system is only as
        production-ready as its weakest link
    """
    with db.connect() as conn, conn.cursor() as cur:
        system = db.get_agent_system(cur, root_slug)
        if system is None:
            raise HTTPException(status_code=404, detail=f"No agent with slug '{root_slug}' found")

        # Pull latest-run summary for each member
        manager_summary = db.latest_run_summary_for_agent(cur, system["manager"]["id"])
        manager_member = AgentSystemMember(
            id=system["manager"]["id"],
            slug=system["manager"]["slug"],
            display_name=system["manager"]["display_name"],
            backend=system["manager"]["backend"],
            role="manager",
            overall_score=manager_summary["overall_score"] if manager_summary else None,
            status=manager_summary["status"] if manager_summary else None,
            pass_rate=manager_summary["pass_rate"] if manager_summary else None,
            runs_count=manager_summary["runs_count"] if manager_summary else 0,
            last_run_at=manager_summary["last_run_at"] if manager_summary else None,
            cost_usd=manager_summary["cost_usd"] if manager_summary else None,
        )

        sub_members: list[AgentSystemMember] = []
        for sub in system["sub_agents"]:
            sub_summary = db.latest_run_summary_for_agent(cur, sub["id"])
            sub_members.append(AgentSystemMember(
                id=sub["id"],
                slug=sub["slug"],
                display_name=sub["display_name"],
                backend=sub["backend"],
                role="sub_agent",
                overall_score=sub_summary["overall_score"] if sub_summary else None,
                status=sub_summary["status"] if sub_summary else None,
                pass_rate=sub_summary["pass_rate"] if sub_summary else None,
                runs_count=sub_summary["runs_count"] if sub_summary else 0,
                last_run_at=sub_summary["last_run_at"] if sub_summary else None,
                cost_usd=sub_summary["cost_usd"] if sub_summary else None,
            ))

    # Composite — manager-weighted mean of overall_score, only over members
    # that have at least one run. Members with no runs don't drag the average
    # to 0 but DO drag composite_status (a system with un-evaluated sub-agents
    # cannot be "production_ready" yet).
    weighted_sum = 0.0
    weight_total = 0.0
    members_evaluated = 0
    members_total = 1 + len(sub_members)
    statuses: list[str] = []

    if manager_member.overall_score is not None:
        weighted_sum += manager_member.overall_score * 1.5
        weight_total += 1.5
        members_evaluated += 1
        statuses.append(manager_member.status or "not_ready")
    for sm in sub_members:
        if sm.overall_score is not None:
            weighted_sum += sm.overall_score * 1.0
            weight_total += 1.0
            members_evaluated += 1
            statuses.append(sm.status or "not_ready")

    composite_score = round(weighted_sum / weight_total, 2) if weight_total > 0 else 0.0

    # Worst-of status. Order: not_ready < needs_improvement < pilot_ready < production_ready.
    status_rank = {"not_ready": 0, "needs_improvement": 1, "pilot_ready": 2, "production_ready": 3}
    if members_evaluated < members_total:
        # Some members haven't been evaluated — system can't be > pilot_ready
        composite_status = "needs_improvement" if statuses else "not_ready"
    elif statuses:
        composite_status = min(statuses, key=lambda s: status_rank.get(s, 0))
    else:
        composite_status = "not_ready"

    return AgentSystemResponse(
        engagement_slug=system["engagement_slug"],
        engagement_name=system["engagement_name"],
        manager=manager_member,
        sub_agents=sub_members,
        composite_score=composite_score,
        composite_status=composite_status,
        members_evaluated=members_evaluated,
        members_total=members_total,
    )


@app.patch(
    "/api/agents/{agent_id}",
    dependencies=[Depends(require_api_key)],
)
def relink_agent(
    agent_id: Annotated[int, PathParam(...)],
    req: AgentRelinkRequest,
) -> dict:
    """PATCH an agent's parent_agent_id and/or backend_id.

    Two recovery flows it supports:

    1. **Re-link sub-agent under a manager** — pass `parent_agent_slug` to set,
       or `null` to unlink. Used when sub-agents were uploaded before their
       manager (legacy multi-agent assembly).

    2. **Repair missing backend_id** — pass `backend_id` to set the platform
       agent ID. Used when the original upload didn't include `_id` / `id` /
       `agent_id` and runs against this agent are failing with "Lyzr adapter
       requires 'agent_id'". Without a valid backend_id, runs cannot dispatch.

    Body is partial — pass only the fields you want to change. Returns the
    final state of the agent record.
    """
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT engagement_id, slug, backend, backend_id FROM agent WHERE id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        engagement_id, current_slug, current_backend, current_backend_id = row

        # ---- parent re-link ----
        new_parent_id: int | None = None
        parent_changed = False
        if req.parent_agent_slug is not None:
            parent = db.find_agent_by_slug(cur, engagement_id, req.parent_agent_slug)
            if parent is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"parent_agent_slug='{req.parent_agent_slug}' "
                           f"not found in engagement_id={engagement_id}",
                )
            if parent["id"] == agent_id:
                raise HTTPException(status_code=400, detail="Agent cannot be its own parent")
            new_parent_id = int(parent["id"])
            parent_changed = True
        elif "parent_agent_slug" in req.model_fields_set:
            # explicit None → unlink
            parent_changed = True

        if parent_changed:
            ok = db.update_agent_parent(cur, agent_id, new_parent_id)
            if not ok:
                raise HTTPException(
                    status_code=503,
                    detail="Multi-agent linkage requires migration 007. Apply it via Supabase Studio.",
                )

        # ---- backend_id repair ----
        if req.backend_id is not None:
            new_backend_id = req.backend_id.strip()
            if not new_backend_id:
                raise HTTPException(
                    status_code=400,
                    detail="backend_id cannot be empty string. Omit the field to leave unchanged.",
                )
            cur.execute(
                "UPDATE agent SET backend_id = %s WHERE id = %s",
                (new_backend_id, agent_id),
            )
            current_backend_id = new_backend_id

        conn.commit()

    status_parts = []
    if parent_changed:
        status_parts.append("linked" if new_parent_id else "unlinked")
    if req.backend_id is not None:
        status_parts.append("backend_id_updated")
    return {
        "agent_id": agent_id,
        "slug": current_slug,
        "backend": current_backend,
        "backend_id": current_backend_id,
        "parent_agent_id": new_parent_id if parent_changed else None,
        "status": ",".join(status_parts) if status_parts else "no_changes",
    }


# ----------------------------- admin cleanup -----------------------------


@app.get(
    "/api/agents/{agent_id}/cleanup-preview",
    response_model=AgentCleanupPreviewResponse,
    dependencies=[Depends(require_api_key)],
)
def preview_agent_cleanup(
    agent_id: Annotated[int, PathParam(...)],
) -> AgentCleanupPreviewResponse:
    """Dry-run preview: show what would be deleted by `DELETE /api/agents/{id}`.

    Powers Bolt's "you're about to delete this agent — here's the blast
    radius" confirmation modal. Read-only; safe to call any time.

    Returns 404 if the agent doesn't exist.
    """
    with db.connect() as conn, conn.cursor() as cur:
        result = db.count_agent_cleanup_cascade(cur, agent_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    return AgentCleanupPreviewResponse(
        agent=AgentMeta(**result["agent"]),
        would_delete=AgentCleanupCounts(**result["would_delete"]),
        would_orphan_sub_agents=[AgentMeta(**a) for a in result["would_orphan_sub_agents"]],
    )


@app.delete(
    "/api/agents/{agent_id}",
    response_model=AgentDeleteResponse,
    dependencies=[Depends(require_api_key)],
)
async def delete_agent_endpoint(
    agent_id: Annotated[int, PathParam(...)],
) -> AgentDeleteResponse:
    """Hard-delete an agent + everything cascading from it.

    Cascades (per FK schema):
      - agent → scenario_set → scenario
      - agent → run → evaluation_summary, scenario_aggregate, scenario_run,
                       finding, failure_cluster, risk_item

    Sub-agents (those with `parent_agent_id = this`) are NOT deleted — their
    parent_agent_id is set to NULL (per `ON DELETE SET NULL`). The response's
    `orphaned_sub_agents` lists what was de-parented so the caller can choose
    to clean those up too.

    **Sandbox-mode agents** (per migration 009): if `is_sandbox=true`, this
    endpoint ALSO calls Lyzr's `DELETE /v3/agents/{lyzr_id}` first to tear
    down the ephemeral Lyzr-side instance. Failure on the Lyzr side is
    surfaced as a warning in `notes` but does NOT block the local delete —
    operator intent ("get this row out of my DB") wins over Lyzr-side
    state-sync. A leaked Lyzr agent will be caught by the 24h cleanup
    cron (Phase 2) or via Lyzr Studio.

    Returns 404 if the agent doesn't exist. Does not require explicit
    confirmation — Bolt's UI is responsible for the "are you sure" modal.
    Use the `/cleanup-preview` endpoint above to populate that modal.
    """
    notes: list[str] = []
    sandbox_state = None
    with db.connect() as conn, conn.cursor() as cur:
        # Read sandbox state up front (before delete) so we know whether
        # to call Lyzr DELETE.
        sandbox_state = db.get_agent_sandbox_state(cur, agent_id)
        if sandbox_state is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

        # Snapshot the cascade for the response.
        snapshot = db.count_agent_cleanup_cascade(cur, agent_id)
        if not snapshot:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    # Lyzr-side teardown FIRST (outside the DB transaction so a slow
    # Lyzr API doesn't hold a row lock). Best-effort: never blocks local
    # delete on Lyzr-side failure.
    if (
        sandbox_state.get("is_sandbox")
        and sandbox_state.get("backend") == "lyzr"
        and sandbox_state.get("backend_id")
    ):
        from ..integrations import lyzr_admin
        try:
            await lyzr_admin.delete_agent(sandbox_state["backend_id"])
            notes.append(
                f"Sandbox tear-down: Lyzr agent `{sandbox_state['backend_id']}` deleted."
            )
        except lyzr_admin.LyzrAdminError as e:
            # Don't block local delete — leaked Lyzr-side agent gets caught
            # by the cleanup cron (Phase 2) or manual cleanup in Lyzr Studio.
            notes.append(
                f"Sandbox tear-down WARNING: Lyzr DELETE failed for agent "
                f"`{sandbox_state['backend_id']}` ({e}). Local agent record "
                f"deleted; Lyzr-side instance may have leaked — clean up via "
                f"Lyzr Studio or wait for the TTL cron."
            )

    with db.connect() as conn, conn.cursor() as cur:
        ok = db.delete_agent(cur, agent_id)
        if not ok:
            conn.rollback()
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        conn.commit()

    return AgentDeleteResponse(
        agent=AgentMeta(**snapshot["agent"]),
        deleted=AgentCleanupCounts(**snapshot["would_delete"]),
        orphaned_sub_agents=[AgentMeta(**a) for a in snapshot["would_orphan_sub_agents"]],
        notes=notes,
    )


@app.delete(
    "/api/runs/{run_pk}",
    response_model=RunDeleteResponse,
    dependencies=[Depends(require_api_key)],
)
def delete_run_endpoint(
    run_pk: Annotated[int, PathParam(..., description="The run.id PK (not the timestamped run_id string)")],
) -> RunDeleteResponse:
    """Hard-delete a single run + all of its scoring artifacts.

    Use this to remove failed / test / no-longer-relevant runs without
    deleting the agent itself. The agent stays; the agent's other runs stay;
    only this one run and its scenario_aggregate / scenario_run / finding /
    failure_cluster / evaluation_summary rows are removed.

    The `web_run_job` row that triggered this run keeps its history but its
    `result_run_id` is set to NULL (audit trail preserved).

    Returns 404 if the run doesn't exist.
    """
    with db.connect() as conn, conn.cursor() as cur:
        snapshot = db.count_run_cleanup_cascade(cur, run_pk)
        if not snapshot:
            raise HTTPException(status_code=404, detail=f"Run pk={run_pk} not found")
        ok = db.delete_run(cur, run_pk)
        if not ok:
            conn.rollback()
            raise HTTPException(status_code=404, detail=f"Run pk={run_pk} not found")
        conn.commit()

    return RunDeleteResponse(
        run=RunMeta(**snapshot["run"]),
        deleted=RunCleanupCounts(**snapshot["would_delete"]),
    )


# ----------------------------- preview + audit -----------------------------


@app.get(
    "/api/extraction/prompts",
    response_model=ExtractionPromptsResponse,
    dependencies=[Depends(require_api_key)],
)
def get_extraction_prompts() -> ExtractionPromptsResponse:
    """Return the per-category system prompts the LLM extractor uses.

    Powers two things:
      1. The Test Mix Designer's per-category tooltip — "what does 'adversarial'
         mean in this system?" — so users understand what each slider buys.
      2. Audit / risk-review compliance — the prompts we ask the LLM to
         produce scenarios with are visible, not hidden in source code.
    """
    from ..ingest.extractors import llm as llm_extractor
    registry = llm_extractor.categories_registry()
    default_mix = dict(llm_extractor.DEFAULT_MIX)
    categories = [
        CategoryInfo(
            name=name,
            label=info["label"],
            description=info["description"],
            default_severity=info["default_severity"],
            directive=info["directive"],
            default_count=default_mix.get(name, 0),
        )
        for name, info in registry.items()
    ]
    return ExtractionPromptsResponse(
        base_system_prompt=llm_extractor._BASE_SYSTEM_PROMPT,
        categories=categories,
        default_mix=default_mix,
    )


@app.get(
    "/api/mix-presets",
    response_model=MixPresetsResponse,
    dependencies=[Depends(require_api_key)],
)
def get_mix_presets() -> MixPresetsResponse:
    """Return the built-in behavioral mix presets for the Test Mix Designer.

    Each preset is a normalised distribution over behavioral categories
    (standard / edge / adversarial / safety / honesty / multi_turn /
    performance). The Mix Designer multiplies these against per-topic counts
    to derive the 2D mix passed to /api/agent-definitions/preview.

    Frontend should also offer a 'Custom' option that lets the user edit the
    ratios directly; that case sends `behavior_preset_name="custom"` plus
    `behavior_mix_json` to the preview endpoint instead of relying on a
    server-side preset name.
    """
    from ..ingest import mix_presets

    presets = [
        MixPresetResponse(
            name=p.name,
            label=p.label,
            description=p.description,
            ratios=p.normalised_ratios(),
        )
        for p in mix_presets.list_presets()
    ]
    return MixPresetsResponse(
        presets=presets,
        default=mix_presets.default_preset_name(),
    )


@app.post(
    "/api/agent-definitions/preview",
    response_model=IngestPreviewResponse,
    dependencies=[Depends(require_api_key)],
)
async def preview_ingest(
    file: Annotated[UploadFile, File(..., description="Lyzr agent definition JSON")],
    mix_json: Annotated[str | None, Form(description="Legacy 1D mix: JSON dict mapping category -> count. Mutually exclusive with topic_mix_json.")] = None,
    topic_mix_json: Annotated[str | None, Form(description="2D mix path: JSON dict mapping topic_slug -> count. Use with behavior_preset_name (or behavior_mix_json for custom ratios).")] = None,
    behavior_preset_name: Annotated[str | None, Form(description="Named behavioral preset to overlay on top of topic_mix_json. One of: balanced, compliance_heavy, reliability_focused, custom. Default: balanced.")] = None,
    behavior_mix_json: Annotated[str | None, Form(description="When behavior_preset_name=custom, the user-supplied behavioral ratios. JSON dict mapping category -> ratio (positive floats; normalised internally).")] = None,
    focus: Annotated[str | None, Form()] = None,
    custom_directive: Annotated[str | None, Form()] = None,
    stream: Annotated[bool, Query(
        description=(
            "When true, return Server-Sent Events with phase-progress messages while "
            "the preview is built. Use this from Bolt's Mix Designer 'Generate scenarios' "
            "flow to rotate spinner messages while the LLM extraction runs (typically "
            "30-120s for 13 scenarios). Final SSE event has phase=\"done\" and a "
            "`preview` field with the full IngestPreviewResponse shape."
        )
    )] = False,
):
    """Preview an ingest WITHOUT persisting anything.

    Two paths:
      - **2D (preferred)**: pass `topic_mix_json` (slug -> count) plus
        `behavior_preset_name` (or `behavior_mix_json` for custom ratios).
        For each topic, the preset distributes its count across behavioral
        categories. Each generated scenario is tagged with both
        `topic:<slug>` and `category:<behavior>`.
      - **1D (legacy)**: pass `mix_json` (category -> count) only. Same
        contract as before this change. No topic tags applied.

    Either way the response includes:
      - heuristic-extractor scenarios (always; free + deterministic)
      - LLM-proposed scenarios for the requested mix (cached server-side, so
        identical previews on the same file cost $0)
      - estimated EVAL cost (judges per scenario), not extraction cost

    Sanitises the upload (strips api_keys and similar) before any processing.

    Streaming path (`?stream=true`):
      Returns text/event-stream with these events:
        loading           — file read + sanitize + JSON parse (< 100ms)
        mix_validation    — parse + validate mix params (< 100ms)
        topic_metadata    — topic extraction LLM call (~2-3s, cached: instant)
        heuristic_extract — heuristic ingestor (< 1s)
        llm_extract       — per-cell LLM scenario generation (the bulk: 30-120s for 13 scenarios)
        done              — `{phase: "done", preview: <IngestPreviewResponse>}`
      Each event is `data: {phase, message}\n\n`. The `done` event terminates
      the stream and carries the full IngestPreviewResponse payload as `preview`.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    filename_for_stream = file.filename or "upload.json"

    # ---- streaming branch ----
    # Yield phase milestones BEFORE awaiting the actual work so the UI can
    # rotate spinner messages. Total wall-clock dominated by the LLM
    # extraction phase (30-120s); the other phases each fire in <1s.
    if stream:
        async def _sse_emitter():
            yield f"data: {json.dumps({'phase': 'loading', 'message': 'Reading agent definition...'})}\n\n"
            await asyncio.sleep(0)
            yield f"data: {json.dumps({'phase': 'mix_validation', 'message': 'Validating mix configuration...'})}\n\n"
            await asyncio.sleep(0)
            using_2d_for_msg = bool(topic_mix_json)
            if using_2d_for_msg:
                yield f"data: {json.dumps({'phase': 'topic_metadata', 'message': 'Extracting topical categories from agent...'})}\n\n"
                await asyncio.sleep(0)
            yield f"data: {json.dumps({'phase': 'heuristic_extract', 'message': 'Running deterministic heuristic extractor...'})}\n\n"
            await asyncio.sleep(0)
            yield f"data: {json.dumps({'phase': 'llm_extract', 'message': 'Generating scenarios (LLM call — typically 30-120s)...'})}\n\n"

            # Now do the actual work. Errors that would normally raise
            # HTTPException become 'error' events instead — the stream
            # has already started so we can't bubble HTTP-level errors.
            try:
                preview_response = await _preview_ingest_core(
                    raw=raw, filename=filename_for_stream,
                    mix_json=mix_json, topic_mix_json=topic_mix_json,
                    behavior_preset_name=behavior_preset_name,
                    behavior_mix_json=behavior_mix_json,
                    focus=focus, custom_directive=custom_directive,
                )
            except HTTPException as e:
                yield f"data: {json.dumps({'phase': 'error', 'detail': e.detail, 'status_code': e.status_code})}\n\n"
                return
            except Exception as e:  # pragma: no cover — defensive
                yield f"data: {json.dumps({'phase': 'error', 'detail': f'{type(e).__name__}: {e}'})}\n\n"
                return

            # Final payload: dump the model so SSE consumers can render
            # the result without an extra round-trip.
            yield f"data: {json.dumps({'phase': 'done', 'preview': preview_response.model_dump(mode='json')})}\n\n"

        return StreamingResponse(
            _sse_emitter(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ---- non-streaming path: same logic, returned as one JSON ----
    return await _preview_ingest_core(
        raw=raw, filename=filename_for_stream,
        mix_json=mix_json, topic_mix_json=topic_mix_json,
        behavior_preset_name=behavior_preset_name,
        behavior_mix_json=behavior_mix_json,
        focus=focus, custom_directive=custom_directive,
    )


async def _preview_ingest_core(
    *,
    raw: bytes,
    filename: str,
    mix_json: str | None,
    topic_mix_json: str | None,
    behavior_preset_name: str | None,
    behavior_mix_json: str | None,
    focus: str | None,
    custom_directive: str | None,
) -> IngestPreviewResponse:
    """The actual preview work — refactored out so both the JSON and SSE
    paths share one implementation. Takes pre-read raw bytes (because the
    SSE wrapper reads them into memory once before yielding the first
    progress event)."""
    from ..ingest.extractors import llm as llm_extractor
    from ..ingest.lyzr import LyzrIngestor
    from ..ingest import mix_presets
    from ..insights import topic_extractor
    raw, redactions = sanitize_bytes(raw)
    try:
        agent_def = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Not valid JSON: {e}")
    if not isinstance(agent_def, dict):
        raise HTTPException(status_code=400, detail="Agent definition must be a JSON object.")

    source_sha256 = hashlib.sha256(raw).hexdigest()
    warnings: list[str] = []
    if redactions:
        warnings.append(
            f"Sanitized {len(redactions)} secret-shaped field(s) before processing: "
            + ", ".join(r["path"] for r in redactions)
        )

    # ---- mode selection: 1D vs 2D ----
    if topic_mix_json and mix_json:
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide exactly one of: `mix_json` (legacy 1D — behavioral category -> count) "
                "OR `topic_mix_json` (2D — topical slug -> count, combined with behavior_preset_name). "
                "Got both."
            ),
        )

    using_2d = bool(topic_mix_json)

    # ---- parse 1D mix (legacy path) ----
    mix: dict[str, int] | None = None
    if mix_json:
        try:
            mix = json.loads(mix_json)
            if not isinstance(mix, dict) or not all(isinstance(v, int) for v in mix.values()):
                raise ValueError("mix must be a dict mapping category -> int count")
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid mix_json: {e}")
        # Filter mix to only LLM-known categories. Frontends sometimes include
        # heuristic-only chip names (e.g. 'tool_sequence') — drop them with a
        # warning rather than 400'ing the whole preview.
        unknown_cats = sorted(c for c in mix if c not in llm_extractor.CATEGORIES)
        if unknown_cats:
            for c in unknown_cats:
                del mix[c]
            warnings.append(
                "Ignored unknown LLM categor"
                + ("ies" if len(unknown_cats) > 1 else "y")
                + f": {', '.join(unknown_cats)}. "
                f"Valid: {', '.join(sorted(llm_extractor.CATEGORIES))}."
            )

    # ---- parse 2D mix ----
    topic_counts: dict[str, int] = {}
    cells: list = []
    behavior_ratios: dict[str, float] = {}
    topic_meta: dict[str, dict[str, str]] = {}
    if using_2d:
        try:
            tm = json.loads(topic_mix_json or "")
            if not isinstance(tm, dict) or not all(isinstance(v, int) and v >= 0 for v in tm.values()):
                raise ValueError("topic_mix_json must be a dict mapping topic_slug -> non-negative int count")
            topic_counts = tm
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid topic_mix_json: {e}")

        preset_name = (behavior_preset_name or mix_presets.default_preset_name()).strip()
        if preset_name == "custom":
            if not behavior_mix_json:
                raise HTTPException(
                    status_code=400,
                    detail="behavior_preset_name='custom' requires behavior_mix_json with category -> ratio.",
                )
            try:
                bm = json.loads(behavior_mix_json)
                if not isinstance(bm, dict) or not all(isinstance(v, (int, float)) and v >= 0 for v in bm.values()):
                    raise ValueError("behavior_mix_json must be a dict mapping category -> non-negative number")
                behavior_ratios = {k: float(v) for k, v in bm.items() if v > 0}
            except (json.JSONDecodeError, ValueError) as e:
                raise HTTPException(status_code=400, detail=f"Invalid behavior_mix_json: {e}")
            unknown = sorted(c for c in behavior_ratios if c not in llm_extractor.CATEGORIES)
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown behavioral category in custom mix: {unknown}. "
                        f"Valid: {sorted(llm_extractor.CATEGORIES)}."
                    ),
                )
        else:
            preset = mix_presets.get_preset(preset_name)
            if preset is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown behavior_preset_name: {preset_name!r}. "
                        f"Valid: {[p.name for p in mix_presets.list_presets()] + ['custom']}."
                    ),
                )
            behavior_ratios = preset.normalised_ratios()

        # Look up topic metadata so the LLM prompts get topic descriptions.
        # This is cached by agent SHA so it's free on repeats.
        try:
            topic_result = await topic_extractor.extract_topics_async(agent_def)
            topic_meta = {
                t.slug: {"name": t.name, "description": t.description}
                for t in topic_result.topics
            }
        except Exception as e:  # pragma: no cover — keep preview alive on extractor outage
            warnings.append(f"Topic metadata lookup failed: {type(e).__name__}: {e}")

        # Warn on requested topics we can't enrich (likely typo'd slug).
        unknown_topics = sorted(s for s in topic_counts if s not in topic_meta)
        if unknown_topics:
            warnings.append(
                "Topic metadata not found for: "
                + ", ".join(unknown_topics)
                + ". Scenarios will be tagged but the LLM prompt won't include a topic description."
            )

        cells = mix_presets.expand_topic_mix(
            topic_counts,
            behavior_ratios=behavior_ratios,
            topic_names={s: topic_meta.get(s, {}).get("name", s) for s in topic_counts},
        )
        # Validate that the expansion produced any cells; an all-zero topic_mix
        # is legitimate (yields heuristic-only preview), so don't error.
        if not cells and any(v > 0 for v in topic_counts.values()):
            warnings.append(
                "Topic mix has positive counts but expanded to zero cells — "
                "behavior preset may produce all-zero ratios. Check behavior_mix_json."
            )

    # ---- heuristic ingest (free + deterministic, runs in both modes) ----
    from pathlib import Path as _P
    ingestor = LyzrIngestor()
    heuristic_result = ingestor.ingest(_P(filename), raw)
    warnings.extend(heuristic_result.warnings)

    # ---- LLM extraction ----
    llm_proposed = []
    if using_2d:
        total_llm_count = mix_presets.total_scenarios(cells)
    else:
        total_llm_count = sum((mix or llm_extractor.DEFAULT_MIX).values())

    if total_llm_count > 0:
        if not llm_extractor.is_available():
            warnings.append(
                "LLM extractor skipped — no OPENAI_API_KEY (or ANTHROPIC_API_KEY) configured "
                "on the server."
            )
        else:
            try:
                if using_2d:
                    llm_proposed = await llm_extractor.extract_with_topics_async(
                        agent_def,
                        cells=cells,
                        topic_meta=topic_meta,
                        focus=focus,
                        custom_directive=custom_directive,
                    )
                else:
                    llm_proposed = await llm_extractor.extract_async(
                        agent_def, mix=mix, focus=focus, custom_directive=custom_directive,
                    )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                warnings.append(f"LLM extraction failed: {type(e).__name__}: {e}")

    # ---- assemble preview payload ----
    from ..models import Scenario as ScenarioModel
    preview_scenarios: list[dict] = []
    counts_by_cat: dict[str, int] = {"heuristic": 0}
    counts_by_topic_cat: dict[str, dict[str, int]] = {}

    for s in heuristic_result.scenarios:
        preview_scenarios.append(s.model_dump(mode="json"))
        counts_by_cat["heuristic"] += 1

    for p in llm_proposed:
        sd = llm_extractor.to_scenario_dict(
            p,
            name_prefix="preview",
            source_path=filename,
            source_sha256=source_sha256,
            model=os.getenv("MDK_INGEST_LLM_MODEL") or llm_extractor.DEFAULT_MODEL,
            provider=os.getenv("MDK_INGEST_LLM_PROVIDER") or llm_extractor.DEFAULT_PROVIDER,
        )
        try:
            preview_scenarios.append(ScenarioModel.model_validate(sd).model_dump(mode="json"))
            counts_by_cat[p.category] = counts_by_cat.get(p.category, 0) + 1
            # 2D breakdown: extract the topic:<slug> tag if present.
            topic_slug = next(
                (t.split(":", 1)[1] for t in p.tags if t.startswith("topic:")),
                None,
            )
            if topic_slug:
                counts_by_topic_cat.setdefault(topic_slug, {})[p.category] = (
                    counts_by_topic_cat.setdefault(topic_slug, {}).get(p.category, 0) + 1
                )
        except Exception as e:
            warnings.append(f"LLM proposal {p.id!r} failed validation: {e}")

    # Estimate token cost — note: this estimates the EVAL cost (judge calls per
    # scenario), not the extraction cost. Extraction itself is ~$0.005 per
    # category and cached. Eval cost is the larger number the user needs.
    est = cost.estimate_run_cost(
        num_scenarios=len(preview_scenarios),
        runs_per_scenario=1,
        judges_enabled=True,
    )

    return IngestPreviewResponse(
        source_sha256=source_sha256,
        scenarios=preview_scenarios,
        counts_by_category=counts_by_cat,
        counts_by_topic_category=counts_by_topic_cat,
        estimated_cost_usd=est.estimated_cost_usd,
        estimated_total_judge_calls=est.estimated_total_judge_calls,
        warnings=warnings,
    )


# ----------------------------- scenario review -----------------------------


@app.get(
    "/api/scenario-sets/{scenario_set_id}",
    dependencies=[Depends(require_api_key)],
)
def get_scenario_set(
    scenario_set_id: Annotated[int, PathParam(...)],
    status: str | None = None,  # filter by status if provided
) -> list[dict]:
    """List scenarios in a set, optionally filtered by status."""
    with db.connect() as conn, conn.cursor() as cur:
        return db.list_scenarios(cur, scenario_set_id, status=status)


# ----------------------------- propose-one + add scenarios -----------------------------


@app.post(
    "/api/scenarios/propose-one",
    response_model=ProposeOneResponse,
    dependencies=[Depends(require_api_key)],
)
async def propose_one_scenario(req: ProposeOneRequest) -> ProposeOneResponse:
    """LLM-generates ONE scenario from a natural-language description.

    Stateless — does NOT persist. Use this for:
      - Mix Designer "Add one more" button (frontend appends to its in-memory list)
      - Saved scenario set "Quick add" button (frontend takes the response and
        POSTs it back to /api/scenario-sets/{id}/scenarios to commit)

    Source of agent context (exactly one required):
      - scenario_set_id: looks up the agent_definition stored on the set
      - agent_definition: pass the dict directly (during preview)
    """
    if (req.scenario_set_id is None) == (req.agent_definition is None):
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide exactly one of: `scenario_set_id` (looks up the stored agent "
                "definition) OR `agent_definition` (inline JSON; use this during Mix "
                "Designer preview before any set has been committed)."
            ),
        )
    if req.category not in llm_extractor.CATEGORIES:
        valid = sorted(llm_extractor.CATEGORIES)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown behavioral category: '{req.category}'. Valid categories: "
                f"{valid}. If you meant a topical category (e.g., 'Movate Services'), "
                f"pass it as the `topic` field instead and keep `category` as one of "
                f"the behavioral options above."
            ),
        )
    if req.category == "custom" and not req.custom_directive:
        raise HTTPException(status_code=400, detail="category='custom' requires a custom_directive")

    # Resolve agent definition.
    agent_def: dict[str, Any] | None = req.agent_definition
    if agent_def is None:
        with db.connect() as conn, conn.cursor() as cur:
            agent_def = db.get_scenario_set_agent_definition(cur, req.scenario_set_id)
        if not agent_def:
            raise HTTPException(
                status_code=400,
                detail=(
                    "scenario_set has no stored agent_definition (pre-migration-006). "
                    "Re-upload the agent JSON to enable propose-one for this set."
                ),
            )

    if not llm_extractor.is_available():
        raise HTTPException(
            status_code=503,
            detail="LLM not configured (server-side OPENAI_API_KEY missing).",
        )

    # Compose the focus string from natural-language + optional topic.
    # When a topic is supplied, the LLM is told both what topic to anchor in
    # AND what specific behavior to test. Resulting scenario gets tagged
    # `topic:<slug>` so the dashboard can filter by topic later.
    focus_parts: list[str] = []
    if req.topic:
        focus_parts.append(f"Topical scope: {req.topic}.")
    if req.natural_language_request:
        focus_parts.append(req.natural_language_request)
    composed_focus = " ".join(focus_parts).strip() or req.natural_language_request

    try:
        proposed = await llm_extractor.extract_async(
            agent_def,
            mix={req.category: 1},
            focus=composed_focus,
            custom_directive=req.custom_directive,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"LLM proposal failed: {type(e).__name__}: {e}",
        )

    if not proposed:
        raise HTTPException(
            status_code=502,
            detail="LLM returned no scenarios. Try rephrasing the request or different category.",
        )

    p = proposed[0]
    # Tag the proposed scenario with the topic before serialization so it
    # rides through to_scenario_dict and into the persisted record.
    if req.topic:
        from ..insights.topic_extractor import _slugify
        topic_tag = f"topic:{_slugify(req.topic)}"
        if topic_tag not in p.tags:
            p.tags.append(topic_tag)

    sd = llm_extractor.to_scenario_dict(
        p,
        name_prefix="",
        source_path="propose-one",
        source_sha256="",
        model=os.getenv("MDK_INGEST_LLM_MODEL") or llm_extractor.DEFAULT_MODEL,
        provider=os.getenv("MDK_INGEST_LLM_PROVIDER") or llm_extractor.DEFAULT_PROVIDER,
    )
    return ProposeOneResponse(
        scenario=sd,
        category=req.category,
        warnings=[],
    )


@app.post(
    "/api/scenario-sets/{scenario_set_id}/scenarios",
    response_model=AddScenariosResponse,
    dependencies=[Depends(require_api_key)],
)
def add_scenarios(
    scenario_set_id: Annotated[int, PathParam(...)],
    req: AddScenariosRequest,
) -> AddScenariosResponse:
    """Persist one or more scenarios into an existing scenario_set.

    Powers three UX flows from one endpoint:
      - **Manual create**: frontend collected payload from a form, posts list of 1
      - **Quick-add commit**: user accepted a /propose-one result, posts list of 1
      - **Bulk import**: frontend parsed a CSV/JSONL, posts list of N

    Validation:
      - Each payload validated against the Scenario model BEFORE any DB writes
      - Duplicate scenario_ids (vs existing rows in the set) are SKIPPED, not errored
        — returned in `skipped[]` so the user knows what didn't land
      - Whole batch fails (transaction rolls back) if any payload is malformed
    """
    if not req.scenarios:
        raise HTTPException(status_code=400, detail="scenarios list is empty")

    from ..models import Scenario as ScenarioModel

    # Validate every payload first — fail fast with which one and why.
    validated: list[ScenarioModel] = []
    for i, payload in enumerate(req.scenarios):
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail=f"scenarios[{i}] is not a dict")
        try:
            validated.append(ScenarioModel.model_validate(payload))
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"scenarios[{i}] failed Scenario validation: {e}",
            )

    # Tag provenance on any scenario that doesn't already carry it.
    source_label = req.source or "manual"
    for s in validated:
        meta = s.meta or {}
        if "derived_from" not in meta:
            meta = {
                **meta,
                "derived_from": {
                    "extractor": source_label,
                    "added_via": "POST /api/scenario-sets/{id}/scenarios",
                    "added_by": req.created_by,
                },
            }
            s.meta = meta

    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []

    with db.connect() as conn, conn.cursor() as cur:
        # Confirm the set exists before any inserts (prevents creating orphans).
        cur.execute("SELECT id FROM scenario_set WHERE id = %s", (scenario_set_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="scenario_set not found")

        existing_ids = db.get_existing_scenario_ids(cur, scenario_set_id)

        for s in validated:
            if s.id in existing_ids:
                skipped.append({"scenario_id": s.id, "reason": "already exists in set"})
                continue
            scenario_pk = db.insert_scenario(
                cur,
                scenario_set_id=scenario_set_id,
                scenario_id=s.id,
                payload=s.model_dump(mode="json"),
                severity=str(s.severity.value) if hasattr(s.severity, "value") else str(s.severity),
                tags=list(s.tags),
                derived_from=(s.meta or {}).get("derived_from"),
            )
            added.append({"id": scenario_pk, "scenario_id": s.id})
            existing_ids.add(s.id)
        conn.commit()

    if skipped:
        warnings.append(
            f"{len(skipped)} scenario(s) were skipped because their scenario_id already "
            "exists in this set. To replace, edit the existing one or assign a new id."
        )

    return AddScenariosResponse(added=added, skipped=skipped, warnings=warnings)


@app.post(
    "/api/scenario-sets/{scenario_set_id}/scenarios/from-jsonl",
    response_model=AddScenariosResponse,
    dependencies=[Depends(require_api_key)],
)
async def add_scenarios_from_jsonl(
    scenario_set_id: Annotated[int, PathParam(...)],
    file: Annotated[UploadFile, File(..., description="A .jsonl file, one scenario per line")],
    source: Annotated[str, Form()] = "bulk-import",
    created_by: Annotated[str | None, Form()] = None,
) -> AddScenariosResponse:
    """Bulk-import scenarios from a JSONL file upload.

    One scenario per line. Empty lines and `# ...` comments are ignored.
    Lines that fail to parse abort the entire batch (atomic — partial imports
    are not allowed; the user fixes the file and re-uploads).

    Output is the same shape as POST /scenarios, including the `skipped[]`
    array for scenario_ids that already exist in the set.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    scenarios: list[dict[str, Any]] = []
    for lineno, raw_line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Line {lineno}: not valid JSON: {e}",
            )
        if not isinstance(obj, dict):
            raise HTTPException(
                status_code=400,
                detail=f"Line {lineno}: each line must be a JSON object (got {type(obj).__name__})",
            )
        scenarios.append(obj)

    if not scenarios:
        raise HTTPException(status_code=400, detail="No scenarios found in the uploaded file.")

    return add_scenarios(
        scenario_set_id=scenario_set_id,
        req=AddScenariosRequest(
            scenarios=scenarios,
            source=source,
            created_by=created_by,
        ),
    )


@app.patch(
    "/api/scenarios/{scenario_pk}",
    dependencies=[Depends(require_api_key)],
)
def update_scenario(
    scenario_pk: Annotated[int, PathParam(...)],
    new_status: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None,
    verified_by: Annotated[str | None, Form()] = None,
    payload_patch_json: Annotated[str | None, Form(
        description=(
            "Optional JSON patch to merge into the scenario's payload. "
            "Allowed top-level keys: input, description, forbidden_phrases, "
            "severity, expected_tools, workflow, rubric, latency_budget_ms, "
            "max_retries, tags. Validation runs after merge — if the result "
            "fails the Scenario model, returns 400."
        ),
    )] = None,
) -> dict:
    """Approve / reject / annotate / inline-edit a scenario.

    `new_status` must be one of 'unverified' | 'approved' | 'rejected' if set.

    `payload_patch_json` enables inline editing without going through the
    regenerate flow. Use this for small tweaks (typo fix in a prompt,
    adding a forbidden phrase) — it's a shallow merge into the existing
    payload, validated against the Scenario model after merge.

    Editing a scenario auto-resets its status to 'unverified' (an edited
    scenario needs re-approval).
    """
    if new_status and new_status not in ("unverified", "approved", "rejected"):
        raise HTTPException(status_code=400, detail=f"invalid status: {new_status}")

    # Parse the patch first so we can fail fast on bad JSON.
    payload_patch: dict[str, Any] | None = None
    if payload_patch_json:
        try:
            payload_patch = json.loads(payload_patch_json)
            if not isinstance(payload_patch, dict):
                raise ValueError("payload_patch_json must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid payload_patch_json: {e}")
        # Restrict allowed keys to fields the user can safely edit. NOT
        # editable: id (would break references), meta.derived_from
        # (provenance is read-only), tags that contain 'derived:llm' or
        # 'category:*' (those track origin, not user choice).
        ALLOWED_PATCH_KEYS = {
            "input", "description", "forbidden_phrases", "forbidden_claims",
            "severity", "expected_tools", "workflow", "rubric",
            "latency_budget_ms", "max_retries", "tags", "expected_output",
            "expected_schema", "required_fields", "context",
        }
        unknown = set(payload_patch) - ALLOWED_PATCH_KEYS
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"payload_patch contains non-editable fields: {sorted(unknown)}",
            )

    with db.connect() as conn, conn.cursor() as cur:
        # Apply payload_patch first (if any) — needs the existing payload.
        if payload_patch:
            cur.execute("SELECT payload FROM scenario WHERE id = %s", (scenario_pk,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="scenario not found")
            current_payload = row[0] or {}
            merged = {**current_payload, **payload_patch}
            # Validate the result against Scenario before persisting.
            from ..models import Scenario as ScenarioModel
            try:
                ScenarioModel.model_validate(merged)
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Edited payload fails Scenario validation: {e}",
                )
            from psycopg.types.json import Json
            cur.execute(
                """
                UPDATE scenario
                SET payload = %s,
                    severity = COALESCE(%s, severity),
                    status = 'unverified',
                    verified_by = NULL,
                    verified_at = NULL
                WHERE id = %s
                """,
                (Json(merged), payload_patch.get("severity"), scenario_pk),
            )

        # Now apply the existing PATCH semantics for status/notes/verified_by.
        # If the user is BOTH editing AND approving in one call, the edit
        # happened first and reset the status; the explicit new_status
        # below overrides.
        sets, params = [], []
        if new_status is not None:
            sets.append("status = %s"); params.append(new_status)
            if new_status == "approved":
                sets.append("verified_at = now()")
                if verified_by:
                    sets.append("verified_by = %s"); params.append(verified_by)
        if notes is not None:
            sets.append("notes = %s"); params.append(notes)

        if not sets and not payload_patch:
            raise HTTPException(status_code=400, detail="no fields to update")

        if sets:
            params.append(scenario_pk)
            cur.execute(
                f"UPDATE scenario SET {', '.join(sets)} WHERE id = %s",
                params,
            )

        # Return final state.
        cur.execute("SELECT id, status FROM scenario WHERE id = %s", (scenario_pk,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="scenario not found")
        conn.commit()
        return {"id": row[0], "status": row[1], "edited": payload_patch is not None}


@app.post(
    "/api/scenarios/{scenario_pk}/regenerate",
    response_model=ScenarioRegenerateResponse,
    dependencies=[Depends(require_api_key)],
)
async def regenerate_scenario(
    scenario_pk: Annotated[int, PathParam(...)],
    req: ScenarioRegenerateRequest | None = None,
) -> ScenarioRegenerateResponse:
    """Regenerate one scenario via the LLM, preserving its category.

    Reuses the agent_definition stored on the scenario_set (no re-upload
    required). Returns both the OLD payload (so the frontend can show a
    diff) and the new one. Status is reset to 'unverified' on the server —
    a regenerated scenario is fundamentally a new test.

    Returns 400 if the scenario_set predates migration 006 (no
    agent_definition stored). The user is asked to re-upload in that case.
    """
    if req is None:
        req = ScenarioRegenerateRequest()

    with db.connect() as conn, conn.cursor() as cur:
        ctx = db.get_scenario_for_regenerate(cur, scenario_pk)
    if not ctx:
        raise HTTPException(status_code=404, detail="scenario not found")

    if not ctx["agent_definition"]:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot regenerate: this scenario_set predates per-set agent definition "
                "storage (migration 006). Re-upload the agent JSON to enable regenerate."
            ),
        )

    # Resolve the category — request override wins; otherwise use the original
    # category from derived_from; if neither, default to 'standard'.
    derived_from = ctx["derived_from"] or {}
    current_category = derived_from.get("category") or "standard"
    new_category = req.category_override or current_category

    if new_category not in llm_extractor.CATEGORIES:
        raise HTTPException(status_code=400, detail=f"unknown category: {new_category}")
    if new_category == "custom" and not req.custom_directive:
        raise HTTPException(
            status_code=400,
            detail="category_override='custom' requires a custom_directive",
        )

    # Generate exactly 1 scenario in the requested category.
    try:
        proposed = await llm_extractor.extract_async(
            ctx["agent_definition"],
            mix={new_category: 1},
            focus=req.focus,
            custom_directive=req.custom_directive,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"LLM regeneration failed: {type(e).__name__}: {e}",
        )

    if not proposed:
        raise HTTPException(
            status_code=502,
            detail=(
                "LLM returned no scenarios. Common causes: API key invalid, "
                "model down, or the agent definition has been sanitized too aggressively."
            ),
        )

    new_proposal = proposed[0]

    # Convert to scenario dict shape, then validate.
    name_prefix = ""  # the scenario keeps its original DB id; we just replace payload
    sd = llm_extractor.to_scenario_dict(
        new_proposal,
        name_prefix=name_prefix,
        source_path=ctx["source_filename"] or "regenerated",
        source_sha256=ctx["source_sha256"] or "",
        model=os.getenv("MDK_INGEST_LLM_MODEL") or llm_extractor.DEFAULT_MODEL,
        provider=os.getenv("MDK_INGEST_LLM_PROVIDER") or llm_extractor.DEFAULT_PROVIDER,
    )
    # Preserve the scenario_id slug from the existing row so it stays linked
    # in any external references (e.g. dataset.snapshot.jsonl historicals).
    sd["id"] = ctx["scenario_id"]

    # Persist atomically.
    with db.connect() as conn, conn.cursor() as cur:
        result = db.replace_scenario_payload(
            cur, scenario_pk,
            new_payload=sd,
            new_severity=sd.get("severity"),
            new_tags=sd.get("tags"),
            new_derived_from=sd.get("meta", {}).get("derived_from"),
        )
        conn.commit()

    return ScenarioRegenerateResponse(
        scenario_pk=scenario_pk,
        scenario_id=ctx["scenario_id"],
        old_payload=ctx["payload"] or {},
        new_payload=sd,
        regeneration_count=(result or {}).get("regeneration_count", 1),
        category=new_category,
    )


# ----------------------------- runs -----------------------------


@app.post(
    "/api/runs/preview",
    response_model=CostPreviewResponse,
    dependencies=[Depends(require_api_key)],
)
def preview_run_cost(req: RunRequest) -> CostPreviewResponse:
    """Return token + dollar estimate for a proposed run, WITHOUT queuing it.

    The dashboard's Run Evaluation modal calls this when the user adjusts
    judges_enabled / runs_per_scenario / only_approved, so the cost number
    updates live. Only the scenario count comes from the DB; everything
    else is derived from the request.
    """
    with db.connect() as conn, conn.cursor() as cur:
        meta = db.get_agent_and_scenario_set(cur, req.agent_id, req.scenario_set_id)
        if not meta:
            raise HTTPException(status_code=404, detail="agent or scenario_set not found")
        scenarios = db.list_scenarios(
            cur, req.scenario_set_id,
            status="approved" if req.only_approved else None,
        )
        if not scenarios and req.only_approved:
            scenarios = db.list_scenarios(cur, req.scenario_set_id)
    num_scenarios = len(scenarios)

    est = cost.estimate_run_cost(
        num_scenarios=num_scenarios,
        runs_per_scenario=req.runs_per_scenario,
        judges_enabled=req.judges_enabled,
    )
    return CostPreviewResponse(
        estimated_cost_usd=est.estimated_cost_usd,
        estimated_total_judge_calls=est.estimated_total_judge_calls,
        estimated_input_tokens=est.estimated_input_tokens,
        estimated_output_tokens=est.estimated_output_tokens,
        breakdown_per_call_usd=est.breakdown_per_call_usd,
        num_scenarios=num_scenarios,
        runs_per_scenario=req.runs_per_scenario,
        judges_enabled=req.judges_enabled,
        notes=est.notes,
    )


@app.post(
    "/api/runs",
    response_model=RunQueuedResponse,
    dependencies=[Depends(require_api_key)],
)
def queue_run(req: RunRequest) -> RunQueuedResponse:
    """Queue an evaluation. Returns the job_id; poll GET /api/runs/{job_id}.

    Atomic: the web_run_job row is inserted AND the pgmq message is enqueued
    in the same transaction. If pgmq enqueue fails, the row is rolled back —
    no orphan rows that look queued but have no message.
    """
    with db.connect() as conn, conn.cursor() as cur:
        meta = db.get_agent_and_scenario_set(cur, req.agent_id, req.scenario_set_id)
        if not meta:
            raise HTTPException(status_code=404, detail="agent or scenario_set not found")
        scenarios_for_count = db.list_scenarios(
            cur, req.scenario_set_id,
            status="approved" if req.only_approved else None,
        )
        if not scenarios_for_count and req.only_approved:
            # fall back to all scenarios if none approved (so a fresh upload
            # can be evaluated without an approval click first).
            scenarios_for_count = db.list_scenarios(cur, req.scenario_set_id)
        if not scenarios_for_count:
            raise HTTPException(status_code=400, detail="scenario_set has no scenarios")

        job_id = db.new_job_id()
        db.insert_job(
            cur,
            job_id=job_id,
            agent_id=req.agent_id,
            scenario_set_id=req.scenario_set_id,
            judges_enabled=req.judges_enabled,
            runs_per_scenario=req.runs_per_scenario,
            triggered_by=req.triggered_by,
            total_scenarios=len(scenarios_for_count),
        )
        # pgmq.send happens in the same transaction so the row+message land
        # atomically. If pgmq blows up here, the COMMIT below never fires.
        cur.execute(
            "SELECT pgmq.send(%s, %s::jsonb)",
            (queue.QUEUE_NAME, f'{{"job_id": "{job_id}"}}'),
        )
        conn.commit()

    return RunQueuedResponse(job_id=job_id, status="queued", total_scenarios=len(scenarios_for_count))


@app.get(
    "/api/portfolio/at-a-glance",
    response_model=AtAGlanceResponse,
    dependencies=[Depends(require_api_key)],
)
def portfolio_at_a_glance(
    sparkline_runs: int = 10,
    days: int = 30,
    leaderboard_limit: int = 5,
    stale_after_days: int = 7,
    include_provisional: bool = False,
) -> AtAGlanceResponse:
    """Everything the portfolio overview needs in ONE round-trip.

    By default returns only **active** agents — those that have produced at
    least one successful run (per migration 008). Provisional agents (newly
    uploaded but not yet run, or whose runs all failed) are filtered out so
    the portfolio view stays clean. Pass `include_provisional=true` to see
    drafts.

    Bolt's frontend was previously making 1 (list agents) + N (sparkline per
    agent) + 2 (cost + leaderboard) = N+3 round-trips for the portfolio view.
    At 50 agents that's 53 sequential REST calls. This endpoint flattens it
    to 4 SQL queries (latest-per-agent + last-N-per-agent + cost rollup +
    leaderboard) — performance stays flat as the portfolio grows.

    Aggregations done server-side:
      - status_counts (production_ready / pilot_ready / etc.)
      - per-engagement rollup (mean score, agent count, status distribution)
      - per-platform rollup (mean score + per-category averages — answers
        "is Lyzr better at grounding than LangGraph?")
      - sparkline + delta_vs_prior per agent
      - days_since_last_run + stale flag per agent
      - leaderboard preview (top N failing scenarios across portfolio)
      - cost rollup for the requested window

    Recommend caching client-side for 30s — the portfolio view is the
    landing page and reloads frequently.
    """
    if sparkline_runs < 1 or sparkline_runs > 100:
        raise HTTPException(status_code=400, detail="sparkline_runs must be 1–100")
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be 1–365")
    if leaderboard_limit < 0 or leaderboard_limit > 25:
        raise HTTPException(status_code=400, detail="leaderboard_limit must be 0–25")

    with db.connect() as conn, conn.cursor() as cur:
        # Detect migration 008 (is_active column). If absent, the endpoint
        # behaves as before — every agent shown — so a stale schema doesn't
        # blank the portfolio.
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='agent' "
            "AND column_name='is_active'"
        )
        has_is_active = cur.fetchone() is not None
        active_filter = (
            "WHERE a.is_active = TRUE"
            if has_is_active and not include_provisional
            else ""
        )

        # Query 1: latest run per agent + agent + engagement metadata.
        # Uses the latest_agent_run view we shipped in migration 001.
        # Joins web_run_job (since migration 005) to surface cost_usd. We
        # tolerate missing column for pre-migration-005 envs by retrying.
        try:
            cur.execute(
                f"""
                SELECT
                    a.id, a.slug, a.display_name, a.backend,
                    e.id, e.slug, e.display_name,
                    r.id AS run_pk, r.run_id, r.started_at, r.ended_at,
                    r.judges_enabled, r.runs_per_scenario,
                    s.overall_score, s.status, s.confidence,
                    s.passing_scenarios, s.total_scenarios, s.scorecard,
                    j.cost_usd
                FROM agent a
                JOIN engagement e ON a.engagement_id = e.id
                LEFT JOIN LATERAL (
                    SELECT id, run_id, started_at, ended_at, judges_enabled, runs_per_scenario
                    FROM run WHERE agent_id = a.id
                    ORDER BY started_at DESC LIMIT 1
                ) r ON true
                LEFT JOIN evaluation_summary s ON s.run_id = r.id
                LEFT JOIN web_run_job j ON j.result_run_id = r.id::text
                {active_filter}
                ORDER BY e.display_name, a.display_name
                """
            )
            _has_cost_col = True
        except psycopg.errors.UndefinedColumn:
            conn.rollback()
            cur.execute(
                f"""
                SELECT
                    a.id, a.slug, a.display_name, a.backend,
                    e.id, e.slug, e.display_name,
                    r.id AS run_pk, r.run_id, r.started_at, r.ended_at,
                    r.judges_enabled, r.runs_per_scenario,
                    s.overall_score, s.status, s.confidence,
                    s.passing_scenarios, s.total_scenarios, s.scorecard
                FROM agent a
                JOIN engagement e ON a.engagement_id = e.id
                LEFT JOIN LATERAL (
                    SELECT id, run_id, started_at, ended_at, judges_enabled, runs_per_scenario
                    FROM run WHERE agent_id = a.id
                    ORDER BY started_at DESC LIMIT 1
                ) r ON true
                LEFT JOIN evaluation_summary s ON s.run_id = r.id
                {active_filter}
                ORDER BY e.display_name, a.display_name
                """
            )
            _has_cost_col = False
        agent_rows = cur.fetchall()

        # Query 2: last N runs per agent for sparklines (and delta calculation).
        # One query, partitioned via window function — flat in agent count.
        cur.execute(
            """
            WITH ranked AS (
                SELECT r.agent_id, s.overall_score, r.started_at,
                       row_number() OVER (PARTITION BY r.agent_id
                                          ORDER BY r.started_at DESC) AS rn
                FROM run r
                JOIN evaluation_summary s ON s.run_id = r.id
            )
            SELECT agent_id, overall_score, started_at
            FROM ranked
            WHERE rn <= %s
            ORDER BY agent_id, rn ASC
            """,
            (sparkline_runs,),
        )
        sparkline_rows = cur.fetchall()
        sparklines: dict[int, list[float]] = {}
        for agent_id, score, _ts in sparkline_rows:
            sparklines.setdefault(agent_id, []).append(float(score))

        # Query 3: cost rollup for the window.
        cur.execute(
            """
            SELECT
                COALESCE(sum(j.cost_usd), 0) AS total_cost,
                count(j.id) FILTER (WHERE j.cost_usd IS NULL AND j.status = 'done') AS untracked_runs,
                count(j.id) FILTER (WHERE j.status = 'done') AS total_runs
            FROM web_run_job j
            WHERE j.created_at >= now() - make_interval(days => %s)
            """,
            (days,),
        )
        cost_total, untracked_runs, total_runs = cur.fetchone()

        # Query 4: leaderboard preview (top N failing scenarios).
        leaderboard: list[AtAGlanceLeaderboardEntry] = []
        if leaderboard_limit > 0:
            cur.execute(
                """
                WITH recent AS (
                    SELECT sa.id, sa.scenario_id, sa.pass_rate, sa.severity, r.agent_id
                    FROM scenario_aggregate sa
                    JOIN run r ON r.id = sa.run_id
                    WHERE r.started_at >= now() - make_interval(days => %s)
                ),
                top_failure AS (
                    SELECT sa.scenario_id, f.failure_class,
                           row_number() OVER (PARTITION BY sa.scenario_id
                                              ORDER BY count(*) DESC) AS rk
                    FROM scenario_aggregate sa
                    JOIN finding f ON f.scenario_aggregate_id = sa.id
                    JOIN run r ON r.id = sa.run_id
                    WHERE r.started_at >= now() - make_interval(days => %s)
                    GROUP BY sa.scenario_id, f.failure_class
                )
                SELECT
                    r.scenario_id,
                    count(DISTINCT r.agent_id) AS agents_affected,
                    round(avg(r.pass_rate)::numeric, 3) AS mean_pass_rate,
                    max(r.severity) AS severity_max,
                    (SELECT failure_class FROM top_failure t
                      WHERE t.scenario_id = r.scenario_id AND t.rk = 1) AS dominant_failure_class
                FROM recent r
                GROUP BY r.scenario_id
                HAVING avg(r.pass_rate) < 1.0
                ORDER BY avg(r.pass_rate) ASC, count(DISTINCT r.agent_id) DESC
                LIMIT %s
                """,
                (days, days, leaderboard_limit),
            )
            for row in cur.fetchall():
                leaderboard.append(AtAGlanceLeaderboardEntry(
                    scenario_id=row[0],
                    agents_affected=row[1],
                    mean_pass_rate=float(row[2]),
                    severity_max=row[3],
                    dominant_failure_class=row[4],
                ))

    # ---- shape the response in Python (no further DB queries) ----

    now_utc = datetime.now(timezone.utc)
    agents: list[AtAGlanceAgent] = []
    status_counts: dict[str, int] = {}
    engagements_acc: dict[str, dict[str, Any]] = {}
    platforms_acc: dict[str, dict[str, Any]] = {}

    for row in agent_rows:
        if _has_cost_col:
            (a_id, a_slug, a_name, backend,
             e_id, e_slug, e_name,
             run_pk, run_id, started_at, ended_at, judges_enabled, runs_per_scenario,
             overall_score, status_value, confidence,
             passing_scenarios, total_scenarios, scorecard,
             cost_usd) = row
        else:
            (a_id, a_slug, a_name, backend,
             e_id, e_slug, e_name,
             run_pk, run_id, started_at, ended_at, judges_enabled, runs_per_scenario,
             overall_score, status_value, confidence,
             passing_scenarios, total_scenarios, scorecard) = row
            cost_usd = None

        latest_run: AtAGlanceLatestRun | None = None
        days_since: int | None = None
        delta: float | None = None
        if run_pk is not None:
            latest_run = AtAGlanceLatestRun(
                run_id_pk=run_pk,
                run_id=run_id,
                started_at=started_at,
                ended_at=ended_at,
                overall_score=float(overall_score) if overall_score is not None else 0.0,
                status=status_value or "not_ready",
                confidence=float(confidence) if confidence is not None else 0.0,
                passing_scenarios=passing_scenarios or 0,
                total_scenarios=total_scenarios or 0,
                scorecard=scorecard or {},
                judges_enabled=bool(judges_enabled or []),
                cost_usd=float(cost_usd) if cost_usd is not None else None,
            )
            days_since = (now_utc - started_at).days if started_at else None
            # Sparkline is newest-first; index 1 (if present) is the prior run.
            sparkline = sparklines.get(a_id, [])
            if len(sparkline) >= 2:
                delta = round(sparkline[0] - sparkline[1], 2)

        agent_obj = AtAGlanceAgent(
            id=a_id, slug=a_slug, display_name=a_name, backend=backend,
            engagement_id=e_id, engagement_slug=e_slug, engagement_name=e_name,
            latest_run=latest_run,
            sparkline=sparklines.get(a_id, []),
            delta_vs_prior=delta,
            days_since_last_run=days_since,
            stale=(days_since is not None and days_since > stale_after_days),
        )
        agents.append(agent_obj)

        # Aggregate status counts (latest run only)
        if latest_run:
            status_counts[latest_run.status] = status_counts.get(latest_run.status, 0) + 1

            # Engagement rollup
            eng_bucket = engagements_acc.setdefault(e_slug, {
                "slug": e_slug, "display_name": e_name,
                "scores": [], "status_counts": {}
            })
            eng_bucket["scores"].append(latest_run.overall_score)
            eng_bucket["status_counts"][latest_run.status] = eng_bucket["status_counts"].get(latest_run.status, 0) + 1

            # Platform rollup — also accumulates per-category for the benchmark bar
            plat_bucket = platforms_acc.setdefault(backend, {
                "name": backend, "scores": [], "category_scores": {}
            })
            plat_bucket["scores"].append(latest_run.overall_score)
            for cat, val in (scorecard or {}).items():
                if isinstance(val, (int, float)):
                    plat_bucket["category_scores"].setdefault(cat, []).append(float(val))

    # Finalize rollups
    engagements = [
        AtAGlanceEngagementRollup(
            slug=b["slug"],
            display_name=b["display_name"],
            agent_count=len(b["scores"]),
            mean_score=round(sum(b["scores"]) / len(b["scores"]), 2) if b["scores"] else 0.0,
            status_counts=b["status_counts"],
        )
        for b in engagements_acc.values()
    ]
    platforms = [
        AtAGlancePlatformRollup(
            name=b["name"],
            agent_count=len(b["scores"]),
            mean_score=round(sum(b["scores"]) / len(b["scores"]), 2) if b["scores"] else 0.0,
            mean_per_category={
                cat: round(sum(vals) / len(vals), 2)
                for cat, vals in b["category_scores"].items()
                if vals
            },
        )
        for b in platforms_acc.values()
    ]

    # Recency alerts: agents not run in > stale_after_days
    recency_alerts = [
        {
            "agent_slug": a.slug,
            "agent_name": a.display_name,
            "engagement_name": a.engagement_name,
            "days_since": a.days_since_last_run,
            "last_score": a.latest_run.overall_score if a.latest_run else None,
        }
        for a in agents if a.stale
    ]

    return AtAGlanceResponse(
        generated_at=now_utc,
        window_days=days,
        summary=AtAGlanceSummary(
            total_agents=len(agents),
            total_engagements=len(engagements_acc),
            status_counts=status_counts,
            total_runs_in_window=int(total_runs or 0),
            total_cost_usd_in_window=round(float(cost_total or 0), 4),
            runs_without_cost=int(untracked_runs or 0),
        ),
        agents=agents,
        engagements=engagements,
        platforms=platforms,
        leaderboard_preview=leaderboard,
        recency_alerts=recency_alerts,
    )


@app.get("/api/portfolio/leaderboard", dependencies=[Depends(require_api_key)])
def portfolio_leaderboard(limit: int = 25, days: int = 90) -> list[dict]:
    """Scenarios that fail most often across the entire portfolio.

    Aggregates over all `scenario_aggregate` rows in the last `days` days.
    A high failure rate across multiple agents = systemic issue (KB problem,
    methodology gap, evaluation design flaw) rather than agent-specific bug.

    Powers the "Patterns" tab — the headline differentiating feature for
    portfolios with 10+ agents.

    Returns: scenario_id, agents_affected, runs_observed, mean_pass_rate,
             mean_score, dominant_failure_class, severity_max.
    """
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be 1–100")
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be 1–365")
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH recent AS (
                SELECT sa.id, sa.scenario_id, sa.pass_rate, sa.mean_score,
                       sa.severity, r.agent_id
                FROM scenario_aggregate sa
                JOIN run r ON r.id = sa.run_id
                WHERE r.started_at >= now() - make_interval(days => %s)
            ),
            top_failure_per_scenario AS (
                SELECT sa.scenario_id, f.failure_class, count(*) AS occurrences,
                       row_number() OVER (PARTITION BY sa.scenario_id
                                          ORDER BY count(*) DESC) AS rk
                FROM scenario_aggregate sa
                JOIN finding f ON f.scenario_aggregate_id = sa.id
                JOIN run r ON r.id = sa.run_id
                WHERE r.started_at >= now() - make_interval(days => %s)
                GROUP BY sa.scenario_id, f.failure_class
            )
            SELECT
                r.scenario_id,
                count(DISTINCT r.agent_id) AS agents_affected,
                count(*) AS runs_observed,
                round(avg(r.pass_rate)::numeric, 3) AS mean_pass_rate,
                round(avg(r.mean_score)::numeric, 2) AS mean_score,
                max(r.severity) AS severity_max,
                (SELECT failure_class FROM top_failure_per_scenario t
                  WHERE t.scenario_id = r.scenario_id AND t.rk = 1) AS dominant_failure_class
            FROM recent r
            GROUP BY r.scenario_id
            HAVING avg(r.pass_rate) < 1.0   -- only show scenarios that ever fail
            ORDER BY avg(r.pass_rate) ASC, count(DISTINCT r.agent_id) DESC
            LIMIT %s
            """,
            (days, days, limit),
        )
        cols = ("scenario_id", "agents_affected", "runs_observed",
                "mean_pass_rate", "mean_score", "severity_max",
                "dominant_failure_class")
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get("/api/portfolio/cost", dependencies=[Depends(require_api_key)])
def portfolio_cost(days: int = 30) -> dict:
    """Cost rollup across the portfolio. Total spend + per-agent breakdown.

    Powers the "Cost" panel + budget alerting in the dashboard. Costs are
    populated when a job lands; older jobs without cost_usd return 0 and
    are flagged in `notes`.
    """
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be 1–365")
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                e.slug AS engagement_slug, e.display_name AS engagement_name,
                a.slug AS agent_slug,      a.display_name AS agent_name,
                count(j.id) AS run_count,
                COALESCE(sum(j.cost_usd), 0) AS total_cost_usd,
                count(j.id) FILTER (WHERE j.cost_usd IS NULL) AS runs_without_cost
            FROM web_run_job j
            JOIN agent a ON a.id = j.agent_id
            JOIN engagement e ON e.id = a.engagement_id
            WHERE j.created_at >= now() - make_interval(days => %s)
              AND j.status = 'done'
            GROUP BY e.slug, e.display_name, a.slug, a.display_name
            ORDER BY total_cost_usd DESC
            """,
            (days,),
        )
        rows = cur.fetchall()
        per_agent = [
            {
                "engagement_slug": r[0], "engagement_name": r[1],
                "agent_slug": r[2], "agent_name": r[3],
                "run_count": r[4], "total_cost_usd": float(r[5]),
                "runs_without_cost": r[6],
            }
            for r in rows
        ]
    total = sum(r["total_cost_usd"] for r in per_agent)
    untracked = sum(r["runs_without_cost"] for r in per_agent)
    notes = []
    if untracked > 0:
        notes.append(
            f"{untracked} run(s) in the window pre-date cost tracking and aren't included in the total."
        )
    return {
        "window_days": days,
        "total_cost_usd": round(total, 4),
        "per_agent": per_agent,
        "notes": notes,
    }


@app.get(
    "/api/insights/portfolio/{kind}/{name}",
    response_model=InsightResponse,
    dependencies=[Depends(require_api_key)],
)
def get_portfolio_insight(
    kind: Annotated[str, PathParam(..., description="'category' or 'kpi'")],
    name: Annotated[str, PathParam(..., description="Category or KPI name")],
) -> InsightResponse:
    """LLM narrative explaining a dimension's portfolio-wide pattern.

    Operates over the latest run of every agent. Surfaces systemic issues
    (cross-agent failure modes, platform trade-offs) — the kind of insight
    no single-agent view can produce. Cached via the judge cache.
    """
    try:
        ins = insights.generate_portfolio(kind=kind, name=name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return InsightResponse(
        title=ins.title, score=ins.score, narrative=ins.narrative,
        top_offenders=ins.top_offenders, suggested_fixes=ins.suggested_fixes,
        suggested_new_scenarios=ins.suggested_new_scenarios,
        confidence=ins.confidence, notes=ins.notes,
    )


@app.get(
    "/api/insights/{run_id}/{kind}/{name}",
    response_model=InsightResponse,
    dependencies=[Depends(require_api_key)],
)
def get_insight(
    run_id: Annotated[int, PathParam(..., description="The run.id PK (not the timestamped run_id string)")],
    kind: Annotated[str, PathParam(..., description="'category' or 'kpi'")],
    name: Annotated[str, PathParam(..., description="Category or KPI name (e.g. 'correctness', 'accuracy')")],
) -> InsightResponse:
    """LLM-generated insight explaining why a category/KPI scored what it did.

    Cached: same (run, kind, name) returns bit-identical output on subsequent
    calls. Cost ~$0.01 on first call, $0 thereafter.
    """
    try:
        ins = insights.generate(run_pk=run_id, kind=kind, name=name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return InsightResponse(
        title=ins.title,
        score=ins.score,
        narrative=ins.narrative,
        top_offenders=ins.top_offenders,
        suggested_fixes=ins.suggested_fixes,
        suggested_new_scenarios=ins.suggested_new_scenarios,
        confidence=ins.confidence,
        notes=ins.notes,
        score_band=ins.score_band,
        weight_pct=ins.weight_pct,
        raw_weight=ins.raw_weight,
        distribution=ins.distribution,
    )


# ----------------------------- scoring profiles -----------------------------


@app.get(
    "/api/scoring-profiles",
    response_model=ScoringProfileCatalogResponse,
    dependencies=[Depends(require_api_key)],
)
def list_scoring_profiles() -> ScoringProfileCatalogResponse:
    """Return the curated preset library + framework defaults.

    Powers Bolt's preset-picker UI (the "Scoring Profile" step on the upload
    flow). Cache client-side — presets only change when the backend redeploys
    with edits, so a 5-minute cache is fine.

    The `default_*` fields let Bolt render "preset overrides X relative to
    default Y" without a second round-trip.
    """
    from . import scoring_profiles as sp
    return ScoringProfileCatalogResponse(
        presets=[ScoringProfileResponse(**p.model_dump()) for p in sp.list_presets()],
        default_weights=sp.DEFAULT_WEIGHTS,
        default_status_bands=sp.DEFAULT_STATUS_BANDS,
        default_pass_threshold=sp.DEFAULT_PASS_THRESHOLD,
        default_hard_gates=sp.DEFAULT_HARD_GATES,
        all_categories=sp.ALL_CATEGORIES,
    )


@app.post(
    "/api/scoring-profiles/recommend",
    response_model=ScoringProfileRecommendationResponse,
    dependencies=[Depends(require_api_key)],
)
async def recommend_scoring_profile(
    file: Annotated[UploadFile | None, File(description="Lyzr agent definition JSON (optional)")] = None,
    agent_definition_json: Annotated[str | None, Form(description="Inline JSON if not uploading a file")] = None,
) -> ScoringProfileRecommendationResponse:
    """Read an agent definition and recommend a scoring profile + per-category overrides.

    Accepts either an uploaded JSON file or the inline JSON string (Bolt's
    upload page already has the JSON in memory before submitting). At least
    one of `file` / `agent_definition_json` must be supplied.

    LLM-backed (Anthropic) with cached responses: same agent definition →
    same response, $0. Falls back to a deterministic heuristic if the LLM is
    unavailable so the endpoint always returns useful output.

    Cost: ~$0.01-0.02 per uncached call.
    """
    if not file and not agent_definition_json:
        raise HTTPException(status_code=400, detail="Provide either `file` or `agent_definition_json`.")

    if file:
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        from ..ingest.sanitize import sanitize_bytes
        raw, _redactions = sanitize_bytes(raw)
        try:
            agent_def = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise HTTPException(status_code=400, detail=f"Not valid JSON: {e}")
    else:
        try:
            agent_def = json.loads(agent_definition_json or "{}")
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid agent_definition_json: {e}")

    if not isinstance(agent_def, dict):
        raise HTTPException(status_code=400, detail="Agent definition must be a JSON object.")

    from . import scoring_profiles as sp
    rec = await sp.recommend_async(agent_def)

    return ScoringProfileRecommendationResponse(
        recommended_preset=rec.recommended_preset,
        profile=ScoringProfileResponse(**rec.profile.model_dump()),
        reasoning=rec.reasoning,
        category_recommendations=[
            ScoringProfileCategoryReco(**cr.model_dump())
            for cr in rec.category_recommendations
        ],
        confidence=rec.confidence,
        notes=rec.notes,
    )


@app.post(
    "/api/agent-definitions/topics",
    response_model=TopicExtractionResponse,
    dependencies=[Depends(require_api_key)],
)
async def extract_agent_topics(
    file: Annotated[UploadFile | None, File(description="Lyzr agent definition JSON (optional)")] = None,
    agent_definition_json: Annotated[str | None, Form(description="Inline JSON if not uploading a file")] = None,
) -> TopicExtractionResponse:
    """Extract agent-specific TOPICAL categories from an agent definition.

    Topics are an orthogonal axis to the framework's behavioral categories
    (standard / edge / adversarial / safety / honesty / multi_turn /
    performance / custom). Where behavioral categories describe HOW a test
    stresses the agent, topics describe WHAT the test is about — the business
    domains the agent actually operates in.

    Example: the Movate FAQ agent might return topics like 'Movate Services',
    'Company Information', 'Career & Hiring'. The Returns Manager might return
    'Order Lookup', 'Refund Eligibility', 'Image Validation'.

    Accepts either an uploaded JSON file or the inline JSON string (Bolt's
    upload page typically has the JSON in memory before submitting). At least
    one of `file` / `agent_definition_json` must be supplied.

    LLM-backed (Claude Haiku for cost), cached by agent fingerprint, falls
    back to a deterministic heuristic when the LLM is unavailable. Always
    returns a non-empty topic list.

    Cost: ~$0.005-0.01 per uncached call.

    Pair with POST /api/scenarios/propose-one (pass the topic name as the
    `topic` field) to generate scenarios specific to a chosen topic.
    """
    if not file and not agent_definition_json:
        raise HTTPException(status_code=400, detail="Provide either `file` or `agent_definition_json`.")

    if file:
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        from ..ingest.sanitize import sanitize_bytes
        raw, _redactions = sanitize_bytes(raw)
        try:
            agent_def = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise HTTPException(status_code=400, detail=f"Not valid JSON: {e}")
    else:
        try:
            agent_def = json.loads(agent_definition_json or "{}")
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid agent_definition_json: {e}")

    if not isinstance(agent_def, dict):
        raise HTTPException(status_code=400, detail="Agent definition must be a JSON object.")

    from ..insights.topic_extractor import extract_topics_async
    result = await extract_topics_async(agent_def)

    return TopicExtractionResponse(
        topics=[TopicResponse(**t.__dict__) for t in result.topics],
        source=result.source,
        notes=result.notes,
    )


# ----------------------------- business report -----------------------------


@app.get(
    "/api/runs/{run_id}/topic-breakdown",
    response_model=TopicBreakdownResponse,
    dependencies=[Depends(require_api_key)],
)
def get_run_topic_breakdown(
    run_id: Annotated[int, PathParam(..., description="The run.id PK (not the timestamped run_id string)")],
    topic_names: Annotated[str | None, Query(
        description=(
            "Optional JSON object mapping topic_slug -> display_name to override the default "
            "(slug used as display name). Useful if the caller has already fetched topics "
            "via /api/agent-definitions/topics and wants names rendered server-side."
        )
    )] = None,
) -> TopicBreakdownResponse:
    """Per-topic scoring rollup for one completed run.

    Groups all `scenario_aggregate` rows by their `topic:<slug>` tags and
    returns mean score / pass rate / failure count / max severity per topic,
    plus a category breakdown showing how each behavioral category did
    *within* that topic.

    Topics are sorted worst-first (lowest mean_score). Scenarios with no
    `topic:<slug>` tag bucket under `'untagged'` so legacy / mixed runs still
    return a complete view.

    Cost: zero LLM calls. Pure SQL groupby + Python aggregation.

    Returns 404 if the run doesn't exist. Returns 200 with empty `topics`
    if the run exists but has no aggregate rows yet (still running, or no
    scenarios produced).
    """
    from ..insights.topic_breakdown import compute_topic_breakdown

    # Optional name override map (slug -> display name)
    name_overrides: dict[str, str] = {}
    if topic_names:
        try:
            parsed = json.loads(topic_names)
            if not isinstance(parsed, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
            ):
                raise ValueError("topic_names must be a JSON object mapping str->str")
            name_overrides = parsed
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid topic_names: {e}")

    with db.connect() as conn, conn.cursor() as cur:
        meta = db.get_run_metadata(cur, run_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"Run pk={run_id} not found")
        rows = db.fetch_scenario_aggregate_for_run(cur, run_id)

    breakdown = compute_topic_breakdown(
        rows,
        run_pk=meta["id"],
        run_id=meta["run_id"],
        topic_display_names=name_overrides,
    )

    return TopicBreakdownResponse(
        run_pk=breakdown.run_pk,
        run_id=breakdown.run_id,
        topics=[
            TopicScoreEntryResponse(
                slug=t.slug,
                display_name=t.display_name,
                scenarios_count=t.scenarios_count,
                mean_score=t.mean_score,
                pass_rate=t.pass_rate,
                failures_count=t.failures_count,
                severity_max=t.severity_max,
                category_breakdown={
                    cat: TopicScoreCategoryEntry(
                        mean_score=stats["mean_score"],
                        pass_rate=stats["pass_rate"],
                        scenarios_count=stats["scenarios_count"],
                    )
                    for cat, stats in t.category_breakdown.items()
                },
                scenario_ids=t.scenario_ids,
            )
            for t in breakdown.topics
        ],
        untagged_count=breakdown.untagged_count,
        total_scenarios=breakdown.total_scenarios,
    )


@app.get(
    "/api/runs/{run_pk}/provenance",
    response_model=RunProvenanceResponse,
    dependencies=[Depends(require_api_key)],
)
def get_run_provenance(
    run_pk: Annotated[int, PathParam(..., description="The run.id PK (not the timestamped run_id string)")],
) -> RunProvenanceResponse:
    """Full provenance for a completed run.

    Powers Bolt's Run Detail → Provenance tab. Returns every fingerprint
    that affects this run's score, library versions snapshotted at run time
    AND at query time (so auditors can spot drift between when the run
    happened and when it's being inspected), plus the LLM models the
    post-run analysis surfaces currently default to.

    Audit semantics:
      - `run.*`        — schema/methodology/mdk_eval/manifest/dataset/config
                          fingerprints. Reproducible: an auditor with these
                          can replay the exact run.
      - `scoring.*`    — judge models + prompt SHAs + arbitration policy.
                          Pinned to the run; immutable.
      - `tool_versions_at_run_time` — packages installed in the worker
                          when this run executed. Pinned; immutable.
      - `tool_versions_at_query_time` — packages installed RIGHT NOW on
                          the API server. Differences from at_run_time
                          highlight drift since the run.
      - `downstream_llm_models` — what the doctor / insights / topic
                          extractor would use IF called against this run
                          right now. Reflects current server defaults,
                          not historical fact. (For per-call audit, use
                          the `prompt_sha` returned by each surface.)

    Returns 404 if the run doesn't exist.
    """
    from ..storage.versioning import tool_versions as current_tool_versions
    from ..insights import agent_doctor as _doctor_mod
    from ..insights import topic_extractor as _topics_mod
    from . import insights as _insights_mod
    from . import business_report as _br_mod

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.run_id, r.agent_id, a.slug,
                   r.schema_version, r.methodology_version, r.mdk_eval_version,
                   r.manifest_sha256, r.dataset_sha256, r.config_sha256,
                   r.started_at, r.ended_at, r.ingested_at,
                   r.judges_enabled, r.judge_models, r.judge_prompts_sha256,
                   r.meta_judge_model, r.arbitration_threshold, r.runs_per_scenario,
                   r.tool_versions, r.triggered_by, r.ci_url, r.notes
            FROM run r
            JOIN agent a ON a.id = r.agent_id
            WHERE r.id = %s
            """,
            (run_pk,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Run pk={run_pk} not found")

    (
        rid, run_id_str, agent_id, agent_slug,
        schema_v, methodology_v, mdk_v,
        manifest_sha, dataset_sha, config_sha,
        started_at, ended_at, ingested_at,
        judges_enabled, judge_models, judge_prompts_sha,
        meta_judge_model, arb_threshold, runs_per_scenario,
        tool_vers_at_run, triggered_by, ci_url, notes_field,
    ) = row

    return RunProvenanceResponse(
        run_pk=int(rid),
        run_id=run_id_str,
        agent_id=int(agent_id),
        agent_slug=agent_slug,
        run=RunProvenanceCore(
            schema_version=schema_v or "",
            methodology_version=methodology_v or "",
            mdk_eval_version=mdk_v or "",
            manifest_sha256=manifest_sha or "",
            dataset_sha256=dataset_sha or "",
            config_sha256=config_sha or "",
            started_at=started_at.isoformat() if started_at else None,
            ended_at=ended_at.isoformat() if ended_at else None,
            ingested_at=ingested_at.isoformat() if ingested_at else None,
        ),
        scoring=ScoringProvenance(
            judges_enabled=list(judges_enabled or []),
            judge_models=judge_models or {},
            judge_prompts_sha256=judge_prompts_sha or {},
            meta_judge_model=meta_judge_model,
            arbitration_threshold=(
                float(arb_threshold) if arb_threshold is not None else None
            ),
            runs_per_scenario=int(runs_per_scenario or 1),
        ),
        tool_versions_at_run_time=dict(tool_vers_at_run or {}),
        tool_versions_at_query_time=current_tool_versions(),
        downstream_llm_models=[
            DownstreamLLMModel(
                provider=_topics_mod.DEFAULT_PROVIDER,
                model=_topics_mod.DEFAULT_MODEL,
                max_tokens=2048,
                purpose="topic_extractor (pre-run; categorizes agent into business topics)",
            ),
            DownstreamLLMModel(
                provider=_insights_mod.DEFAULT_INSIGHT_PROVIDER,
                model=_insights_mod.DEFAULT_INSIGHT_MODEL,
                max_tokens=1024,
                purpose="category/KPI insights (per-dimension drill-down narrative)",
            ),
            DownstreamLLMModel(
                provider=_doctor_mod.DEFAULT_PROVIDER,
                model=_doctor_mod.DEFAULT_MODEL,
                max_tokens=4096,
                purpose="agent_doctor (3-tier diagnostic: exec summary + prescriptions + specific changes)",
            ),
            DownstreamLLMModel(
                provider=getattr(_br_mod, "DEFAULT_PROVIDER", "anthropic"),
                model=getattr(_br_mod, "DEFAULT_MODEL", "claude-sonnet-4-6"),
                max_tokens=4096,
                purpose="business_report (executive narrative + production recommendation)",
            ),
        ],
        triggered_by=triggered_by,
        ci_url=ci_url,
        notes=notes_field,
    )


@app.get(
    "/api/runs/{run_id}/doctor",
    response_model=AgentDoctorResponse,
    dependencies=[Depends(require_api_key)],
)
async def get_agent_doctor(
    run_id: Annotated[int, PathParam(..., description="The run.id PK (not the timestamped run_id string)")],
    regenerate: Annotated[bool, Query(
        description=(
            "When true, bypass the content-fingerprint cache and force a fresh "
            "LLM call. Use for the dashboard's 'Regenerate' button — gives the "
            "user a new framing of the same run data. Each fresh call costs "
            "$0.02-0.05 (Claude Sonnet); cached re-fetches are $0. Default false."
        )
    )] = False,
    stream: Annotated[bool, Query(
        description=(
            "When true, return Server-Sent Events (Content-Type: text/event-stream) "
            "with phase-progress messages while the report is built. Use this from "
            "Bolt's diagnostics tab to rotate spinner messages ('Loading run data', "
            "'Generating diagnosis', etc.) instead of showing one static spinner "
            "for the full 5-15s LLM wall-clock. The final SSE event has "
            "phase=\"done\" and a `report` field with the full AgentDoctorResponse "
            "shape. Default false (returns the report as one JSON response)."
        )
    )] = False,
) -> AgentDoctorResponse:
    """Agent Doctor (Rx) — 3-tier diagnostic for a completed run.

    Tier 1 (executive summary + headline action) is for delivery managers.
    Tier 2 (top 3 prescriptions, cited and confidence-tagged) is for the
    engineering team.
    Tier 3 (specific suggested changes to the agent definition) is for the
    engineer making the next edit.

    Cached by run-content fingerprint — re-fetching the same run is $0.
    Falls back to a deterministic template when the LLM is unavailable, so
    the endpoint always returns useful content.

    Cost: ~$0.02-0.05 on first call per run; $0 on subsequent calls.
    Pass `?regenerate=true` to invalidate cache + force a fresh LLM call.

    Response includes `last_generated_at` (ISO-8601 UTC) so the dashboard
    can render a "Generated 2h ago" tooltip alongside the Regenerate button.
    """
    from ..insights.agent_doctor import generate_from_db_async, generate_from_db_streaming

    # SSE streaming path. Returns text/event-stream with one event per phase
    # progress milestone, terminating with a `phase=done` event carrying the
    # full report. Bolt should use EventSource(`...?stream=true`) and listen
    # for messages; the `done` event ends the stream and unblocks rendering.
    if stream:
        async def _sse_emitter():
            try:
                async for msg in generate_from_db_streaming(
                    run_pk=run_id, regenerate=regenerate,
                ):
                    yield f"data: {json.dumps(msg, default=str)}\n\n"
            except ValueError as e:
                # Convert run-not-found to a structured error event so the
                # client's onmessage handler can surface it cleanly.
                yield f"data: {json.dumps({'phase': 'error', 'detail': str(e)})}\n\n"

        return StreamingResponse(
            _sse_emitter(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",   # disable nginx-style buffering
                "Connection": "keep-alive",
            },
        )

    # Non-streaming (default) — single JSON response.
    try:
        doctor = await generate_from_db_async(run_pk=run_id, regenerate=regenerate)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return AgentDoctorResponse(
        run_id=doctor.run_id,
        overall_score=doctor.overall_score,
        status=doctor.status,
        executive_summary=doctor.executive_summary,
        headline_action=doctor.headline_action,
        prescriptions=[
            AgentDoctorPrescriptionResponse(
                title=p.title,
                diagnosis=p.diagnosis,
                treatment=p.treatment,
                expected_impact=p.expected_impact,
                confidence=p.confidence,
                cited_scenarios=p.cited_scenarios,
                cited_findings=p.cited_findings,
            )
            for p in doctor.prescriptions
        ],
        specific_changes=[
            AgentDoctorSpecificChangeResponse(
                target=c.target, change=c.change, rationale=c.rationale,
            )
            for c in doctor.specific_changes
        ],
        confidence=doctor.confidence,
        source=doctor.source,
        prompt_sha=doctor.prompt_sha,
        notes=doctor.notes,
        last_generated_at=(
            doctor.generated_at.isoformat() if doctor.generated_at else None
        ),
    )


@app.get(
    "/api/runs/{run_id}/business-report",
    response_model=BusinessReportResponse,
    dependencies=[Depends(require_api_key)],
)
def get_business_report(
    run_id: Annotated[int, PathParam(..., description="The run.id PK (not the timestamped run_id string)")],
) -> BusinessReportResponse:
    """Executive-facing report for a completed run.

    Differs from `/api/runs/{job_id}` (which is a thin status poll) in that
    this endpoint produces a polished business narrative, top wins / losses,
    a prioritized fix list, and a production recommendation. Powers Bolt's
    "Executive view" tab.

    The narrative paragraph uses the LLM (cached by content fingerprint, so
    re-fetching the same run costs $0). If the LLM is unavailable, falls back
    to a deterministic templated narrative — the endpoint always returns
    useful content.

    Cost: ~$0.02-0.05 on first call per run; $0 on subsequent calls.
    """
    try:
        report = business_report_mod.generate(run_pk=run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return BusinessReportResponse(
        run_pk=report.run_pk,
        run_id=report.run_id,
        agent_slug=report.agent_slug,
        agent_display_name=report.agent_display_name,
        overall_score=report.overall_score,
        overall_score_ci=report.overall_score_ci,
        status=report.status,
        pass_rate=report.pass_rate,
        pass_rate_ci=report.pass_rate_ci,
        total_scenarios=report.total_scenarios,
        passing_scenarios=report.passing_scenarios,
        headline=report.headline,
        executive_narrative=report.executive_narrative,
        narrative_source=report.narrative_source,
        top_wins=report.top_wins,
        top_losses=report.top_losses,
        failure_clusters=report.failure_clusters,
        risk_register=report.risk_register,
        what_to_fix_first=report.what_to_fix_first,
        production_recommendation=report.production_recommendation,
        production_recommendation_text=report.production_recommendation_text,
    )


@app.get(
    "/api/runs/{job_id}",
    response_model=RunStatusResponse,
    dependencies=[Depends(require_api_key)],
)
def get_run_status(job_id: Annotated[str, PathParam(...)]) -> RunStatusResponse:
    """Poll status of a queued/running/done evaluation."""
    with db.connect() as conn, conn.cursor() as cur:
        job = db.get_job(cur, job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")
        return RunStatusResponse(**job)

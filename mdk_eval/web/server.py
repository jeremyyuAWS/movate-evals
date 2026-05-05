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
from typing import Annotated

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Path as PathParam,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db, jobs
from .schemas import (
    IngestedScenario,
    IngestResponse,
    RunQueuedResponse,
    RunRequest,
    RunStatusResponse,
)
from .. import __version__
from ..ingest.lyzr import LyzrIngestor
from ..ingest.extractors import llm as llm_extractor


# ----------------------------- app + middleware -----------------------------


app = FastAPI(
    title="Movate Agent Assurance — Web API",
    version=__version__,
    description=(
        "Backend for the Bolt-generated Movate Agent Assurance dashboard. "
        "Drives ingestion + eval execution; reads/writes the same Postgres "
        "schema the dashboard renders."
    ),
)

_cors_origins = [o.strip() for o in (os.getenv("MDK_WEB_CORS_ORIGINS") or "").split(",") if o.strip()]
if not _cors_origins:
    # Permissive default for local dev only. Production deploys MUST set the env var.
    _cors_origins = ["http://localhost:3000", "http://localhost:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


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
    """Liveness probe. No auth — Fly's health checks need to reach this."""
    return {"status": "ok", "version": __version__}


# ----------------------------- agents (read) -----------------------------


@app.get("/api/agents", dependencies=[Depends(require_api_key)])
def list_agents() -> list[dict]:
    """List agents the dashboard can target for evaluation runs."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.slug, a.display_name, a.backend, a.backend_id,
                   e.slug AS engagement_slug, e.display_name AS engagement_name
            FROM agent a
            JOIN engagement e ON a.engagement_id = e.id
            ORDER BY e.display_name, a.display_name
            """
        )
        cols = ("id", "slug", "display_name", "backend", "backend_id",
                "engagement_slug", "engagement_name")
        return [dict(zip(cols, row)) for row in cur.fetchall()]


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
) -> IngestResponse:
    """Upload + ingest a Lyzr agent definition.

    The heuristic extractor always runs. When `synthesize=true` and the LLM
    extractor is available (OPENAI_API_KEY is set in the service env), LLM-
    proposed scenarios are appended. Every scenario carries provenance per
    PRD §6.10 — `derived_from` shows which extractor produced it and which
    quote of the agent definition it tests.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    try:
        agent_def = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Not valid JSON: {e}")
    if not isinstance(agent_def, dict):
        raise HTTPException(status_code=400, detail="Agent definition must be a JSON object.")

    source_sha256 = hashlib.sha256(raw).hexdigest()
    backend = "lyzr"  # this endpoint is Lyzr-specific today
    backend_id = str(agent_def.get("_id") or agent_def.get("id") or "")

    # 1. Run heuristic ingest. We pass a stub Path so the existing LyzrIngestor
    #    treats this as a normal ingest call; nothing is written to disk.
    from pathlib import Path as _P
    ingestor = LyzrIngestor()
    result = ingestor.ingest(_P(file.filename or "upload.json"), raw)

    warnings: list[str] = list(result.warnings)
    source_label = "lyzr-ingest"

    # 2. Optional LLM synthesis.
    proposed = []
    if synthesize:
        if not llm_extractor.is_available():
            warnings.append("--synthesize requested but no OPENAI_API_KEY set; skipped LLM extraction.")
        else:
            try:
                proposed = llm_extractor.extract(agent_def)
                source_label = "lyzr-ingest+llm"
            except Exception as e:
                warnings.append(f"LLM extraction failed: {type(e).__name__}: {e}")

    # 3. Persist to Postgres in one transaction.
    inserted: list[IngestedScenario] = []
    with db.connect() as conn, conn.cursor() as cur:
        engagement_id = db.upsert_engagement(cur, engagement_slug, engagement_name)
        agent_id = db.upsert_agent(
            cur, engagement_id, agent_slug, agent_name, backend, backend_id,
        )
        scenario_set_id = db.insert_scenario_set(
            cur,
            agent_id=agent_id,
            name=scenario_set_name,
            source=source_label,
            source_sha256=source_sha256,
            source_filename=file.filename,
            created_by=triggered_by,
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

    return IngestResponse(
        scenario_set_id=scenario_set_id,
        agent_id=agent_id,
        engagement_id=engagement_id,
        source_sha256=source_sha256,
        scenarios=inserted,
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


@app.patch(
    "/api/scenarios/{scenario_pk}",
    dependencies=[Depends(require_api_key)],
)
def update_scenario(
    scenario_pk: Annotated[int, PathParam(...)],
    new_status: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None,
    verified_by: Annotated[str | None, Form()] = None,
) -> dict:
    """Approve / reject / annotate a scenario.

    `new_status` must be one of 'unverified' | 'approved' | 'rejected' if set.
    """
    if new_status and new_status not in ("unverified", "approved", "rejected"):
        raise HTTPException(status_code=400, detail=f"invalid status: {new_status}")
    with db.connect() as conn, conn.cursor() as cur:
        sets, params = [], []
        if new_status is not None:
            sets.append("status = %s"); params.append(new_status)
            if new_status == "approved":
                sets.append("verified_at = now()")
                if verified_by:
                    sets.append("verified_by = %s"); params.append(verified_by)
        if notes is not None:
            sets.append("notes = %s"); params.append(notes)
        if not sets:
            raise HTTPException(status_code=400, detail="no fields to update")
        params.append(scenario_pk)
        cur.execute(f"UPDATE scenario SET {', '.join(sets)} WHERE id = %s RETURNING id, status", params)
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="scenario not found")
        conn.commit()
        return {"id": row[0], "status": row[1]}


# ----------------------------- runs -----------------------------


@app.post(
    "/api/runs",
    response_model=RunQueuedResponse,
    dependencies=[Depends(require_api_key)],
)
def queue_run(req: RunRequest, background_tasks: BackgroundTasks) -> RunQueuedResponse:
    """Queue an evaluation. Returns the job_id; poll GET /api/runs/{job_id}."""
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
        conn.commit()

    jobs.submit(background_tasks, job_id)
    return RunQueuedResponse(job_id=job_id, status="queued", total_scenarios=len(scenarios_for_count))


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

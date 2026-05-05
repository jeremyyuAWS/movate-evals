"""Request/response Pydantic models for the FastAPI surface.

Kept in a separate module from the route handlers so the dashboard frontend
can import / generate types from this file alone (FastAPI's OpenAPI export
covers everything declared here).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ----------------------------- ingest -----------------------------


class IngestRequest(BaseModel):
    """Request body for POST /api/agent-definitions.

    The agent definition itself is sent as the uploaded file (multipart).
    These fields come from form data that accompanies it.
    """
    engagement_slug: str = Field(..., description="e.g. 'sandisk-returns'; created on first use.")
    engagement_name: str | None = Field(None, description="Display name; defaults to slug.")
    agent_slug: str = Field(..., description="e.g. 'returns-manager'; unique within engagement.")
    agent_name: str | None = Field(None, description="Display name; defaults to slug.")
    scenario_set_name: str = Field(..., description="Name for this bundle, e.g. 'v3-baseline'.")
    synthesize: bool = Field(False, description="Augment heuristic scenarios with LLM proposals.")
    triggered_by: str | None = Field(None, description="Who uploaded — captured for audit.")


class IngestedScenario(BaseModel):
    id: int
    scenario_id: str
    severity: str | None
    tags: list[str] = Field(default_factory=list)
    derived_from: dict[str, Any] | None = None


class IngestResponse(BaseModel):
    scenario_set_id: int
    agent_id: int
    engagement_id: int
    source_sha256: str
    scenarios: list[IngestedScenario]
    warnings: list[str] = Field(default_factory=list)


# ----------------------------- runs -----------------------------


class RunRequest(BaseModel):
    """POST /api/runs body."""
    agent_id: int
    scenario_set_id: int
    judges_enabled: bool = True
    runs_per_scenario: int = Field(1, ge=1, le=10)
    triggered_by: str | None = None
    only_approved: bool = Field(
        True,
        description=(
            "If True (default), evaluate only scenarios with status='approved'. "
            "Set False to include unverified scenarios as well."
        ),
    )


class RunQueuedResponse(BaseModel):
    job_id: str
    status: str
    total_scenarios: int


class RunStatusResponse(BaseModel):
    job_id: str
    status: str
    judges_enabled: bool
    runs_per_scenario: int
    triggered_by: str | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    total_scenarios: int | None
    completed_scenarios: int
    error_message: str | None
    result_run_id: str | None  # the run_id (e.g. "run_2026-..."), only set when status='done'
    overall_score: float | None
    result_status: str | None  # production_ready / pilot_ready / etc.


# ----------------------------- error envelope -----------------------------


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None

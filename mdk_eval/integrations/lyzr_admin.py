"""Lyzr platform management — agent create / delete.

Used by sandbox-mode upload to provision an ephemeral Lyzr agent from a
JSON definition, and by the sandbox-aware DELETE flow to tear it down.

This is a thin async client over Lyzr's REST API. Auth is `x-api-key`
header (same as the inference adapter). Errors are mapped to a single
`LyzrAdminError` so the caller can decide whether to surface as 4xx/5xx.

The endpoints used here are:
  - POST  /v3/agents/template/single-task   — create one agent from JSON
  - DELETE /v3/agents/{agent_id}            — tear down

Phase 1 limitations (intentional):
  - Single-agent only. A manager with `managed_agents` requires depth-first
    provisioning + reverse-order teardown — Phase 2.
  - No retry/backoff on 5xx. Surface and let the caller decide.
  - No internal token-level rate limiting; relies on Lyzr's own quotas.

References:
  - https://docs.lyzr.ai/lyzr-adk/agents/creating-agents
  - https://docs.lyzr.ai/lyzr-adk/agents/managing-agents
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx


log = logging.getLogger(__name__)


# Default base URL for Lyzr's agent management API. Production. Can be
# overridden via env var for staging environments.
DEFAULT_LYZR_API_BASE = os.getenv(
    "LYZR_API_BASE",
    "https://agent-prod.studio.lyzr.ai",
)

# Conservative timeout for management ops. Provisioning a single agent
# typically completes in < 5s; we give 30 to absorb tail latency.
DEFAULT_TIMEOUT_S = 30.0


class LyzrAdminError(RuntimeError):
    """Raised when a Lyzr management call returns 4xx/5xx or fails to
    reach the service. The message is suitable for direct user display
    in an HTTPException(detail=...) — it includes the failing endpoint
    and the response body's detail field where present.
    """

    def __init__(self, message: str, *, status_code: int | None = None,
                 endpoint: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint


# ----------------------------- internal helpers -----------------------------


def _api_key(api_key: str | None) -> str:
    """Resolve the Lyzr API key. Explicit param wins; fall back to the
    `LYZR_API_KEY` environment variable (same name the inference adapter
    uses, so we automatically pick up the Container App secret)."""
    key = api_key or os.getenv("LYZR_API_KEY", "")
    if not key:
        raise LyzrAdminError(
            "No Lyzr API key configured. Set the LYZR_API_KEY env var or pass "
            "api_key= explicitly. For Movate Container App deployments the "
            "secret name is `lyzr-api-key`."
        )
    return key


def _shape_create_payload(agent_def: dict[str, Any]) -> dict[str, Any]:
    """Build the request body for POST /v3/agents/template/single-task.

    The user's uploaded JSON has many fields that aren't accepted by the
    create endpoint (timestamps, internal Lyzr metadata, the `_id` itself,
    api_key — already redacted by sanitize). We pass through the fields
    Lyzr's create endpoint documents as accepted.
    """
    PASSTHROUGH = (
        "name", "description",
        "agent_role", "agent_instructions", "agent_goal", "agent_context",
        "examples", "tools", "tool_usage_description", "tool_configs",
        "provider_id", "model", "temperature", "top_p",
        "llm_credential_id",
        "features", "managed_agents", "a2a_tools",
        "additional_model_params", "response_format",
        "store_messages", "disable_artifacts", "file_output",
        "image_output_config", "max_iterations",
        "skills_catalog", "mcp_resources", "mcp_prompts",
    )
    payload = {k: agent_def[k] for k in PASSTHROUGH if k in agent_def}

    # Sandbox-mode UX — prefix the name so the user can see in Lyzr Studio
    # which agents are ephemeral mdk-eval provisions vs their real ones.
    if "name" in payload and not str(payload["name"]).startswith("[mdk-sandbox]"):
        payload["name"] = f"[mdk-sandbox] {payload['name']}"

    return payload


# ----------------------------- public API -----------------------------


async def create_agent(
    agent_def: dict[str, Any],
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Provision a single-task agent on Lyzr from a JSON definition.

    Returns the new agent's Lyzr `_id` string. Raises `LyzrAdminError` on
    any non-2xx response or transport failure.

    Phase 1 only handles single-task agents. If `agent_def` has a non-empty
    `managed_agents` list, raise — caller (the upload endpoint) checks for
    this before calling and returns 400 to the user.
    """
    if agent_def.get("managed_agents"):
        raise LyzrAdminError(
            "Sandbox provisioning of manager agents (with managed_agents) is "
            "not yet supported. Phase 1 of sandbox mode covers single-task "
            "agents only. Track Phase 2 for multi-agent provisioning."
        )

    key = _api_key(api_key)
    base = (base_url or DEFAULT_LYZR_API_BASE).rstrip("/")
    endpoint = f"{base}/v3/agents/template/single-task"
    payload = _shape_create_payload(agent_def)
    headers = {"Content-Type": "application/json", "x-api-key": key}

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            resp = await client.post(endpoint, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise LyzrAdminError(
                f"Failed to reach Lyzr API at {endpoint}: {type(e).__name__}: {e}",
                endpoint=endpoint,
            ) from e

    if resp.status_code >= 400:
        # Try to surface Lyzr's own error message; fall back to the body.
        body_excerpt = resp.text[:500] if resp.text else "(empty body)"
        raise LyzrAdminError(
            f"Lyzr returned HTTP {resp.status_code} from {endpoint}: {body_excerpt}",
            status_code=resp.status_code,
            endpoint=endpoint,
        )

    try:
        body = resp.json()
    except ValueError as e:
        raise LyzrAdminError(
            f"Lyzr returned non-JSON 200 from {endpoint}: {resp.text[:200]}",
            status_code=resp.status_code,
            endpoint=endpoint,
        ) from e

    # Lyzr's create response shape varies slightly across endpoint versions.
    # Try the common keys in priority order. If none resolve, raise — we
    # can't proceed without an ID to track.
    new_id = (
        body.get("agent_id")
        or body.get("_id")
        or body.get("id")
        or (body.get("data") or {}).get("agent_id")
        or (body.get("data") or {}).get("_id")
    )
    if not new_id:
        raise LyzrAdminError(
            f"Lyzr accepted the create request but didn't return an agent ID. "
            f"Response keys: {list(body.keys())}. Body excerpt: {str(body)[:300]}",
            status_code=resp.status_code,
            endpoint=endpoint,
        )
    log.info("lyzr_admin.created agent_id=%s", new_id)
    return str(new_id)


async def delete_agent(
    lyzr_agent_id: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> None:
    """Tear down a Lyzr agent. Idempotent: 404 (already gone) is treated
    as success, since the goal is "no longer exists" not "I deleted it now."

    Raises `LyzrAdminError` on any other non-2xx, non-404 response.
    """
    if not lyzr_agent_id:
        raise LyzrAdminError("delete_agent requires a non-empty lyzr_agent_id")

    key = _api_key(api_key)
    base = (base_url or DEFAULT_LYZR_API_BASE).rstrip("/")
    endpoint = f"{base}/v3/agents/{lyzr_agent_id}"
    headers = {"x-api-key": key}

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            resp = await client.delete(endpoint, headers=headers)
        except httpx.HTTPError as e:
            raise LyzrAdminError(
                f"Failed to reach Lyzr API at {endpoint}: {type(e).__name__}: {e}",
                endpoint=endpoint,
            ) from e

    if resp.status_code == 404:
        log.info("lyzr_admin.delete agent already gone id=%s", lyzr_agent_id)
        return
    if resp.status_code >= 400:
        body_excerpt = resp.text[:500] if resp.text else "(empty body)"
        raise LyzrAdminError(
            f"Lyzr returned HTTP {resp.status_code} from DELETE {endpoint}: {body_excerpt}",
            status_code=resp.status_code,
            endpoint=endpoint,
        )
    log.info("lyzr_admin.deleted agent_id=%s", lyzr_agent_id)

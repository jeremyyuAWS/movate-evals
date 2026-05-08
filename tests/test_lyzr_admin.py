"""Unit tests for the Lyzr management client (`mdk_eval.integrations.lyzr_admin`).

Mocks httpx so no network calls hit the real Lyzr API. Covers:
  - create_agent: success, error mapping, payload shaping
  - delete_agent: success, idempotent on 404, error mapping
  - api_key resolution via env var fallback
  - manager rejection (Phase 1 limitation)
"""
from __future__ import annotations

from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from mdk_eval.integrations import lyzr_admin
from mdk_eval.integrations.lyzr_admin import LyzrAdminError


# -------------------------- _api_key --------------------------


def test_api_key_explicit_param_wins(monkeypatch):
    monkeypatch.setenv("LYZR_API_KEY", "from-env")
    assert lyzr_admin._api_key("from-arg") == "from-arg"


def test_api_key_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("LYZR_API_KEY", "from-env")
    assert lyzr_admin._api_key(None) == "from-env"


def test_api_key_raises_when_missing(monkeypatch):
    monkeypatch.delenv("LYZR_API_KEY", raising=False)
    with pytest.raises(LyzrAdminError, match="No Lyzr API key"):
        lyzr_admin._api_key(None)


# -------------------------- _shape_create_payload --------------------------


def test_shape_payload_only_passthrough_fields():
    """Internal Lyzr metadata, timestamps, and the agent's own _id are
    stripped — Lyzr's create endpoint rejects unexpected fields."""
    full = {
        "_id": "abc",
        "name": "Test",
        "agent_role": "Tester",
        "created_at": "2026-05-07T00:00:00",
        "updated_at": "2026-05-07T01:00:00",
        "version": "3",
        "api_key": "[REDACTED]",
        "managed_agents": [],
    }
    payload = lyzr_admin._shape_create_payload(full)
    assert "_id" not in payload
    assert "created_at" not in payload
    assert "updated_at" not in payload
    assert "version" not in payload
    assert "api_key" not in payload  # already redacted, but defense-in-depth
    assert payload["agent_role"] == "Tester"


def test_shape_payload_prefixes_name():
    """Sandbox prefix on the Lyzr-side name so the user can spot mdk-eval
    agents in their Lyzr Studio."""
    p = lyzr_admin._shape_create_payload({"name": "FAQ Bot", "agent_role": "X"})
    assert p["name"].startswith("[mdk-sandbox]")
    assert "FAQ Bot" in p["name"]


def test_shape_payload_idempotent_prefix():
    """Re-shaping an already-prefixed payload doesn't double up."""
    p = lyzr_admin._shape_create_payload({"name": "[mdk-sandbox] FAQ Bot", "agent_role": "X"})
    assert p["name"].count("[mdk-sandbox]") == 1


# -------------------------- create_agent --------------------------


@pytest.mark.asyncio
async def test_create_agent_returns_id_on_success(monkeypatch):
    monkeypatch.setenv("LYZR_API_KEY", "test-key")
    fake_client = MagicMock()
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"agent_id": "new-lyzr-id-123"}
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch("mdk_eval.integrations.lyzr_admin.httpx.AsyncClient",
               return_value=fake_client):
        result = await lyzr_admin.create_agent({"name": "X", "agent_role": "X"})
    assert result == "new-lyzr-id-123"


@pytest.mark.asyncio
async def test_create_agent_extracts_id_from_nested_data(monkeypatch):
    """Lyzr's response shape varies; we try several keys including data.agent_id."""
    monkeypatch.setenv("LYZR_API_KEY", "test-key")
    fake_client = MagicMock()
    fake_resp = MagicMock(status_code=201)
    fake_resp.json.return_value = {"data": {"_id": "nested-id-456"}}
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch("mdk_eval.integrations.lyzr_admin.httpx.AsyncClient",
               return_value=fake_client):
        result = await lyzr_admin.create_agent({"name": "X", "agent_role": "X"})
    assert result == "nested-id-456"


@pytest.mark.asyncio
async def test_create_agent_raises_on_4xx(monkeypatch):
    monkeypatch.setenv("LYZR_API_KEY", "test-key")
    fake_client = MagicMock()
    fake_resp = MagicMock(status_code=400, text='{"error": "missing field"}')
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch("mdk_eval.integrations.lyzr_admin.httpx.AsyncClient",
               return_value=fake_client):
        with pytest.raises(LyzrAdminError) as exc_info:
            await lyzr_admin.create_agent({"name": "X", "agent_role": "X"})
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_create_agent_rejects_managers(monkeypatch):
    """Phase 1 limitation: managers with managed_agents not yet supported."""
    monkeypatch.setenv("LYZR_API_KEY", "test-key")
    with pytest.raises(LyzrAdminError, match="manager agents"):
        await lyzr_admin.create_agent({
            "name": "Mgr", "managed_agents": [{"id": "x", "name": "y"}]
        })


@pytest.mark.asyncio
async def test_create_agent_raises_when_response_has_no_id(monkeypatch):
    monkeypatch.setenv("LYZR_API_KEY", "test-key")
    fake_client = MagicMock()
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"status": "ok"}  # no id field
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch("mdk_eval.integrations.lyzr_admin.httpx.AsyncClient",
               return_value=fake_client):
        with pytest.raises(LyzrAdminError, match="didn't return an agent ID"):
            await lyzr_admin.create_agent({"name": "X", "agent_role": "X"})


# -------------------------- delete_agent --------------------------


@pytest.mark.asyncio
async def test_delete_agent_success(monkeypatch):
    monkeypatch.setenv("LYZR_API_KEY", "test-key")
    fake_client = MagicMock()
    fake_resp = MagicMock(status_code=200)
    fake_client.delete = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch("mdk_eval.integrations.lyzr_admin.httpx.AsyncClient",
               return_value=fake_client):
        # Should not raise
        await lyzr_admin.delete_agent("some-lyzr-id")


@pytest.mark.asyncio
async def test_delete_agent_idempotent_on_404(monkeypatch):
    """404 = "already gone"; treat as success since the goal is "no longer
    exists" not "I deleted it now"."""
    monkeypatch.setenv("LYZR_API_KEY", "test-key")
    fake_client = MagicMock()
    fake_resp = MagicMock(status_code=404, text="not found")
    fake_client.delete = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch("mdk_eval.integrations.lyzr_admin.httpx.AsyncClient",
               return_value=fake_client):
        await lyzr_admin.delete_agent("some-lyzr-id")  # no raise


@pytest.mark.asyncio
async def test_delete_agent_raises_on_5xx(monkeypatch):
    monkeypatch.setenv("LYZR_API_KEY", "test-key")
    fake_client = MagicMock()
    fake_resp = MagicMock(status_code=500, text="server error")
    fake_client.delete = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch("mdk_eval.integrations.lyzr_admin.httpx.AsyncClient",
               return_value=fake_client):
        with pytest.raises(LyzrAdminError) as exc:
            await lyzr_admin.delete_agent("some-id")
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_delete_agent_rejects_empty_id(monkeypatch):
    monkeypatch.setenv("LYZR_API_KEY", "test-key")
    with pytest.raises(LyzrAdminError, match="non-empty"):
        await lyzr_admin.delete_agent("")

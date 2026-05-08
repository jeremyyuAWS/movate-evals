"""Unit tests for `mdk_eval.web.jobs._build_adapter_config`.

Specifically exercises the diagnostic error path when an agent record is
missing its backend_id (the 'Lyzr adapter requires agent_id' failure mode).
"""
from __future__ import annotations

import pytest

from mdk_eval.web.jobs import _build_adapter_config


def test_lyzr_with_backend_id_produces_valid_config():
    cfg = _build_adapter_config({
        "backend": "lyzr",
        "backend_id": "69ef90f1d56224d9f94c61cc",
        "slug": "movate-faq",
    })
    assert cfg.target == "lyzr"
    assert cfg.agent_id == "69ef90f1d56224d9f94c61cc"


def test_lyzr_without_backend_id_raises_diagnostic_error():
    """Empty backend_id should fail at config-build time with an actionable
    error, not surface as a generic 'Lyzr adapter requires agent_id' deeper
    in the run pipeline."""
    with pytest.raises(ValueError, match=r"backend_id is empty"):
        _build_adapter_config({
            "backend": "lyzr",
            "backend_id": "",
            "slug": "broken-agent",
        })


def test_lyzr_without_backend_id_error_mentions_agent_slug():
    """Error should name the offending agent so the user knows which one
    to repair."""
    with pytest.raises(ValueError, match=r"broken-agent"):
        _build_adapter_config({
            "backend": "lyzr",
            "backend_id": None,
            "slug": "broken-agent",
        })


def test_lyzr_without_backend_id_error_suggests_recovery_path():
    """Error should point the user at the PATCH endpoint to fix the record
    without re-uploading the agent."""
    with pytest.raises(ValueError, match=r"PATCH"):
        _build_adapter_config({
            "backend": "lyzr",
            "backend_id": "",
            "slug": "broken",
        })


def test_mock_backend_works_without_backend_id():
    """Mock backend doesn't need an agent_id — config builds fine."""
    cfg = _build_adapter_config({
        "backend": "mock",
        "backend_id": "",
        "slug": "any-slug",
    })
    assert cfg.target == "mock"


def test_unknown_backend_falls_back_to_mock():
    """Defensive fallback: an unrecognized backend doesn't crash the worker;
    it gets a mock adapter so the user can see something happened."""
    cfg = _build_adapter_config({
        "backend": "some_future_platform",
        "backend_id": "",
        "slug": "agent",
    })
    assert cfg.target == "mock"


def test_openai_compat_uses_backend_id_as_endpoint():
    cfg = _build_adapter_config({
        "backend": "openai_compat",
        "backend_id": "https://my.endpoint.example/v1",
        "slug": "custom",
    })
    assert cfg.target == "openai_compat"
    assert cfg.endpoint == "https://my.endpoint.example/v1"

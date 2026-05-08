"""Observability foundation — JSON formatter, trace ctx, request middleware.

Application Insights and Langfuse integrations are env-gated and tested
indirectly (the no-op shim path is exercised; the live path lights up only
when the real env vars + packages are present, which we verify in the
post-deploy smoke tests).
"""
from __future__ import annotations

import importlib
import io
import json
import logging

import pytest


@pytest.fixture(autouse=True)
def _reset_log_handlers():
    """Each test starts with a clean root logger so tests don't leak handlers."""
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def _capture_log(observability, msg: str, **extra) -> dict:
    """Capture one log line through the JSON formatter, return the parsed dict."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(observability.JsonFormatter())
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("mdk_eval.test").info(msg, extra=extra)
    raw = buf.getvalue().strip().splitlines()[-1]
    return json.loads(raw)


# ---------------------------------------------------------------- json formatter


def test_json_formatter_emits_required_fields():
    obs = importlib.import_module("mdk_eval.web.observability")
    parsed = _capture_log(obs, "hello")
    for k in ("timestamp", "level", "logger", "message", "trace_id", "request_id"):
        assert k in parsed
    assert parsed["message"] == "hello"
    assert parsed["level"] == "INFO"


def test_json_formatter_propagates_extra_fields():
    obs = importlib.import_module("mdk_eval.web.observability")
    parsed = _capture_log(obs, "x", custom_field=42, name_str="ok")
    assert parsed["custom_field"] == 42
    assert parsed["name_str"] == "ok"


def test_json_formatter_does_not_let_extra_overwrite_reserved():
    obs = importlib.import_module("mdk_eval.web.observability")
    # Try to overwrite trace_id via extra=. The formatter must protect it.
    parsed = _capture_log(obs, "x", trace_id="malicious")
    assert parsed["trace_id"] != "malicious"


def test_json_formatter_handles_unserializable_extra():
    """A repr fallback must apply if a caller passes a non-JSON-able value
    via extra={...} — formatter must not throw."""
    obs = importlib.import_module("mdk_eval.web.observability")

    class NotJSONable:
        def __repr__(self):
            return "<weird>"

    parsed = _capture_log(obs, "x", thing=NotJSONable())
    assert parsed["thing"] == "<weird>"


def test_json_formatter_captures_exception():
    obs = importlib.import_module("mdk_eval.web.observability")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(obs.JsonFormatter())
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    log = logging.getLogger("mdk_eval.test")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("oops")

    raw = buf.getvalue().strip().splitlines()[-1]
    parsed = json.loads(raw)
    assert parsed["exc_type"] == "ValueError"
    assert parsed["exc_message"] == "boom"
    assert "Traceback" in parsed["exc_traceback"]


# ---------------------------------------------------------------- trace context


def test_trace_context_propagates_to_log_lines():
    obs = importlib.import_module("mdk_eval.web.observability")

    with obs.trace_context(trace_id="my-trace", request_id="my-req"):
        parsed = _capture_log(obs, "in scope")
        assert parsed["trace_id"] == "my-trace"
        assert parsed["request_id"] == "my-req"

    # Outside the with-block, the ids reset to empty
    parsed = _capture_log(obs, "out of scope")
    assert parsed["trace_id"] == ""
    assert parsed["request_id"] == ""


def test_trace_context_generates_id_when_omitted():
    obs = importlib.import_module("mdk_eval.web.observability")
    with obs.trace_context() as (tid, rid):
        assert tid and rid
        # 26-char hex per the implementation
        assert len(tid) == 26
        assert len(rid) == 26
        assert tid != rid


# ---------------------------------------------------------------- emit_event


def test_emit_event_uses_event_name_as_message():
    obs = importlib.import_module("mdk_eval.web.observability")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(obs.JsonFormatter())
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    obs.emit_event("eval.queued", job_id="j1", scenarios=10)

    raw = buf.getvalue().strip().splitlines()[-1]
    parsed = json.loads(raw)
    assert parsed["message"] == "eval.queued"
    assert parsed["event_name"] == "eval.queued"
    assert parsed["job_id"] == "j1"
    assert parsed["scenarios"] == 10


# ---------------------------------------------------------------- request middleware


def test_request_middleware_attaches_correlation_headers(monkeypatch):
    monkeypatch.setenv("MDK_WEB_API_KEY", "test-key")
    monkeypatch.setenv("MDK_WEB_CORS_ORIGINS", "http://localhost:3000")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    import mdk_eval.web.server as srv
    importlib.reload(srv)

    from fastapi.testclient import TestClient
    client = TestClient(srv.app)

    r = client.get("/healthz")
    assert r.status_code == 200
    assert "X-Trace-Id" in r.headers
    assert "X-Request-Id" in r.headers
    # Both should be 26-hex
    assert len(r.headers["X-Trace-Id"]) == 26
    assert len(r.headers["X-Request-Id"]) == 26


def test_request_middleware_echoes_client_supplied_trace_id(monkeypatch):
    monkeypatch.setenv("MDK_WEB_API_KEY", "test-key")
    monkeypatch.setenv("MDK_WEB_CORS_ORIGINS", "http://localhost:3000")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    import mdk_eval.web.server as srv
    importlib.reload(srv)

    from fastapi.testclient import TestClient
    client = TestClient(srv.app)

    r = client.get("/healthz", headers={"X-Trace-Id": "client-trace-abc"})
    assert r.status_code == 200
    assert r.headers["X-Trace-Id"] == "client-trace-abc"


# ---------------------------------------------------------------- langfuse no-op shim


def test_langfuse_client_returns_noop_when_unconfigured(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    obs = importlib.reload(importlib.import_module("mdk_eval.web.observability"))
    # Reset module-level cached client
    obs._lf_client = None

    client = obs.langfuse_client()
    # Methods must not throw
    span = client.span(name="test")
    span.update(input="x")
    span.end()
    with client.generation(name="test") as g:
        g.update(output="y")
    client.flush()


# ---------------------------------------------------------------- /version, /readyz


def test_version_endpoint_includes_observability_status(monkeypatch):
    monkeypatch.setenv("MDK_WEB_API_KEY", "test-key")
    monkeypatch.setenv("MDK_WEB_CORS_ORIGINS", "http://localhost:3000")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    import mdk_eval.web.server as srv
    importlib.reload(srv)

    from fastapi.testclient import TestClient
    client = TestClient(srv.app)

    r = client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body
    assert "git_sha" in body
    assert "image_tag" in body
    assert "observability" in body
    # When env vars are absent (test env), all should be False
    assert body["observability"]["app_insights"] is False
    assert body["observability"]["langfuse"] is False
    assert body["observability"]["structured_logging"] is True

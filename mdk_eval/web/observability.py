"""Observability foundation for the production web service.

What this module owns:
  - Process-wide structured (JSON) log formatter — every line a parseable
    record with trace_id, request_id, and arbitrary contextual fields.
  - `trace_id` / `request_id` context vars propagated through async tasks so
    log lines from a single API request are correlatable end-to-end.
  - Request middleware that captures method/path/status/latency/trace_id and
    sets them as response headers (Bolt can echo them in error reports).
  - `emit_event` helper for one-off structured events (cost recorded, queue
    enqueued, eval finished). These show up in Log Analytics / App Insights
    as queryable events.
  - Optional Application Insights integration via the
    `azure-monitor-opentelemetry` package — gated by the
    APPLICATIONINSIGHTS_CONNECTION_STRING env var (no-op if missing).
  - Optional Langfuse client wired up here so judge calls and adapter calls
    can use a single shared client (gated by LANGFUSE_PUBLIC_KEY /
    LANGFUSE_SECRET_KEY).

Design notes:
  - All integration is *optional and gated by env*. Missing dependencies and
    missing env vars degrade gracefully to no-op. This keeps tests and local
    dev cheap; production lights up when secrets are set.
  - We log JSON to stdout. Azure Container Apps already pipes container
    stdout into Log Analytics; no extra agent or sidecar required.
  - We emit `request_id` (per HTTP request, generated server-side) and
    `trace_id` (correlation id; if the client sends `X-Request-ID`,
    we use that; otherwise we mint a new ULID-shaped value). Both come
    back in the response headers so Bolt can show them in error UIs and
    paste them into bug reports.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


# ----------------------------- context vars -----------------------------

# Per-request correlation id. Survives across async hops (asyncio task spawn).
# Read these from anywhere in the codebase via current_trace_id() etc.
_trace_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("mdk_trace_id", default="")
_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("mdk_request_id", default="")


def current_trace_id() -> str:
    return _trace_id_ctx.get()


def current_request_id() -> str:
    return _request_id_ctx.get()


@contextmanager
def trace_context(trace_id: str | None = None, request_id: str | None = None):
    """Bind trace/request ids for the duration of a block.

    Used by the request middleware. Workers / background tasks should also
    bind this so their log lines correlate with the API request that
    triggered them.
    """
    tid = trace_id or _gen_id()
    rid = request_id or _gen_id()
    t_token = _trace_id_ctx.set(tid)
    r_token = _request_id_ctx.set(rid)
    try:
        yield (tid, rid)
    finally:
        _trace_id_ctx.reset(t_token)
        _request_id_ctx.reset(r_token)


def _gen_id() -> str:
    """26-char hex id without dashes — short enough for headers + log fields,
    long enough to be globally unique across the eval workload."""
    return uuid.uuid4().hex[:26]


# ----------------------------- structured JSON logging -----------------------------


class JsonFormatter(logging.Formatter):
    """Serialize a LogRecord to a single-line JSON object.

    Stable shape — Log Analytics / Datadog / etc. parse this:
      {timestamp, level, logger, message, trace_id, request_id, ...extra}

    Any keyword args passed via `logger.info("msg", extra={...})` show up as
    top-level keys. Reserved names (timestamp, level, logger, message,
    trace_id, request_id) are not overwritten.
    """

    RESERVED = {"timestamp", "level", "logger", "message", "trace_id", "request_id"}

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": current_trace_id(),
            "request_id": current_request_id(),
        }
        # Attach exception info if present
        if record.exc_info:
            out["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            out["exc_message"] = str(record.exc_info[1]) if record.exc_info[1] else None
            out["exc_traceback"] = self.formatException(record.exc_info)
        # Attach any caller-supplied `extra={}` fields
        for key, value in record.__dict__.items():
            if key in {
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "message", "module",
                "msecs", "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName",
                "taskName",
            }:
                continue
            if key in self.RESERVED:
                continue
            try:
                json.dumps(value)
                out[key] = value
            except (TypeError, ValueError):
                out[key] = repr(value)
        return json.dumps(out, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", *, force: bool = False) -> None:
    """Install the JSON formatter on the root logger.

    Idempotent — safe to call multiple times. `force=True` removes any
    existing handlers first (useful when uvicorn has already configured
    its own).
    """
    root = logging.getLogger()
    if force:
        for h in list(root.handlers):
            root.removeHandler(h)

    if not any(isinstance(h, _MdkJsonHandler) for h in root.handlers):
        handler = _MdkJsonHandler(stream=sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Quiet a few noisy loggers we don't need at INFO
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class _MdkJsonHandler(logging.StreamHandler):
    """Marker subclass — lets configure_logging detect prior installation."""


# ----------------------------- event emission -----------------------------


def emit_event(name: str, **fields: Any) -> None:
    """Log a structured event. Convention: `name` is dotted (e.g.
    'eval.queued', 'cost.recorded') so events are easily filterable
    in Log Analytics with `customDimensions.event_name == "..."`.
    """
    logger = logging.getLogger("mdk_eval.event")
    extra = {"event_name": name, **fields}
    logger.info(name, extra=extra)


# ----------------------------- request middleware -----------------------------


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind trace/request ids per HTTP request, log a request_completed event,
    and surface ids back in response headers.

    Header contract (Bolt should rely on these):
      X-Trace-Id     — ours OR echoed from client (if client sent X-Trace-Id)
      X-Request-Id   — always server-generated, unique per request
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        client_trace = request.headers.get("X-Trace-Id") or ""
        with trace_context(trace_id=client_trace or None) as (tid, rid):
            start = time.perf_counter()
            status_code = 500  # default if something throws before we read it
            try:
                response = await call_next(request)
                status_code = response.status_code
            except Exception:
                # Re-raise after logging so FastAPI's exception handlers run.
                # The log line below still fires via the `finally`.
                logging.getLogger("mdk_eval.web").exception(
                    "request_failed",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                    },
                )
                raise
            finally:
                duration_ms = round((time.perf_counter() - start) * 1000, 2)
                emit_event(
                    "http.request_completed",
                    method=request.method,
                    path=request.url.path,
                    status=status_code,
                    duration_ms=duration_ms,
                )

            # Echo correlation ids back so the frontend can show them in error UIs.
            response.headers["X-Trace-Id"] = tid
            response.headers["X-Request-Id"] = rid
            return response


# ----------------------------- Application Insights -----------------------------


def configure_app_insights(app: FastAPI) -> bool:
    """If APPLICATIONINSIGHTS_CONNECTION_STRING is set, instrument the FastAPI
    app with Azure Monitor OpenTelemetry. Returns True if instrumentation was
    applied. Silent no-op otherwise.

    Auto-instruments:
      - HTTP server (FastAPI requests + responses)
      - HTTP client (httpx — used by Lyzr / OpenAI / Anthropic SDKs)
      - psycopg (Postgres queries)
      - Logging (Python logging records → Application Insights traces)
    """
    conn_str = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn_str:
        return False

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor  # type: ignore
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # type: ignore
    except ImportError:
        # Soft-fail: env var present but package missing → log and continue.
        logging.getLogger("mdk_eval.observability").warning(
            "APPLICATIONINSIGHTS_CONNECTION_STRING set but azure-monitor-opentelemetry "
            "is not installed. Run `pip install azure-monitor-opentelemetry` "
            "or rebuild the container image."
        )
        return False

    configure_azure_monitor(
        connection_string=conn_str,
        resource_attributes={"service.name": "mdk-eval-web", "service.namespace": "mdk-eval"},
    )
    # FastAPI auto-instrumentation is separate from `configure_azure_monitor`.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,version,metrics")

    logging.getLogger("mdk_eval.observability").info(
        "app_insights_enabled",
        extra={"event_name": "observability.app_insights_enabled"},
    )
    return True


# ----------------------------- Langfuse client -----------------------------


_lf_client = None


def langfuse_client():
    """Process-wide Langfuse client.

    Returns a real client when LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are set
    AND the `langfuse` package is installed. Otherwise returns a no-op shim
    whose methods all return inert objects — so callers can write
    ``with langfuse_client().span(...)`` without env-checking everywhere.
    """
    global _lf_client
    if _lf_client is not None:
        return _lf_client

    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        _lf_client = _NoopLangfuse()
        return _lf_client

    try:
        from langfuse import Langfuse  # type: ignore
        _lf_client = Langfuse(
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
            host=os.getenv("LANGFUSE_HOST"),  # Langfuse Cloud uses the default
        )
        logging.getLogger("mdk_eval.observability").info(
            "langfuse_enabled",
            extra={"event_name": "observability.langfuse_enabled"},
        )
        return _lf_client
    except Exception as e:
        logging.getLogger("mdk_eval.observability").warning(
            f"langfuse_init_failed: {type(e).__name__}: {e}"
        )
        _lf_client = _NoopLangfuse()
        return _lf_client


class _NoopLangfuse:
    """Silently swallow all calls. Lets call sites use the real Langfuse API
    without env-gating each one."""

    def trace(self, *_a, **_k):
        return _NoopSpan()

    def span(self, *_a, **_k):
        return _NoopSpan()

    def generation(self, *_a, **_k):
        return _NoopSpan()

    def flush(self):
        pass


class _NoopSpan:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def update(self, *_a, **_k):
        pass

    def end(self, *_a, **_k):
        pass

    def span(self, *_a, **_k):
        return _NoopSpan()

    def generation(self, *_a, **_k):
        return _NoopSpan()


# ----------------------------- one-shot startup helper -----------------------------


def install(app: FastAPI, *, log_level: str = "INFO") -> dict[str, bool]:
    """Wire up everything in one call. Use from the FastAPI app factory.

    Returns a dict describing what was enabled — useful for the /version
    endpoint to surface "observability: {app_insights: true, langfuse: false}".
    """
    configure_logging(level=log_level, force=True)
    app.add_middleware(RequestContextMiddleware)
    enabled = {
        "app_insights": configure_app_insights(app),
        "langfuse": not isinstance(langfuse_client(), _NoopLangfuse),
        "structured_logging": True,
    }
    emit_event("observability.installed", **enabled)
    return enabled


__all__ = [
    "configure_logging",
    "configure_app_insights",
    "current_request_id",
    "current_trace_id",
    "emit_event",
    "install",
    "JsonFormatter",
    "langfuse_client",
    "RequestContextMiddleware",
    "trace_context",
]

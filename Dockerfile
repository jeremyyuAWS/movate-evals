# Multi-stage build for the Movate Agent Assurance web service.
#
# Stage 1 (builder): install deps into a virtualenv with uv (faster than pip).
# Stage 2 (runtime): copy the venv + code into a slim Python image. Smaller
# final image, no build tools in the runtime layer.
#
# The container only runs the web service. CLI evaluation runs that the web
# service kicks off happen in-process via the same Python — same Dockerfile
# is sufficient for both.

# ---------- Stage 1: build ----------
FROM python:3.11-slim AS builder

# uv is dramatically faster than pip for resolves; install once, reuse layers.
RUN pip install --no-cache-dir uv==0.5.5

WORKDIR /build

# Copy only the bits needed to resolve dependencies. Keeps this layer cached
# across code-only changes.
# - pyproject.toml + README.md: build metadata
# - mdk_eval/: the package source
# - migrations/: referenced by pyproject's force-include rule (the running
#   container reads these SQL files via /api/agent-definitions → ensure_schema)
COPY pyproject.toml README.md ./
COPY mdk_eval ./mdk_eval
COPY migrations ./migrations

# Install the package + the extras the web service needs:
# - judges:        OpenAI + Anthropic clients (for eval execution + LLM ingest)
# - viz:           Altair + vl-convert for chart rendering in reports
# - push:          psycopg (DB writes)
# - web:           FastAPI + uvicorn + python-multipart
# - observability: Application Insights (azure-monitor-opentelemetry) + Langfuse
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python -e ".[web,push,judges,viz,observability]"

# ---------- Stage 2: runtime ----------
FROM python:3.11-slim AS runtime

# Minimal runtime tools. Postgres SSL needs ca-certificates; tini is a tiny
# init that handles signals correctly so SIGTERM from Fly cleanly shuts down
# the FastAPI workers.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates tini && \
    rm -rf /var/lib/apt/lists/*

# Run as a non-root user — Fly machines run as root by default, but we don't
# need to.
RUN useradd --create-home --shell /bin/bash mdk
USER mdk
WORKDIR /home/mdk

# Pull the prebuilt venv + the package source from the builder stage.
COPY --from=builder --chown=mdk:mdk /opt/venv /opt/venv
COPY --from=builder --chown=mdk:mdk /build /home/mdk/app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Cache directory inside the container. Judge-cache lives here; on Azure
    # Container Apps it's ephemeral per-revision (Fly era used a volume) —
    # caching is best-effort. The auditable copy of judge responses lives in
    # Postgres regardless.
    MDK_EVAL_CACHE_DIR=/home/mdk/.mdk-eval

# Build-time provenance — surfaced via /version endpoint and observability.
# Pass via `--build-arg GIT_SHA=...` or set in CI; default to "unknown" so
# local builds don't fail.
ARG GIT_SHA=unknown
ARG IMAGE_TAG=unknown
ENV MDK_EVAL_GIT_SHA=$GIT_SHA \
    MDK_EVAL_IMAGE_TAG=$IMAGE_TAG

WORKDIR /home/mdk/app

EXPOSE 8080

# tini reaps zombies and forwards SIGTERM. Uvicorn binds to 0.0.0.0:8080
# (Fly's expected ingress port). Single worker — async handlers + the
# in-process BackgroundTasks model don't benefit from multiple workers,
# and one worker keeps the job state consistent.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "mdk_eval.web.server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

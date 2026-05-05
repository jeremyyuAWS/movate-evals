"""Web service: FastAPI surface for the dashboard.

Three endpoints:
- POST /api/agent-definitions   — upload + ingest a Lyzr agent JSON; persists scenarios
- POST /api/runs                — kick off an evaluation as a background job
- GET  /api/runs/{job_id}       — poll job status

Lives separately from the CLI so the dashboard can drive ingestion + eval
execution without engineers needing a Python venv. Reuses the existing
mdk_eval modules 1:1 — no duplicated logic.

Optional install:
    pip install -e '.[web,push]'
Run locally:
    uvicorn mdk_eval.web.server:app --reload
Deploy:
    docker build -t mdk-eval-web . && fly deploy
"""

# Deploying the Movate Agent Assurance web service to Azure Container Apps

This document covers shipping the FastAPI backend (the one that powers Bolt's dashboard) to Azure Container Apps. The infrastructure is already provisioned under resource group `mdk-eval-rg` (East US) — most of this doc is about **redeploying changes**, not standing up the platform from scratch.

> Migrated off Fly.io on 2026-05-06. Old `mdk-eval-web.fly.dev` URLs are dead.

---

## What's deployed

| Thing | Where |
|---|---|
| Backend container | `mdk-eval-web` Container App in `mdk-eval-rg` (eastus) |
| Public URL | `https://mdk-eval-web.whitefield-b83c207d.eastus.azurecontainerapps.io` |
| Container image registry | `mdkevalacr151f8c.azurecr.io` (Basic SKU, eastus) |
| Container Apps environment | `mdk-eval-env` (consumption-only workload profile) |
| Log Analytics workspace | `workspace-mdkevalrgxRrK` (auto-created with the env) |
| Postgres | Supabase (unchanged from the Fly era — `aws-1-us-east-2.pooler.supabase.com:5432`) |

All resources are tagged `project=movate-evals`, `owner=jeremy.yu@movate.com`, `env=prod`. Filter by tag in the Azure portal to find them grouped.

---

## Endpoints exposed

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | Liveness check |
| `GET /openapi.json` | none | OpenAPI schema (Bolt generates types from this) |
| `POST /api/agent-definitions` | Bearer | Upload + ingest a Lyzr agent JSON |
| `POST /api/agent-definitions/preview` | Bearer | Mix Designer cost preview without persisting |
| `POST /api/runs` | Bearer | Queue an evaluation (durable via pgmq) |
| `GET /api/runs/{job_id}` | Bearer | Poll job status |
| `GET /api/portfolio/at-a-glance` | Bearer | Portfolio overview (all agents in one round-trip) |
| `GET /api/scenario-sets/{id}` | Bearer | List scenarios in a set |
| `PATCH /api/scenarios/{id}` | Bearer | Approve / reject / annotate a scenario |
| Plus: scenario regenerate, propose-one, add-scenarios, from-jsonl, insights, leaderboard, etc. |

---

## Prerequisites

- `az` CLI installed (`brew install azure-cli`)
- Logged in: `az login` (you should see CSS Corp Global tenant, subscription `Azure subscription 1`)
- Docker NOT required — we use `az acr build` for cloud-side builds
- The four secrets already exist as Container App secrets (see "Secret management" below)

---

## Redeploying after a code change

This is the everyday flow.

### 1. Build a new image in Azure Container Registry

```bash
TAG=$(date +%Y%m%d-%H%M%S)
az acr build \
  --registry mdkevalacr151f8c \
  --resource-group mdk-eval-rg \
  --image mdk-eval-web:$TAG \
  --image mdk-eval-web:latest \
  .
```

Build runs in Azure (no local Docker needed), takes ~3 min cold, ~30 s warm.

### 2. Roll the Container App to the new image

```bash
az containerapp update \
  --name mdk-eval-web \
  --resource-group mdk-eval-rg \
  --image mdkevalacr151f8c.azurecr.io/mdk-eval-web:$TAG
```

A new revision starts in parallel; the old one drains. Zero-downtime by default.

### 3. Verify

```bash
URL="https://mdk-eval-web.whitefield-b83c207d.eastus.azurecontainerapps.io"
curl -s "$URL/healthz"
# expect: {"status":"ok","version":"0.1.0"}

KEY=$(az containerapp secret show -n mdk-eval-web -g mdk-eval-rg \
  --secret-name mdk-web-api-key --query value -o tsv)
curl -s -H "Authorization: Bearer $KEY" "$URL/api/agents" | jq length
# expect: integer count of agents in the DB
```

---

## Secret management

Secrets live as Container App secrets and are mounted as env vars. Names:

| Container App secret | Env var | Used by |
|---|---|---|
| `mdk-web-api-key` | `MDK_WEB_API_KEY` | Bearer-token auth on `/api/*` |
| `database-url` | `DATABASE_URL` | Supabase Postgres connection |
| `openai-api-key` | `OPENAI_API_KEY` | LLM extractor + judges (OpenAI side) |
| `anthropic-api-key` | `ANTHROPIC_API_KEY` | Multi-judge panel (Anthropic side) |
| `lyzr-api-key` | `LYZR_API_KEY` | Lyzr adapter when running real agents |
| `applicationinsights-connection-string` | `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights telemetry export. Resource: `mdk-eval-insights` (tied to the existing Log Analytics workspace). |
| (optional) `langfuse-public-key` | `LANGFUSE_PUBLIC_KEY` | LLM-call tracing in Langfuse. Off when unset; turn on to see judge/adapter spans. |
| (optional) `langfuse-secret-key` | `LANGFUSE_SECRET_KEY` | Same. |
| (optional) `langfuse-host` | `LANGFUSE_HOST` | Optional self-hosted host. Default: Langfuse Cloud. |

Plus the registry credential `mdkevalacr151f8cazurecrio-mdkevalacr151f8c` (auto-managed by Azure).

### Read a secret

```bash
az containerapp secret show \
  --name mdk-eval-web --resource-group mdk-eval-rg \
  --secret-name mdk-web-api-key --query value -o tsv
```

### Rotate a secret

```bash
az containerapp secret set \
  --name mdk-eval-web --resource-group mdk-eval-rg \
  --secrets anthropic-api-key="sk-ant-NEW-VALUE"
# A new revision starts automatically with the new value.
```

### Add a brand-new secret + env var

```bash
# 1. Add the secret
az containerapp secret set --name mdk-eval-web --resource-group mdk-eval-rg \
  --secrets new-thing="value"

# 2. Reference it in env vars (this replaces the env list, so include all)
az containerapp update --name mdk-eval-web --resource-group mdk-eval-rg \
  --set-env-vars NEW_THING=secretref:new-thing
```

### Plain (non-secret) env vars

Already set: `MDK_WEB_CORS_ORIGINS`, `MDK_WEB_CORS_ORIGIN_REGEX`. To add or update:

```bash
az containerapp update --name mdk-eval-web --resource-group mdk-eval-rg \
  --set-env-vars MDK_WEB_CORS_ORIGINS='https://bolt.host,https://your-vercel.app'
```

---

## Observability

**Application Insights** (`mdk-eval-insights` in `mdk-eval-rg`) auto-captures:

- HTTP server requests (FastAPI middleware) — latency, status, path
- HTTP client requests (httpx — Lyzr / OpenAI / Anthropic SDKs use this)
- Postgres queries (psycopg auto-instrumentation)
- Python exceptions (every uncaught error is a tracked exception)
- Custom events emitted by `mdk_eval.web.observability.emit_event`:
  `http.request_completed`, `judge.call_completed`, `judge.cache_hit`,
  `job.started`, `job.completed`, `job.failed`, etc.

### Useful KQL queries

Find every request in a single trace_id:
```kusto
union requests, customEvents, exceptions, dependencies
| where customDimensions.trace_id == "<paste from X-Trace-Id header>"
| order by timestamp asc
```

Judge throughput + cache hit ratio:
```kusto
customEvents
| where name in ("judge.call_completed", "judge.cache_hit")
| summarize calls=count() by name, bin(timestamp, 1h)
| render timechart
```

Slowest endpoints:
```kusto
customEvents
| where name == "http.request_completed"
| extend duration_ms = todouble(customDimensions.duration_ms)
| summarize p50=percentile(duration_ms, 50), p95=percentile(duration_ms, 95), count() by tostring(customDimensions.path)
| order by p95 desc
```

**Langfuse** (optional, env-gated). When `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` are set on the Container App, every judge call, adapter call, and LLM extractor call becomes a Langfuse generation span with full prompt + response capture. Sign up at langfuse.com (free tier handles solo workloads), create a project, paste the keys via `az containerapp secret set`, and `/version` will start reporting `observability.langfuse: true`.

---

## Logs & monitoring

### Tail live logs

```bash
az containerapp logs show --name mdk-eval-web --resource-group mdk-eval-rg --follow
```

### Query historical logs (Log Analytics)

```bash
az monitor log-analytics query \
  --workspace $(az monitor log-analytics workspace show \
                  -g mdk-eval-rg -n workspace-mdkevalrgxRrK \
                  --query customerId -o tsv) \
  --analytics-query "ContainerAppConsoleLogs_CL | where TimeGenerated > ago(1h) | take 100"
```

Or open the workspace in the portal → Logs → run KQL.

### Watch a long-running eval

```bash
JOB_ID="job-abc123"
KEY=$(az containerapp secret show -n mdk-eval-web -g mdk-eval-rg --secret-name mdk-web-api-key --query value -o tsv)
URL="https://mdk-eval-web.whitefield-b83c207d.eastus.azurecontainerapps.io"
watch -n 5 "curl -s -H 'Authorization: Bearer $KEY' $URL/api/runs/$JOB_ID | jq"
```

---

## Scaling

The app uses Azure Container Apps' consumption-only profile with **schedule-driven scaling** via KEDA cron scalers (built into ACA — no extra dependency). The full scale config lives in [`infra/scale.yaml`](./infra/scale.yaml) and is the source of truth.

### Current schedule

| Window | Days | Hours (America/Chicago) | Pinned replicas |
|---|---|---|---|
| Weekday business hours | Mon–Fri | 07:00 – 19:00 | 1 |
| Weekend hours | Sat & Sun | 10:00 – 18:00 | 1 |
| All other times | — | — | 0 (scale-to-zero) |

`timezone: America/Chicago` is an IANA name — DST flips automatically.

**Cost:** ~76 of 168 hrs/week pinned (45% uptime) at one `0.5 vCPU / 1 GiB` replica ≈ **$13.50/mo**. Off-hours = effectively free. Burst-up to `maxReplicas: 3` is governed by the HTTP scaler at `concurrentRequests=30`.

### Apply a scale change

Edit `infra/scale.yaml`, then:

```bash
az containerapp update \
  --name mdk-eval-web \
  --resource-group mdk-eval-rg \
  --yaml infra/scale.yaml
```

> ⚠️ **Do NOT use `az containerapp update --min-replicas N`.** The flag-based path overrides `infra/scale.yaml` and silently sets a 24/7 floor — breaking the schedule and the cost model. Always go through the YAML.

### Verify

```bash
# Confirm the rules are applied
az containerapp show -n mdk-eval-web -g mdk-eval-rg \
  --query "properties.template.scale" -o yaml

# Watch live replica counts
az containerapp revision list -n mdk-eval-web -g mdk-eval-rg \
  --query "[].{name:name,replicas:properties.replicas,active:properties.active}" -o table

# Tail logs across a window boundary (e.g. 06:55–07:05 CT) to see the KEDA
# cron activation event fire
az containerapp logs show -n mdk-eval-web -g mdk-eval-rg --follow
```

### Operational caveats

**Mid-window scale-down kills in-flight runs.** At 19:00 CT a 17-minute eval queued at 18:55 gets terminated. The worker's startup sweep handles this safely on the next boot: stale `running` rows are marked `failed: 'worker restarted mid-job'`, and the pgmq message reappears after its visibility timeout. The job is never lost — but it will only complete on the next morning's window. Push the cron `end` 10–15 min past your stated cutoff if you want a graceful tail (e.g. `0 19 → 15 19`).

**Off-hours queueing accepts requests but jobs may stall.** The worker is an embedded asyncio task inside the API container's lifespan. Off-hours, a `POST /api/runs` cold-starts the container via the HTTP scaler — the request enqueues, the worker boots and starts draining. But the HTTP scaler doesn't know the worker is busy (only counts HTTP requests), so after ~5 min idle it scales to zero and kills the worker mid-job. Recovery is automatic next morning via the startup sweep, but jobs queued at 11pm don't run overnight. Document this for whoever might queue a long eval expecting it to drain off-hours.

**The Bolt dashboard's first request after 07:00 CT pays the cold-start once.** With `desiredReplicas=1` set by the cron rule, the 2–3s cold-start happens at the activation boundary, not per user. The very first dashboard hit between 06:55–07:00 will see it.

**`maxReplicas=3` is the ceiling.** The cron rule pins the floor; the HTTP rule + maxReplicas govern the ceiling. If portfolio-eval traffic grows past 3 concurrent evals queued during business hours, bump it.

### One container, both surfaces

The current setup runs the API and the embedded worker in one ACA app, so this schedule covers both. If the worker ever gets split into its own ACA (see *Future: split worker* below), the same `infra/scale.yaml` block can be repeated on the worker app — same cron, same TZ, no other change.

### Future: split worker for off-hours job draining

If off-hours job draining becomes a real requirement (e.g. you want a 9pm `POST /api/runs` to actually run overnight), the proper fix is to split the worker into its own Container App with a **pgmq-driven KEDA scaler** that scales on visible message count. The worker app would have `minReplicas: 0`, no cron schedule, and would only spin up when there's queue depth. The API app keeps the current cron schedule. The worker code itself doesn't change — only the deployment topology.

---

## Database

Postgres lives at Supabase, unchanged. Migrations land via `psql` against `DATABASE_URL`:

```bash
DATABASE_URL=$(az containerapp secret show -n mdk-eval-web -g mdk-eval-rg \
  --secret-name database-url --query value -o tsv)
psql "$DATABASE_URL" -f migrations/006_<latest>.sql
```

Migrations are idempotent (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`). Re-running is safe.

---

## Wiring Bolt's frontend

In Bolt's project env vars:

```
VITE_API_BASE=https://mdk-eval-web.whitefield-b83c207d.eastus.azurecontainerapps.io
VITE_MDK_API_KEY=<value of mdk-web-api-key from Azure>
```

Bolt's frontend should send every API request with:
```
Authorization: Bearer ${VITE_MDK_API_KEY}
```

Generate types from the OpenAPI spec on app build:

```bash
npx openapi-typescript $VITE_API_BASE/openapi.json -o src/lib/apiTypes.ts
```

CORS allowlist on the backend (already configured) accepts:
- `https://*.bolt.host`, `https://*.bolt.new`, `https://*.webcontainer-api.io`
- `https://*.vercel.app`
- `http://localhost:*`

To add a new origin:
```bash
az containerapp update -n mdk-eval-web -g mdk-eval-rg \
  --set-env-vars MDK_WEB_CORS_ORIGINS='https://bolt.host,https://NEW-ORIGIN'
```

---

## Troubleshooting

**"Invalid or missing bearer token" on every request**
The `Authorization: Bearer …` value Bolt is sending doesn't match the current `mdk-web-api-key`. Re-fetch with `az containerapp secret show … --secret-name mdk-web-api-key --query value -o tsv` and paste into Bolt's env. After rotation, also restart Bolt's webcontainer (the env is captured at boot).

**"Service auth is not configured" (HTTP 503)**
`MDK_WEB_API_KEY` env var is unset on the container. Run `az containerapp show -n mdk-eval-web -g mdk-eval-rg --query "properties.template.containers[0].env"` — the var should reference `secretref:mdk-web-api-key`. If missing, re-add via `--set-env-vars MDK_WEB_API_KEY=secretref:mdk-web-api-key`.

**Eval runs fail with `AuthenticationError: invalid x-api-key`**
The Anthropic key (or OpenAI key, depending on which provider raised the error) on Container Apps is invalid. Rotate via `az containerapp secret set --secrets anthropic-api-key=sk-ant-NEW`. Then re-queue the failed run by `POST /api/runs` again — pgmq has at-least-once semantics so the prior failed job stays marked failed; you queue a fresh one.

**Deploy succeeded but the new revision isn't serving traffic**
Run `az containerapp revision list -n mdk-eval-web -g mdk-eval-rg -o table`. Look for `Active=False` on the new revision. Often means the container failed its readiness probe — check `az containerapp logs show --revision <revision-name>` for the boot error. Common cause: a missing env var that boot-time code accesses.

**`prepared statement already exists` errors against Supabase**
You're connected via the Transaction Pooler (port 6543) instead of the Session Pooler (5432). Fix `DATABASE_URL` to `pooler.supabase.com:5432`.

**OpenAPI docs page is empty**
Browser cache. Hard-refresh, or hit `/openapi.json` directly to confirm the schema is being served.

**Cold start latency on first request**
Outside the cron windows defined in `infra/scale.yaml`, the app is at zero replicas and the first request takes ~2–3 s to spin up a new replica. Inside the windows, the cron rule pins one replica so the cold-start cost is paid only once at the window's start. If you need always-on for a demo, edit `infra/scale.yaml` to widen the cron `start`/`end` (or temporarily add a third 24/7 cron rule) and re-apply with `az containerapp update --yaml infra/scale.yaml`. Don't reach for `--min-replicas 1` — see the warning in the Scaling section.

---

## CI/CD

GitHub Actions (`.github/workflows/test.yml`) runs an e2e smoke test against the live deployment after every push to main. The `MDK_API_BASE` GitHub variable points at the Azure URL; `MDK_WEB_API_KEY` is a GitHub secret matching the Container App secret.

If the smoke test starts failing after a deploy, suspect (in order): broken migrations on Supabase, expired LLM provider keys, or a CORS regression that breaks the test's `OPTIONS` preflight.

---

## What changed from Fly

For anyone reading old commit messages or chat history:

| Fly | Azure |
|---|---|
| `fly deploy` | `az acr build` + `az containerapp update --image …` |
| `fly secrets set X=…` | `az containerapp secret set --secrets x=…` |
| `fly secrets list` | `az containerapp secret list` |
| `fly logs` | `az containerapp logs show --follow` |
| `mdk-eval-web.fly.dev` | `mdk-eval-web.whitefield-b83c207d.eastus.azurecontainerapps.io` |
| Fly volume `mdk_cache` | Not used — judge cache lives in Supabase via migration 003 |
| `fly.toml` | Container Apps revision config (`az containerapp show`) |

The application code is identical across both. Schema is identical. The CLI push tool works against either era's `DATABASE_URL`.

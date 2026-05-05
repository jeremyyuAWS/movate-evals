# Deploying the Movate Agent Assurance web service to Fly.io

This document covers shipping the FastAPI backend (the one that powers Bolt's dashboard) to Fly.io. When you cut over to Azure Container Apps later, see the "Migrating to Azure" section at the bottom — the only thing that changes is the host.

---

## What you're deploying

A single FastAPI container that exposes:

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness (no auth) |
| `GET /api/agents` | List agents (for dropdowns) |
| `POST /api/agent-definitions` | Upload + ingest a Lyzr agent JSON |
| `POST /api/runs` | Kick off an evaluation |
| `GET /api/runs/{job_id}` | Poll job status |
| `GET /api/scenario-sets/{id}` | List scenarios in a set |
| `PATCH /api/scenarios/{id}` | Approve / reject / annotate a scenario |

The container also runs evaluations as background tasks. No separate worker — eval execution is in-process via FastAPI BackgroundTasks. Fine for prototype scale.

---

## Prerequisites

- Fly account with `flyctl` installed (`brew install flyctl`)
- The Supabase Postgres connection string (you already have this — it's in our session history)
- An OpenAI API key (for judges + LLM ingest)
- An Anthropic API key (for the meta-judge + the second model in the panel)
- A Lyzr API key if you'll evaluate Lyzr agents
- A strong shared-secret token for `MDK_WEB_API_KEY` — generate with: `python -c "import secrets; print(secrets.token_urlsafe(32))"`

---

## Deploy in 6 steps

### 1. Initialize the Fly app

From the repo root:

```bash
fly launch --no-deploy --copy-config
```

When prompted:
- Choose the app name (e.g. `mdk-eval-web` if available; Fly will suggest if taken)
- Region: `ord` is set in fly.toml; `fly regions set <code>` to change
- Postgres / Redis: **No** (we're using Supabase + in-process queue)
- Deploy now: **No** (we need to set secrets first)

This reads our `fly.toml` and registers the app.

### 2. Create a persistent disk for the judge cache

```bash
fly volumes create mdk_cache --size 1 --region ord
```

A 1 GB volume is plenty. The cache pays for itself within a single evaluation — re-running the same dataset costs $0 in tokens.

### 3. Set the secrets

```bash
fly secrets set \
  DATABASE_URL='postgresql://postgres.ycyormadqjiwagfohbmo:<password>@aws-1-us-east-2.pooler.supabase.com:5432/postgres?sslmode=require' \
  MDK_WEB_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  OPENAI_API_KEY='sk-...' \
  ANTHROPIC_API_KEY='sk-ant-...' \
  LYZR_API_KEY='sk-default-...' \
  MDK_WEB_CORS_ORIGINS='https://<your-bolt-dashboard-url>.bolt.host'
```

Notes:
- `DATABASE_URL` — the same connection string you used for the local push. Note the `?sslmode=require` suffix — Supabase requires it.
- `MDK_WEB_API_KEY` — this is what Bolt's frontend sends as `Authorization: Bearer <token>`. Print it once with `fly secrets list` to copy into Bolt's env vars.
- `MDK_WEB_CORS_ORIGINS` — comma-separated list of allowed origins. Bolt's deploy URL goes here. Do **not** use `*` in production.

### 4. Deploy

```bash
fly deploy
```

First deploy takes ~3 minutes (image build + push). Subsequent deploys are ~30s thanks to layer caching.

### 5. Verify it's up

```bash
# liveness
curl https://<app-name>.fly.dev/healthz
# expect: {"status":"ok","version":"0.1.0"}

# auth
curl -H "Authorization: Bearer <MDK_WEB_API_KEY>" https://<app-name>.fly.dev/api/agents
# expect: a JSON array of agents (the ones you've already pushed)
```

If `/api/agents` returns something other than `[]`, your DB connection is working and the data from earlier pushes is visible.

### 6. Apply migration 002 to Supabase

The web service needs the `scenario_set`, `scenario`, and `web_run_job` tables that migration 001 doesn't include. Apply migration 002:

```bash
psql "$DATABASE_URL" -f migrations/002_scenario_storage.sql
```

Or paste the contents of [migrations/002_scenario_storage.sql](migrations/002_scenario_storage.sql) into Supabase Studio → SQL Editor → Run. The migration is idempotent (`CREATE TABLE IF NOT EXISTS`), safe to run multiple times.

---

## Wiring Bolt's frontend

In Bolt's project settings (or `.env` file in the generated dashboard):

```
NEXT_PUBLIC_API_BASE=https://<app-name>.fly.dev
MDK_WEB_API_KEY=<the same secret from step 3>
```

Bolt's frontend should send every API request with:
```
Authorization: Bearer ${MDK_WEB_API_KEY}
```

Tell Bolt:

> The backend lives at `${NEXT_PUBLIC_API_BASE}`. All endpoints under `/api/*` require a Bearer token from `MDK_WEB_API_KEY`. The OpenAPI schema is at `${NEXT_PUBLIC_API_BASE}/openapi.json` — generate types from it.
>
> Add an "Upload Agent Definition" page that:
> 1. File picker accepting `.json` files
> 2. Form fields: engagement_slug, agent_slug, scenario_set_name, optional engagement_name + agent_name, optional `synthesize` checkbox
> 3. POST as multipart/form-data to `/api/agent-definitions`
> 4. On response, navigate to `/scenario-sets/{scenario_set_id}` to review the derived scenarios
>
> Add a "Scenario Review" page at `/scenario-sets/{id}`:
> 1. Lists scenarios from `GET /api/scenario-sets/{id}`
> 2. Each row shows: scenario_id, status badge, severity, tags, "view payload" expand, derived_from provenance (extractor, model, constraint_quote)
> 3. Approve / Reject buttons hit `PATCH /api/scenarios/{id}` with `new_status=approved` or `new_status=rejected`
> 4. "Run Evaluation" button at the top → opens a modal with judges_enabled toggle + runs_per_scenario slider → POSTs `/api/runs` → navigates to `/runs/{job_id}` showing live status

---

## Operations

### Logs
```bash
fly logs                  # tail
fly logs --since 1h       # historical
```

### Status
```bash
fly status
fly machine status        # per-machine
```

### Open the deployed URL
```bash
fly open                  # opens the FastAPI auto-docs at /docs
```

### Scale up if you need always-on
Edit `fly.toml`: `min_machines_running = 1`. Costs ~$2/mo for a shared-cpu-1x.

### Watch a long-running eval
```bash
# from a local terminal:
JOB_ID="job-abc123"
watch -n 5 "curl -s -H 'Authorization: Bearer \$MDK_WEB_API_KEY' \
  https://<app>.fly.dev/api/runs/\$JOB_ID | jq"
```

---

## Migrating to Azure Container Apps later

When you're ready to move off Supabase + Fly to Azure Postgres + Azure Container Apps, the only changes are infrastructural:

1. **Build** the same Dockerfile against Azure Container Registry:
   ```bash
   az acr build -r <registry> -t mdk-eval-web:latest .
   ```
2. **Create** the Azure Postgres Flexible Server, run migrations 001 + 002 against it
3. **Deploy** to Container Apps with the same env vars (just point `DATABASE_URL` at Azure Postgres):
   ```bash
   az containerapp create -n mdk-eval-web -g <rg> --image <registry>/mdk-eval-web:latest \
     --secrets database-url="..." mdk-web-api-key="..." openai-api-key="..." anthropic-api-key="..." \
     --env-vars DATABASE_URL=secretref:database-url \
                MDK_WEB_API_KEY=secretref:mdk-web-api-key \
                OPENAI_API_KEY=secretref:openai-api-key \
                ANTHROPIC_API_KEY=secretref:anthropic-api-key \
                MDK_WEB_CORS_ORIGINS="https://your-bolt-url" \
     --ingress external --target-port 8080
   ```
4. **Mount** Azure Files for the cache equivalent of the Fly volume (or accept that cache is per-revision)
5. **Update** Bolt's `NEXT_PUBLIC_API_BASE` to the new `<app>.azurecontainerapps.io` URL

Application code is identical. Schema is identical. The push CLI works against either DB. Switchover is mostly a DNS + env-var change once the Azure side is provisioned.

---

## Troubleshooting

**"Invalid or missing bearer token" on every request**
The `MDK_WEB_API_KEY` your client is sending doesn't match what's in Fly secrets. Check with `fly secrets list` (shows hashes, not values — you'll need to compare lengths or rotate).

**"Service auth is not configured" (HTTP 503)**
`MDK_WEB_API_KEY` is unset in Fly secrets. Set it. The service refuses to run open by design.

**Eval runs hang at "queued"**
Check `fly logs` — the BackgroundTask probably crashed. Common cause: the agent's backend (e.g. Lyzr) returned an error and the job_id wasn't updated. The job's `error_message` column should have the traceback; SELECT it from the `web_run_job` table.

**"prepared statement already exists" errors when pushing**
You're connected to Supabase via the Transaction Pooler (port 6543) instead of the Session Pooler (port 5432). Update `DATABASE_URL` to use port 5432 / `pooler.supabase.com:5432`.

**OpenAPI docs page is empty**
Browser cache; hard-refresh. Or hit `/openapi.json` directly to see the JSON schema FastAPI auto-generates.

---

## Continuous verification — wire e2e smoke into GitHub Actions

The `e2e_live` job in `.github/workflows/test.yml` runs `tests/test_e2e_smoke.sh` against the deployed Fly backend on every PR + push to main. To enable it:

### One-time setup

1. Generate a CI bearer token (separate from your dev token, so you can rotate without breaking local work):
   ```bash
   CI_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
   # Add it as a SECOND valid token. Easiest path: the service treats
   # MDK_WEB_API_KEY as a single shared secret, so for now use the same one
   # as your local dev token. Rotate together when needed.
   ```

2. In GitHub: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `MDK_WEB_API_KEY`
   - Value: the same token Fly uses (or your separate CI token if you've added multi-key auth)

3. Optional: **Settings → Secrets and variables → Actions → Variables → New repository variable**
   - Name: `MDK_API_BASE`
   - Value: `https://mdk-eval-web.fly.dev` (only set if it differs from the default)

### What the job does

- Runs after the `test` job succeeds (no point hitting prod if local tests are red)
- Executes `tests/test_e2e_smoke.sh` with `API_BASE` + `MDK_WEB_API_KEY` injected from secrets
- Uses `AGENT_SLUG=ci-smoke-${{ github.run_number }}` so each CI run creates a fresh agent in Supabase (no UNIQUE constraint conflicts)
- Skips gracefully (with a warning, not a failure) when `MDK_WEB_API_KEY` is unset — important for forks and external PRs that can't access org secrets

### Cleanup

Each CI run leaves an `agent` row in Supabase named `ci-smoke-N`. They accumulate. Add a periodic cleanup query (manual for now; could become a scheduled GHA later):

```sql
DELETE FROM engagement WHERE slug = 'ci-smoke';
-- cascades to agents → runs → scenarios automatically per the FK ON DELETE rules.
```

Run that monthly (or whenever the dashboard's portfolio view starts looking cluttered with `ci-smoke-N` agents).

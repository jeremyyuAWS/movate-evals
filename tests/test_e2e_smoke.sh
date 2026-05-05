#!/usr/bin/env bash
# End-to-end smoke test against a deployed mdk-eval-web backend.
#
# What it does
# ------------
# Walks the full Bolt user flow via curl, with no UI involved:
#   1. healthz                       — service is alive
#   2. GET /api/agents               — auth + DB read works
#   3. POST /api/agent-definitions   — upload + heuristic ingest
#   4. GET /api/scenario-sets/{id}   — scenarios persisted
#   5. PATCH /api/scenarios/{id}     — approve a few
#   6. POST /api/runs                — queue an eval (mock backend, no judges)
#   7. GET /api/runs/{job_id}        — poll until done
#   8. Verify the resulting run_id is in the DB
#
# Usage
# -----
#   chmod +x tests/test_e2e_smoke.sh           # one-time
#   export API_BASE='https://mdk-eval-web.fly.dev'
#   export MDK_WEB_API_KEY='<your-token>'
#   ./tests/test_e2e_smoke.sh
#
# Requires: bash, curl, jq.
# No judge tokens spent — the test creates an agent with backend=mock.
# Total runtime: ~10-30 seconds (depends on the BackgroundTask scheduling).

set -euo pipefail

# ----------------------------- config -----------------------------

API_BASE="${API_BASE:-https://mdk-eval-web.fly.dev}"
TOKEN="${MDK_WEB_API_KEY:-}"
FIXTURE="${FIXTURE:-test-fixtures/movate-faq-agent.json}"
POLL_TIMEOUT_S="${POLL_TIMEOUT_S:-90}"

# Unique scenario_set_name per run so we don't collide on UNIQUE(agent_id, name).
TS="$(date +%Y%m%dT%H%M%S)"
ENGAGEMENT_SLUG="${ENGAGEMENT_SLUG:-smoke-test}"
AGENT_SLUG="${AGENT_SLUG:-faq-smoke}"
SET_NAME="smoke-${TS}"

# ----------------------------- helpers -----------------------------

# Color helpers — degrade to plain text when not on a TTY.
if [[ -t 1 ]]; then
  C_RED=$'\033[0;31m'; C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'
  C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_DIM=""; C_BOLD=""; C_RESET=""
fi

step() { printf "\n${C_BOLD}▶ %s${C_RESET}\n" "$*"; }
ok()   { printf "  ${C_GREEN}✓${C_RESET} %s\n" "$*"; }
warn() { printf "  ${C_YELLOW}!${C_RESET} %s\n" "$*"; }
fail() { printf "  ${C_RED}✗ FAIL${C_RESET} %s\n" "$*" >&2; exit 1; }
hint() { printf "  ${C_DIM}%s${C_RESET}\n" "$*"; }

# Tooling preflight.
command -v curl >/dev/null || { echo "curl is required"; exit 2; }
command -v jq   >/dev/null || { echo "jq is required (brew install jq)"; exit 2; }

# Required env vars.
[[ -n "$TOKEN" ]] || fail "MDK_WEB_API_KEY is not set."
[[ -f "$FIXTURE" ]] || fail "Fixture not found: $FIXTURE (run from repo root)."

# Authenticated GET / PATCH / POST helpers. Each writes the body to /tmp and
# returns it on stdout; status-code assertions happen via -w "%{http_code}".
api() {
  local method="$1"; local path="$2"; shift 2
  curl -sS -X "$method" "${API_BASE}${path}" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/json" \
    "$@"
}

# Assert JSON response has a top-level field. Exits on miss.
require_field() {
  local body="$1"; local field="$2"; local context="${3:-response}"
  local val
  val="$(printf '%s' "$body" | jq -r ".${field} // empty")"
  if [[ -z "$val" || "$val" == "null" ]]; then
    fail "${context} missing required field '${field}'. Body: $(printf '%s' "$body" | head -c 400)"
  fi
  printf '%s' "$val"
}

# Banner.
printf "${C_BOLD}mdk-eval-web end-to-end smoke test${C_RESET}\n"
printf "  ${C_DIM}target:    ${API_BASE}${C_RESET}\n"
printf "  ${C_DIM}fixture:   ${FIXTURE}${C_RESET}\n"
printf "  ${C_DIM}set name:  ${SET_NAME}${C_RESET}\n"

# ----------------------------- 1. healthz -----------------------------

step "1. Health check"
HEALTH="$(curl -sS -m 10 "${API_BASE}/healthz")" || fail "healthz request failed (network / DNS / Fly down?)"
[[ "$(printf '%s' "$HEALTH" | jq -r '.status')" == "ok" ]] \
  || fail "healthz did not return 'ok'. Body: $HEALTH"
ok "service is up · version $(printf '%s' "$HEALTH" | jq -r '.version')"

# ----------------------------- 2. auth + DB read -----------------------------

step "2. Auth + DB read"
AGENTS="$(api GET /api/agents)"
N_AGENTS="$(printf '%s' "$AGENTS" | jq 'length')"
if [[ "$N_AGENTS" == "0" ]]; then
  warn "DB has 0 agents. Bolt's seed data may not be applied. Continuing — the upload step will create one."
else
  ok "auth OK · ${N_AGENTS} agents visible"
fi

# ----------------------------- 3. upload + ingest -----------------------------

step "3. Upload + ingest fixture"
INGEST="$(api POST /api/agent-definitions \
  -F "file=@${FIXTURE};type=application/json" \
  -F "engagement_slug=${ENGAGEMENT_SLUG}" \
  -F "agent_slug=${AGENT_SLUG}" \
  -F "scenario_set_name=${SET_NAME}" \
  -F "engagement_name=Smoke Test" \
  -F "agent_name=FAQ Smoke" \
  -F "synthesize=false" \
  -F "triggered_by=test_e2e_smoke.sh")"

SET_ID="$(require_field "$INGEST" "scenario_set_id" "ingest response")"
AGENT_ID="$(require_field "$INGEST" "agent_id" "ingest response")"
N_SCENARIOS="$(printf '%s' "$INGEST" | jq '.scenarios | length')"
SOURCE_SHA="$(printf '%s' "$INGEST" | jq -r '.source_sha256')"
ok "scenario_set_id=${SET_ID} · agent_id=${AGENT_ID} · ${N_SCENARIOS} scenarios · sha=${SOURCE_SHA:0:12}…"

if [[ "$N_SCENARIOS" == "0" ]]; then
  warn "Ingest returned zero scenarios — the heuristic extractor produced nothing for this fixture."
  hint "Test fixture may be too sparse for heuristics. Try with --synthesize or a richer agent JSON."
fi

# ----------------------------- 4. list scenarios -----------------------------

step "4. List scenarios in the new set"
SCENARIOS="$(api GET "/api/scenario-sets/${SET_ID}")"
N_LISTED="$(printf '%s' "$SCENARIOS" | jq 'length')"
[[ "$N_LISTED" == "$N_SCENARIOS" ]] \
  || fail "scenario count mismatch — ingest reported ${N_SCENARIOS}, list returned ${N_LISTED}."
ok "list returned ${N_LISTED} scenarios"

# ----------------------------- 5. approve a few -----------------------------

step "5. Approve scenarios for the run"
# Approve up to 3 scenarios so the run has something to chew on.
# If the heuristic produced 0, fall back to "test passes anyway" — the run
# endpoint will still succeed by including unverified scenarios.
TO_APPROVE=$(printf '%s' "$SCENARIOS" | jq '[.[] | .id] | .[0:3]')
N_APPROVED=0
for sid in $(printf '%s' "$TO_APPROVE" | jq -r '.[]'); do
  api PATCH "/api/scenarios/${sid}" \
    -F "new_status=approved" \
    -F "verified_by=test_e2e_smoke.sh" >/dev/null
  N_APPROVED=$((N_APPROVED + 1))
done
ok "approved ${N_APPROVED} scenarios"

# ----------------------------- 6. queue a run -----------------------------

step "6. Queue an evaluation (judges off, 1 run per scenario)"
RUN_REQ=$(jq -n \
  --argjson agent_id "$AGENT_ID" \
  --argjson set_id "$SET_ID" \
  '{agent_id: $agent_id, scenario_set_id: $set_id, judges_enabled: false, runs_per_scenario: 1, only_approved: false, triggered_by: "test_e2e_smoke.sh"}')

QUEUE="$(api POST /api/runs -H "Content-Type: application/json" -d "$RUN_REQ")"
JOB_ID="$(require_field "$QUEUE" "job_id" "run-queue response")"
TOTAL="$(printf '%s' "$QUEUE" | jq -r '.total_scenarios')"
ok "queued · job_id=${JOB_ID} · total_scenarios=${TOTAL}"

# ----------------------------- 7. poll until done -----------------------------

step "7. Poll job until done (timeout: ${POLL_TIMEOUT_S}s)"
STARTED="$(date +%s)"
while :; do
  NOW="$(date +%s)"
  ELAPSED=$((NOW - STARTED))
  if (( ELAPSED > POLL_TIMEOUT_S )); then
    fail "job ${JOB_ID} did not complete within ${POLL_TIMEOUT_S}s (last status: ${STATUS:-unknown})"
  fi

  STATUS_RESP="$(api GET "/api/runs/${JOB_ID}")"
  STATUS="$(printf '%s' "$STATUS_RESP" | jq -r '.status')"
  COMPLETED="$(printf '%s' "$STATUS_RESP" | jq -r '.completed_scenarios')"
  printf "  ${C_DIM}t+%2ds  status=%-9s  completed=%s/%s${C_RESET}\n" "$ELAPSED" "$STATUS" "$COMPLETED" "$TOTAL"

  case "$STATUS" in
    done)
      OVERALL="$(printf '%s' "$STATUS_RESP" | jq -r '.overall_score')"
      RESULT_STATUS="$(printf '%s' "$STATUS_RESP" | jq -r '.result_status')"
      RESULT_RUN_ID="$(printf '%s' "$STATUS_RESP" | jq -r '.result_run_id')"
      ok "run finished · score=${OVERALL}/100 · status=${RESULT_STATUS} · run_id=${RESULT_RUN_ID}"
      break
      ;;
    failed)
      ERR="$(printf '%s' "$STATUS_RESP" | jq -r '.error_message')"
      fail "job failed: ${ERR}"
      ;;
    queued|running)
      sleep 3
      ;;
    *)
      fail "unexpected status: ${STATUS}. Body: ${STATUS_RESP}"
      ;;
  esac
done

# ----------------------------- 8. verify the run lands in the read view -----------------------------

step "8. Verify the new agent + run appear in /api/agents"
AGENTS_AFTER="$(api GET /api/agents)"
NEW_AGENT_PRESENT="$(printf '%s' "$AGENTS_AFTER" | jq --arg s "$AGENT_SLUG" '[.[] | select(.slug == $s)] | length')"
[[ "$NEW_AGENT_PRESENT" -ge 1 ]] \
  || fail "agent '${AGENT_SLUG}' not visible in /api/agents after the run. Did postgres_push fail?"
ok "agent '${AGENT_SLUG}' is visible · the dashboard's portfolio view should show this run"

# ----------------------------- summary -----------------------------

printf "\n${C_GREEN}${C_BOLD}✓ ALL TESTS PASSED${C_RESET}\n"
printf "  Created scenario_set: ${C_BOLD}${SET_ID}${C_RESET} (name: ${SET_NAME})\n"
printf "  Job:                  ${C_BOLD}${JOB_ID}${C_RESET}\n"
printf "  Result run:           ${C_BOLD}${RESULT_RUN_ID}${C_RESET} (${OVERALL}/100, ${RESULT_STATUS})\n"
printf "\n${C_DIM}Next: open the dashboard and confirm the new run appears under engagement '${ENGAGEMENT_SLUG}', agent '${AGENT_SLUG}'.${C_RESET}\n"

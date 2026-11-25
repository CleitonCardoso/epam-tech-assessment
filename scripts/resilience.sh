#!/usr/bin/env bash
#
# Fault-injection checks. These are destructive on purpose: they kill processes and restart
# Redis. Run them against a stack you can recreate.
#
#   ./scripts/resilience.sh            # all scenarios
#   ./scripts/resilience.sh crash      # only the worker-crash recovery
#   ./scripts/resilience.sh redis      # only the Redis-restart scenario
#
# Requires: docker (with compose), curl. No jq.
#
# Regression note: with WORKER_CONCURRENCY > 1 several jobs run at once, so these scenarios also
# prove that one dying job does not take its siblings down with it.

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SAMPLE="${SAMPLE:-samples/sample-text.pdf}"
# Short lease so recovery is observable in seconds instead of the 90s production default.
LEASE_SECONDS="${LEASE_SECONDS:-15}"
HEARTBEAT_SECONDS="${HEARTBEAT_SECONDS:-5}"
RECOVERY_TIMEOUT="${RECOVERY_TIMEOUT:-180}"

PASS=0
FAIL=0

# Which worker to run the scenarios against. "live" uses the compose worker (real providers);
# "fake" runs a disposable worker with the deterministic stand-ins, so fault injection costs no
# API quota. Default "live" so the script tests what production runs.
WORKER_MODE="${WORKER_MODE:-live}"

start_worker() {
  if [[ "$WORKER_MODE" == "fake" ]]; then
    docker compose run -d --name resilience-main --no-deps \
      -e AI_MODE=fake -e FAKE_DELAY_SECONDS=0.05 -e WORKER_CONCURRENCY=4 \
      -e LEASE_SECONDS="$LEASE_SECONDS" -e HEARTBEAT_SECONDS="$HEARTBEAT_SECONDS" \
      worker python -m app.worker >/dev/null
    sleep 4
  else
    docker compose start worker >/dev/null
    sleep 2
  fi
}

stop_worker() {
  docker rm -f resilience-main >/dev/null 2>&1 || true
  docker compose stop worker >/dev/null 2>&1 || true
}

check() {
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf '  \033[32mPASS\033[0m %s\n' "$name"
    PASS=$((PASS + 1))
  else
    printf '  \033[31mFAIL\033[0m %s\n' "$name"
    FAIL=$((FAIL + 1))
  fi
}

# Minimal JSON readers: the API returns compact JSON.
json_str() {
  printf '%s' "$1" | grep -oE "\"$2\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -n1 \
    | sed -E "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"([^\"]*)\"/\1/" || true
}

submit() {
  curl -sS --max-time 60 -X POST "$BASE_URL/api/v1/documents" \
    -F "parser=${1:-pypdf}" -F "files=@${SAMPLE};type=application/pdf"
}

status_of() { curl -sS --max-time 10 "$BASE_URL/api/v1/documents/$1" | sed -n 's/.*"status":"\([a-z]*\)".*/\1/p'; }
error_code_of() { curl -sS --max-time 10 "$BASE_URL/api/v1/documents/$1" | sed -n 's/.*"error":{"code":"\([A-Z_]*\)".*/\1/p'; }

wait_terminal() {
  local id="$1" deadline=$((SECONDS + RECOVERY_TIMEOUT)) last=""
  while (( SECONDS < deadline )); do
    last="$(status_of "$id")"
    case "$last" in
      completed|failed) printf '%s' "$last"; return 0 ;;
    esac
    sleep 1
  done
  printf '%s' "${last:-unknown}"; return 1
}

pending_count() {
  docker compose exec -T redis redis-cli XPENDING docs:jobs document-workers 2>/dev/null \
    | sed -n '1p' | tr -d '[:space:]'
}

cleanup() {
  docker rm -f resilience-slow resilience-recovery resilience-main resilience-fence-a resilience-fence-b >/dev/null 2>&1 || true
  docker compose start worker >/dev/null 2>&1 || true
}
trap cleanup EXIT

scenario_crash() {
  echo
  echo "== Scenario A: worker is SIGKILLed mid-job; a replacement must reclaim it =="

  echo "   stopping the main worker so only the doomed one is running"
  stop_worker

  echo "   starting a slow worker (lease ${LEASE_SECONDS}s, heartbeat ${HEARTBEAT_SECONDS}s)"
  docker compose run -d --name resilience-slow \
    -e AI_MODE=fake -e FAKE_DELAY_SECONDS=25 \
    -e LEASE_SECONDS="$LEASE_SECONDS" -e HEARTBEAT_SECONDS="$HEARTBEAT_SECONDS" \
    --no-deps worker python -m app.worker >/dev/null
  sleep 6

  local body id
  body="$(submit pypdf)"
  id="$(json_str "$body" id)"
  [[ -n "$id" ]] || { echo "   could not submit a document: $body" >&2; return 1; }
  echo "   submitted $id, letting it reach the summarisation stage"
  sleep 8

  local before_pending before_status
  before_status="$(status_of "$id")"
  before_pending="$(pending_count)"
  echo "   before kill: status=$before_status pending=$before_pending"
  check "job is in flight (not yet terminal)" test "$before_status" != "completed" -a "$before_status" != "failed"
  check "stream entry is pending while the lease is held" test "${before_pending:-0}" -ge 1

  echo "   docker kill (SIGKILL, no graceful shutdown)"
  docker kill resilience-slow >/dev/null 2>&1 || true
  docker rm -f resilience-slow >/dev/null 2>&1 || true
  sleep 1
  check "job did not magically complete after the kill" test "$(status_of "$id")" != "completed"

  echo "   starting a replacement worker; it must wait for the lease, then resume"
  docker compose run -d --name resilience-recovery \
    -e AI_MODE=fake -e LEASE_SECONDS="$LEASE_SECONDS" -e HEARTBEAT_SECONDS="$HEARTBEAT_SECONDS" \
    --no-deps worker python -m app.worker >/dev/null

  local final after_pending
  final="$(wait_terminal "$id")" || true
  echo "   final status: ${final:-unknown} (error code: $(error_code_of "$id" 2>/dev/null || true))"
  check "reclaimed job completed" test "$final" = "completed"

  sleep 2
  after_pending="$(pending_count)"
  echo "   pending after recovery: ${after_pending:-?}"
  check "no stream entry left pending" test "${after_pending:-0}" -eq 0
}

scenario_redis() {
  echo
  echo "== Scenario B: Redis restarts while a backlog exists =="

  start_worker

  local ids=() i=0 body id
  echo "   submitting 30 documents to build a backlog (worker concurrency is 4)"
  while (( i < 30 )); do
    body="$(submit pypdf)"
    id="$(json_str "$body" id)"
    [[ -n "$id" ]] && ids+=("$id")
    i=$((i + 1))
  done
  echo "   accepted: ${#ids[@]}/30"
  check "backlog submitted" test "${#ids[@]}" -ge 25

  echo "   restarting redis now"
  docker compose restart redis >/dev/null
  sleep 3

  local deadline=$((SECONDS + RECOVERY_TIMEOUT)) completed=0 failed=0 lost=0 unknown=0
  for doc in "${ids[@]}"; do
    local st
    st="$(wait_terminal "$doc" 2>/dev/null || true)"
    case "$st" in
      completed) completed=$((completed + 1)) ;;
      failed) failed=$((failed + 1)) ;;
      404|lost) lost=$((lost + 1)) ;;
      *) unknown=$((unknown + 1)) ;;
    esac
  done
  echo "   after recovery: completed=$completed failed=$failed lost=$lost unresolved=$unknown"
  check "every accepted document reached a terminal state" test "$unknown" -eq 0
  check "no accepted document was lost across the restart" test "$lost" -eq 0
}

scenario_fencing() {
  echo
  echo "== Scenario C: a stale worker must not overwrite a result it lost the lease on =="
  echo "   worker A takes a job and is PAUSED mid-summary; its lease expires; worker B finishes"
  echo "   the job; then A resumes and tries to commit with a dead token."

  stop_worker
  docker rm -f resilience-fence-a resilience-fence-b >/dev/null 2>&1 || true

  docker compose run -d --name resilience-fence-a --no-deps \
    -e AI_MODE=fake -e FAKE_DELAY_SECONDS=30 -e WORKER_CONCURRENCY=1 \
    -e LEASE_SECONDS="$LEASE_SECONDS" -e HEARTBEAT_SECONDS="$HEARTBEAT_SECONDS" \
    worker python -m app.worker >/dev/null
  sleep 6

  local body id
  body="$(submit pypdf)"
  id="$(json_str "$body" id)"
  [[ -n "$id" ]] || { echo "   could not submit: $body" >&2; return 1; }
  echo "   submitted $id; waiting for A to reach the paused stage"
  sleep 6

  echo "   docker pause A (SIGSTOP: heartbeat stops, lease will expire)"
  docker pause resilience-fence-a >/dev/null
  check "job is not terminal while A is paused" test "$(status_of "$id")" != "completed"

  echo "   starting worker B, which must take over after the lease expires"
  docker compose run -d --name resilience-fence-b --no-deps \
    -e AI_MODE=fake -e LEASE_SECONDS="$LEASE_SECONDS" -e HEARTBEAT_SECONDS="$HEARTBEAT_SECONDS" \
    worker python -m app.worker >/dev/null

  local final
  final="$(wait_terminal "$id")" || true
  echo "   job reached: ${final:-unknown}"
  check "replacement worker completed the job while the old one was frozen" test "$final" = "completed"

  echo "   docker unpause A: it resumes and tries to write with a dead token"
  docker unpause resilience-fence-a >/dev/null
  sleep 8

  local a_logs
  a_logs="$(docker logs resilience-fence-a 2>&1 || true)"
  printf '%s' "$a_logs" | grep -q "lease_lost\|lease_renewal_rejected" \
    && echo "   A reported the lost lease" || echo "   (A produced no lease warning - see logs)"
  check "stale worker detected that it lost the lease" \
    bash -c "docker logs resilience-fence-a 2>&1 | grep -qE 'lease_lost|lease_renewal_rejected'"
  check "stale worker never reported completing the job" \
    bash -c "! docker logs resilience-fence-a 2>&1 | grep -q 'job_completed id=${id}'"
  check "document is still completed after the stale write attempt" test "$(status_of "$id")" = "completed"
  check "no pending entry left behind" test "$(pending_count)" -eq 0

  docker rm -f resilience-fence-a resilience-fence-b >/dev/null 2>&1 || true
}

main() {
  local which="${1:-all}"
  echo "Resilience checks against ${BASE_URL}"
  case "$which" in
    crash) scenario_crash ;;
    redis) scenario_redis ;;
    fencing) scenario_fencing ;;
    all) scenario_crash; scenario_redis; scenario_fencing ;;
    *) echo "usage: $0 [all|crash|redis|fencing]" >&2; exit 2 ;;
  esac
  echo
  echo "resilience: ${PASS} passed, ${FAIL} failed"
  [[ "$FAIL" -eq 0 ]]
}

main "$@"

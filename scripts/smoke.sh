#!/usr/bin/env bash
#
# Reviewer-facing end-to-end walkthrough for the async PDF-processing API.
#
#   1. wait for GET /health/ready with a finite deadline
#   2. upload samples/sample-text.pdf with parser=pypdf
#   3. poll GET /api/v1/documents/<id> until completed|failed with a finite deadline
#   4. assert completed / page_count >= 2 / pages ordered from 1 / non-empty summary
#   5. second section: a corrupt upload must be rejected without aborting the walkthrough
#
# Requirements: bash + curl only (no jq, no python needed).
# Run from anywhere:   ./scripts/smoke.sh
# Base URL override:   BASE_URL=http://127.0.0.1:8000 ./scripts/smoke.sh
# The stack must already be running (docker compose up -d); this script makes HTTP calls only
# and is safe to run repeatedly: it never mutates or flushes anything.

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
BASE_URL="${BASE_URL%/}"
READY_TIMEOUT="${READY_TIMEOUT:-60}"   # seconds to wait for /health/ready
JOB_TIMEOUT="${JOB_TIMEOUT:-120}"      # seconds to wait for the document to finish
POLL_INTERVAL="${POLL_INTERVAL:-1}"    # seconds between document status polls

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# `pwd -W` yields a Windows-style path (C:/...) under Git Bash on Windows. Native curl.exe
# cannot open a POSIX path like /c/Users/..., and fails with "curl: (26) Failed to open/read
# local data". On Linux/macOS `pwd -W` is not supported, so the plain `pwd` is the fallback.
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && (pwd -W 2>/dev/null || pwd))"
SAMPLE_TEXT="${REPO_ROOT}/samples/sample-text.pdf"
SAMPLE_CORRUPT="${REPO_ROOT}/samples/sample-corrupt.pdf"

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------------------------
# Tiny JSON readers so the script needs neither jq nor python.
#
# The API returns compact JSON (FastAPI/Starlette JSONResponse), e.g.
#   {"documents":[{"id":"0f6c...","filename":"...","status":"queued"}],"rejected":[]}
# so `grep -oE` on the quoted key and a `sed` to strip the key and quotes is sufficient.
# `|| true` keeps a no-match grep from tripping `set -e`/`pipefail`; callers fail loudly when
# the extracted value is empty.
# ---------------------------------------------------------------------------------------------
json_string() {
    local body="$1" key="$2"
    printf '%s' "$body" \
        | grep -oE "\"${key}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" \
        | head -n 1 \
        | sed -E "s/.*\"${key}\"[[:space:]]*:[[:space:]]*\"([^\"]*)\"/\1/" \
        || true
}

json_number() {
    local body="$1" key="$2"
    printf '%s' "$body" \
        | grep -oE "\"${key}\"[[:space:]]*:[[:space:]]*[0-9]+" \
        | head -n 1 \
        | sed -E 's/^.*[^0-9]//' \
        || true
}

# ---------------------------------------------------------------------------------------------
# 1. Readiness
# ---------------------------------------------------------------------------------------------
step "Waiting for ${BASE_URL}/health/ready (timeout ${READY_TIMEOUT}s)"
ready_deadline=$((SECONDS + READY_TIMEOUT))
while :; do
    if ready_body="$(curl -fsS --max-time 5 "${BASE_URL}/health/ready" 2>/dev/null)"; then
        info "$ready_body"
        break
    fi
    if (( SECONDS >= ready_deadline )); then
        fail "/health/ready did not return 200 within ${READY_TIMEOUT}s. Is the stack up? Try: docker compose up -d"
    fi
    sleep 1
done

# ---------------------------------------------------------------------------------------------
# 2. Upload a valid sample
# ---------------------------------------------------------------------------------------------
step "Uploading samples/sample-text.pdf with parser=pypdf"
[[ -f "$SAMPLE_TEXT" ]] || fail "missing sample file: ${SAMPLE_TEXT} (run: python scripts/make_samples.py)"
if ! upload_body="$(curl -fsS --max-time 60 -X POST "${BASE_URL}/api/v1/documents" \
        -F "parser=pypdf" \
        -F "files=@${SAMPLE_TEXT};type=application/pdf")"; then
    fail "POST /api/v1/documents failed"
fi
printf '    %s\n' "$upload_body"

# The batch response is {"documents":[{...,"id":"<uuid>",...}],"rejected":[]}; the first "id"
# is the document we created.
doc_id="$(json_string "$upload_body" id)"
[[ -n "$doc_id" ]] || fail "could not extract a document id from the upload response: ${upload_body}"
info "document id: ${doc_id}"

# ---------------------------------------------------------------------------------------------
# 3. Poll until terminal
# ---------------------------------------------------------------------------------------------
step "Polling ${BASE_URL}/api/v1/documents/${doc_id} (timeout ${JOB_TIMEOUT}s)"
job_deadline=$((SECONDS + JOB_TIMEOUT))
last_status=""
final_body=""
while :; do
    if ! detail_body="$(curl -fsS --max-time 10 "${BASE_URL}/api/v1/documents/${doc_id}")"; then
        fail "GET /api/v1/documents/${doc_id} failed"
    fi
    status="$(json_string "$detail_body" status)"
    [[ -n "$status" ]] || fail "could not read status from the document JSON: ${detail_body}"
    if [[ "$status" != "$last_status" ]]; then
        info "status: ${last_status:-<none>} -> ${status}"
        last_status="$status"
    fi
    if [[ "$status" == "completed" ]]; then
        final_body="$detail_body"
        break
    fi
    if [[ "$status" == "failed" ]]; then
        printf '\n%s\n' "$detail_body" >&2
        fail "document ${doc_id} failed processing"
    fi
    if (( SECONDS >= job_deadline )); then
        fail "document ${doc_id} did not reach a terminal status within ${JOB_TIMEOUT}s (last status: ${status})"
    fi
    sleep "$POLL_INTERVAL"
done

# ---------------------------------------------------------------------------------------------
# 4. Assert the completed document
# ---------------------------------------------------------------------------------------------
step "Asserting the completed document"
completed_status="$(json_string "$final_body" status)"
[[ "$completed_status" == "completed" ]] || fail "expected status=completed, got '${completed_status}'"

page_count="$(json_number "$final_body" page_count)"
[[ -n "$page_count" ]] || fail "page_count is missing or not a number in: ${final_body}"
(( page_count >= 2 )) || fail "expected page_count >= 2, got ${page_count}"

# pages[] must be non-empty and ordered exactly 1..N.
page_numbers="$(printf '%s' "$final_body" \
    | grep -oE '"page_number"[[:space:]]*:[[:space:]]*[0-9]+' \
    | sed -E 's/^.*[^0-9]//' \
    || true)"
[[ -n "$page_numbers" ]] || fail "the document has no pages[]: ${final_body}"
mapfile -t numbers <<< "$page_numbers"
(( ${#numbers[@]} > 0 )) || fail "the document has no pages[]"
(( ${#numbers[@]} == page_count )) \
    || fail "page_count=${page_count} but pages[] holds ${#numbers[@]} entries"
for i in "${!numbers[@]}"; do
    expected=$((i + 1))
    [[ "${numbers[$i]}" == "$expected" ]] \
        || fail "pages are not ordered 1..N: expected page_number ${expected} at position ${i}, got ${numbers[$i]}"
done
info "pages: ${#numbers[@]} (page_number ordered 1..${#numbers[@]})"

# Every page must be format "text" with non-empty content.
content_fields="$(printf '%s' "$final_body" | grep -oE '"content"[[:space:]]*:' | wc -l | tr -d '[:space:]' || true)"
empty_contents="$(printf '%s' "$final_body" | grep -oE '"content"[[:space:]]*:[[:space:]]*""' | wc -l | tr -d '[:space:]' || true)"
text_formats="$(printf '%s' "$final_body" | grep -oE '"format"[[:space:]]*:[[:space:]]*"text"' | wc -l | tr -d '[:space:]' || true)"
(( content_fields == page_count )) \
    || fail "expected ${page_count} page contents, found ${content_fields}"
(( empty_contents == 0 )) \
    || fail "found ${empty_contents} page(s) with empty content"
(( text_formats == page_count )) \
    || fail "expected ${page_count} pages with format='text', found ${text_formats}"

summary_fields="$(printf '%s' "$final_body" | grep -oE '"summary"[[:space:]]*:' | wc -l | tr -d '[:space:]' || true)"
(( summary_fields == 1 )) || fail "expected exactly one summary field, found ${summary_fields}"
summary="$(json_string "$final_body" summary)"
[[ -n "$summary" ]] || fail "summary is empty or null: ${final_body}"
info "summary: $(printf '%s' "$summary" | cut -c 1-200)"

step "Final document JSON"
printf '%s\n' "$final_body" | sed 's/^/    /'

# ---------------------------------------------------------------------------------------------
# 5. Rejected upload — expected client error, must not abort the walkthrough
# ---------------------------------------------------------------------------------------------
step "Rejected upload: samples/sample-corrupt.pdf (expected 422 or 202 with rejected[])"
[[ -f "$SAMPLE_CORRUPT" ]] || fail "missing sample file: ${SAMPLE_CORRUPT} (run: python scripts/make_samples.py)"

# No -f here: a 4xx is the expected outcome, and we still want the body and status code.
set +e
reject_response="$(curl -sS --max-time 30 -w $'\n%{http_code}' -X POST "${BASE_URL}/api/v1/documents" \
    -F "parser=pypdf" \
    -F "files=@${SAMPLE_CORRUPT};type=application/pdf")"
curl_status=$?
set -e
(( curl_status == 0 )) || fail "curl failed while uploading the corrupt sample (exit ${curl_status})"

http_code="${reject_response##*$'\n'}"
reject_body="${reject_response%$'\n'*}"
info "HTTP ${http_code}"
printf '    %s\n' "$reject_body"

case "$http_code" in
    422)
        info "OK: corrupt upload rejected with 422 (contract: every file invalid)."
        ;;
    202)
        if [[ "$reject_body" == *'"rejected"'* ]]; then
            info "OK: HTTP 202 with a rejected[] entry (partial rejection is allowed by contract)."
        else
            fail "HTTP 202 without a rejected[] entry for a corrupt upload: ${reject_body}"
        fi
        ;;
    *)
        fail "expected HTTP 422 (or 202 with rejected[]), got HTTP ${http_code}: ${reject_body}"
        ;;
esac

step "Smoke test passed"

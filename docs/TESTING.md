# Testing

What is tested, how to run each layer, what each layer actually proves, and - explicitly - what is
not covered by any automated test.

Inventory: **45 test functions** in four files. **17 are integration-marked**; a default
`-m integration` run executes 13 and skips 4 opt-in tests (3 concurrency + 1 live AI).

| Suite | File | Tests | Runs by default | Needs a running stack | Calls a paid provider |
|---|---|---|---|---|---|
| Unit (contract) | `tests/unit/test_contract.py` | 14 | yes | no | no |
| Unit (hardening) | `tests/unit/test_hardening.py` | 14 | yes | no | no |
| Integration (HTTP end to end) | `tests/integration/test_api.py` | 13 | yes | yes | no |
| Integration (concurrency, opt-in) | `tests/integration/test_concurrency.py` | 3 | no - needs `RUN_CONCURRENCY_TESTS=1` | yes, plus a disposable fake-mode worker | no (`pypdf` only; the summary is the configured summarizer) |
| Opt-in live AI | `tests/integration/test_live_ai.py` | 1 | no - needs `RUN_LIVE_AI_TESTS=1` | yes, in `AI_MODE=live` with a real key | yes |

Verified first-hand in this pass: unit suite **28 passed, 17 deselected in 0.77 s**; the integration
directory collects **17 tests** (13 default + 3 concurrency + 1 live). Ledger results for the gated
runs: default integration **13 passed + 4 skipped**; concurrency **3 passed** when opted in; live AI
**1 passed** when opted in. Measurements from the load/fault campaign are in
[PERFORMANCE.md](PERFORMANCE.md).

## Running the suites

### Unit suite (offline, no stack)

The Compose `test` profile exists for exactly this: the same image with `pytest` installed, and the
`tests/` + `pyproject.toml` mounted read-only. Its command is baked in:

```bash
docker compose --profile test run --rm test
# equivalent to: python -m pytest -q -m "not integration"
```

This does not start any service and does not need Redis, S3, or HTTP. It may build the test image
the first time (`INSTALL_DEV=1`), but it never touches the running `api`/`worker` containers.

The same suite can be run without Compose by using the test image directly. This is the command
that was actually **executed** during this documentation pass, against the current working-tree
`app/` and `tests/` (mounted read-only over the image):

```powershell
$root = (Get-Location).Path.Replace('\','/')
docker run --rm --network none `
  -v "${root}/tests:/app/tests:ro" `
  -v "${root}/app:/app/app:ro" `
  -v "${root}/pyproject.toml:/app/pyproject.toml:ro" `
  epam-tech-assessment-test python -m pytest -q -m "not integration"
```

Observed result in this pass: **28 passed, 17 deselected in 0.77 s** (the 17 deselected are the 13
`test_api.py` tests, the 3 opt-in concurrency tests and the opt-in live test - all marked
`integration`). Mounting `app/` and `tests/` was deliberate: the built image can be older than the
working tree (see the OneDrive build-context caveat in [PROGRESS.md](PROGRESS.md)), and the point of
a unit run is to test the code you are looking at. Nothing in this run touched Redis, S3, HTTP or a
provider.

The host has `py -3` = Python 3.14.0b2 with **no pytest installed**, so the host-side variant

```bash
py -3 -m pytest tests/unit -q
```

was **not executed** here (it would need `pip install -r requirements-dev.txt`). The project
targets Python 3.12 and tests are normally run in the container.

### Integration suite (running stack required)

```bash
docker compose up -d
docker compose --profile test run --rm \
  -e API_BASE_URL=http://api:8000 \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python -m pytest tests/integration -m integration -q
```

Two details that cost time if missed:

- `-e API_BASE_URL=http://api:8000`: inside the Compose network the API is `api:8000`, not
  `localhost:8000`. The default in `tests/conftest.py` is `http://localhost:8000` for host-side
  runs.
- `-v "$(pwd)/samples:/app/samples:ro"`: `samples/` is excluded by `.dockerignore`, so the test
  image has no sample PDFs. `tests/conftest.py` resolves samples as `<repo>/samples`, which is
  `/app/samples` in the container. Without the mount, every test that needs a sample skips with
  `sample ... missing; run python scripts/make_samples.py` instead of failing - easy to misread
  as a pass.

The autouse `stack_ready` fixture polls `GET /health/ready` for up to `TEST_READY_TIMEOUT` seconds
(default 60) and exits the whole run with a clear message if the stack never becomes ready, so a
forgotten `docker compose up -d` produces one useful error instead of a wall of connection errors.
Note that `docker compose up -d` now also runs the one-shot Terraform `provisioner`; `api`/`worker`
are gated on it, so on a first run (provider download) the stack takes a minute or two longer to
become ready. If readiness never comes up, check `docker compose logs provisioner` before blaming
the tests.

Host-side variant (not executed here because the host has no pytest; it would also need network
access to `localhost:8000`):

```bash
API_BASE_URL=http://localhost:8000 py -3 -m pytest tests/integration -m integration -q
```

Recorded results from the verification ledger: default run **13 passed, 4 skipped** (the 4 skipped
are the 3 concurrency tests and the 1 live-AI test - both groups are opt-in and report themselves
as skipped, never passed). With the stack in `AI_MODE=live` and a real key, an earlier full run was
**13 passed, 1 skipped** in 25.99 s before the concurrency tests were added; the live test still
skips without its flag.

### Opt-in live AI test

```bash
# stack must be in AI_MODE=live with a real GEMINI_API_KEY in .env
docker compose up -d --force-recreate api worker
docker compose --profile test run --rm \
  -e API_BASE_URL=http://api:8000 -e RUN_LIVE_AI_TESTS=1 \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python -m pytest tests/integration/test_live_ai.py -q
```

Skip rules and what it asserts:

- Skipped by default (`pytest.mark.skipif(not RUN_LIVE_AI_TESTS)`); skipped is reported as skipped,
  never as passed.
- If `RUN_LIVE_AI_TESTS=1` but the stack is not in live mode (no key), the test **fails** with an
  explicit message including the observed `ai_mode`, rather than silently passing.
- With a real key it asserts: the document completes, has at least 2 pages, every page is
  `format: "markdown"`, no `ai_mode=fake` warning is present, the summary is non-empty and does not
  say "fake mode", and page 1's content contains "page 1" and not "fake markdown". That is a real
  model call, not a mock.
- Recorded ledger result with a real key: **1 passed in 12.58 s**; the demo document was a real
  per-page transcription plus a genuine summary.

### Concurrency tests (opt-in, needs a fake-mode worker)

`tests/integration/test_concurrency.py` contains 3 tests: a 20-document simultaneous burst that must
be fully accepted and settle, an API-responsiveness probe during that burst, and a mixed batch under
load. They are **skipped by default** (`RUN_CONCURRENCY_TESTS=1` to enable).

Why opt-in: the burst uploads ~20 documents at once. Against a live provider that is dozens of real
model calls, so the tests would be slow, cost quota, and could fail on the provider's rate limit
rather than on a defect. Run them against a disposable **fake-mode** worker:

```bash
# 1. Replace the normal worker with a disposable fake-mode one for the run
docker compose stop worker
docker compose run -d --name concurrency-w --no-deps \
  -e AI_MODE=fake -e FAKE_DELAY_SECONDS=0.05 -e WORKER_CONCURRENCY=4 \
  worker python -m app.worker

# 2. Run the opt-in suite (samples mount is still required)
docker compose --profile test run --rm \
  -e API_BASE_URL=http://api:8000 -e RUN_CONCURRENCY_TESTS=1 \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python -m pytest tests/integration/test_concurrency.py -q

# 3. Restore the normal worker
docker rm -f concurrency-w && docker compose start worker
```

Tunables (env vars, for a weaker machine): `TEST_BURST_SIZE` (default 20), `TEST_MIXED_LOAD_SIZE`
(default 8), `TEST_BURST_TIMEOUT` (default 180 s), `TEST_REQUEST_TIMEOUT` (default 60 s). The tests
use `parser=pypdf` only, so they never make a paid extraction call; the summary is the configured
summarizer (the labelled fake stand-in in fake mode). Recorded ledger result when opted in:
**3 passed**.

The default `test_api.py::TestConcurrency` test is different: it asserts that `/health/live` and a
status read stay under 2 s immediately after a `202`, for one queued job, without a burst.

### A single test or a subset

```bash
docker compose --profile test run --rm test python -m pytest tests/unit -q -k "SettingsValidation"
docker compose --profile test run --rm test python -m pytest tests/unit/test_contract.py::TestModels::test_terminal_statuses -q
```

Markers: `integration` is registered both in `pyproject.toml` and by `pytest_configure` in
`tests/conftest.py`. `pyproject.toml` sets `testpaths = ["tests"]`, so a bare `pytest -q` from the
repository root collects everything, including the integration tests (which need the stack) and the
opt-in concurrency/live suites (which skip themselves without `RUN_CONCURRENCY_TESTS=1` /
`RUN_LIVE_AI_TESTS=1`). The `test` Compose profile is what selects the offline-only subset.

## Fixtures and sample PDFs

Shared helpers in `tests/conftest.py` (no session-wide autouse fixture - this is what keeps the
unit suite offline):

| Helper / constant | Purpose |
|---|---|
| `API_BASE_URL` | `API_BASE_URL` env var, default `http://localhost:8000` |
| `sample(name)` | Path to a sample; skips the test with a clear message if the file is missing |
| `submit(client, files, parser, extra_forms=None)` | POST a multipart batch; returns the raw `httpx.Response` |
| `wait_for_terminal(client, id, timeout)` | Polls every 0.3 s until `completed`/`failed`; fails with the last body on timeout |
| `upload_and_wait(client, files, parser, timeout)` | `submit` + first document id + `wait_for_terminal` |
| `READY_TIMEOUT`, `JOB_TIMEOUT` | 60 s defaults, overridable via `TEST_READY_TIMEOUT` / `TEST_JOB_TIMEOUT` |
| `unique_name(prefix)` | Unique name helper (not used by the current tests) |

Integration-only fixtures in `tests/integration/conftest.py`: a session-scoped `httpx.Client`
(base URL from `API_BASE_URL`, 30 s timeout), the autouse `stack_ready` readiness gate, and
`ai_mode` (the `ai_mode` value reported by `/api/v1/parsers`).

Sample PDFs under `samples/` are generated deterministically by `scripts/make_samples.py`
(stdlib only, byte-identical on regeneration):

| File | Size here | Contents | Expected outcome |
|---|---|---|---|
| `samples/sample-text.pdf` | 1,253 bytes | 2 pages of real, distinct Helvetica text ("Piscina maintenance report - page 1/2") | `pypdf` -> `completed`, 2 ordered `text` pages, one summary |
| `samples/sample-blank.pdf` | 576 bytes | 1 page with an empty content stream (no text operators) | `pypdf` -> `failed`, `error.code = OCR_REQUIRED`, `retryable: false` |
| `samples/sample-corrupt.pdf` | 2,058 bytes | Not a PDF: no `%PDF-` header, deterministic filler bytes | rejected at upload with `code = NOT_A_PDF` (`422` when it is the only file) |

## Unit suite - what each test proves

All 28 unit tests live under `tests/unit/` and are dependency-free: no Redis, S3, network, or API
process. They prove the frozen wire contract, the settings validation and the provider-hardening
policy, i.e. the assumptions every other module depends on.

`tests/unit/test_contract.py` (14) - the frozen contract and settings:

`TestModels` (7):

| Test | Proves |
|---|---|
| `test_parser_ids_match_the_api` | `ParserId` values are exactly `pypdf`, `gemini`, `gpt-5-mini` - the upload form and the API enum cannot drift |
| `test_terminal_statuses` | Only `completed` and `failed` are terminal; `queued`, `parsing`, `summarizing`, `retrying` are not |
| `test_timestamp_is_utc_z` | `utc_now_iso()` matches `YYYY-MM-DDTHH:MM:SSZ` exactly |
| `test_page_number_is_one_based` | `PageResult(page_number=0)` raises a validation error |
| `test_page_formats_are_distinct` | `text` and `markdown` are distinct values and serialize as such |
| `test_document_detail_composes_job_and_result` | `DocumentDetail` = job fields + pages + summary; queued detail has empty pages and no error |
| `test_error_info_round_trips` | `ErrorInfo` JSON round-trip preserves `code`, `message`, `retryable` |

`TestSettingsValidation` (7):

| Test | Proves |
|---|---|
| `test_defaults_are_compose_happy` | Defaults target the Compose network (`redis://redis:6379`, `http://localstack:4566`, stream/group names) and heartbeat < lease |
| `test_heartbeat_must_be_lower_than_lease` | Cross-field validator rejects heartbeat >= lease |
| `test_lease_must_fit_the_deadline` | Rejects a lease longer than a third of the job deadline |
| `test_ai_pages_cannot_exceed_pages` | Rejects `MAX_AI_PAGES > MAX_PAGES` |
| `test_batch_must_admit_one_max_file` | Rejects `MAX_BATCH_BYTES < MAX_UPLOAD_BYTES` |
| `test_reclaim_idle_defaults_to_the_lease` | `RECLAIM_MIN_IDLE_MS=0` derives the lease length in ms |
| `test_ai_availability_by_mode` | `ai_available`: fake always true; live only with a key |

`tests/unit/test_hardening.py` (14) - regression tests for what the load/fault campaign found:

| Class | Tests | Proves |
|---|---|---|
| `TestRetryAfterParsing` (3) | header read as seconds; HTTP-date form ignored instead of guessed; absent header -> `None` | Provider `Retry-After` parsing is deliberate, not best-effort guessing |
| `TestProviderErrorMapping` (4) | 429 -> transient **carrying `retry_after`**; 503 -> transient; 401 -> permanent; an existing `ParserError` passes through unchanged | The provider failure taxonomy that the worker's retry policy depends on |
| `TestRetryDelayPolicy` (3) | provider `Retry-After` honoured; clamped by `RETRY_AFTER_MAX_SECONDS`; configured backoff grows and carries +/-20% jitter | A provider asking for ten minutes cannot hold a job past its deadline, and workers do not retry in lockstep |
| `TestSettingsForPacing` (2) | pacing defaults sane (`RPM=0` means disabled); `PROVIDER_MAX_CONCURRENCY=0` rejected | The new knobs cannot be configured into nonsense |
| `TestParserErrorContract` (2) | `retry_after` defaults to `None` and is preserved | The new field on the frozen error type round-trips |

Notably, the unit tests deliberately do not exercise parsers, Redis, S3, HTTP, or the timed token
bucket. Passing them means "the contract, configuration and retry policy are internally consistent",
not "the system works". In particular, the fix that a transient Redis error must never become a
terminal document failure has **no unit test** (it is covered by `scripts/resilience.sh redis`), and
the token bucket's actual pacing rate is not unit-tested.

## Integration suite - what each test proves

All 13 live in `tests/integration/test_api.py` and talk to the real stack over HTTP. They assert
outcomes (status codes, page ordering, stable error codes), not that a mock was called.

| Class | Test | Proves |
|---|---|---|
| `TestReadiness` | `test_live_is_cheap_and_ready_reports_dependencies` | `/health/live` is exactly `{"status":"ok"}`; `/health/ready` is 200 in a healthy stack, `checks.redis == "ok"`, `checks.s3` contains `ok`, `ai_mode` is `live` or `fake` |
| | `test_parsers_endpoint_only_advertises_what_works` | The parsers payload has a valid `ai_mode`, `pypdf` is enabled, and **every disabled parser carries a reason** (an unexplained disabled parser is a documentation lie) |
| `TestHappyPath` | `test_upload_completes_with_ordered_pages_and_one_summary` | Real end-to-end with `pypdf`: `completed`, `error` null, `page_count >= 2`, one page per number 1..N, every page `format: "text"` with non-empty content, exactly one non-empty summary, filename preserved |
| | `test_gemini_parser_returns_markdown_pages` | The `gemini` parser completes and returns `markdown` pages; in fake mode the document carries an explicit `ai_mode=fake` warning (the stand-in is labelled at the document level, not only in the UI) |
| `TestBatchIndependence` | `test_one_corrupt_file_does_not_block_the_valid_one` | A valid + corrupt batch returns `202`, one accepted and one rejected with `NOT_A_PDF`, and the valid document still completes |
| `TestValidation` | `test_unknown_parser_is_rejected` | `422` and the rejected parser name appears in the response |
| | `test_corrupt_file_only_is_rejected_with_a_reason` | `422` with `rejected[0].code == "NOT_A_PDF"` |
| | `test_no_files_is_rejected` | A request with only `parser` returns `422` |
| | `test_oversized_file_is_refused` | A 21 MB file with a valid `%PDF-` signature returns `413` (the size check, not the signature check) |
| | `test_unknown_document_id_is_404` | An all-zero UUID returns `404` |
| `TestFailureBehaviour` | `test_blank_pdf_fails_with_ocr_required_and_keeps_a_stable_code` | The blank sample ends `failed` with `error.code = "OCR_REQUIRED"`, `retryable: false`, and no `Traceback` in the client-visible message |
| `TestConcurrency` | `test_api_stays_responsive_while_a_job_is_queued` | Immediately after a `202`, `/health/live` and the status read each answer in under 2 s, and the job still completes within the timeout - the assessment's "serve concurrent API requests" requirement, at HTTP level |
| `TestIdempotentAcceptance` | `test_two_uploads_of_the_same_file_get_independent_ids` | Two uploads of the same bytes get two distinct ids and both complete |

The default integration run never calls a paid provider (the concurrency and live-AI tests are
skipped without their flags): in `AI_MODE=fake` the AI parsers are deterministic stand-ins; in live
mode the `gemini` test in this file would make real per-page calls, which is why the contract keeps
the default stack in fake mode for test runs.

## What is not covered

Stated plainly, because a gap that is not named reads as a claim.

- **No pytest test for crash recovery or fencing.** The three fault scenarios live in
  `scripts/resilience.sh` (SIGKILL recovery, Redis restart with a backlog, stale-writer fencing),
  not in the pytest suite: they are destructive, need a stack they can kill and restore, and run on
  a short lease. They pass (5/5, 3/3, 6/6 checks in the ledger) and are documented in
  [PERFORMANCE.md](PERFORMANCE.md), but `pytest` alone cannot prove recovery. There is also no
  unit-level test for the fenced-write rejection path (stale token -> `False`) or for `LeaseLost`.
- **The Redis-blip regression is not unit-tested.** The fix that a `RedisError` inside a commit must
  defer the job instead of failing it permanently is only exercised by the Redis-restart fault
  scenario; `tests/unit/test_hardening.py` covers the pacing/retry policy, not the worker's
  `RedisError` branch.
- **Load and performance are opt-in, not part of the default suite or CI.** `test_concurrency.py`
  is skipped unless `RUN_CONCURRENCY_TESTS=1` (3 tests, 20-document bursts, `pypdf` only), and
  `scripts/stress.py` is a manual gate. There is no CI job that runs either, no multiprocess worker
  scaling test (`WORKER_CONCURRENCY > 8` is untested), and the measured numbers come from one
  machine - treat the ratios as portable and the absolutes as indicative. There is no load test
  against real AWS.
- **No test for graceful drain.** SIGTERM draining (`worker_draining` -> finish or
  `worker_shutdown_incomplete`) is implemented, but the fault script tests SIGKILL and `docker
  pause`, not the shutdown path. The graceful path is reasoned about, not asserted.
- **No automated memory-bound test.** The upload buffering bound (`MAX_BATCH_BYTES` x in-flight
  requests) is measured manually in [PERFORMANCE.md](PERFORMANCE.md); nothing asserts it.
- **The pacing layers are not behaviourally tested.** Retry-After parsing/clamping and the retry
  delay are unit-tested, but the token bucket's actual requests-per-minute rate and the interaction
  between `PROVIDER_MAX_CONCURRENCY`, `WORKER_CONCURRENCY` and the provider's limit are only
  evidenced by the manual live run (unpaced failures vs 20/20 paced).
- **No browser-level UI test.** The UI was verified over HTTP only (page, JS bundle, CSS return
  `200`; the `/api` proxy works). Layout, interaction, and polling behaviour in a real browser are
  unconfirmed (gap G3 in [REQUIREMENTS.md](REQUIREMENTS.md)).
- **No automated dependency-outage tests** in `pytest`. `STORAGE_UNAVAILABLE`,
  `QUEUE_UNAVAILABLE`, `503` readiness shapes, and Redis/S3 connection loss are not exercised by the
  default suite; the Redis-restart case is covered by the destructive `scripts/resilience.sh redis`
  scenario instead.
- **Provider-error mapping is only partly tested.** `tests/unit/test_hardening.py` covers
  `Retry-After` parsing, 429/503/401 mapping and the retry-delay policy offline; the SDK-facing
  request/extraction code still has no tests, and the token bucket/timed pacing has none.
- **No parser-level unit tests for error paths.** Encrypted PDFs (`ENCRYPTED_PDF`), page limits
  (`TOO_MANY_PAGES`, `TOO_MANY_PAGES_FOR_AI`), text limits (`TOO_MUCH_TEXT`), invalid PDFs
  (`INVALID_PDF`), and page-splitting failures are implemented but not covered by a test. Only the
  `OCR_REQUIRED` path (blank sample) is asserted end to end.
- **No test for `gpt-5-mini` beyond advertisement.** The registry now disables it unconditionally
  (the `IMPLEMENTED = False` check runs before the fake-mode branch), and the integration suite
  asserts that disabled parsers carry a reason; the parsing fallback error
  (`PROVIDER_NOT_IMPLEMENTED`) is not reachable and not tested.
- **No automated Terraform tests.** The infra tree is checked manually with
  `terraform fmt -check -recursive` and `terraform validate` for both `infra/envs/local` and
  `infra/envs/prod` (verified clean in this delivery), but there is no tflint/terraform-plan test,
  no CI job, and no automated drift test. The local apply and drift healing were verified manually
  through `docker compose up`; the production environment was validated only, never applied.
  Terraform is not part of the pytest suite at all.
- **No test for batch-size `413` (`MAX_BATCH_FILES`).** The single-file oversize path is covered;
  the too-many-files path is not.
- **No end-to-end retry-state test.** The retry-delay policy is unit-tested, but nothing asserts
  that `status: "retrying"` is observable between attempts, or drives a document to
  `ATTEMPTS_EXHAUSTED` / `DEADLINE_EXCEEDED`.
- **No test for the 24 h result TTL expiry** (it would need a clock/time-travel or a shortened
  configuration).
- **Live provider behaviour only when a key is present.** Without a key the live test is skipped,
  never passed; the suite cannot prove Gemini works in an environment with no credentials.
- **No automated check that logs contain no document content, filenames, or secrets** - that is
  enforced by code review and by the logging conventions, not by a test.
- **Integration tests are black-box HTTP, not the contract's "own key prefix and bucket".**
  [CONTRACT.md](CONTRACT.md) section 9 says integration tests must use their own key prefix/bucket
  and never flush; the delivered tests never touch Redis directly and never flush, but because they
  drive the public API they naturally create documents in the application's own namespace and
  bucket (they expire via the result TTL). The "never flush" half holds; the "own namespace" half is
  not applicable to an HTTP-level suite.

## Manual end-to-end check

`scripts/smoke.sh` is the reviewer-facing walkthrough (bash + curl only). It waits for readiness,
uploads `samples/sample-text.pdf` with `pypdf`, polls to terminal, and asserts: `completed`,
`page_count >= 2`, pages ordered exactly 1..N, every page `format: "text"` and non-empty, exactly
one non-empty summary; then it uploads the corrupt sample and asserts a `422` (or `202` with a
`rejected[]`). Recorded ledger result: `Smoke test passed`. It is safe to re-run: it makes HTTP
calls only and never mutates or flushes state.

### Load harness - `scripts/stress.py`

`scripts/stress.py` is the load harness (a manual gate, not part of pytest). It sends exactly
`--docs` uploads through `--concurrency` parallel requests, polls every accepted document to a
terminal state, and exits non-zero if any upload was not `202`, any 5xx occurred, any file was
rejected, or any document failed or timed out. It needs `httpx`, so run it in the test image with
`scripts/` and `samples/` mounted (both are dockerignored):

```bash
docker compose --profile test run --rm \
  -v "$(pwd)/scripts:/app/scripts:ro" \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python /app/scripts/stress.py --docs 200 --concurrency 200 --label burst
```

Useful flags: `--docs`, `--concurrency`, `--parser pypdf|gemini`, `--file`, `--poll-timeout`,
`--request-timeout`, `--json-out PATH`, `--label`. It prints a one-line machine-readable summary
prefixed `JSON ` (and writes the same object with `--json-out`), so it can be grepped as a gate.
Exit codes: `0` clean, `1` failed run (reason on the `RESULT:` line), `2` CLI misuse.

Throughput runs are repeatable when they hit a disposable fake-mode worker with a fixed
`FAKE_DELAY_SECONDS`; the recipe is in [PERFORMANCE.md](PERFORMANCE.md) section 1:
`docker compose stop worker`, then
`docker compose run -d --name stress-w --no-deps -e AI_MODE=fake -e FAKE_DELAY_SECONDS=0.2 -e WORKER_CONCURRENCY=8 worker python -m app.worker`,
run the harness, then `docker rm -f stress-w && docker compose start worker`.

Recorded campaign numbers (coordinator-run; method in [PERFORMANCE.md](PERFORMANCE.md)): worker
parallelism 1/4/8 -> 4.22/16.32/22.99 settled docs/s; client concurrency 1/10/50/100 -> 0 errors
with p95 upload 9/88/651/802 ms; 200 concurrent uploads -> 200/200 accepted, 0 errors, 31.0 docs/s;
50 x 200 KB all completed and 10 x 1 MB failed cleanly with `TOO_MUCH_TEXT` and 0 HTTP errors.

### Fault injection - `scripts/resilience.sh`

`scripts/resilience.sh` runs three destructive scenarios against a stack you can recreate: `crash`
(SIGKILL a worker mid-job; a replacement must reclaim and finish it), `redis` (restart Redis with a
30-document backlog; nothing may be lost) and `fencing` (`docker pause` a worker past its lease, let
another worker finish, then unpause; the stale writer must not commit). `all` runs the three.

```bash
WORKER_MODE=fake ./scripts/resilience.sh all      # costs no API quota; recommended
./scripts/resilience.sh crash                     # one scenario only
```

- `WORKER_MODE=fake` starts disposable fake-mode workers; the default is `live`, which uses the real
  providers. `LEASE_SECONDS` (default 15) / `HEARTBEAT_SECONDS` (default 5) make recovery observable
  in seconds; `RECOVERY_TIMEOUT` (default 180 s) bounds each check. It needs docker + curl and no jq,
  restores the normal worker on exit via a trap, and exits non-zero if any check fails - so it is
  usable as a gate.
- Recorded results (coordinator-run): crash 5/5, redis 3/3 (30/30 completed, 0 lost), fencing 6/6
  (the resumed zombie detected its lost lease and could not overwrite the completed document). This
  campaign is what found the transient-Redis-error classification bug, now fixed. See
  [PERFORMANCE.md](PERFORMANCE.md) section 5.

These two harnesses were **not re-run during this documentation pass** (destructive/load); their
results are the coordinator's, recorded in [PROGRESS.md](PROGRESS.md) and
[PERFORMANCE.md](PERFORMANCE.md).

### Standalone delivery check (clean extraction) - verified

Verified by the coordinator in this session from a **clean extraction of the deliverable archive**
into a temporary directory outside the repository, with no `.env` file and `AI_MODE=fake` supplied
through the Compose environment passthrough:

- `docker compose up --build -d` ran the one-shot Terraform `provisioner` first (`3 added,
  0 changed, 0 destroyed`), then brought up all five long-running services
  (api/worker/frontend/redis/localstack); `api` reported healthy.
- `GET /health/ready` returned `200` with `redis: ok`, `bucket 'documents' ok`,
  `summarizer: fake summarizer`, `ai_mode: fake`.
- The S3 bucket (plus its public-access block and versioning) was created by Terraform before
  api/worker started.
- A `parser=pypdf` upload completed with 2 pages, `format: text`.
- A `parser=gemini` upload completed with 2 pages, `format: markdown`, with the fake warning
  present.
- The UI served `200` on `/` and on `/api/v1/parsers`.

Terraform static checks verified during this documentation pass with a direct `docker run` of the
pinned image against the working tree: `terraform fmt -check -recursive` -> clean; `terraform
validate` for both `infra/envs/local` and `infra/envs/prod` -> `Success! The configuration is
valid.` Drift healing (`down -v` with state intact -> bucket re-created, readiness `200`) was
verified by the coordinator. The production environment is validated only, not applied.

Documented defaults on a clean host were also verified by the coordinator: after removing 18 stale
containers from unrelated projects (no volumes touched), the stack ran on its documented defaults -
LocalStack on host port `4566`, readiness `200`, and an upload completed. `S3_BUCKET` was verified
end to end in both directions: `S3_BUCKET=pdf-docs-custom` re-provisioned the bucket
(`3 added, 0 changed, 3 destroyed`), readiness reported `bucket 'pdf-docs-custom' ok`, and reverting
to the default did the same in reverse. That is a rename (destroy + recreate), not a migration.

This is the reviewer-machine check: only Docker + Compose v2 required. Terraform runs inside a
container, so there is no Terraform install, no AWS account and no cloud credentials for the local
demo, and no local Python/Node install.

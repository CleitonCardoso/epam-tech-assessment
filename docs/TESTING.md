# Testing

What is tested, how to run each layer, what each layer actually proves, and - explicitly - what is
not covered by any automated test.

## Inventory

**82 backend checks run by default** (44 unit + 16 API integration + 14 repository/lease + 8 proxy
e2e), **4 opt-in** backend tests (3 concurrency + 1 live AI), and **29 frontend vitest tests**.

| Suite | File | Tests | Runs by default | Needs |
|---|---|---|---|---|
| Unit (contract) | `tests/unit/test_contract.py` | 14 | yes | nothing |
| Unit (hardening) | `tests/unit/test_hardening.py` | 17 | yes | nothing |
| Unit (GPT-5 Mini adapter/parser) | `tests/unit/test_openai_parser.py` | 13 | yes | nothing (stubbed SDK) |
| API integration | `tests/integration/test_api.py` | 16 | yes | running stack |
| Repository/lease | `tests/integration/test_repository.py` | 14 | yes (separate CI job) | **Redis only** |
| Proxy e2e | `tests/e2e/test_proxy.py` | 8 | yes (separate CI job) | stack incl. frontend |
| Concurrency (opt-in) | `tests/integration/test_concurrency.py` | 3 | no - `RUN_CONCURRENCY_TESTS=1` | stack + disposable fake-mode worker |
| Live AI (opt-in) | `tests/integration/test_live_ai.py` | 1 | no - `RUN_LIVE_AI_TESTS=1` | `AI_MODE=live` + real `GEMINI_API_KEY` |
| Frontend unit/component | `frontend/src/*.test.ts(x)` | 29 | yes (`npm test`, CI) | Node 22 + `npm ci` |

Verified first-hand in this documentation pass: unit suite **44 passed, 42 deselected in 2.01 s**;
collection counts **16 API / 14 repository / 8 e2e / 3 concurrency / 1 live**; the repository/lease
suite ran against the live Redis and passed **14/14 in 0.69 s**; frontend test files contain **29**
`it(...)` cases (the vitest suite was not executed here - Node 22 is present but `node_modules` is
not installed in this checkout). Quality gates were also run against the working tree with the dev
image: `ruff check .` -> "All checks passed!", `ruff format --check` -> "52 files already
formatted", `mypy` -> "no issues found in 24 source files", and `scripts/check_encoding.py` ->
all text files valid UTF-8. The verification ledger (what was actually run, and what was skipped)
is [PROGRESS.md](PROGRESS.md); measurements from the load/fault campaign are in
[PERFORMANCE.md](PERFORMANCE.md).

## Running the suites

### Unit suite (offline, no stack)

The Compose `test` profile is the supported path: same image with `pytest`, `tests/` and
`pyproject.toml` mounted read-only. Its baked-in command is:

```bash
docker compose --profile test run --rm test
# equivalent to: python -m pytest -q -m "not integration"
```

That excludes repository/e2e/concurrency/live tests because all of them also carry the
`integration` marker. It needs no Redis, S3, HTTP or provider.

The same suite can be run directly with the test image (this is what was **executed** in this
pass, against the working-tree `app/` and `tests/` mounted read-only over the image):

```powershell
$root = (Get-Location).Path.Replace('\','/')
docker run --rm --network none `
  -v "${root}/tests:/app/tests:ro" `
  -v "${root}/app:/app/app:ro" `
  -v "${root}/pyproject.toml:/app/pyproject.toml:ro" `
  epam-tech-assessment-test python -m pytest -q -m "not integration"
```

Observed: **44 passed, 42 deselected in 2.01 s** (42 = 16 API + 14 repository + 8 e2e + 3
concurrency + 1 live). Mounting `app/` and `tests/` was deliberate: the built image can be older
than the working tree (file-synced build-context caveat), and a unit run should test the code you are
looking at.

The host has `py -3` = Python 3.14.0b2 with **no pytest installed**, so `py -3 -m pytest tests/unit`
was **not executed**. Tests normally run in the container (Python 3.12).

### API integration suite (running stack required)

```bash
docker compose up -d
docker compose --profile test run --rm \
  -e API_BASE_URL=http://api:8000 \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python -m pytest tests/integration/test_api.py -m integration -q
```

- `API_BASE_URL=http://api:8000` is the Compose-network name; the default in `tests/conftest.py`
  is `http://localhost:8000` for host-side runs.
- The `samples/` mount is required: `samples/` is dockerignored, so the image has no fixtures, and
  `tests/conftest.py` resolves them as `/app/samples`. Without it tests that need a sample skip
  with a clear message instead of failing.
- The readiness wait lives in the session `client` fixture (not an autouse fixture), so a suite
  that does not request `client` - the repository suite - does not wait for the API.
- `docker compose up -d` also runs the one-shot Terraform `provisioner`; api/worker are gated on it,
  so a first `up` takes a minute or two longer. If readiness never comes up, check
  `docker compose logs provisioner` first.

### Repository/lease suite (Redis only)

The fenced-commit heart, tested against a real Redis with per-run stream keys, a short lease and
`reclaim_min_idle_ms`; it never flushes the database.

```bash
docker compose up -d redis
docker compose --profile test run --rm \
  -e REDIS_URL=redis://redis:6379/0 \
  test python -m pytest tests/integration/test_repository.py -m repository -q
```

Observed in this pass with the current tree: **14 passed in 0.69 s**.

### Proxy e2e suite (stack incl. frontend)

```bash
docker compose --profile test run --rm \
  -e FRONTEND_URL=http://frontend:80 \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python -m pytest tests/e2e -m e2e -q
```

`FRONTEND_URL` defaults to `http://frontend:80`; the suite fails with a clear message if nginx is
not reachable. Recorded ledger result: **8 passed in 8.46 s**.

### Concurrency tests (opt-in, fake-mode worker)

3 tests: a 20-document simultaneous burst that must all be accepted and settle, API responsiveness
during that burst, and a mixed batch under load. Skipped unless `RUN_CONCURRENCY_TESTS=1`.

Why opt-in: the burst uploads ~20 documents at once; against a live provider that is dozens of real
model calls, so the tests would be slow, cost quota, and could fail on the provider's rate limit
rather than on a defect. Use a disposable fake-mode worker:

```bash
docker compose stop worker
docker compose run -d --name concurrency-w --no-deps \
  -e AI_MODE=fake -e FAKE_DELAY_SECONDS=0.05 -e WORKER_CONCURRENCY=4 \
  worker python -m app.worker

docker compose --profile test run --rm \
  -e API_BASE_URL=http://api:8000 -e RUN_CONCURRENCY_TESTS=1 \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python -m pytest tests/integration/test_concurrency.py -q

docker rm -f concurrency-w && docker compose start worker
```

Tunables: `TEST_BURST_SIZE` (20), `TEST_MIXED_LOAD_SIZE` (8), `TEST_BURST_TIMEOUT` (180 s),
`TEST_REQUEST_TIMEOUT` (60 s). `parser=pypdf` only. Recorded: **3 passed** when opted in.

### Opt-in live AI test

```bash
# stack must be in AI_MODE=live with a real GEMINI_API_KEY in .env
docker compose up -d --force-recreate api worker
docker compose --profile test run --rm \
  -e API_BASE_URL=http://api:8000 -e RUN_LIVE_AI_TESTS=1 \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python -m pytest tests/integration/test_live_ai.py -q
```

Skipped by default and reported as skipped, never passed. If the flag is set but the stack is not
in live mode, the test **fails** with the observed `ai_mode`. With a real key it asserts: completed
document, >= 2 pages, every page `markdown`, no `ai_mode=fake` warning, non-empty summary that does
not say "fake mode", and page 1 content containing "page 1" and not "fake markdown". Recorded:
**1 passed in 12.58 s**.

### Frontend tests (vitest)

```bash
cd frontend
npm ci
npm test          # vitest run: 29 tests
npx tsc --noEmit  # type-check
npm run build
```

Covered: API error-body parsing and list-envelope parsing (`api.test.ts`), polling cadence/backoff
(`polling.test.ts`), rehydrating and merging recent documents (`recentDocuments.test.ts`), status
labels/terminality (`status.test.ts`), and the App's reload-recovery behaviour (`App.test.tsx`).
The suite was not executed in this documentation pass (no `node_modules` here); CI runs it.

### A single test or a subset

```bash
docker compose --profile test run --rm test python -m pytest tests/unit -q -k "SettingsValidation"
docker compose --profile test run --rm test python -m pytest tests/unit/test_contract.py::TestModels::test_terminal_statuses -q
```

Markers: `integration`, `e2e`, `repository` (registered in `pyproject.toml` and by
`pytest_configure` in `tests/conftest.py`). `asyncio_mode = "auto"` - the repository suite is async
because the Redis client is loop-bound, and tests need no per-test decorator. A bare `pytest -q`
collects everything including the opt-in suites (which skip without their env flags).

## Fixtures and sample PDFs

Shared helpers in `tests/conftest.py` (no session-wide autouse fixture - this keeps the unit suite
offline):

| Helper / constant | Purpose |
|---|---|
| `API_BASE_URL` | `API_BASE_URL` env var, default `http://localhost:8000` |
| `sample(name)` | Path to a sample; skips with a clear message if missing |
| `submit(client, files, parser, extra_forms=None)` | POST a multipart batch |
| `wait_for_terminal(client, id, timeout)` | Polls every 0.3 s until terminal; fails with the last body |
| `upload_and_wait(client, files, parser, timeout)` | `submit` + first id + `wait_for_terminal` |
| `READY_TIMEOUT`, `JOB_TIMEOUT` | 60 s defaults, overridable via `TEST_READY_TIMEOUT` / `TEST_JOB_TIMEOUT` |
| `unique_name(prefix)` | Unique name helper (not used by current tests) |

Integration fixtures in `tests/integration/conftest.py`: a session `httpx.Client` whose fixture
performs the readiness wait, and `ai_mode`. The repository suite builds its own `Redis` client and
`Settings` per test with unique stream keys.

Sample PDFs under `samples/` are generated deterministically by `scripts/make_samples.py`
(stdlib only, byte-identical regeneration):

| File | Size here | Contents | Expected outcome |
|---|---|---|---|
| `samples/sample-text.pdf` | 1,253 bytes | 2 pages of real, distinct Helvetica text ("Piscina maintenance report - page 1/2") | `pypdf` -> `completed`, 2 ordered `text` pages, one summary |
| `samples/sample-blank.pdf` | 576 bytes | 1 page with an empty content stream (no text operators) | `pypdf` -> `failed`, `error.code = OCR_REQUIRED`, `retryable: false` |
| `samples/sample-corrupt.pdf` | 2,058 bytes | Not a PDF: no `%PDF-` header, deterministic filler bytes | rejected at upload with `code = NOT_A_PDF` (`422` when it is the only file) |

## Unit suite - what each test proves

All 44 unit tests live under `tests/unit/` and are dependency-free: no Redis, S3, network, API
process, or provider. They prove the wire contract, settings validation, retry/pacing policy, and
the OpenAI adapter's request/error surface against a stub.

### `test_contract.py` (14) - frozen contract and settings

`TestModels` (7): `ParserId` values match the API; only `completed`/`failed` are terminal; UTC-Z
timestamp format; `page_number` is 1-based; `text` vs `markdown` are distinct; `DocumentDetail`
composes job + pages + summary; `ErrorInfo` round-trips.

`TestSettingsValidation` (7): Compose-happy defaults (heartbeat < lease); heartbeat must be lower
than lease; lease must fit the deadline; AI pages cannot exceed pages; batch must admit one max
file; reclaim idle defaults to the lease; `ai_available` semantics per mode.

### `test_hardening.py` (17) - regression tests from the load/fault campaign

| Class | Tests | Proves |
|---|---|---|
| `TestRetryAfterParsing` (3) | header read as seconds; HTTP-date form ignored instead of guessed; absent -> `None` | `Retry-After` parsing is deliberate, not best-effort |
| `TestProviderErrorMapping` (4) | 429 -> transient carrying `retry_after`; 503 -> transient; 401 -> permanent; existing `ParserError` passes through | The provider failure taxonomy the retry policy depends on |
| `TestRetryDelayPolicy` (3) | `Retry-After` honoured, clamped by `RETRY_AFTER_MAX_SECONDS`, backoff grows with +/-20% jitter | A provider asking for ten minutes cannot hold a job past its deadline; workers do not retry in lockstep |
| `TestSettingsForPacing` (2) | pacing defaults sane (`RPM=0` disabled); `PROVIDER_MAX_CONCURRENCY=0` rejected | The knobs cannot be configured into nonsense |
| `TestParserErrorContract` (2) | `retry_after` defaults to `None` and is preserved | The new field on the frozen error type round-trips |
| `TestAdmissionControlSettings` (3) | `MAX_CONCURRENT_UPLOADS >= 1`; `UPLOAD_SLOT_TIMEOUT_SECONDS > 0`; defaults bound memory (`16 x 50 MB` worst case) | Admission knobs are validated and conservative by default |

### `test_openai_parser.py` (13) - GPT-5 Mini adapter against a stub

| Class | Tests | Proves |
|---|---|---|
| `TestRequestPayload` (2) | one inline PDF page per Responses-API request (base64 `data:application/pdf` in `input_file` + `input_text`); an empty provider answer becomes a permanent `PROVIDER_EMPTY_RESPONSE` | The exact request shape and the empty-output rule, without a key |
| `TestErrorMapping` (5) | 429 transient + `retry_after`; 5xx transient; 401 permanent `PROVIDER_CONFIG`; unknown model permanent; HTTP-date `Retry-After` ignored | OpenAI failures land in the same `TransientError`/`PermanentError` vocabulary as Gemini |
| `TestParserContract` (6) | one call per page, markdown, in order; `(no extractable text)` -> empty page + warning; no text at all -> `OCR_REQUIRED`; transient propagates for a retry; a permanent page failure names the page; `MAX_AI_PAGES` is enforced before any call | The parser satisfies the frozen `Parser` contract and shares the splitter's limits |

The OpenAI adapter has **never made a live call**: no OpenAI credentials exist here. The unit
tests pin the payload and mapping against a stub; the registry keeps the parser disabled until
`OPENAI_API_KEY` is set (and, in live mode, summarization also needs `GEMINI_API_KEY`).

## API integration suite - what each test proves

All 16 live in `tests/integration/test_api.py` and talk to the real stack over HTTP. They assert
outcomes (status codes, page ordering, stable error codes), not that a mock was called.

| Class | Test | Proves |
|---|---|---|
| `TestReadiness` (2) | `/health/live` exactly `{"status":"ok"}`; `/health/ready` reports dependencies; `/api/v1/parsers` only advertises working parsers and every disabled parser carries a reason | Readiness and the honesty rule of the parser list |
| `TestHappyPath` (2) | `pypdf` upload -> completed, ordered `text` pages 1..N, one non-empty summary; `gemini` -> `markdown`, and fake mode is labelled in the document warnings | The core upload -> poll -> completed path for both parser types |
| `TestBatchIndependence` (1) | valid + corrupt batch -> 202, one accepted, one rejected with `NOT_A_PDF`, accepted one completes | A bad file does not block a good one |
| `TestValidation` (5) | unknown parser 422; corrupt-only 422 with reason; no files 422; 21 MB file 413; unknown id 404 | The documented rejection contract |
| `TestFailureBehaviour` (1) | blank PDF -> `failed`, `error.code = OCR_REQUIRED`, `retryable: false`, no `Traceback` in the message | A stable, safe failure shape |
| `TestConcurrency` (1) | immediately after a 202, health and status reads answer in under 2 s and the job still completes | The API is not blocked by work |
| `TestIdempotentAcceptance` (1) | two uploads of the same bytes get two ids and both complete | Independent documents |
| `TestListing` (3) | recent documents are listed newest first; the `before` cursor paginates without repeats; `limit` is bounded (`0`/`101` -> 422) | The refresh-recovery endpoint and its cursor |

The default API run never calls a paid provider (concurrency and live tests are skipped without
their flags): in `AI_MODE=fake` the AI parsers are deterministic stand-ins; in live mode the
`gemini` happy-path test would make real per-page calls, which is why CI runs fake mode.

## Repository/lease suite - what each test proves

14 tests in 5 classes against a real Redis; this is the automated version of what used to be
exercised only by `scripts/resilience.sh`.

| Class | Proves |
|---|---|
| `TestEnqueueAndRead` | acceptance writes job hash + stream entry + `docs:index` together; reads round-trip the hash, including the null encodings; `pending()` reports the right counts (the regression that crashed it) |
| `TestLease` | the lease is exclusive (`SET NX`), a second owner is refused, renewal is token-fenced, release/expiry behave |
| `TestFencedWrites` | `set_stage` and `save_pages` are rejected with a stale token, so a worker that lost the lease cannot mutate state |
| `TestFencedCommit` | terminal commits are fenced by token, `XACK`+`XDEL` happen atomically with the state write, TTLs are applied, and a failed summary keeps the pages |
| `TestRecoveryMechanics` | checkpoint/resume skips extraction, and `XAUTOCLAIM`-style reclaim can take over a pending entry after the idle threshold |

Run command and prerequisites are above. Observed in this pass: **14 passed**.

## Proxy e2e suite - what each test proves

8 tests in 3 classes through nginx (`tests/e2e/test_proxy.py`); this is the path a browser actually
takes.

| Class | Proves |
|---|---|
| `TestShell` (3) | the SPA shell is served; every referenced hashed asset loads; an unknown SPA route falls back to `index.html` |
| `TestApiThroughProxy` (4) | `/health` and `/api/v1/parsers` are proxied as JSON; an unknown document is a JSON `404` and does **not** fall through to the SPA; a full upload -> poll -> result works through nginx |
| `TestProxyLimits` (1) | a 21 MB body is refused with `413` by nginx or the API (the 25 MB proxy cap / 20 MB API cap), not buffered and accepted |

## Concurrency and live suites

The 3 concurrency tests are described with their commands above. The 1 live-AI test is the only
test that can call a paid provider; it is skipped without both a live stack and
`RUN_LIVE_AI_TESTS=1`. Neither runs in CI - CI has no provider credentials and runs `AI_MODE=fake`.

## Frontend suite - what each test proves

29 vitest cases under `frontend/src/`:

- `api.test.ts` (8): error-body flattening (FastAPI `detail[]`, plain `detail`, non-JSON, transport
  errors) and parsing of the `GET /api/v1/documents` envelope + `before` cursor.
- `polling.test.ts` (6): 1.5 s cadence, 3 s doubling backoff capped and never decreasing, terminal
  statuses stop the chain, only 404/410 are fatal.
- `recentDocuments.test.ts` (7): mapping a `DocumentSummary` onto card state, order preservation,
  malformed-row skipping, polling only non-terminal rehydrated cards, merge behaviour.
- `status.test.ts` (5): a label and CSS class for every wire status, only `completed`/`failed`
  terminal, unknown statuses rejected.
- `App.test.tsx` (3): the page rebuilds cards from `GET /api/v1/documents` on load, shows the
  Recent documents heading, and degrades quietly when the list request fails.

These are component/unit tests, not a browser test: layout and real interaction are still not
automated.

## What is not covered

Stated plainly, because a gap that is not named reads as a claim.

- **No process-level fault automation in pytest.** The SIGKILL/Redis-restart/fencing scenarios are
  `scripts/resilience.sh` (destructive shell, needs a disposable stack), not a pytest suite. The
  repository suite covers the fencing *mechanics* in-process against real Redis, which is strong,
  but nothing in `pytest` kills a worker process and watches a replacement reclaim it. The script
  passed (5/5, 3/3, 6/6 checks) and found the transient-Redis-error bug.
- **No live OpenAI call.** The adapter is implemented and unit-tested against a stub; a real
  Responses-API call has never been made here (no `OPENAI_API_KEY`). Its live behaviour, latency
  and error shapes beyond the stub's are unverified.
- **No live provider in CI.** CI runs `AI_MODE=fake`; the live Gemini test is opt-in on a
  developer machine and was verified there once (1 passed), not on every commit.
- **Rate limiting and admission control are not asserted by a test.** The e2e suite tests the
  25 MB body limit, not `limit_req`. The 429s and admission 503s are configuration plus manual
  measurements (64 x 429 for a 150-request burst in the ledger; 90/200 in a documentation-pass
  repeat). A runaway client hitting the limiter is not in the automatic suite.
- **No queue-depth/admission load test in the default suite.** `scripts/stress.py` is a manual gate;
  the concurrency tests are opt-in and only assert settlement, not the ceiling.
- **No browser-level UI test.** The UI has vitest component tests and the 8 proxy e2e tests, but no
  Playwright/Selenium run; layout and interaction are still unverified in a real browser
  (REQUIREMENTS G3). The page was never opened visually here.
- **No test of the `docs:index` retention window.** Listing order, pagination and `limit` bounds are
  tested; pruning at 24 h and skipping expired hashes are not (they would need time travel or a
  shortened TTL).
- **No test for graceful drain (`worker_draining` / `worker_shutdown_incomplete`).** The fault
  script covers SIGKILL and `docker pause`, not SIGTERM draining.
- **No memory-bound assertion.** The upload-buffer ceiling is documented and measured manually, not
  asserted.
- **Pacing timing is not unit-tested.** Retry-After/clamp/jitter and the settings are covered; the
  token bucket's actual RPM and the interaction of parser+summarizer pacers are evidenced only by
  the manual live run.
- **No parser error-path integration tests for PyPDF/Gemini.** Blank/`OCR_REQUIRED` is covered end
  to end; encrypted PDFs, page limits, text limits and corrupt PDFs are unit/parser-level behaviour
  without dedicated integration tests. The OpenAI parser's own error paths are unit-tested.
- **No automated Terraform tests.** `fmt -check` and `validate` run in CI; `plan`/`apply` do not,
  and production has never been applied.
- **Integration tests are black-box HTTP, not the contract's "own key prefix and bucket".** They
  drive the public API, create real documents, and never flush; the repository suite uses its own
  unique stream keys. The "never flush" half holds.
- **No automated check that logs contain no document content, filenames, or secrets** - enforced by
  review and convention. The encoding gate and the CI secret grep cover adjacent risks, not log
  content.
- **No real-AWS load test.** All performance numbers come from one machine with LocalStack; treat
  ratios as portable and absolutes as indicative.

## Manual end-to-end check

`scripts/smoke.sh` is the reviewer-facing walkthrough (bash + curl only). It waits for readiness,
uploads `samples/sample-text.pdf` with `pypdf`, polls to terminal, and asserts: `completed`,
`page_count >= 2`, pages ordered exactly 1..N, every page `format: "text"` and non-empty, exactly
one non-empty summary; then it uploads the corrupt sample and asserts a `422` (or `202` with a
`rejected[]`). Recorded ledger result: `Smoke test passed`. It only makes HTTP calls and never
mutates or flushes state.

### Load and fault harnesses

- `scripts/stress.py` - bounded concurrent uploads, polls to terminal, exits non-zero on any
  failure. Run in the test image with `scripts/` and `samples/` mounted; flags `--docs`,
  `--concurrency`, `--parser`, `--poll-timeout`, `--request-timeout`, `--json-out`, `--label`.
  Commands and the recorded numbers are in [RUNBOOK.md](RUNBOOK.md) section 11 and
  [PERFORMANCE.md](PERFORMANCE.md).
- `scripts/resilience.sh` - **destructive** fault injection (`crash`, `redis`, `fencing`, `all`);
  prefer `WORKER_MODE=fake`; recorded 5/5, 3/3, 6/6. It exits non-zero on failure.

Neither harness was re-run during this documentation pass (destructive/load); their results are the
coordinator's, recorded in [PROGRESS.md](PROGRESS.md) and [PERFORMANCE.md](PERFORMANCE.md).

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
pinned image against the working tree: `terraform fmt -check -recursive` -> clean;
`terraform validate` for both `infra/envs/local` and `infra/envs/prod` -> `Success! The
configuration is valid.` Drift healing (`down -v` with state intact -> bucket re-created, readiness
`200`) was verified by the coordinator. The production environment is validated only, not applied.

Documented defaults on a clean host were also verified by the coordinator: after removing 18 stale
containers from unrelated projects (no volumes touched), the stack ran on its documented defaults -
LocalStack on host port `4566`, readiness `200`, and an upload completed. `S3_BUCKET` was verified
end to end in both directions: `S3_BUCKET=pdf-docs-custom` re-provisioned the bucket
(`3 added, 0 changed, 3 destroyed`), readiness reported `bucket 'pdf-docs-custom' ok`, and reverting
to the default did the same in reverse. That is a rename (destroy + recreate), not a migration.

This is the reviewer-machine check: only Docker + Compose v2 required. Terraform runs inside a
container, so there is no Terraform install, no AWS account and no cloud credentials for the local
demo, and no local Python/Node install.

## Quality gates

The quality gates are not part of `pytest`: `ruff check`, `ruff format --check`, `mypy`, the
UTF-8 encoding check (`scripts/check_encoding.py`), the CI secret grep, the frontend `tsc`/`npm
test`/`npm run build`, and Terraform `fmt`/`validate` all run in `.github/workflows/ci.yml`. The
commands to reproduce them locally are in [RUNBOOK.md](RUNBOOK.md) section 12. Dependency builds
use the committed `requirements.lock.txt` / `requirements-dev.lock.txt` (generated with `pip
freeze` from the verified image); `requirements*.txt` are the human-edited intent.

# Developing

Guide for changing this codebase: where each responsibility lives, the conventions it follows,
how to add a parser, how to change a limit safely, and how to add an endpoint.

`docs/CONTRACT.md` is the frozen interface document. If a change touches a frozen file (settings,
models, parser protocols, the HTTP API, the Redis schema), coordinate it before editing - several
components were built against those interfaces in parallel. Everything here assumes that rule.

## 1. Module responsibilities

| Path | Responsibility | May import | Notes |
|---|---|---|---|
| `app/config.py` | `Settings` (pydantic-settings), defaults, cross-field validation, `get_settings()` (cached) | pydantic only | Every field is an env var of the same name upper-cased. Single source of truth for limits. |
| `app/models.py` | Wire + internal models, enums, `utc_now_iso()` | pydantic only | Must not import `app.api`, `app.storage`, `app.queue`, or `app.processing`. Shared by both processes. |
| `app/parsers/base.py` | `Parser`/`Summarizer` protocols, `ParseLimits`, `ParseResult`, `ParserError`/`PermanentError`/`TransientError`/`OcrRequiredError`, `validate_pages` | `app.models` | Frozen contract. The worker only talks to these types. |
| `app/parsers/pypdf_parser.py` | Real PyPDF text extraction, off the event loop | pypdf, base, models | Returns `PageFormat.TEXT`; empty pages are kept to preserve numbering. |
| `app/parsers/gemini_parser.py` | One model call per page via the shared splitter | pypdf splitter, providers/gemini, base | Returns `PageFormat.MARKDOWN`; capped by `MAX_AI_PAGES`; sequential within a document. |
| `app/parsers/openai_parser.py` | GPT-5 Mini: one Responses-API call per page via the shared splitter | pdf_pages, providers/openai_adapter, base | Returns `PageFormat.MARKDOWN`; same contract as the Gemini parser; enabled only when `OPENAI_API_KEY` is set (and a Gemini key is configured for the summary). |
| `app/parsers/pdf_pages.py` | Shared page splitter for the AI parsers: split, encryption/invalid/zero-page/page-cap checks | pypdf, base | Deterministic page attribution lives here, not duplicated per provider. |
| `app/providers/openai_adapter.py` | The only module that touches the OpenAI SDK: Responses API with one inline-PDF page per request, error mapping (`429`/`5xx` -> transient + `Retry-After`, `401/403/4xx` -> `PROVIDER_CONFIG`, empty -> `PROVIDER_EMPTY_RESPONSE`) | openai SDK, pacing, base | Vendor SDK types never escape; live call not verified (no key here). |
| `app/providers/pacing.py` | `ProviderPacer`: semaphore + token bucket shared by every provider client | asyncio only | Per client instance, not per process. |
| `app/providers/gemini.py` | The only module that touches `google-genai`: `GeminiClient` (timeout, error mapping, pacer), `GeminiSummarizer` (one document summary) | google-genai, pacing, base, models | Vendor SDK types never escape this module. |
| `app/providers/fake.py` | Deterministic, network-free `FakeParser`/`FakeSummarizer` + `FAKE_WARNING` | pypdf (page count), base, models | Used only when `AI_MODE=fake`; output depends only on input. |
| `app/registry.py` | Decides which parsers/summarizer exist and are enabled, and why not; `ParserUnavailable`; `describe()` for the API | config type only, parsers, providers | The only place that decides availability. The API reports its decision verbatim. |
| `app/storage/objects.py` | Four-operation S3 adapter (`put_source`, `get_source`, `delete_source`, `healthy`) | boto3, config type | boto3 is blocking, so every call runs in a thread. Generated keys only. |
| `app/storage/jobs.py` | Redis repository: keys (including the `docs:index` sorted set), atomic create+enqueue+index, reads/list/pagination, lease acquire/renew/release (Lua), fenced stage/pages/terminal writes, TTLs | redis, models, config type | The only place that writes job state or the index. Fencing lives here. |
| `app/queue.py` | Consumer-group operations: ensure group, read new, stale claim, ack+delete, pending diagnostics | redis, config type | Single-group assumption is load-bearing (XDEL after XACK). |
| `app/processing.py` | One job: deadline check, resume-from-checkpoint, extract, checkpoint, summarize, fenced commit, source delete; `classify()` maps exceptions to `ErrorInfo` | models, base, registry, storage | Every state write goes through the fenced repository. |
| `app/worker.py` | Process entrypoint: bounded concurrent dispatch (`WORKER_CONCURRENCY` slots), per-job lease + heartbeat, attempt/backoff loop with `Retry-After`/jitter, SIGTERM drain, logging setup | everything above | Serve HTTP; share mutable state between jobs; treat a `RedisError` as a document failure. |
| `app/api/documents.py` | `POST /api/v1/documents` (admission control + upload), `GET /api/v1/documents` (list/pagination), `GET /api/v1/documents/{id}`, `GET /api/v1/parsers`; `AppContext` dependency | registry, storage, models, config | Never parses and never calls a model. |
| `app/main.py` | App factory, lifespan (Redis client, repository, S3 store, registry), health endpoints, `/` redirect, app-level `RedisError` handler | api, registry, storage, config, models | No blocking work in handlers. |
| `frontend/src/` | React UI: `api.ts` (relative `/api/v1` calls incl. list envelope), `types.ts`, `App.tsx`, `recentDocuments.ts` (rebuild cards after refresh), `polling.ts`/`hooks/useDocumentPolling.ts` (1.5 s poll, backoff, stops on terminal), `status.ts`, `components/MarkdownView.tsx` (sanitized markdown); vitest tests beside the modules | - | No key, no API hostname, no secrets in the bundle. |
| `tests/unit/test_contract.py` | Offline contract/settings tests (14) | app.config, app.models | Must stay offline (no Redis/S3/HTTP). |
| `tests/unit/test_hardening.py` | Offline regression tests for `Retry-After` parsing/clamping, provider error mapping, retry-delay policy and admission settings (17) | app.config, app.parsers.base, app.providers.gemini, app.worker | Must stay offline; no token-bucket timing. |
| `tests/unit/test_openai_parser.py` | GPT-5 Mini payload/error-mapping/parser contract against a stub (13) | app.parsers.openai_parser, app.providers.openai_adapter | No live call; monkeypatches the SDK client. |
| `tests/integration/test_api.py` | Black-box HTTP acceptance tests (16) | helpers in `tests/conftest.py` | Needs the running stack; samples mounted. |
| `tests/integration/test_repository.py` | Repository/lease/fenced-commit tests against a real Redis (14) | app.storage.jobs, app.queue | Marker `repository`; unique streams per test, never flushes. |
| `tests/integration/test_concurrency.py` | Opt-in burst/load acceptance tests (`RUN_CONCURRENCY_TESTS=1`, 3) | same helpers | Destructive-ish (20-doc bursts); use a disposable fake-mode worker. |
| `frontend/src/*.test.ts(x)` | Vitest unit/component tests (29) | - | `npm test` in `frontend/`. |
| `tests/e2e/test_proxy.py` | Proxy-level end-to-end tests through nginx (8) | - | Marker `e2e`; needs the frontend service; `FRONTEND_URL`. |
| `tests/integration/conftest.py` | Session client (readiness wait lives here, not autouse) and `ai_mode` | - | Fixtures that need a live stack live here, not in `tests/conftest.py`. |
| `scripts/make_samples.py` | Deterministic sample PDFs (stdlib only) | - | Byte-identical regeneration. |
| `scripts/smoke.sh` | Reviewer end-to-end walkthrough with assertions | - | bash + curl only. |
| `scripts/stress.py` | Load harness: bounded concurrent uploads, polls to terminal, exits non-zero on failure | httpx (test image) | Not a pytest test; needs `scripts/` + `samples/` mounts. |
| `scripts/resilience.sh` | Destructive fault injection: SIGKILL recovery, Redis restart, stale-writer fencing | - | Kills workers/restarts Redis; run against a stack you can recreate. |
| `scripts/check_encoding.py` | CI gate: every text file must be valid UTF-8 | - | stdlib only; also run by the quality CI job. |
| `scripts/package.py` | Builds and verifies the delivery archive | - | Refuses to include excluded paths; excludes Terraform state/caches. |
| `.github/workflows/ci.yml` | Five CI jobs: quality (ruff, format, mypy, secret grep, encoding), unit, integration (API + repository/lease + e2e), frontend, terraform | - | Runs with `AI_MODE=fake`; no provider credentials. |
| `requirements.lock.txt`, `requirements-dev.lock.txt` | Exact pins generated with `pip freeze` from the verified images; installed by the Dockerfile | - | `requirements*.txt` stay the human-edited intent; regenerate locks when the intent changes. |
| `infra/versions.tf`, `infra/modules/s3/`, `infra/modules/ec2-app/` | Terraform: shared S3 bucket module (private, versioned, optional lifecycle) and production EC2 host module (security group, IAM instance profile, Elastic IP, user_data) | - | One module serves both environments; no forked local/prod modules. |
| `infra/envs/local/`, `infra/envs/prod/` | Environment composition: LocalStack provider vs real AWS provider, state/backend, module inputs, tfvars | - | Local is applied automatically by Compose; production is validated, not applied, in this delivery. |
| `infra/envs/*/.terraform.lock.hcl` | Committed provider pin (same lock in both envs) | - | Never commit `*.tfstate*`, `.terraform/` or `*.tfvars`. |
| `compose.yaml` `provisioner` service | One-shot Terraform init+apply before api/worker, so a reviewer needs no host Terraform | - | Never long-running; `restart: "no"`. |

Dependency direction: `models` <- `parsers/base` <- parsers/providers <- `registry` <- `processing`
<- `worker`; `storage` depends on `models`; `api` depends on `storage`/`registry`; `main` wires
everything. The API process never imports `app.worker` or `app.processing`.

## 2. How to add a parser

A parser is a class satisfying the `Parser` protocol in `app/parsers/base.py`:

```python
class Parser(Protocol):
    id: ParserId

    async def parse(self, pdf: bytes, *, filename: str, limits: ParseLimits) -> ParseResult: ...
```

The provider side (if any) belongs in `app/providers/`. Steps, in order:

1. **Decide the output format.** `PageFormat.TEXT` (local extraction) or `PageFormat.MARKDOWN`
   (model transcription). Do not return markdown from a text extractor.
2. **Add the parser id** in `ParserId` (`app/models.py`). This is a frozen-contract change: it
   changes the upload-form enum, the `/api/v1/parsers` id space, the registry labels, and
   `tests/unit/test_contract.py::test_parser_ids_match_the_api`. Coordinate it.
3. **Write the implementation** in `app/parsers/<name>_parser.py`:
   - class attribute `id = ParserId.<NAME>`;
   - `async def parse(self, pdf, *, filename, limits) -> ParseResult`;
   - blocking work (PDF parsing, SDK sync calls) through `await asyncio.to_thread(...)` or the
     adapter's thread wrapper;
   - every expected failure raised as `TransientError` (429, timeout, 5xx, connection reset,
     unknown) or `PermanentError` (bad input, unsupported document, 4xx other than 429, config,
     safety block), always with a stable `code=` string - a code becomes part of the API surface;
   - never log PDF bytes, extracted content, prompts, or keys;
   - never let a vendor SDK type escape the module;
   - return pages 1-based and in document order (the worker runs `validate_pages`; the parser does
     not need to).
4. **If it uses a provider**, add `app/providers/<vendor>.py` with the client and error mapping
   (model `app/providers/gemini.py`), and keep the SDK import lazy (inside the client constructor)
   so a missing/broken SDK disables the provider through the registry instead of crashing the
   process. Add a deterministic fake in `app/providers/fake.py` if the parser should work in
   `AI_MODE=fake` (mirror the real parser's limits: AI parsers are capped by `MAX_AI_PAGES`).
5. **Add settings** in `app/config.py` for anything configurable (key, model, timeout) using the
   same naming convention, and add any cross-field rule to the model validator. Mirror every new
   variable in `.env.example` with the same default.
6. **Register it** in `app/registry.py`:
   - add the label/format/base-note entry to `_LABELS`;
   - add a `_build_<name>()` method and call it from `_build()`;
   - add the enum to the iteration order in `describe()`;
   - in fake mode decide explicitly: substitute `FakeParser` (and be labelled), or stay disabled
     with a reason. If the document still needs a summary, `_blocking_reason()` already disables
     everything when summarization is unavailable - do not duplicate that logic.
   - the registry is the only place allowed to decide `enabled`; the API must never advertise a
     parser that cannot run.
7. **Tests.**
   - `tests/unit/test_contract.py`: update `test_parser_ids_match_the_api`.
   - Add offline tests for the parser's failure mapping where possible (no network), e.g. corrupt
     bytes -> `INVALID_PDF`, encrypted PDF -> `ENCRYPTED_PDF`.
   - Add an integration test in `tests/integration/test_api.py` that uploads
     `samples/sample-text.pdf` with the new parser and asserts the terminal status, page count,
     ordering, `format`, and summary. Provider-backed tests must not call a paid provider in the
     default run - use fake mode, or gate behind an opt-in env flag like
     `tests/integration/test_live_ai.py` does with `RUN_LIVE_AI_TESTS=1`.
8. **Docs and ledger.** Update `README.md` (parser table), `docs/API.md` (parsers payload,
   error codes for new codes), `docs/TESTING.md` (counts/coverage), and record what was verified
   in `docs/PROGRESS.md`. A parser that is not listed in the parsers endpoint with an honest note
   should not ship.

There are **two worked examples**, and they differ only in the provider:

- **Gemini** (`app/parsers/gemini_parser.py` + `app/providers/gemini.py`): real live calls, a
  `GeminiClient` behind the adapter, one request per page, live-verified.
- **GPT-5 Mini** (`app/parsers/openai_parser.py` + `app/providers/openai_adapter.py`): same shape,
  different SDK - Responses API with the page sent as an inline PDF. Its payload, page ordering,
  page cap and error mapping are pinned by `tests/unit/test_openai_parser.py` against a stub, and
  no live OpenAI call has been made here. Copy this pattern when a provider's credentials are not
  available in the development environment: implement against the documented API surface, stub the
  SDK in tests, and let the registry keep the parser disabled until the key exists.

Both AI parsers share `pdf_pages.split_pdf_pages` (do not fork the splitter) and both clients use
`ProviderPacer` (do not implement your own rate limiting). Add a deterministic fake only if the
parser should run in `AI_MODE=fake`; the registry substitutes `FakeParser` for parsers with a real
implementation, and the fake must mimic the real limits.

## 3. How to change a limit safely

`app/config.py` is the single source of truth. Practical checklist:

1. **Change the default in `app/config.py`.** Do not hardcode the number anywhere else.
2. **Mirror the value in `.env.example`.** The file is documentation *and* a template; a mismatch
   means a reviewer copying it gets different behaviour. (It currently mirrors every default.)
3. **Check where it is actually enforced** - a setting that nothing reads is a lie:

   | Setting | Enforced in |
   |---|---|
   | `MAX_UPLOAD_BYTES`, `MAX_BATCH_FILES`, `MAX_BATCH_BYTES` | `app/api/documents.py` (HTTP `413`) |
   | `MAX_PAGES` | `app/parsers/pypdf_parser.py`, `app/providers/fake.py` (PyPDF path) |
   | `MAX_AI_PAGES` | `app/parsers/gemini_parser.py`, `app/providers/fake.py` (AI path) |
   | `MAX_EXTRACTED_CHARS` | `pypdf_parser` (cumulative), `gemini_parser` (per page) |
   | `PROVIDER_TIMEOUT_SECONDS` | `app/providers/gemini.py` (client `wait_for`), `app/processing.py` (`+30` parser, `+15` summarizer guards), `ParseLimits.timeout_seconds` |
   | `MAX_JOB_ATTEMPTS` | `app/worker.py` attempt loop |
   | `JOB_DEADLINE_SECONDS` | `app/api/documents.py` (`deadline_at` at accept), `app/processing.py` (checked per attempt) |
   | `LEASE_SECONDS` | `app/storage/jobs.py` lease TTL, `effective_reclaim_idle_ms` |
   | `HEARTBEAT_SECONDS` | `app/worker.py` heartbeat task |
   | `RESULT_TTL_SECONDS` | `commit_completed` / `commit_failed` TTLs |
   | `RETRY_BACKOFF_SECONDS` | `app/worker.py` between attempts |
   | `WORKER_READ_BLOCK_MS` | `app/queue.py` `XREADGROUP` block |
   | `RECLAIM_MIN_IDLE_MS` | `app/queue.py` `XAUTOCLAIM` threshold (`0` = lease ms) |
   | `FAKE_DELAY_SECONDS` | `app/providers/fake.py` |
4. **Keep the cross-field rules true.** The validator enforces heartbeat < lease, lease * 3 <=
   deadline, AI pages <= pages, batch bytes >= upload bytes, non-empty retry backoff. If a new
   limit interacts with another, add the rule there and a test in
   `tests/unit/test_contract.py::TestSettingsValidation`.
5. **Check the edges outside the app.**
   - nginx `client_max_body_size 25m` in `frontend/nginx.conf` caps a request through the UI
     before the API sees it. Raise it if `MAX_BATCH_BYTES` should be usable through `:3000`.
   - Larger `MAX_AI_PAGES` multiplies provider calls (one call per page) and therefore cost and
     latency; larger `PROVIDER_TIMEOUT_SECONDS` must stay compatible with the heartbeat/lease
     and the job deadline.
   - The `JOB_DEADLINE_SECONDS`/`LEASE_SECONDS` relation means raising the lease forces a longer
     deadline.
6. **Update the docs that state the numbers**: `README.md` limits table, `docs/API.md#limits`,
   the bounds paragraph in `docs/ARCHITECTURE.md`, and anything operational in `docs/RUNBOOK.md`
   (for example the reclaim threshold).
7. **Re-run**: unit suite (settings validation), then the integration suite with the stack
   restarted with the new environment. Record it in the ledger.

Do not add a second place where a default lives. If a limit needs to be visible to the frontend,
expose it through the API (`/api/v1/parsers` already returns parser capability) rather than
duplicating a constant in the bundle.

### Tuning knobs

Throughput and pacing have their own knobs. They are all in `app/config.py`, all cross-field
validated, and they only take effect when the worker process starts (recreate the container after
changing `.env`).

| Knob | Default | Enforced in | Guidance |
|---|---|---|---|
| `WORKER_CONCURRENCY` | `4` | `app/worker.py` dispatcher semaphore | Jobs processed in parallel per worker process. The main throughput knob for I/O-bound work; measured 4.22 -> 16.32 -> 22.99 settled docs/s at 1 -> 4 -> 8 ([PERFORMANCE.md](PERFORMANCE.md)). PyPDF is CPU work, so scaling flattens near the core count; `> 8` is untested. |
| `WORKER_BATCH_SIZE` | `8` | `app/queue.py` `XREADGROUP`/`XAUTOCLAIM` count | Entries fetched per read. Keep it >= `WORKER_CONCURRENCY` so slots are never idle; a very large batch just means more entries held by one consumer before dispatch. |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | `30` | `app/worker.py` `_drain` | On SIGTERM, how long in-flight jobs may finish before they are cancelled and left pending. Longer = fewer wasted provider calls on deploy; shorter = faster restarts. |
| `WORKER_READ_BLOCK_MS` | `2000` | `app/queue.py` | How long an idle read blocks; also bounds how often stale entries are swept. |
| `PROVIDER_MAX_CONCURRENCY` | `4` | `ProviderPacer` in each provider client | Simultaneous provider requests per **client instance**. A live Gemini worker can hold a Gemini parser client, a Gemini summarizer client and an OpenAI client, each with its own pacer. Raise only with `WORKER_CONCURRENCY`. |
| `PROVIDER_MAX_REQUESTS_PER_MINUTE` | `0` (off) | `ProviderPacer` token bucket | Client-side rate cap per client instance. Set it to the provider's published limit (leave headroom for the summarizer client); throughput becomes about `RPM / calls_per_document`. Leaving it at 0 in live mode under bursts turns a rate limit into `PROVIDER_ERROR` failures observed by the user. |
| `MAX_CONCURRENT_UPLOADS` | `16` | `app/api/documents.py` admission semaphore | Simultaneous uploads buffered per API process. This is the upload-memory ceiling: `MAX_CONCURRENT_UPLOADS x MAX_BATCH_BYTES`. Raises latency under load rather than memory. |
| `UPLOAD_SLOT_TIMEOUT_SECONDS` | `10.0` | same | How long an upload waits for a slot before `503` + `Retry-After: 1`. |
| `RETRY_AFTER_MAX_SECONDS` | `30` | `app/worker.py` `_retry_delay` | Clamp for a provider's `Retry-After`. If the RPM is set, keep this meaningfully larger than `60 / RPM`. |
| `FAKE_DELAY_SECONDS` | `0.0` | `app/providers/fake.py` | Deterministic per-document latency for fake-mode load/concurrency runs; makes stress numbers comparable. |

Relations the validator enforces (see `Settings._check_relationships`): `WORKER_CONCURRENCY >= 1`,
`WORKER_BATCH_SIZE >= 1`, `WORKER_SHUTDOWN_GRACE_SECONDS > 0`, `PROVIDER_MAX_CONCURRENCY >= 1`,
`PROVIDER_MAX_REQUESTS_PER_MINUTE >= 0` (0 = disabled), `MAX_CONCURRENT_UPLOADS >= 1`,
`UPLOAD_SLOT_TIMEOUT_SECONDS > 0`, `RETRY_AFTER_MAX_SECONDS > 0`, plus the existing
heartbeat/lease/deadline and limit rules. Pacing is **per provider client instance** (not per
process and not per worker): a job using a Gemini parser plus the Gemini summarizer can consume two
pacers, and multiple worker processes multiply again. Changes here are worth re-measuring with
`scripts/stress.py` and, for provider pacing, with the live-provider recipe in
[PERFORMANCE.md](PERFORMANCE.md) - and the result belongs in the ledger.

The nginx rate limits are **not** in `app/config.py`: they are the two `limit_req_zone` directives
outside the `server` block of `frontend/nginx.conf` (http context, required by nginx) plus
`limit_req` inside the matching `location`s. Changing them is a config/rebuild change, not an env
var, and they only guard the UI origin.

## 4. How to add an endpoint

`app/api/documents.py` owns the `/api/v1` router (prefix and tags set on `APIRouter`). A new route
can go there, or in a new module whose router is included in `app/main.py::create_app`.

1. **Add the response (and request) model** to `app/models.py`. Existing models show the style:
   plain `BaseModel`, `Field(default_factory=...)` for collections, enums for closed sets,
   `Literal` where the set is small and stable.
2. **Add the handler.** Take `ctx: Ctx` to get `settings`, `redis`, `repo`, `store`, `registry`
   (the `AppContext` dataclass). Never construct clients per request; never do blocking I/O on the
   event loop (boto3/SDK/PDF work goes through `asyncio.to_thread`).
3. **Errors.**
   - Client errors raised before anything is written: `HTTPException(status_code=..., detail=...)`
     with a stable, actionable message.
   - Per-file or per-item outcomes that a client must inspect: return a model with a structured
     list (see `UploadResponse.rejected`) rather than aborting the whole request.
   - Redis failures: map to `503` ("the job store is unavailable"). The app-level
     `RedisError` handler in `app/main.py` is a safety net, but handlers usually catch it and
     produce a better message.
   - Never return raw exception text or stack traces; derive a stable code.
4. **Register** the router in `create_app()` if it is a new module, and give the endpoint a
   `responses={...}` map so Swagger documents the non-2xx cases.
5. **Tests.** Add an integration test that asserts status code and body for the success and for
   every documented error case. If it needs the sample mount or the running stack, it belongs in
   `tests/integration/`; a pure function belongs in `tests/unit/`. Never make the unit suite
   require Redis/S3/HTTP - that is why the live fixtures live in `tests/integration/conftest.py`.
6. **Docs.** Add the endpoint to the table and body shapes in `docs/API.md`, add any new error code
   to its error-code table, and mention it in `README.md` if it is reviewer-relevant. The HTTP API
   is frozen in `docs/CONTRACT.md` section 5, so a new or changed endpoint is a coordinated change.

**Worked example - the listing endpoint.** `GET /api/v1/documents` shows the full pattern for a
read endpoint with pagination: a response model pair (`DocumentList` + `DocumentSummary`) in
`app/models.py`, a repository method that owns the Redis access (`JobRepository.list_recent` /
`score_of` backed by `docs:index`), a cursor (`next_before` = zset score, exclusive), FastAPI
`Query(ge=1, le=100)` bounds so out-of-range values are `422`, and Redis errors mapped to `503`.
Its tests are two layers: `tests/integration/test_api.py::TestListing` for HTTP semantics and
`tests/integration/test_repository.py` for the storage behaviour.

Any new **expensive** endpoint (anything that buffers a file, calls a provider, or scans a large
key space) needs two decisions recorded: whether it goes behind admission control
(`AppContext.upload_slots` is the upload example), and whether the proxy rate limit is enough or a
per-op guard is required. Cheap read endpoints are rate-limited by the shared `api` zone.

## 5. How to change infrastructure

Infrastructure is Terraform under `infra/`. One rule carries most of the design: the **same module
serves local and production**; environment differences are provider configuration and module
inputs, not forked modules. Local is applied by Compose; production is validated, never applied,
in this delivery.

1. **Edit the right place.** Shared resource -> `infra/modules/<name>/`; provider, backend,
   wiring or inputs -> `infra/envs/local/` or `infra/envs/prod/`.
2. **Format and validate.** `terraform fmt -recursive infra`, then `terraform validate` in each
   env. Both are expected clean before commit (they are clean in this delivery).
3. **Apply locally.** `docker compose up -d` (the one-shot `provisioner` re-runs init+apply), or
   `docker compose run --rm provisioner`. Ad-hoc commands go through the pinned image, e.g.
   `docker compose run --rm --entrypoint terraform provisioner <command>`; its working directory is
   `/tf/envs/local`.
4. **Production is a normal host-side Terraform run** from `infra/envs/prod`: copy
   `terraform.tfvars.example` to `terraform.tfvars`, then `terraform init && terraform plan &&
   terraform apply`. It is **not** applied by Compose and has not been applied in this delivery.
   State is local by default; enable the commented `backend "s3"` block before more than one
   operator ever applies.
5. **Keep the environment wiring honest.** `S3_BUCKET` feeds the provisioner as
   `TF_VAR_bucket_name: ${S3_BUCKET:-documents}` and is also passed to `api`/`worker`, so both
   sides always agree - but renaming it **destroys and re-creates the bucket** (the name is the
   Terraform resource identity). `enable_lifecycle = false` locally because LocalStack never
   confirms a lifecycle rule (the provider polls for about three minutes and fails the apply);
   `force_destroy = true` only locally. Do not "fix" LocalStack by flipping them.
6. **What to commit.** Commit `.terraform.lock.hcl` and `terraform.tfvars.example`. Never commit
   `*.tfstate*`, `.terraform/` (hundreds of MB of providers), `*.tfvars`, or `*.tfplan`; all of
   those are in `.gitignore` and excluded from the image and the delivery archive.
7. **Update the docs that describe infrastructure.** `docs/ARCHITECTURE.md` section 1 for the
   design and verification status, `docs/RUNBOOK.md` for operations (provision, re-apply, destroy,
   state, troubleshooting), and `docs/README.md` if the tree changes.

Terraform commands need either Terraform installed or the pinned container image
(`hashicorp/terraform:1.9`). The Compose provisioner exists so a reviewer needs neither.

## 6. Conventions this codebase follows

1. **Stable error codes are part of the API.** Every expected failure is a `ParserError` with a
   `code` (and a `retryable` classification). Codes appear verbatim in the client-facing
   `error.code` / `rejected[].code`; do not rename one without treating it as a breaking change.
   Never raise a bare exception for an expected failure. Unknown provider failures default to
   transient so the bounded retry policy gets a chance before a stable code is reported.
2. **No secrets or document content in logs.** Log ids, parser names, page counts, byte counts,
   durations, codes. Never log PDF bytes, extracted text, prompts, API keys, or full Redis URLs
   with credentials (the startup log keeps only the host part after `@`). Treat filenames as
   sensitive-ish and avoid them in log lines where an id will do.
3. **Fenced writes go through the repository.** Any write that changes observable job state
   (`docs:job`, `docs:pages`, `docs:summary`, `docs:lease`, the stream entry's terminal lifecycle)
   goes through `JobRepository` and, for state writes, is fenced by the execution token. A worker
   that lost its lease must be unable to write; if you add a new mutation, decide explicitly
   whether it must be fenced. The one deliberate exception is `increment_attempt`, which is an
   unfenced `HINCRBY` counter used for diagnostics/budget; the terminal writes are fenced.
4. **Assume at-least-once delivery.** Any code between "message claimed" and "terminal commit" can
   run twice. Put expensive, repeatable work behind the pages checkpoint where possible, make the
   terminal commit the single point of truth, and never ACK before the terminal state is durable.
5. **One consumer group.** `XACK`+`XDEL` in the same call is safe only because this design has
   exactly one group. Adding a second group changes queue semantics; do not do it casually.
6. **Concurrency must not become shared state.** Jobs run in parallel inside one worker process;
   each owns its lease token, heartbeat, retry counter and pacer position. Never introduce mutable
   state that spans jobs (counters, caches keyed by user, buffers) without making it explicitly
   safe; the fencing guarantees are per job, not per process.
7. **A dependency failure is never a document failure.** `RedisError` (and any future store-outage
   error) must be re-raised from the attempt loop - the entry stays pending and a reclaim resumes
   from the checkpoint. Mapping a transient store blip to a terminal `failed` is the exact bug the
   fault campaign found; there is an architecture-level test for it only in
   `scripts/resilience.sh redis`.
8. **Provider calls go through an adapter client.** All model requests must go through
   `GeminiClient` (`app/providers/gemini.py`) or `OpenAIClient`
   (`app/providers/openai_adapter.py`), both of which use `ProviderPacer`, honour `Retry-After`
   and map errors. Do not instantiate a vendor SDK anywhere else, and do not write your own
   semaphore/token bucket. AI parsers share `pdf_pages.split_pdf_pages`; do not fork the splitter.
9. **Pages are 1-based, ordered, contiguous.** The worker calls `validate_pages`; empty pages are
   kept so numbering matches the source document. Keep that invariant in any new parser.
10. **Blocking work off the event loop.** `asyncio.to_thread` for PyPDF and boto3, async clients
    for providers, explicit `asyncio.wait_for` timeouts around provider calls.
11. **Vendor SDK types do not escape `app/providers/`.** Parsers deal in bytes, `ParseLimits`,
    `ParseResult`, `PageResult`, and `ParserError` subclasses only.
12. **Timestamps** use `utc_now_iso()` (`YYYY-MM-DDTHH:MM:SSZ`) everywhere - it is the single
    timestamp format in hashes, API responses, and the UI contract.
13. **Line endings, encoding and the doc set.** `.gitattributes` normalizes text files to LF; shell
    scripts must stay LF-only. Every text file must be valid UTF-8 - CI runs
    `scripts/check_encoding.py` (a cp1252 em dash once broke `mypy`). Keep the documentation set
    ASCII-only. Do not name a parser module `pypdf.py` - it shadows the `pypdf` dependency.
14. **Style and types are enforced, not reviewed by eye.** `ruff check` (E,F,W,I,N,B,UP,SIM,RUF,
    line-length 100), `ruff format --check` and `mypy` over `app/` run in CI and are expected clean
    before commit; configuration is in `pyproject.toml`. `# noqa: BLE001` appears only at documented
    resilience boundaries (a bad provider import must not kill the process, one bad job must not
    kill the worker).
15. **Tests assert outcomes and fail loudly.** No mocking of the HTTP layer for integration tests;
    poll with deadlines instead of fixed sleeps; load/fault harnesses exit non-zero on any failure
    so they can be used as gates. An opt-in test must skip (not pass) when its precondition is
    absent. The repository suite uses unique stream keys and never flushes.
16. **Cheap by construction where possible.** Expensive endpoints (buffering a file, calling a
    provider, scanning large key spaces) need an explicit decision about admission control and rate
    limiting; the upload path is the worked example. New read APIs that need to survive a UI
    refresh should be backed by a Redis index written in the same transaction as the state, as
    `docs:index` is.
17. **Dependencies are pinned.** `requirements.txt` / `requirements-dev.txt` are the human-edited
    intent with bounded ranges; `requirements.lock.txt` / `requirements-dev.lock.txt` are exact
    `pip freeze` output from the verified images and are what the Dockerfile installs. Change the
    intent, regenerate both lock files from the image, rebuild. CI installs the locks, so they are
    the real dependency set.

## 7. Local development and running things

- **Python version.** The image is `python:3.12-slim`; `pyproject.toml` requires `>=3.12`. The
  host in this environment has `py -3` = Python 3.14.0b2, but **no project dependencies or pytest
  installed**, so host-side test runs need `pip install -r requirements-dev.txt` first (not
  executed here).
- **Supported run path is Compose.** `docker compose up --build -d`; the API and worker share one
  image, and a one-shot Terraform `provisioner` applies the local infrastructure before
  api/worker start. **The first run needs network and is a minute or two slower** because the AWS
  provider is downloaded into `infra/envs/local/.terraform/`; later runs reuse the cache. After
  changing `.env`, recreate the containers (`--force-recreate`); a `restart` will not re-read
  `env_file`.
- **Host-side app run is not turnkey.** Redis is not published to the host (no `ports:` mapping),
  so a host process cannot reach the Compose Redis without a Compose override or an extra Redis
  instance. LocalStack S3 *is* published (on `127.0.0.1:${LOCALSTACK_PORT:-4566}`) and the local
  bucket is created by Terraform, so host-side tools see the same bucket once the stack is up. This
  is a documented constraint, not a tested workflow.
- **Tests.** See [TESTING.md](TESTING.md): 82 backend checks (44 unit offline, 16 API integration,
  14 repository/lease with Redis only, 8 proxy e2e with the frontend) plus 4 opt-in (3 concurrency,
  1 live AI) and 29 frontend vitest tests. Integration tests create real documents in the running
  stack and never flush Redis. The concurrency suite is opt-in (`RUN_CONCURRENCY_TESTS=1`) and
  wants a disposable fake-mode worker.
- **Frontend.** `cd frontend && npm ci && npm test && npx tsc --noEmit && npm run build`. Vitest
  covers the API error/envelope parsing, polling/backoff, recent-document rehydration, status
  mapping and the App-level reload recovery.
- **Quality gates (all also run in CI).** In the dev image:
  `docker run --rm -v "$PWD:/src" -w /src <test-image> ruff check .`;
  `ruff format --check .`; `mypy --cache-dir=/tmp/mypy`. On the host:
  `python scripts/check_encoding.py`. The CI secret grep is part of the quality job. Configuration
  lives in `pyproject.toml` (ruff select/ignore, line length 100; mypy over `app/` with
  `check_untyped_defs`). Run them before opening a PR; a red quality job is a hard failure. All
  three were clean when checked against the working tree in this documentation pass (`ruff check`
  passed, 52 files formatted, mypy 24 modules clean, encoding valid).
- **Dependency locks.** `requirements.txt` / `requirements-dev.txt` are the intent;
  `requirements.lock.txt` / `requirements-dev.lock.txt` are generated from the verified images
  (`docker run --rm --entrypoint pip <image> freeze | grep '==' | sort`) and installed by the
  Dockerfile. After changing the intent, regenerate **both** locks, rebuild, and commit the locks.
- **Load and fault harnesses.** `scripts/stress.py` (throughput, exits non-zero on failure) and
  `scripts/resilience.sh` (destructive: SIGKILL, Redis restart, fencing). Commands, mounts and
  caveats: [RUNBOOK.md](RUNBOOK.md) section 11; measured method and results:
  [PERFORMANCE.md](PERFORMANCE.md). Do not run the fault script against a stack you cannot recreate.
- **Samples.** Regenerate with `python scripts/make_samples.py`; output is byte-identical, so the
  deterministic fixtures can be diffed.
- **Ops.** Start/stop, health, logs, queue inspection, provisioning, load/fault harnesses, test
  commands and troubleshooting are in [RUNBOOK.md](RUNBOOK.md).

## 8. Change checklist

Before calling a change done:

- [ ] Frozen interfaces untouched, or changed with the coordinator and reflected in
      `docs/CONTRACT.md`.
- [ ] New limit default changed in `app/config.py` only, mirrored in `.env.example`, and reflected
      in `README.md` / `docs/API.md` / `docs/ARCHITECTURE.md` where stated.
- [ ] New parser registered in `app/registry.py` with an honest note, covered by an offline test
      (stub the provider SDK) and, where safe, an integration test; no live provider call in the
      default suite.
- [ ] New state writes and index writes go through `JobRepository` and are fenced if they change
      observable state.
- [ ] New error codes are stable, classified (`retryable` true/false), and documented in
      `docs/API.md`.
- [ ] No secrets, document text, prompts, or PDF bytes in logs or API output.
- [ ] Quality gates clean: `ruff check`, `ruff format --check`, `mypy`,
      `python scripts/check_encoding.py`; no credential-shaped string added.
- [ ] If dependencies changed: both `requirements*.lock.txt` regenerated from the verified image
      and the image rebuilt.
- [ ] If worker concurrency, retry/backoff or provider pacing changed: the unit hardening tests
      still pass, the concurrency suite was run with `RUN_CONCURRENCY_TESTS=1` against a fake-mode
      worker, and `scripts/stress.py` / the relevant `scripts/resilience.sh` scenario was re-run if
      the change could affect throughput or recovery. Record what was re-measured.
- [ ] Infrastructure changes: `terraform fmt -check -recursive` clean and `terraform validate`
      valid for both `infra/envs/local` and `infra/envs/prod`; no `*.tfstate*`, `.terraform/`,
      `*.tfvars` or `*.tfplan` staged; local re-applied through the provisioner.
- [ ] Unit suite still offline; integration, repository and e2e suites run against a stack that was
      actually restarted with the new environment; the result recorded in `docs/PROGRESS.md`.
- [ ] Documentation set updated (this file, plus the affected derived documents), and links still
      resolve.

# API reference

HTTP contract of the async PDF-processing API. Every JSON shape below is copied from
`app/models.py`; every status code and error code is traced to the handler or parser that raises
it. Shapes marked **verified** were observed against the running stack in this environment on
2026-11-25 (see the note at the end of each section for what was and was not executed).

Base URLs:

| Access path | URL |
|---|---|
| Direct to the API | `http://localhost:8000` |
| Through the UI's same-origin proxy (nginx) | `http://localhost:3000/api/...` and `http://localhost:3000/health/...` |
| Interactive docs | `http://localhost:8000/docs` (Swagger UI); `/` redirects to it with `307` (verified) |

There is **no authentication, no API key header, and no tenancy**. Anyone who can reach the port
can upload and read any document id; a UUID is not access control. See
[SECURITY.md](SECURITY.md). All request and response bodies are JSON except the multipart upload
request.

## Endpoints

| Method | Path | Purpose | Success | Errors |
|---|---|---|---|---|
| `POST` | `/api/v1/documents` | Upload one or more PDFs and enqueue one job per accepted file | `202` | `413`, `422`, `503` |
| `GET` | `/api/v1/documents/{document_id}` | Poll a document: state, pages, summary, error | `200` | `404`, `503` |
| `GET` | `/api/v1/parsers` | Which parsers can actually run right now, and why not when they cannot | `200` | none (registry is in-process) |
| `GET` | `/health/live` | Process liveness, no dependency checks | `200` | none |
| `GET` | `/health/ready` | Dependency and AI-config readiness | `200` | `503` |
| `GET` | `/` | Redirect to `/docs` | `307` | none |

There are no list, delete, cancel, retry, or history endpoints. There is no server-sent events or
websocket endpoint; the UI polls. Results expire after `RESULT_TTL_SECONDS` (24 h), after which
the status endpoint returns `404`.

---

## `POST /api/v1/documents`

`multipart/form-data` with:

| Field | Type | Required | Notes |
|---|---|---|---|
| `parser` | text | yes | One value for the whole batch: `pypdf`, `gemini`, or `gpt-5-mini`. |
| `files` | file, repeated | yes | One or more PDF files. Field name must be `files` each time. |

Processing order of validation (this order is observable):

1. FastAPI validates the multipart request shape (both `parser` and `files` are required fields)
   -> missing fields produce FastAPI's `422` validation body before the handler runs.
2. `parser` is a known `ParserId` value -> otherwise `422`.
3. The parser is currently enabled in the registry -> otherwise `503`. This happens **before**
   any file is read, so a request naming a disabled parser returns `503` even when its files are
   corrupt.
4. `len(files) <= MAX_BATCH_FILES` -> otherwise `413`.
5. Per file, in order: bounded read (`MAX_UPLOAD_BYTES`) -> `413` if exceeded; running batch total
   (`MAX_BATCH_BYTES`) -> `413` if exceeded; empty -> `rejected[] EMPTY_FILE`; missing `%PDF-`
   signature -> `rejected[] NOT_A_PDF`.
6. Per accepted file: store the source in S3, then `MULTI {HSET job + XADD}`. A definite S3 or
   Redis failure is reported per file as `rejected[] STORAGE_UNAVAILABLE` / `QUEUE_UNAVAILABLE`
   and the object is deleted on the Redis failure path.
7. At least one accepted file -> `202`. No accepted file -> `422`, unless every failure was a
   storage/queue failure, in which case `503`.

Batch semantics: a batch is **not** atomic. A corrupt file is reported in `rejected[]` while valid
files are accepted with their own UUIDs and processed independently.

Behavioural caveat, traced in code and **not executed** during this documentation pass (it would
have created jobs on the live stack): the size/count checks in steps 4-5 raise immediately. If a
later file trips `413` after earlier files were already stored and enqueued, the response is a
plain `413` without the `documents[]` list, but the earlier jobs exist and will be processed. A
client that needs all-or-nothing must validate sizes before uploading.

### `202` response (at least one accepted)

`UploadResponse` (`app/models.py`):

```json
{
  "documents": [
    {
      "id": "<uuid>",
      "filename": "<sanitized filename>",
      "parser": "pypdf",
      "status": "queued",
      "status_url": "/api/v1/documents/<uuid>"
    }
  ],
  "rejected": [
    {
      "filename": "<sanitized filename>",
      "code": "NOT_A_PDF",
      "message": "The file does not start with the %PDF- signature."
    }
  ]
}
```

Field notes:

- `documents[].status` is always `"queued"` in this response (the accepted state).
- `documents[].status_url` is a relative path; it is not absolute and not a full URL.
- `documents[].filename` is sanitized server-side: basename only, non-printable characters and
  `\ / : * ? " < > |` removed, truncated to 200 characters, fallback `document.pdf`.
- `rejected[]` appears on `202` responses too, for the partial-failure case.
- Accepted files get independent UUIDs even when the same file is uploaded twice (verified in the
  integration suite: `TestIdempotentAcceptance`).

### `422` response

Two different bodies share this status:

- **Unknown parser** - HTTPException shape:

  ```json
  {"detail": "Unknown parser 'not-a-parser'. Use one of: pypdf, gemini, gpt-5-mini."}
  ```

- **Every file rejected at validation time** (empty, or not a PDF) - `UploadResponse` shape with an
  empty `documents` list:

  ```json
  {
    "documents": [],
    "rejected": [
      {"filename": "sample-corrupt.pdf", "code": "NOT_A_PDF",
       "message": "The file does not start with the %PDF- signature."}
    ]
  }
  ```

- **Missing `files` field / other request-shape validation** - FastAPI's validation shape:

  ```json
  {"detail": [{"type": "missing", "loc": ["body", "files"], "msg": "Field required", "input": null}]}
  ```

Verified: unknown parser, empty file (`EMPTY_FILE`), corrupt file (`NOT_A_PDF`), and missing files
were all exercised against the live stack and returned exactly these shapes.

### `413` response

HTTPException shape, `{"detail": "<message>"}`. Three distinct messages:

| Cause | `detail` |
|---|---|
| Too many files | `Too many files: 6 > MAX_BATCH_FILES=5.` |
| One file too large | `<filename> exceeds MAX_UPLOAD_BYTES=20971520 bytes.` |
| Batch total too large | `Batch exceeds MAX_BATCH_BYTES=52428800 bytes.` |

Note that the nginx proxy in front of the UI refuses request bodies over 25 MB with its own
`413` before the API sees them (`client_max_body_size 25m` in `frontend/nginx.conf`). A multi-file
batch between 25 MB and 50 MB is therefore refused by the proxy, not by the API, when accessed
through `:3000`.

The per-file `413` path is covered by `TestValidation.test_oversized_file_is_refused` (ledger:
`413` for a 21 MB file). It was not re-executed during this documentation pass.

### `503` response

Two different bodies share this status:

- **Parser not enabled** - HTTPException shape. Example verified live in live mode with
  `gpt-5-mini`:

  ```json
  {"detail": "Parser 'gpt-5-mini' is not available right now. Parser 'gpt-5-mini' is not available: not implemented in this delivery; use pypdf or gemini"}
  ```

  In `AI_MODE=live` with no Gemini key every parser is disabled, and the detail explains that
  summarization is unavailable and every document requires a summary (the exact reason string is
  produced by `ProviderRegistry._blocking_reason`; see the parsers endpoint below).

- **Every file failed at storage/queue time** - `UploadResponse` shape with `documents: []` and
  `rejected[]` entries whose `code` is `STORAGE_UNAVAILABLE` or `QUEUE_UNAVAILABLE`.

---

## `GET /api/v1/documents/{document_id}`

Path parameter is any string; unknown ids return `404`, not `422`. Response model is
`DocumentDetail`, which is `DocumentJob` plus `pages` and `summary`.

### `200` response - completed document (verified, exact fields and order)

```json
{
  "id": "bb1f64c8-a437-40c7-93b2-05a0b67fa679",
  "filename": "sample-text.pdf",
  "parser": "pypdf",
  "status": "completed",
  "stage": null,
  "attempt": 1,
  "created_at": "2026-11-25T17:43:19Z",
  "updated_at": "2026-11-25T17:43:19Z",
  "source_key": "uploads/bb1f64c8-a437-40c7-93b2-05a0b67fa679/source.pdf",
  "page_count": 2,
  "warnings": [],
  "error": null,
  "deadline_at": 1795629199.082,
  "pages": [
    {"page_number": 1, "format": "text", "content": "Piscina maintenance report - page 1\n..."},
    {"page_number": 2, "format": "text", "content": "Piscina maintenance report - page 2\n..."}
  ],
  "summary": "## Summary (fake mode)\n\n- pages: 2\n..."
}
```

All fields, in declaration order (`DocumentJob` then `DocumentDetail`):

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Document UUID |
| `filename` | string | Sanitized upload filename |
| `parser` | `"pypdf" \| "gemini" \| "gpt-5-mini"` | The parser selected at upload; the one the worker will use |
| `status` | `"queued" \| "parsing" \| "summarizing" \| "retrying" \| "completed" \| "failed"` | Lifecycle state; only `completed`/`failed` are terminal |
| `stage` | `"parsing" \| "summarizing" \| null` | Sub-stage while working; `null` when queued, retrying, or terminal |
| `attempt` | integer | Attempts started so far (incremented before each attempt; starts at `0`) |
| `created_at`, `updated_at` | string | UTC, exact format `YYYY-MM-DDTHH:MM:SSZ` |
| `source_key` | string | **Internal S3 key**, `uploads/<id>/source.pdf`. Exposed because `DocumentDetail` inherits the internal job model; harmless here (server-generated) but documented deliberately. |
| `page_count` | integer or null | Set when pages are checkpointed; `null` before then |
| `warnings` | string[] | Human-readable warnings, including the explicit `ai_mode=fake` labelling in fake mode |
| `error` | object or null | `{"code","message","retryable"}`; non-null only after a failure |
| `deadline_at` | float | Absolute epoch seconds at which the job is refused (`created + JOB_DEADLINE_SECONDS`). Also exposed by inheritance. |
| `pages` | array | `[]` until extraction is checkpointed; ordered `page_number` 1..N |
| `summary` | string or null | `null` until the summary is committed; one summary per document |

`pages[]` items are `PageResult`: `page_number` (1-based integer), `format` (`"text"` for the
PyPDF parser, `"markdown"` for Gemini or fake), `content` (string; may be empty for a page with no
text, in which case numbering stays contiguous and a warning is attached).

Field notes:

- The **exact** response includes `source_key` and `deadline_at`. The abridged example in the
  repository README does not show them; this document is the exact shape.
- `status: "failed"` can still carry `pages[]` when extraction succeeded but summarization failed.
  Pages stay readable until the result TTL expires.
- `status: "completed"` implies `summary` is a non-empty string and `error` is `null` (the
  terminal write is a single transaction).
- A completed-then-expired document returns `404`.
- `warnings` can come from either the job hash or the pages checkpoint; the API merges them
  (checkpoint warnings first, then job warnings that are not already present).

### `404` response

```json
{"detail": "Document not found. Unknown id, or the result has expired."}
```

Raised for an unknown id and for a document whose keys expired after `RESULT_TTL_SECONDS`.
Verified live with an all-zero UUID.

### `503` response

```json
{"detail": "The job store is unavailable; try again shortly."}
```

Raised by the handler when Redis fails during the read. If a `RedisError` escapes to the app-level
handler instead, the shape is `{"detail": "The job store is unavailable; try again shortly.",
"code": "QUEUE_UNAVAILABLE"}`. Clients should treat both as retryable.

---

## `GET /api/v1/parsers`

Always `200`. Response model `ParsersResponse`:

```json
{
  "ai_mode": "live",
  "parsers": [
    {"id": "pypdf", "label": "PyPDF", "output_format": "text",
     "enabled": true, "requires_api_key": false,
     "note": "Local extract_text(); returns plain text, not markdown."},
    {"id": "gemini", "label": "Google Gemini", "output_format": "markdown",
     "enabled": true, "requires_api_key": true,
     "note": "Per-page markdown transcription."},
    {"id": "gpt-5-mini", "label": "GPT-5 Mini", "output_format": "markdown",
     "enabled": false, "requires_api_key": true,
     "note": "not implemented in this delivery; use pypdf or gemini"}
  ]
}
```

The exact payload above is the observed live response (live mode, real key for
`gemini-flash-lite-latest`, `gpt-5-mini` disabled). Fields per `ParserInfo`:

| Field | Type | Meaning |
|---|---|---|
| `id` | `"pypdf" \| "gemini" \| "gpt-5-mini"` | Parser identifier used in the upload form |
| `label` | string | Display name (`PyPDF`, `Google Gemini`, `GPT-5 Mini`) |
| `output_format` | `"text" \| "markdown"` | What `pages[].format` will be for this parser |
| `enabled` | boolean | `true` only if the parser would actually run for a new upload right now |
| `requires_api_key` | boolean | Whether the parser needs a provider credential (false only for PyPDF) |
| `note` | string or null | Why it is disabled, or a description of how it extracts. A disabled parser never has a null/empty note (asserted by the integration suite). |

Why a parser can be disabled, and what the note says (all traced to
`app/registry.py`, and reproduced by constructing the registry with those settings in a throwaway
container after the `gpt-5-mini` change):

| Situation | `ai_mode` | Enabled parsers | Notes |
|---|---|---|---|
| Live mode, Gemini key configured | `live` | `pypdf`, `gemini` | `gpt-5-mini` disabled: "not implemented in this delivery; use pypdf or gemini" |
| Live mode, no Gemini key | `live` | none | Every parser disabled. Note: "summarization is unavailable (gemini_api_key not configured) and every document requires a summary; set GEMINI_API_KEY, or set AI_MODE=fake to run without credentials". Upload returns `503`. |
| Fake mode | `fake` | `pypdf`, `gemini` | The AI parsers that have a real implementation (Gemini) are replaced by deterministic stand-ins and carry the prefix "fake mode: deterministic stand-in, no model call." PyPDF keeps its real note because it is genuinely the local extractor. `gpt-5-mini` is still `enabled: false` with "not implemented in this delivery; use pypdf or gemini". |
| Python adapter import failure (PyPDF) | any | depends | "adapter unavailable: <ExceptionType>" - the process does not crash |
| Fake provider construction failure | `fake` | depends | "fake provider unavailable" |

Two consequences worth knowing:

- The **whole parser list is disabled together** when summarization cannot run. Every document
  requires a summary and the summary is Gemini-only, so accepting any upload would mean accepting
  work that is guaranteed to fail after the expensive extraction.
- **`gpt-5-mini` is never enabled in any mode.** `app/parsers/openai_parser.py` declares
  `IMPLEMENTED = False`, and the registry checks that **before** the fake-mode branch, so fake mode
  substitutes only providers that have a real implementation to stand in for. The note is always
  "not implemented in this delivery; use pypdf or gemini"; an upload naming it always returns
  `503` with that reason. (Earlier builds briefly advertised it as a fake stand-in; that was fixed
  and is documented here as the current behaviour - verified by registry probe in both modes.)

---

## `GET /health/live`

`200` with:

```json
{"status": "ok"}
```

No dependency checks, no Redis/S3 call. Docker Compose watches this endpoint for the `api`
container healthcheck, deliberately not `/health/ready`.
Verified live.

## `GET /health/ready`

`200` when healthy, `503` when degraded. Response model `HealthResponse`:

```json
{
  "status": "ok",
  "checks": {
    "redis": "ok",
    "s3": "bucket 'documents' ok",
    "summarizer": "gemini:gemini-flash-lite-latest",
    "ai_mode": "live"
  },
  "detail": null
}
```

The exact payload above is the observed live response. Semantics of each check:

| Check | Passes when | Failure string (examples from code) |
|---|---|---|
| `redis` | `PING` answers within 5 s | `unavailable: ConnectionError`, `unavailable: TimeoutError` |
| `s3` | `head_bucket` on `S3_BUCKET` succeeds within 5 s | `bucket 'documents' unreachable (HTTP 404)`, `unreachable (BotoCoreError)` |
| `summarizer` | A summarizer exists: the Gemini one in live+key mode, the fake one in fake mode | `gemini_api_key not configured`, `fake summarizer unavailable` |
| `ai_mode` | Always reported | `live` or `fake` |

- In `fake` mode the deterministic summarizer counts as ready: `200` with
  `checks.summarizer = "fake summarizer"`.
- In `live` mode without a Gemini key the endpoint is `503`, because uploads must not be accepted.
- Degraded responses set `"status": "degraded"` and
  `"detail": "One or more dependencies are unavailable; see checks."`
- Each dependency check has a 5 s timeout (`READY_TIMEOUT_SECONDS`), so readiness itself is
  bounded.

---

## Error-code table

There are two distinct surfaces. Upload-time rejections appear in `rejected[].code` with the HTTP
status of the request; processing failures appear in `error.code` on a document that polled to
`status: "failed"` (the HTTP status of that poll is still `200`). `retryable` only exists on the
processing-error record (`ErrorInfo`); upload rejections have `code` and `message` only.

### Upload-time rejections (`rejected[].code`)

| Code | HTTP | Raised in | Meaning / client action |
|---|---|---|---|
| `EMPTY_FILE` | `422` (or inside a `202` batch) | `app/api/documents.py` | Zero-byte upload. Send a real PDF. |
| `NOT_A_PDF` | `422` (or inside a `202` batch) | `app/api/documents.py` | The body does not start with the `%PDF-` signature. The check is on bytes, never on filename or MIME type. |
| `STORAGE_UNAVAILABLE` | `503` when nothing was accepted; inside `202`/`422` otherwise | `app/api/documents.py` (`ObjectStoreError` on PUT) | S3 (LocalStack) is unavailable. Retry later. |
| `QUEUE_UNAVAILABLE` | `503` when nothing was accepted; inside `202`/`422` otherwise | `app/api/documents.py` (`RedisError` on the MULTI) | Redis is unavailable; the just-uploaded object was deleted. Retry later. |

### Processing failures (`error.code` on a `failed` document)

| Code | `retryable` | Raised in | Meaning |
|---|---|---|---|
| `OCR_REQUIRED` | `false` | `pypdf_parser`, `gemini_parser`, `providers/fake.py` | No extractable text at all (PyPDF) or all pages empty (Gemini/fake). The API message suggests choosing another parser. This is the documented outcome for `samples/sample-blank.pdf` with `pypdf`. |
| `INVALID_PDF` | `false` | `pypdf_parser`, `gemini_parser`, `providers/fake.py` | PDF could not be parsed (corrupt xref, truncated file, ...). Upload-time only checks the signature. |
| `ENCRYPTED_PDF` | `false` | `pypdf_parser`, `gemini_parser`, `providers/fake.py` | Password-protected PDF; the app never prompts for a password. |
| `EMPTY_PDF` | `false` | `gemini_parser` | PDF page tree has zero pages. |
| `TOO_MANY_PAGES` | `false` | `pypdf_parser`, `providers/fake.py` | Over `MAX_PAGES` for the document parser. |
| `TOO_MANY_PAGES_FOR_AI` | `false` | `gemini_parser` | Over `MAX_AI_PAGES`; the message suggests the PyPDF parser. |
| `TOO_MUCH_TEXT` | `false` | `pypdf_parser`, `gemini_parser` | Extracted character limit exceeded (cumulative for PyPDF, per page for Gemini). |
| `PAGE_SPLIT_FAILED` | `false` | `gemini_parser` | A page could not be isolated into a single-page PDF. |
| `NO_PAGES` | `false` | `parsers/base.py` (`validate_pages`), `providers/gemini.py` | Parser returned no pages, or there is nothing to summarize. |
| `PAGE_SEQUENCE_INVALID` | `false` | `parsers/base.py` (`validate_pages`) | Pages are not ordered 1..N with no gaps/duplicates. Defensive: every current parser satisfies it. |
| `SOURCE_MISSING` | `false` | `app/processing.py` | The source object is gone from S3 (NoSuchKey/404). Not retryable: the bytes cannot be recovered. |
| `SOURCE_UNAVAILABLE` | `true` | `app/processing.py` | S3 read failed for another reason (connection/timeout). |
| `PARSER_TIMEOUT` | `true` | `app/processing.py` | Parser exceeded `PROVIDER_TIMEOUT_SECONDS + 30` wall clock. |
| `PARSER_UNAVAILABLE` | `false` | `app/processing.py` | The registry no longer offers the parser (configuration changed between accept and process). |
| `SUMMARIZER_UNAVAILABLE` | `false` | `app/processing.py` | No summarizer can be built (for example a job queued before the key was removed). |
| `SUMMARY_TIMEOUT` | `true` | `app/processing.py` | Summarizer exceeded `PROVIDER_TIMEOUT_SECONDS + 15` wall clock. |
| `SUMMARY_EMPTY` | `false` | `app/processing.py`, `providers/gemini.py` | The summarizer returned an empty/whitespace summary. |
| `PROVIDER_TIMEOUT` | `true` | `providers/gemini.py` | The model call exceeded the provider timeout. |
| `PROVIDER_ERROR` | `true` | `providers/gemini.py` | Unclassified provider/SDK failure (the safe default is transient). |
| `PROVIDER_CONFIG` | `false` | `providers/gemini.py` | 4xx other than 429, or a configuration-shaped error: wrong key, wrong model name, unsupported model. |
| `PROVIDER_REJECTED` | `false` | `providers/gemini.py` | Safety block / prompt feedback with a block reason. |
| `PROVIDER_EMPTY_RESPONSE` | `false` | `providers/gemini.py` | The model returned no usable text. |
| `PROVIDER_NOT_IMPLEMENTED` | `false` | `parsers/openai_parser.py` | The GPT-5 Mini adapter is a declaration only. Currently unreachable because the registry disables the parser before it can be selected. |
| `DEADLINE_EXCEEDED` | `false` | `app/processing.py` | The job's absolute deadline (`created_at + JOB_DEADLINE_SECONDS`) had already passed when an attempt started. |
| `ATTEMPTS_EXHAUSTED` | `false` | `app/worker.py` | More than `MAX_JOB_ATTEMPTS` attempts were needed. |
| `INTERNAL_ERROR` | `false` | `app/processing.py` (`classify`) | Unexpected exception. The client-facing message is generic; details are in the worker log only. |
| `PARSER_ERROR` | `false` | `parsers/base.py` (default class attribute) | Fallback code on the base class. No current code path raises `ParserError` without a specific code; listed for completeness. |

`retryable` semantics for a terminal document:

- `retryable: false` means the last failure was permanent (or the budget/deadline made it
  permanent); re-uploading after fixing the cause is the only path.
- `retryable: true` on a `failed` document means the last error was transient but the worker's
  bounded budget (`MAX_JOB_ATTEMPTS` or the deadline) ran out. The document can be retried by
  uploading it again; the API has no retry endpoint.
- While a document is still being retried, it shows `status: "retrying"` with the last `error`
  record, then returns to `parsing` on the next attempt.

No error code ever contains a stack trace, a PDF byte stream, extracted document text, or a
credential. `INTERNAL_ERROR` deliberately says "the server logs have the details".

---

## Limits

Defaults from `app/config.py`. Every limit is configurable through an environment variable of the
same upper-cased name; cross-field constraints are validated at startup (see the last table).

| Environment variable | Default | Enforced by | Visible effect |
|---|---|---|---|
| `MAX_UPLOAD_BYTES` | `20971520` (20 MiB) per file | `app/api/documents.py` bounded read | `413` per file |
| `MAX_BATCH_FILES` | `5` per request | upload handler, before reading | `413` |
| `MAX_BATCH_BYTES` | `52428800` (50 MiB) per request | upload handler, running total | `413` (nginx's 25 MB cap comes first through the UI proxy) |
| `MAX_PAGES` | `50` | `pypdf_parser`, fake PyPDF | `failed` with `TOO_MANY_PAGES` |
| `MAX_AI_PAGES` | `20` | `gemini_parser`, fake AI parsers | `failed` with `TOO_MANY_PAGES_FOR_AI` (or `TOO_MANY_PAGES` for the fake PyPDF path) |
| `MAX_EXTRACTED_CHARS` | `200000` | `pypdf_parser` (cumulative), `gemini_parser` (per page) | `failed` with `TOO_MUCH_TEXT` |
| `PROVIDER_TIMEOUT_SECONDS` | `60.0` | provider client and parser/summarizer `wait_for` guards | `PROVIDER_TIMEOUT` / `PARSER_TIMEOUT` / `SUMMARY_TIMEOUT` |
| `MAX_JOB_ATTEMPTS` | `3` | worker attempt loop | `ATTEMPTS_EXHAUSTED` (or `failed` with the last error) |
| `JOB_DEADLINE_SECONDS` | `600` | upload writes `deadline_at`; processor checks it | `DEADLINE_EXCEEDED` |
| `LEASE_SECONDS` | `90` | per-job lease TTL and reclaim idle threshold | Recovery latency after a killed worker |
| `HEARTBEAT_SECONDS` | `15` | worker heartbeat interval | Must stay below the lease |
| `RESULT_TTL_SECONDS` | `86400` (24 h) | terminal fenced commit | Terminal documents disappear from Redis and return `404` after this |
| `RETRY_BACKOFF_SECONDS` | `(2.0, 5.0, 10.0)` | worker between attempts | Delay before the next attempt |
| `WORKER_READ_BLOCK_MS` | `2000` | `XREADGROUP` block | Idle loop latency; also delays stale-entry sweeps |
| `RECLAIM_MIN_IDLE_MS` | `0` | `0` means "use `LEASE_SECONDS * 1000`" | `XAUTOCLAIM` threshold |
| `FAKE_DELAY_SECONDS` | `0.0` | fake parser/summarizer | Deterministic latency for fake mode |
| `LOG_LEVEL` | `INFO` | both processes' logging setup | Log verbosity |

Startup validation rejects inconsistent combinations (pydantic model validator in
`app/config.py`): `HEARTBEAT_SECONDS >= LEASE_SECONDS`, `LEASE_SECONDS * 3 >
JOB_DEADLINE_SECONDS`, `MAX_AI_PAGES > MAX_PAGES`, `MAX_BATCH_BYTES < MAX_UPLOAD_BYTES`, an
empty `RETRY_BACKOFF_SECONDS`, `WORKER_CONCURRENCY < 1`, `WORKER_BATCH_SIZE < 1`,
`WORKER_SHUTDOWN_GRACE_SECONDS <= 0`, `PROVIDER_MAX_CONCURRENCY < 1`,
`PROVIDER_MAX_REQUESTS_PER_MINUTE < 0`, and `RETRY_AFTER_MAX_SECONDS <= 0`.

**There is no server-side rate limit and no queue admission control.** The API accepts bursts as
fast as the network and S3 allow: 200 simultaneous unauthenticated uploads were accepted with 0
HTTP errors while the worker was saturated ([PERFORMANCE.md](PERFORMANCE.md)). Throughput is bounded
by the worker's `WORKER_CONCURRENCY` and, for AI parsers, by per-process provider pacing; those are
worker-side tuning knobs, not request limits, and they never change an HTTP status code in this
reference. The retry/pacing knobs are tabled in
[DEVELOPING.md](DEVELOPING.md#tuning-knobs).

Two environmental limits sit outside `app/config.py`:

- nginx: `client_max_body_size 25m` and proxy read/send timeouts of 120 s
  (`frontend/nginx.conf`).
- nginx also proxies `/health/`, so the UI's origin can be used for readiness checks.

---

## curl examples

Set a base URL for either direct or proxied access:

```bash
BASE=http://localhost:8000          # direct
# BASE=http://localhost:3000        # through nginx
```

### Upload one PDF

```bash
curl -sS -X POST "$BASE/api/v1/documents" \
  -F 'parser=pypdf' \
  -F 'files=@samples/sample-text.pdf;type=application/pdf'
```

### Upload a batch (one parser for the whole batch)

```bash
curl -sS -X POST "$BASE/api/v1/documents" \
  -F 'parser=gemini' \
  -F 'files=@samples/sample-text.pdf;type=application/pdf' \
  -F 'files=@samples/sample-blank.pdf;type=application/pdf'
```

Response is `202` with one `documents[]` entry per accepted file and one `rejected[]` entry per
rejected file. A batch is not atomic.

### Poll a document

```bash
curl -sS "$BASE/api/v1/documents/<document_id>"
```

Poll until `status` is `completed` or `failed`. For a shell loop, `scripts/smoke.sh` does this
with assertions and no dependencies beyond bash and curl.

### Parser inventory

```bash
curl -sS "$BASE/api/v1/parsers"
```

Executed live in this environment; the observed payload is the `200` example above.

### Health

```bash
curl -fsS "$BASE/health/live"     # 200 {"status":"ok"}
curl -fsS "$BASE/health/ready"    # 200 healthy, 503 degraded (body still has checks[])
```

### Expected rejection examples (safe to run; no job is created)

```bash
# Not a PDF -> 422, rejected[0].code = NOT_A_PDF
curl -sS -X POST "$BASE/api/v1/documents" \
  -F 'parser=pypdf' -F 'files=@samples/sample-corrupt.pdf;type=application/pdf'

# Unknown parser -> 422 with the allowed values in detail
curl -sS -X POST "$BASE/api/v1/documents" \
  -F 'parser=not-a-parser' -F 'files=@samples/sample-text.pdf;type=application/pdf'

# Disabled parser (always, in any mode) -> 503 with the reason
curl -sS -X POST "$BASE/api/v1/documents" \
  -F 'parser=gpt-5-mini' -F 'files=@samples/sample-text.pdf;type=application/pdf'
```

All three were executed against the live stack; bodies matched the examples above. The
`gpt-5-mini` refusal was re-checked in the registry after the change that made it unconditionally
disabled (fake mode: `enabled: false` with "not implemented in this delivery"; live mode with a
key: same; live mode without a key: disabled by the summarizer-blocking rule).

### What was and was not executed for this document

- Executed: `GET /health/live`, `GET /health/ready`, `GET /api/v1/parsers`,
  `GET /api/v1/documents/{unknown}`, `GET /` redirect, and the three rejection uploads above
  plus an empty-file upload and a no-files request. These create no jobs.
- Executed in-process (not over HTTP): constructing `ProviderRegistry` in a throwaway container for
  fake mode, live-with-key and live-without-key to capture the exact `enabled`/`note` matrix after
  the `gpt-5-mini` change.
- Not executed during the documentation pass: an accepted (`202`) upload with a real provider
  call, and the `413` oversize path. Accepted uploads and `413` are covered by the verification
  ledger ([PROGRESS.md](PROGRESS.md)) and the integration tests ([TESTING.md](TESTING.md)), which
  were recorded there before this document was written. An accepted upload was deliberately not
  repeated because the live demo runs in `AI_MODE=live` and would trigger a real model call.

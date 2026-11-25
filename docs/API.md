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
| `POST` | `/api/v1/documents` | Upload one or more PDFs and enqueue one job per accepted file | `202` | `413`, `422`, `429` (proxy only), `503` |
| `GET` | `/api/v1/documents` | List recent documents, newest first, with a backwards cursor (reload recovery) | `200` | `422`, `429` (proxy only), `503` |
| `GET` | `/api/v1/documents/{document_id}` | Poll a document: state, pages, summary, error | `200` | `404`, `503` |
| `GET` | `/api/v1/parsers` | Which parsers can actually run right now, and why not when they cannot | `200` | none (registry is in-process) |
| `GET` | `/health/live` | Process liveness, no dependency checks | `200` | none |
| `GET` | `/health/ready` | Dependency and AI-config readiness | `200` | `503` |
| `GET` | `/` | Redirect to `/docs` | `307` | none |

There are no delete, cancel, retry or history endpoints. There is no server-sent events or
websocket endpoint; the UI polls. Results expire after `RESULT_TTL_SECONDS` (24 h), after which
the status endpoint returns `404` and the document drops out of the list.

Two protective layers exist in front of the upload path and are described under
[Limits](#limits): a per-IP nginx rate limit (only when the request goes through the UI proxy on
`:3000`) and an in-process admission limit on simultaneous uploads (always). Neither is
authentication. `429` therefore only ever comes from the proxy.

---

## `POST /api/v1/documents`

`multipart/form-data` with:

| Field | Type | Required | Notes |
|---|---|---|---|
| `parser` | text | yes | One value for the whole batch: `pypdf`, `gemini`, or `gpt-5-mini`. |
| `files` | file, repeated | yes | One or more PDF files. Field name must be `files` each time. |

Processing order of validation (this order is observable):

1. **Admission control runs first.** The handler waits up to `UPLOAD_SLOT_TIMEOUT_SECONDS` (10 s)
   for one of `MAX_CONCURRENT_UPLOADS` (16) upload slots. If none frees up, the request fails with
   `503` and a `Retry-After: 1` header **before** the parser or file fields are validated. This is
   per API process, not global.
2. FastAPI validates the multipart request shape (both `parser` and `files` are required fields)
   -> missing fields produce FastAPI's `422` validation body.
3. `parser` is a known `ParserId` value -> otherwise `422`.
4. The parser is currently enabled in the registry -> otherwise `503`. This happens **before**
   any file is read, so a request naming a disabled parser returns `503` even when its files are
   corrupt.
5. `len(files) <= MAX_BATCH_FILES` -> otherwise `413`.
6. Per file, in order: bounded read (`MAX_UPLOAD_BYTES`) -> `413` if exceeded; running batch total
   (`MAX_BATCH_BYTES`) -> `413` if exceeded; empty -> `rejected[] EMPTY_FILE`; missing `%PDF-`
   signature -> `rejected[] NOT_A_PDF`.
7. Per accepted file: store the source in S3, then `MULTI {HSET job + XADD + ZADD docs:index}`. A
   definite S3 or Redis failure is reported per file as `rejected[] STORAGE_UNAVAILABLE` /
   `QUEUE_UNAVAILABLE` and the object is deleted on the Redis failure path.
8. At least one accepted file -> `202`. No accepted file -> `422`, unless every failure was a
   storage/queue failure, in which case `503`.

Batch semantics: a batch is **not** atomic. A corrupt file is reported in `rejected[]` while valid
files are accepted with their own UUIDs and processed independently.

Through the UI proxy (`:3000`), nginx also rate-limits this exact path at 20 requests/second per
client IP with a burst of 40 (`limit_req ... zone=uploads`), and answers `429` with a JSON body
instead of reaching the API (see [Limits](#limits)). The limit bucket is keyed on
`$binary_remote_addr`, so it is **per IP**, not per user; the same URL is shared by `GET` and
`POST`, so listing and uploading draw from the same bucket. A direct call to `:8000` bypasses the
nginx limits (the admission semaphore still applies).

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

Three different bodies share this status:

- **Parser not enabled** - HTTPException shape. Observed live in this environment (live mode, no
  OpenAI key):

  ```json
  {"detail": "Parser 'gpt-5-mini' is not available right now. Parser 'gpt-5-mini' is not available: OPENAI_API_KEY is not configured"}
  ```

  In `AI_MODE=live` with no Gemini key every parser is disabled, and the detail explains that
  summarization is unavailable and every document requires a summary (the exact reason string is
  produced by `ProviderRegistry._blocking_reason`; see the parsers endpoint below).

- **Every file failed at storage/queue time** - `UploadResponse` shape with `documents: []` and
  `rejected[]` entries whose `code` is `STORAGE_UNAVAILABLE` or `QUEUE_UNAVAILABLE`.
- **Admission control timed out** - HTTPException shape, with a `Retry-After: 1` header:

  ```json
  {"detail": "The server is at its concurrent-upload limit (MAX_CONCURRENT_UPLOADS=16). Retry shortly."}
  ```

---

## `GET /api/v1/documents`

Lists recent documents, newest first - the endpoint that lets the UI rebuild its cards after a
page refresh. Backed by the Redis sorted set `docs:index` (score = enqueue epoch).

Query parameters (FastAPI-validated):

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `limit` | integer, 1-100 | `20` | Page size. `0`, `101` or a non-integer -> `422`. |
| `before` | float (epoch seconds), optional | none | Only documents created **strictly before** this score. Pass the previous response's `next_before`. |

### `200` response (verified against the live stack)

`DocumentList` (`app/models.py`):

```json
{
  "documents": [
    {
      "id": "9eb651bf-92f4-486a-8d72-d8d9090a2331",
      "filename": "sample-text.pdf",
      "parser": "pypdf",
      "status": "completed",
      "page_count": 2,
      "created_at": "2026-11-25T20:15:32Z",
      "status_url": "/api/v1/documents/9eb651bf-92f4-486a-8d72-d8d9090a2331"
    }
  ],
  "next_before": 1795637732.6776729
}
```

Fields:

- `documents[]` items are `DocumentSummary`: `id`, `filename`, `parser`, `status`, `page_count`
  (null until known), `created_at`, `status_url`. No pages, no summary - fetch the detail endpoint
  for those.
- Order is newest first (descending zset score).
- `next_before` is the **zset score** (enqueue epoch) of the last returned document, not its
  `created_at` string. It is `null` when there are no further rows; pass it back as `before` to
  walk backwards without duplicates (the bound is exclusive).
- Documents whose job hash has expired are skipped, so a page may return fewer than `limit` rows
  even when `next_before` is non-null.
- The index is pruned to `RESULT_TTL_SECONDS` on every accepted upload, so documents older than
  the retention window are not listed; if no uploads happen for a long time, stale index entries
  linger but are skipped because their hashes expired.

### Status codes

| Status | When | Body |
|---|---|---|
| `200` | Always for a valid request | `{documents, next_before}` |
| `422` | `limit` outside 1-100, or `before` not a number | FastAPI validation body (`detail[]`) |
| `429` | Proxy only: nginx per-IP limit on this path (20 r/s, burst 40) | `{"detail": "Rate limit exceeded. Retry shortly.", "code": "RATE_LIMITED"}` |
| `503` | Redis unavailable | `{"detail": "The job store is unavailable; try again shortly."}` |

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
     "note": "OPENAI_API_KEY is not configured"}
  ]
}
```

The payload above is the observed live response in this environment (Gemini key configured, no
OpenAI key). Fields per `ParserInfo`:

| Field | Type | Meaning |
|---|---|---|
| `id` | `"pypdf" \| "gemini" \| "gpt-5-mini"` | Parser identifier used in the upload form |
| `label` | string | Display name (`PyPDF`, `Google Gemini`, `GPT-5 Mini`) |
| `output_format` | `"text" \| "markdown"` | What `pages[].format` will be for this parser |
| `enabled` | boolean | `true` only if the parser would actually run for a new upload right now |
| `requires_api_key` | boolean | Whether the parser needs a provider credential (false only for PyPDF) |
| `note` | string or null | Why it is disabled, or a description of how it extracts. A disabled parser never has a null/empty note (asserted by the integration suite). |

All three parsers are **real implementations**: `pypdf` (local text), `gemini` (one model call per
page) and `gpt-5-mini` (one Responses-API call per page, page sent as an inline PDF). The summary
is always Gemini, whichever parser extracted the pages.

Why a parser can be disabled, and what the note says (traced to `app/registry.py`; the whole matrix
was reproduced in a throwaway container by constructing the registry with each setting):

| Situation | `ai_mode` | Enabled parsers | Notes |
|---|---|---|---|
| Live, Gemini key + OpenAI key | `live` | `pypdf`, `gemini`, `gpt-5-mini` | All three. `gpt-5-mini` note is the base "Optional provider; enabled only when configured." |
| Live, Gemini key only | `live` | `pypdf`, `gemini` | `gpt-5-mini` disabled: "OPENAI_API_KEY is not configured" |
| Live, OpenAI key only | `live` | none | Every parser disabled by the summarization-blocking rule below: the summary is Gemini-only, so an OpenAI extraction would still fail at the end. Note names the missing Gemini key. |
| Live, no provider keys | `live` | none | Every parser disabled, same blocking note ("summarization is unavailable (gemini_api_key not configured) ..."). Upload returns `503`. |
| Fake mode | `fake` | `pypdf`, `gemini`, `gpt-5-mini` | Both AI parsers are replaced by deterministic stand-ins and carry "fake mode: deterministic stand-in, no model call." PyPDF keeps its real note because it is the local extractor. |
| Python adapter import failure | any | depends | "adapter unavailable: <ExceptionType>" - the process does not crash |
| Fake provider construction failure | `fake` | depends | "fake provider unavailable" |

Three consequences worth knowing:

- The **whole parser list is disabled together** when summarization cannot run. Every document
  requires a summary and the summary is Gemini-only (`_blocking_reason` in `app/registry.py`), so
  accepting any upload would mean accepting work guaranteed to fail after the expensive extraction.
  This is why an OpenAI-only live configuration enables nothing.
- **`gpt-5-mini` needs both credentials in live mode**: `OPENAI_API_KEY` to construct the parser and
  `GEMINI_API_KEY` because the summary still comes from Gemini. In fake mode it is a labelled
  stand-in like Gemini.
- **The OpenAI path is implemented but not live-verified.** No OpenAI credentials existed here, so
  the request payload, page ordering, page cap and error mapping are asserted against a stub
  (`tests/unit/test_openai_parser.py`); a live call was never made. The Gemini path *has* been
  live-verified (see [REQUIREMENTS.md](REQUIREMENTS.md) G1).

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

There are three distinct surfaces. Upload-time rejections appear in `rejected[].code` with the HTTP
status of the request; processing failures appear in `error.code` on a document that polled to
`status: "failed"` (the HTTP status of that poll is still `200`); proxy-level rejections (nginx)
never reach the API at all. `retryable` only exists on the processing-error record (`ErrorInfo`);
upload rejections have `code` and `message` only.

### Upload-time rejections (`rejected[].code`)

| Code | HTTP | Raised in | Meaning / client action |
|---|---|---|---|
| `EMPTY_FILE` | `422` (or inside a `202` batch) | `app/api/documents.py` | Zero-byte upload. Send a real PDF. |
| `NOT_A_PDF` | `422` (or inside a `202` batch) | `app/api/documents.py` | The body does not start with the `%PDF-` signature. The check is on bytes, never on filename or MIME type. |
| `STORAGE_UNAVAILABLE` | `503` when nothing was accepted; inside `202`/`422` otherwise | `app/api/documents.py` (`ObjectStoreError` on PUT) | S3 (LocalStack) is unavailable. Retry later. |
| `QUEUE_UNAVAILABLE` | `503` when nothing was accepted; inside `202`/`422` otherwise | `app/api/documents.py` (`RedisError` on the MULTI) | Redis is unavailable; the just-uploaded object was deleted. Retry later. |

### Proxy-level rejection (nginx only, never reaches the API)

| Code | HTTP | Raised in | Meaning / client action |
|---|---|---|---|
| `RATE_LIMITED` | `429` | `frontend/nginx.conf` (`limit_req` + `limit_req_status 429` + a JSON `error_page`) | Per-IP request-rate limit exceeded on the UI origin. Body: `{"detail": "Rate limit exceeded. Retry shortly.", "code": "RATE_LIMITED"}`. Back off and retry; it is not in the FastAPI OpenAPI schema because FastAPI never sees it. Direct access to `:8000` is not rate-limited by nginx. |

Admission-control timeouts are `503` with an HTTPException `detail` (no stable `code` field), plus
a `Retry-After: 1` header - see the `POST` section.

### Processing failures (`error.code` on a `failed` document)

| Code | `retryable` | Raised in | Meaning |
|---|---|---|---|
| `OCR_REQUIRED` | `false` | `pypdf_parser`, `gemini_parser`, `openai_parser`, `pdf_pages`, `providers/fake.py` | No extractable text at all (PyPDF) / all pages empty (AI parsers) / a PDF with zero pages (shared splitter; it raises `OcrRequiredError` for that). The message suggests choosing another parser. Documented outcome for `samples/sample-blank.pdf` with `pypdf`. |
| `INVALID_PDF` | `false` | `pypdf_parser`, `pdf_pages` (both AI parsers), `providers/fake.py` | PDF could not be parsed (corrupt xref, truncated file, ...). Upload-time only checks the signature. |
| `ENCRYPTED_PDF` | `false` | `pypdf_parser`, `pdf_pages` (both AI parsers), `providers/fake.py` | Password-protected PDF; the app never prompts for a password. |
| `TOO_MANY_PAGES` | `false` | `pypdf_parser`, `providers/fake.py` | Over `MAX_PAGES` for the document parser. |
| `TOO_MANY_PAGES_FOR_AI` | `false` | `pdf_pages` (shared by Gemini and GPT-5 Mini) | Over `MAX_AI_PAGES`; the message suggests the PyPDF parser. |
| `TOO_MUCH_TEXT` | `false` | `pypdf_parser`, `gemini_parser`, `openai_parser` | Extracted character limit exceeded (cumulative for PyPDF, per page for the AI parsers). |
| `PAGE_SPLIT_FAILED` | `false` | `pdf_pages` (shared splitter) | A page could not be isolated into a single-page PDF. |
| `NO_PAGES` | `false` | `parsers/base.py` (`validate_pages`), `providers/gemini.py` | Parser returned no pages, or there is nothing to summarize. |
| `PAGE_SEQUENCE_INVALID` | `false` | `parsers/base.py` (`validate_pages`) | Pages are not ordered 1..N with no gaps/duplicates. Defensive: every current parser satisfies it. |
| `SOURCE_MISSING` | `false` | `app/processing.py` | The source object is gone from S3 (NoSuchKey/404). Not retryable: the bytes cannot be recovered. |
| `SOURCE_UNAVAILABLE` | `true` | `app/processing.py` | S3 read failed for another reason (connection/timeout). |
| `PARSER_TIMEOUT` | `true` | `app/processing.py` | Parser exceeded `PROVIDER_TIMEOUT_SECONDS + 30` wall clock. |
| `PARSER_UNAVAILABLE` | `false` | `app/processing.py` | The registry no longer offers the parser (configuration changed between accept and process). |
| `SUMMARIZER_UNAVAILABLE` | `false` | `app/processing.py` | No summarizer can be built (for example a job queued before the key was removed). |
| `SUMMARY_TIMEOUT` | `true` | `app/processing.py` | Summarizer exceeded `PROVIDER_TIMEOUT_SECONDS + 15` wall clock. |
| `SUMMARY_EMPTY` | `false` | `app/processing.py`, `providers/gemini.py` | The summarizer returned an empty/whitespace summary. |
| `PROVIDER_TIMEOUT` | `true` | `providers/gemini.py`, `providers/openai_adapter.py` | The model call exceeded the provider timeout. |
| `PROVIDER_ERROR` | `true` | `providers/gemini.py`, `providers/openai_adapter.py` | Unclassified provider/SDK failure (the safe default is transient). |
| `PROVIDER_CONFIG` | `false` | `providers/gemini.py`, `providers/openai_adapter.py` | Configuration-shaped permanent failure: Gemini 4xx other than 429; OpenAI 401/403 (check `OPENAI_API_KEY`) or another 4xx such as an unknown model/account without access. |
| `PROVIDER_REJECTED` | `false` | `providers/gemini.py` | Safety block / prompt feedback with a block reason (Gemini-specific). |
| `PROVIDER_EMPTY_RESPONSE` | `false` | `providers/gemini.py`, `providers/openai_adapter.py` | The model returned no usable text / an empty transcription. |
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
| `MAX_CONCURRENT_UPLOADS` | `16` | API process semaphore (`app/api/documents.py`) | Past the limit, requests wait and then get `503` + `Retry-After: 1` |
| `UPLOAD_SLOT_TIMEOUT_SECONDS` | `10.0` | same semaphore acquisition | How long a request waits for a free upload slot before the `503` |
| `MAX_PAGES` | `50` | `pypdf_parser`, fake PyPDF | `failed` with `TOO_MANY_PAGES` |
| `MAX_AI_PAGES` | `20` | `pdf_pages` (Gemini and GPT-5 Mini), fake AI parsers | `failed` with `TOO_MANY_PAGES_FOR_AI` (or `TOO_MANY_PAGES` for the fake PyPDF path) |
| `MAX_EXTRACTED_CHARS` | `200000` | `pypdf_parser` (cumulative), Gemini/OpenAI parsers (per page) | `failed` with `TOO_MUCH_TEXT` |
| `PROVIDER_TIMEOUT_SECONDS` | `60.0` | both provider clients and parser/summarizer `wait_for` guards | `PROVIDER_TIMEOUT` / `PARSER_TIMEOUT` / `SUMMARY_TIMEOUT` |
| `PROVIDER_MAX_CONCURRENCY` | `4` | `ProviderPacer` in each provider client | Client-side concurrency per client instance |
| `PROVIDER_MAX_REQUESTS_PER_MINUTE` | `0` (off) | `ProviderPacer` token bucket | Client-side rate per client instance; `0` disables |
| `RETRY_AFTER_MAX_SECONDS` | `30.0` | worker retry delay | Clamp on a provider's `Retry-After` |
| `MAX_JOB_ATTEMPTS` | `3` | worker attempt loop | `ATTEMPTS_EXHAUSTED` (or `failed` with the last error) |
| `JOB_DEADLINE_SECONDS` | `600` | upload writes `deadline_at`; processor checks it | `DEADLINE_EXCEEDED` |
| `LEASE_SECONDS` | `90` | per-job lease TTL and reclaim idle threshold | Recovery latency after a killed worker |
| `HEARTBEAT_SECONDS` | `15` | worker heartbeat interval | Must stay below the lease |
| `RESULT_TTL_SECONDS` | `86400` (24 h) | terminal fenced commit; also the `docs:index` pruning window | Terminal documents disappear from Redis, return `404`, and drop out of the list |
| `RETRY_BACKOFF_SECONDS` | `(2.0, 5.0, 10.0)` | worker between attempts | Base delay before the next attempt (jittered; `Retry-After` preferred) |
| `WORKER_CONCURRENCY` | `4` | worker dispatch semaphore | Jobs in parallel per worker process |
| `WORKER_BATCH_SIZE` | `8` | `XREADGROUP`/`XAUTOCLAIM` count | Entries fetched per read |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | `30.0` | worker SIGTERM drain | How long in-flight jobs may finish before cancellation |
| `WORKER_READ_BLOCK_MS` | `2000` | `XREADGROUP` block | Idle loop latency; also delays stale-entry sweeps |
| `RECLAIM_MIN_IDLE_MS` | `0` | `0` means "use `LEASE_SECONDS * 1000`" | `XAUTOCLAIM` threshold |
| `FAKE_DELAY_SECONDS` | `0.0` | fake parser/summarizer | Deterministic latency for fake mode |
| `LOG_LEVEL` | `INFO` | both processes' logging setup | Log verbosity |

Startup validation rejects inconsistent combinations (pydantic model validator in
`app/config.py`): `HEARTBEAT_SECONDS >= LEASE_SECONDS`, `LEASE_SECONDS * 3 >
JOB_DEADLINE_SECONDS`, `MAX_AI_PAGES > MAX_PAGES`, `MAX_BATCH_BYTES < MAX_UPLOAD_BYTES`, an
empty `RETRY_BACKOFF_SECONDS`, `WORKER_CONCURRENCY < 1`, `WORKER_BATCH_SIZE < 1`,
`WORKER_SHUTDOWN_GRACE_SECONDS <= 0`, `PROVIDER_MAX_CONCURRENCY < 1`,
`PROVIDER_MAX_REQUESTS_PER_MINUTE < 0`, `MAX_CONCURRENT_UPLOADS < 1`,
`UPLOAD_SLOT_TIMEOUT_SECONDS <= 0`, and `RETRY_AFTER_MAX_SECONDS <= 0`.

### Request-rate and concurrency limits (two layers, neither is authentication)

| Layer | Where | Limit | Applies to | Response when exceeded |
|---|---|---|---|---|
| nginx rate limit, uploads zone | `frontend/nginx.conf`, http context | `20 r/s` per client IP, burst `40` (`nodelay`) | **Exactly** `/api/v1/documents`, both `POST` (upload) and `GET` (list) | `429` + JSON `{"detail","code":"RATE_LIMITED"}` (proxy-generated) |
| nginx rate limit, api zone | same file | `100 r/s` per client IP, burst `200` (`nodelay`) | every other `/api/` path | `429` + same JSON body |
| Admission control | `app/api/documents.py` | `16` simultaneous uploads per API process; 10 s wait | `POST /api/v1/documents` only (list requests are not admission-controlled) | `503` + `Retry-After: 1` |

Notes that matter:

- Both rate limits are **per client IP**, not per user or account (there are no users), and they
  only exist on the UI origin. Calling the API directly on `:8000` bypasses nginx; admission
  control still applies there because it lives in the application.
- The upload zone and the list endpoint share one bucket because they share one URL. A heavy
  uploader and a heavy lister from the same IP interfere with each other.
- The limits are configuration, not invariants: raise the zone rates/bursts in
  `frontend/nginx.conf` (http context - **not** inside `server`) and `MAX_CONCURRENT_UPLOADS` in
  `.env` if a legitimate client needs more.
- Measured: a 150-request burst from one IP through nginx produced 64 `429` responses with the
  documented body (coordinator-run); a documentation-pass repeat with 200 rapid `GET`s produced 90
  `429` / 110 `200`. The exact split depends on timing - the limit is a rate, not a hard cap.
- **There is no per-client quota, no authentication and no queue-depth cap.** The API still accepts
  large volumes of work (200 simultaneous uploads were accepted with 0 errors before admission
  control existed; now at most 16 are buffered at once, but the queue behind them is unbounded).
  Throughput is bounded by the worker's `WORKER_CONCURRENCY` and, for AI parsers, by client-side
  pacing - worker-side knobs that are tabled in
  [DEVELOPING.md](DEVELOPING.md#tuning-knobs).

Two environmental limits sit outside `app/config.py`:

- nginx: `client_max_body_size 25m`, proxy read/send timeouts of 120 s, and the two `limit_req`
  zones above (`frontend/nginx.conf`).
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

### List recent documents / paginate

```bash
# Newest 20 (the UI uses this after a page refresh)
curl -sS "$BASE/api/v1/documents?limit=20"

# Walk backwards: pass the previous response's next_before as before
curl -sS "$BASE/api/v1/documents?limit=20&before=1795637732.6776729"

# Out-of-range limit -> 422 (FastAPI validation body)
curl -sS "$BASE/api/v1/documents?limit=101"
```

Executed live in this environment: the `200` shape above, `limit=0/101` -> `422`, and
`before=0` -> `200 {"documents":[],"next_before":null}`.

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

# Disabled parser -> 503 with the reason (gpt-5-mini in live mode without OPENAI_API_KEY)
curl -sS -X POST "$BASE/api/v1/documents" \
  -F 'parser=gpt-5-mini' -F 'files=@samples/sample-text.pdf;type=application/pdf'

# Admission control: only reproducible with 16+ simultaneous uploads; response is 503 +
# Retry-After: 1 and the body names MAX_CONCURRENT_UPLOADS (see Limits).
```

All three rejection examples were executed against the live stack; bodies matched the examples
above. The `gpt-5-mini` case currently returns `503` here only because this environment has no
`OPENAI_API_KEY`; with that key set (and the Gemini summary key, which is already configured) the
parser is enabled - see the parser matrix above.

### What was and was not executed for this document

- Executed against the live stack: `GET /health/live`, `GET /health/ready`, `GET /api/v1/parsers`
  (the exact live payload shown above), `GET /api/v1/documents?limit=2` (exact list payload),
  `GET /api/v1/documents?limit=0`, `limit=101` (both `422`), `before=0` (empty list),
  `GET /api/v1/documents/{unknown}` (`404`), `GET /` redirect, and the three rejection uploads
  above plus an empty-file upload and a no-files request (these create no jobs).
- Executed against the UI proxy: a 200-request rapid `GET /api/v1/documents` burst produced
  90 x `429` and 110 x `200`. The `429` body is specified in `frontend/nginx.conf`; it was not
  captured verbatim in the burst (the bucket refilled between attempts), so treat the body text
  as configured rather than as an observed response here.
- Executed in-process (not over HTTP): constructing `ProviderRegistry` in a throwaway container
  for fake mode, live-no-keys, live-Gemini-only, live-OpenAI-only and live-both to capture the
  exact `enabled`/`note` matrix. Also executed: the repository/lease suite (14 passed) and the
  unit suite (44 passed) - see [TESTING.md](TESTING.md).
- Not executed during the documentation pass: admission-control `503` (needs 16 simultaneous
  uploads), an accepted (`202`) upload with a live provider call, and the `413` oversize path.
  Those are covered by the ledger ([PROGRESS.md](PROGRESS.md)), the integration suite, and the
  e2e suite. An accepted upload was deliberately not repeated by this pass because the live demo
  runs in `AI_MODE=live` and would trigger real provider calls.

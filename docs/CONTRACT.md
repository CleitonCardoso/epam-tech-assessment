# CONTRACT — frozen interfaces

Everything in this file is frozen. If an implementation needs it changed, stop and report;
do not change it silently, because another agent is already building against it.

Source of truth for the assessment: `BWAI2.1 test task.docx` (3 pages).
Design review and deviations from the ChatGPT plan: `docs/DECISIONS.md`.
Live status: `docs/PROGRESS.md`.

---

## 1. Ownership map — do not edit files outside your scope

| Owner | Paths |
|---|---|
| **coordinator** (lead) | `app/config.py`, `app/models.py`, `app/parsers/base.py`, `app/registry.py`, `app/storage/**`, `app/queue.py`, `app/processing.py`, `app/worker.py`, `app/main.py`, `app/api/**`, `docs/**`, `pyproject.toml`, `requirements*.txt`, `README.md`, `.gitignore` |
| **providers** | `app/parsers/pypdf_parser.py`, `app/parsers/gemini_parser.py`, `app/parsers/openai_parser.py`, `app/providers/**`, `tests/unit/test_parsers*.py` |
| **infra** | `Dockerfile`, `.dockerignore`, `compose.yaml`, `infra/**`, `.env.example` |
| **frontend** | `frontend/**` (nothing else) |
| **qa** | `scripts/**`, `samples/**`, `tests/integration/**`, `tests/conftest.py` |

If you believe a file outside your scope must change, message the coordinator instead.

---

## 2. Module layout

```text
app/
  __init__.py
  config.py            Settings (pydantic-settings)         [frozen]
  models.py            API + internal models                [frozen]
  registry.py          builds parsers/summarizers from settings
  main.py              FastAPI app, lifespan, /health/*
  api/
    __init__.py
    documents.py       POST /api/v1/documents, GET /api/v1/documents/{id}, GET /api/v1/parsers
  storage/
    __init__.py
    objects.py         S3 adapter (boto3, offloaded to a thread)
    jobs.py            Redis repository: create/enqueue, get, pages, summary, fenced commit
  queue.py             consumer group create / read / claim / ack / delete
  processing.py        one job end-to-end: parse -> checkpoint -> summarize -> commit
  worker.py            process entrypoint: loop, heartbeat, SIGTERM
  parsers/
    __init__.py
    base.py            Parser/Summarizer protocols, ParseResult, errors   [frozen]
    pypdf_parser.py    PyPdfParser
    gemini_parser.py   GeminiParser
    openai_parser.py   OpenAIParser (optional)
  providers/
    __init__.py
    gemini.py          GeminiClient + GeminiSummarizer
    fake.py            FakeParser + FakeSummarizer
```

**Do not name a module `pypdf.py`.** It shadows the `pypdf` dependency and produces import
errors that look unrelated.

---

## 3. Settings (env var = field name upper-cased)

Defined in `app/config.py`; read it, don't duplicate it. Defaults target the Compose network
(`redis://redis:6379/0`, `http://localstack:4566`), so `docker compose up` works with no `.env`.

Key variables: `REDIS_URL`, `S3_ENDPOINT_URL`, `S3_BUCKET`, `AWS_REGION`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AI_MODE` (`live|fake`), `GEMINI_API_KEY`,
`GEMINI_MODEL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, limits (`MAX_*`), lifecycle
(`MAX_JOB_ATTEMPTS`, `JOB_DEADLINE_SECONDS`, `LEASE_SECONDS`, `HEARTBEAT_SECONDS`,
`RESULT_TTL_SECONDS`), `FAKE_DELAY_SECONDS`, `LOG_LEVEL`.

`AI_MODE=fake` is an explicit, labelled mode for tests/demos without credentials. In `live`
mode there is **no fallback to fakes** — see §7.

---

## 4. Provider interfaces

`app/parsers/base.py` is frozen: `Parser`, `Summarizer`, `ParseLimits`, `ParseResult`,
`ParserError` / `PermanentError` / `TransientError` / `OcrRequiredError`, `validate_pages`.

Classes the registry will import, with **exactly these names and constructor signatures**:

```python
# app/parsers/pypdf_parser.py
class PyPdfParser:                     # id = ParserId.PYPDF
    id = ParserId.PYPDF
    async def parse(self, pdf: bytes, *, filename: str, limits: ParseLimits) -> ParseResult: ...

# app/parsers/gemini_parser.py
class GeminiParser:
    id = ParserId.GEMINI               # output format: markdown
    def __init__(self, *, api_key: str, model: str, timeout_seconds: float) -> None: ...
    async def parse(self, pdf: bytes, *, filename: str, limits: ParseLimits) -> ParseResult: ...

# app/parsers/openai_parser.py       (optional stretch)
class OpenAIParser:
    id = ParserId.GPT5_MINI            # output format: markdown
    def __init__(self, *, api_key: str, model: str, timeout_seconds: float) -> None: ...
    async def parse(self, pdf: bytes, *, filename: str, limits: ParseLimits) -> ParseResult: ...

# app/providers/gemini.py
class GeminiSummarizer:
    def __init__(self, *, api_key: str, model: str, timeout_seconds: float) -> None: ...
    async def summarize(self, pages: list[PageResult], *, timeout_seconds: float) -> str: ...

# app/providers/fake.py
class FakeParser:
    def __init__(self, parser_id: ParserId, *, delay_seconds: float = 0.0) -> None: ...
class FakeSummarizer:
    def __init__(self, *, delay_seconds: float = 0.0) -> None: ...
```

Rules for every implementation:

- Never let a vendor SDK type escape the module.
- Map every failure to `TransientError` (429, timeout, 5xx, connection reset) or
  `PermanentError` (4xx other than 429, unsupported model, bad input, safety block).
  Never raise a bare exception for an expected failure.
- Never log PDF bytes, extracted content, prompts or API keys.
- `PageResult.page_number` is 1-based; return pages in document order. The parser does not
  need to call `validate_pages` — the worker does.
- Blocking work (PyPDF) must run off the event loop: `await asyncio.to_thread(...)`.
- `FakeParser`/`FakeSummarizer` must be deterministic (same input → same output), honour
  `delay_seconds`, and never touch the network. `FakeParser` produces `markdown` pages.

---

## 5. HTTP API (already frozen; do not change without the coordinator)

### `POST /api/v1/documents`
`multipart/form-data`: repeated `files`, one `parser` per batch (`pypdf|gemini|gpt-5-mini`).

Per file: bounded chunked read, reject empty, reject `%PDF-` signature missing, reject
`> MAX_UPLOAD_BYTES`. Batch: `> MAX_BATCH_FILES` or aggregate `> MAX_BATCH_BYTES` → 413.

| Status | When |
|---|---|
| `202` | at least one file accepted → `{documents:[...], rejected:[...]}` |
| `422` | bad `parser`, no files, every file invalid |
| `413` | a size/count limit was exceeded |
| `503` | Redis or S3 unavailable, or the selected parser is not enabled |

`DocumentAccepted` = `{id, filename, parser, status, status_url}` (`status` always `queued`).

### `GET /api/v1/documents/{id}`
`200` for any known state (`pages`/`summary` null-or-empty until ready), `404` unknown or
expired, `503` Redis unavailable. `DocumentDetail` adds `pages[]` and `summary`.

### `GET /api/v1/parsers`
`{ai_mode, parsers:[{id,label,output_format,enabled,requires_api_key,note}]}`. Only parsers
that would actually work are `enabled: true`. Never advertise an unimplemented provider.

### Health
- `/health/live` → `200 {"status":"ok"}`; no dependency checks.
- `/health/ready` → `200` when Redis `PING`, S3 `head_bucket` succeeds and, in live mode,
  a Gemini key is configured; otherwise `503` with `checks` naming what failed.

---

## 6. Redis schema (authoritative state)

| Key | Type | Contents |
|---|---|---|
| `docs:jobs` | Stream | field `document_id` only |
| `docs:job:<id>` | Hash | `id, filename, parser, status, stage, attempt, created_at, updated_at, source_key, page_count, warnings (JSON), error (JSON or empty), deadline_at` |
| `docs:pages:<id>` | String (JSON) | `{"pages":[…],"warnings":[…]}` |
| `docs:summary:<id>` | String | summary markdown |
| `docs:lease:<id>` | String + TTL | unique execution token |

- Consumer group `document-workers` on `docs:jobs`, created at `0` with `MKSTREAM`;
  tolerate only `BUSYGROUP`.
- Terminal jobs: set `RESULT_TTL_SECONDS` on every record key.
- No PDF bytes in Redis or in stream entries.

---

## 7. Live vs fake AI

| `AI_MODE` | Gemini/OpenAI parsers | `/health/ready` | Upload of an AI parser |
|---|---|---|---|
| `live` + key | real SDK calls | `200` | accepted |
| `live` + no key | not enabled | `503` (`gemini_api_key`) | `503`, never queued |
| `fake` | `FakeParser` / `FakeSummarizer` | `200`, `ai_mode` reported | accepted |

`fake` is never a silent fallback. It is visible in `/api/v1/parsers`, in `/health/ready`
(`checks.ai_mode`), in the worker's startup log and in the README.

---

## 8. Compose topology (infra agent)

| Service | Image | Ports | Notes |
|---|---|---|---|
| `redis` | pinned `redis:7.x-alpine` | internal only | AOF on, named volume, healthcheck `redis-cli ping` |
| `localstack` | pinned `localstack/localstack:3.x` | `127.0.0.1:4566` | `SERVICES=s3` only; it creates no buckets — the `provisioner` service applies Terraform; healthcheck through python urllib on `/_localstack/health` |
| `provisioner` | `hashicorp/terraform:1.9` | none | one-shot: `terraform init -backend=false && apply -auto-approve` in `/tf/envs/local`, mounted from `./infra`; `api`/`worker` wait on `service_completed_successfully` |
| `api` | built image | `127.0.0.1:8000:8000` | `uvicorn app.main:app`, waits for deps, healthcheck `/health/live` |

**Why the container healthcheck is `/health/live` and not `/health/ready`:** readiness returns
`503` when Redis/S3 are down *or*, in `live` mode, when no Gemini key is configured. That is a
deliberate work-availability signal for a human — it must not make Docker mark a perfectly
healthy process unhealthy (and, with `depends_on: service_healthy`, hold the whole stack).
Compose therefore watches liveness; readiness stays the reviewer-facing signal.
| `worker` | **same image**, `python -m app.worker` | none | same env as api |
| `frontend` | built from `frontend/` | `127.0.0.1:3000:80` | nginx proxies `/api` + `/health` to `api:8000` |

Bind published ports to `127.0.0.1` — Redis and S3 must not be exposed on the LAN.

The bucket is **not** created by an init script. Terraform owns it (`infra/envs/local`), applied by
the `provisioner` service; `TF_VAR_bucket_name` is wired from `S3_BUCKET` so the provisioned bucket
and the bucket the app reads are always the same name.

The API and worker must tolerate the bucket not existing yet at process start: readiness reports it,
job processing retries, and nothing crashes the process.

---

## 9. Testing contract

- `pytest -q` runs everything; `pytest -m "not integration"` runs the offline unit tests.
- Integration tests may assume `redis` and `localstack` from Compose are reachable at
  `REDIS_URL`/`S3_ENDPOINT_URL`; they must use their **own key prefix and bucket** and never
  flush the database.
- No test may call a paid AI provider. Deterministic fakes only. A live Gemini smoke test is
  opt-in via `RUN_LIVE_AI_TESTS=1` and skipped by default, and it must be reported as
  *skipped*, never as *passed*, when credentials are absent.
- Prefer synchronization events over sleeps; poll with deadlines.

---

## 10. Definition of honest

Never in this repository: a claimed test result that was not executed, a parser advertised
as working when it is a stub, a hidden mock fallback in live mode, a progress percentage we
do not actually compute, or a README command that has not been run.

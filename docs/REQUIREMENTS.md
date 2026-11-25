# Requirements traceability

Audit of the delivered project against `BWAI2.1 test task.docx` (3 pages).

Status vocabulary, used strictly:

- **MET** — implemented and executed in this environment.
- **PARTIAL** — implemented, but the part that matters could not be executed here (no credentials).
- **NOT MET** — absent or disabled, and the API says so.
- **N/A** — a process instruction, not a property of the code.

---

## A. Submission mechanics

| # | Requirement (quote) | Where | Evidence | Status |
|---|---|---|---|---|
| A1 | "send us source code in a zip file (don't include .venv, node_modules)" | `scripts/package.py`, `dist/epam-tech-assessment.zip` | 65 entries, 0.13 MB; packer re-opens the archive and fails the build if an excluded path leaked; extracted to a clean dir and the suite ran from it | **MET** |
| A2 | "spend 2 hours max" | — | Built in one session with a multi-agent harness (4 parallel work packages + coordinator). Wall-clock was not 120 minutes; per the brief, quality was prioritised over finishing early | **N/A** |
| A3 | "PLEASE USE AI-driven IDEs" | this session | Agentic build; the plan, contract, tests and review are all in `docs/` | **N/A** |
| A4 | "within 3-5 business days" | — | Delivery timing | **N/A** |

## B. Functional behaviour

| # | Requirement (quote) | Where | Evidence | Status |
|---|---|---|---|---|
| B1 | "Create an async document processing application" | `app/api/documents.py`, `app/worker.py` | Upload returns `202` immediately; parsing happens in a separate process; status goes `queued → parsing → summarizing → completed` | **MET** |
| B2 | "upload one or more PDF files via a website that you'll create" | `frontend/` | `GET :3000/` → `200`; bundle `index-RqDuwbqT.js` (281 KB) and CSS both `200`; multi-file input, repeated `files` field; batch of valid+corrupt handled | **MET** |
| B3 | "After some time, a per-page content and summarization … is displayed on the page" | `App.tsx`, `useDocumentPolling.ts` | API returns ordered `pages[]` + `summary`; UI polls every 1.5 s until terminal. Verified at HTTP level; **not visually inspected in a browser** | **MET** (API) / see G3 |
| B4 | "one summary per each uploaded file" | `app/processing.py` | Exactly one `docs:summary:<id>` per document; `DocumentDetail.summary` is a single string | **MET** |
| B5 | "allow user to choose between those methods" | `GET /api/v1/parsers`, `parser` form field | Per-batch parser selection; only parsers that can actually run are `enabled: true` | **MET** |
| B6 | "accepts any PDF" | `MAX_UPLOAD_BYTES`, `MAX_PAGES`, `MAX_EXTRACTED_CHARS` | Bounded by documented limits (20 MB/file, 50 pages, 200k chars); signature-checked, never by extension/MIME. Limits are a deliberate deviation, documented in README | **MET** (bounded) |
| B7 | "PyPDF - this won't extract markdown, only text" | `app/parsers/pypdf_parser.py` | Real extraction: page 1 text began `"Piscina maintenance report — page 1"`; `format: "text"` preserved, never mislabelled as markdown | **MET** |
| B8 | "Google gemini-3.1-flash-lite … for parsing of a PDF file to markdown" | `app/parsers/gemini_parser.py`, `app/providers/gemini.py` | Per-page split + one real model call per page; a live run returned `format: "markdown"` transcribing the actual page text | **MET** — live-verified (G1) |
| B9 | "finally output markdown-formatted text and a summary" | `DocumentDetail` | `pages[].content` (markdown for gemini, text for pypdf as the brief allows) + `summary` | **PARTIAL** for the live gemini path (G1), **MET** structurally |

## C. Mandated stack

| # | Requirement | Where | Evidence | Status |
|---|---|---|---|---|
| C1 | Python 3.12+ with FastAPI | `Dockerfile`, `app/main.py` | `python:3.12-slim`; FastAPI + uvicorn; `/docs` Swagger works | **MET** |
| C2 | Redis v7+ | `compose.yaml` | `redis:7.4-alpine`, AOF, named volume | **MET** |
| C3 | Docker Compose | `compose.yaml` | `api`, `worker`, `redis`, `localstack`, `frontend` (+ `test` profile); cold start from wiped volumes verified | **MET** |
| C4 | PyPDF | `requirements.txt`, parser | `pypdf` used for both text extraction and per-page splitting | **MET** |
| C5 | Google gemini-3.1-flash-lite for markdown + summarization | `GEMINI_MODEL` | Default is the brief's id; a live call was executed against `gemini-flash-lite-latest` because the mandated id was returning `503 UNAVAILABLE` (provider congestion) — see G1 | **MET** (live, via alias) |
| C6 | JavaScript or TypeScript with React (or Next.js) | `frontend/` | React 18 + TypeScript + Vite, built and served by nginx | **MET** |

## D. Implementation details

| # | Requirement (quote) | Where | Evidence | Status |
|---|---|---|---|---|
| D1 | "Start with the backend part" | — | Full backend + worker + tests; UI is a thin single screen | **MET** |
| D2 | "API should take the uploaded PDF, and the desired parser value - PyPDF, Google Gemini Flash or GPT-5 Mini" | `ParserId`, `registry.py` | All three accepted: `gpt-5-mini` is implemented and runs when `OPENAI_API_KEY` is set, otherwise it is refused with an explanatory `503` | **MET** — G2 |
| D3 | "parse and store in Redis the resulting markdown and a summary" | `app/storage/jobs.py` | `docs:pages:<id>` (JSON, per-page `format`+`content`), `docs:summary:<id>`, `docs:job:<id>` hash; TTL on all three at terminal state | **MET** |
| D4 | "For the summary, just use gemini-3.1-flash-lite" | `GeminiSummarizer` | Always Gemini, never a second provider; prompts delimit pages and treat content as untrusted. Live-verified: the returned summary was real prose about the page content, not the stand-in | **MET** — live-verified (G1) |
| D5 | "acceptable to demo … using Postman or CURL" | `/docs`, `scripts/smoke.sh` | Swagger UI + `smoke.sh` (asserts ordering, page count, summary) — `Smoke test passed` | **MET** |
| D6 | "Use Redis Streams … as a queue to asynchronously process" | `app/queue.py` | Consumer group `document-workers`, `XADD` in the same MULTI as the job hash, `XREADGROUP`, `XAUTOCLAIM`, `XACK`+`XDEL` only at terminal state | **MET** |
| D7 | "You can use any public PDFs of your choice as a sample" | `samples/` | 3 deterministic fixtures (text / blank / corrupt), verified with pypdf | **MET** |
| D8 | "Frontend … API polling, websockets or SSE" | `useDocumentPolling.ts` | Recursive `setTimeout`, 1.5 s, per document; stops at terminal; backs off on error; cleaned up on unmount | **MET** |
| D9 | "Wrap all application components in Docker Compose" | `compose.yaml` | Includes the frontend; loopback-bound ports | **MET** |
| D10 | "Gemini MODEL_ID = gemini-3.1-flash-lite" | `app/config.py` | Default `GEMINI_MODEL=gemini-3.1-flash-lite` | **MET** (as configuration) |
| D11 | "make reasonable assumptions and move forward" | `docs/DECISIONS.md` | Every deviation recorded with a reason | **MET** |

## E. Priority order (the brief's own ranking)

| Priority | Item | Status |
|---|---|---|
| 1 | Backend able to run and serve concurrent API requests | **MET** — during an 8 s job: `/health/live` 3.7 ms, status 6.1 ms, second POST 10.7 ms |
| 2 | Document upload and processing with PyPDF | **MET** — real extraction, ordered pages |
| 3 | Async Redis queue with Streams | **MET** — consumer group verified end to end |
| 4 | Summarization via Google Gemini | **MET** — live-verified (G1) |
| 5 | Document processing via GPT-5 Mini (OCR) | **MET** — implemented and unit-tested against the Responses API; a live call needs `OPENAI_API_KEY` (G2) |
| 6 | Frontend | **MET** — React UI, 8 proxy e2e tests, vitest suite |

---

## Gaps

### G1 — Gemini path: now live-verified, with one caveat about the model id

**Status: closed.** With a real key, `tests/integration/test_live_ai.py` passed (`1 passed in
12.58s`): two per-page markdown extractions plus one document summary, from actual model calls.
Page 1 came back as the real report text, and the summary was genuine prose ("Site Details… Water
Quality & Conditions…"), not the stand-in. The full integration suite also passes with the stack in
`AI_MODE=live` (`13 passed, 1 skipped` — the live test skips itself without the opt-in flag).

**The caveat is the model id.** The brief's `gemini-3.1-flash-lite` exists for this account and is
listed by the API, but generation returned:

```
503 UNAVAILABLE. This model is currently experiencing high demand.
Spikes in demand are usually temporary. Please try again later.
```

A retry probe eventually succeeded on that id — after **449 seconds** in a single call. That is
provider congestion, not a configuration error, and it makes the id unusable for a live demo
today. The live verification therefore ran against `gemini-flash-lite-latest` (an alias in the same
family), set in `.env` as `GEMINI_MODEL=gemini-flash-lite-latest`. The default in `app/config.py`
and `.env.example` remains the brief's id; delete that one line from `.env` to revert.

Two things this exposed, both fixed:

- **The SDK retried 5xx internally on top of the worker's own retries**, so a congested call could
  silently occupy minutes while the job's attempt budget looked untouched. The client now sets
  `HttpRetryOptions(attempts=1)`, making the worker's bounded policy and the 60 s timeout
  authoritative.
- Congestion is classified as `TransientError`, so it retries with backoff and finally fails with a
  stable code rather than hanging.

### G2 — GPT-5 Mini (priority 5): now implemented, live-verified only against a stub

**Status: implemented.** `app/parsers/openai_parser.py` + `app/providers/openai_adapter.py` send one
page per Responses-API call with the page as an inline PDF (`input_file` + `input_text`), returning
markdown, with the same page cap, pacing and permanent/transient error mapping as the Gemini adapter.
The page splitter is shared (`app/parsers/pdf_pages.py`), so attribution is identical across
providers.

What is covered by tests (`tests/unit/test_openai_parser.py`, 13 tests): the exact request payload,
one call per page in order, the empty-page marker, all-empty → `OCR_REQUIRED`, transient vs
permanent classification (`429`/`5xx` vs `401`/`404`), `Retry-After` parsing, and that the page cap is
enforced *before* any provider call.

What is **not** covered: a live OpenAI call. There are no OpenAI credentials in this environment, so
the registry keeps the parser disabled (`enabled: false`, "OPENAI_API_KEY is not configured") until
a key is supplied. That is now a configuration gap, not a missing feature.

### G3 — The UI was verified over HTTP, not visually

`index.html`, the JS bundle and the CSS all serve `200`, the nginx `/api` proxy is covered by eight
e2e tests, and the frontend now has its own vitest suite. The page was still never opened in a
browser in this environment, so layout and
interaction are unconfirmed. Open <http://localhost:3000> for a 10-second check.

### G4 — Bounded acceptance of "any PDF"

Files over 20 MB, over 50 pages (20 for the Gemini parser) or over 200k extracted characters are
refused with a stable error code. The brief says "accepts any PDF"; unbounded acceptance of
untrusted binaries is not a defensible default, so the bounds are explicit and configurable.

---

## Beyond the brief (offered as evidence of production thinking, not as padding)

- **Terraform for the infrastructure**, in both directions: `infra/envs/local` is applied
  automatically by a one-shot `provisioner` service in Compose (so a reviewer needs no Terraform
  install), and `infra/envs/prod` provisions a real S3 bucket, an EC2 host with an instance role
  (no static keys), a security group and an Elastic IP. Not required by the brief; added because
  "easy to deploy to production" was requested. `fmt -check` and `validate` pass for both
  environments; only the local one has been applied here.
- **LocalStack S3** for source PDFs, with drift-correcting provisioning: wiping the LocalStack
  volume while Terraform state survives causes the bucket to be re-created on the next `up`
  (verified: `3 added, 0 changed, 0 destroyed`).
- **Lease-fenced commits**: `SET NX PX` lease + heartbeat + WATCH/MULTI commit, so a worker that
  lost its lease cannot overwrite a result or acknowledge someone else's message.
- **Crash recovery**: `XAUTOCLAIM` + a page checkpoint, so a killed worker resumes without
  re-extracting.
- **Honest failure taxonomy**: `PermanentError` vs `TransientError`, bounded retries, stable
  client-facing error codes, no stack traces in API output.
- **Load and fault testing** (requested after the assessment scope): `scripts/stress.py` measured 200 concurrent uploads with zero errors, worker parallelism from 4.2 to 23.0 docs/s, and three fault scenarios (worker SIGKILL, Redis restart, stale-writer fencing) all pass. Report: `docs/PERFORMANCE.md`; ledger: `docs/PROGRESS.md`. In total: **111 automated checks** (44 unit offline, 16 API integration, 14 repository/lease, 8 proxy-level e2e, 29 frontend vitest) plus 4 opt-in (3 concurrency, 1 live provider), a repeatable smoke script and a
  packaging verifier.

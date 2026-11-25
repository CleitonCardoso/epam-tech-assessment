# Async PDF processing

Upload one or more PDFs, pick a parser, and read **ordered per-page content** plus **one
document-level summary** once a background worker finishes. The HTTP API never blocks on
parsing or on a model call — it validates, stores the file, enqueues a job, and returns
`202` immediately.

Built for the BWAI 2.1 test task: Python 3.12 + FastAPI, Redis 7 Streams as the queue,
LocalStack S3 for source PDFs, PyPDF and Google Gemini for extraction, React UI on top.

---

## Architecture

```text
                       â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
  React UI :3000 â”€â”€â”€â”€â”€â”€â–¶â”‚ nginx  /api/*  →  api:8000                    â”‚
  curl / Swagger  â”€â”€â”€â”€â”€â–¶â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                                        â”‚
                             â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
                             â”‚  api  (FastAPI)      â”‚  validate → upload → enqueue
                             â”‚  uvicorn :8000       â”‚  never parses, never calls AI
                             â””â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”˜
                     PUT source.pdf  â”‚      â”‚ MULTI: HSET job + XADD
                                     â–¼      â–¼
                        â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”  â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
                        â”‚ LocalStack   â”‚  â”‚ Redis 7          â”‚
                        â”‚ S3 :4566     â”‚  â”‚ state + Stream   â”‚
                        â””â”€â”€â”€â”€â”€â”€â–²â”€â”€â”€â”€â”€â”€â”€â”˜  â””â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                               â”‚ GET source.pdf    â”‚ XREADGROUP / XAUTOCLAIM
                        â”Œâ”€â”€â”€â”€â”€â”€â”´â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
                        â”‚  worker  (same image, other CMD)    â”‚
                        â”‚  lease → parse → checkpoint →       â”‚
                        â”‚  summarize → fenced commit → ACK    â”‚
                        â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                                        â”‚
                             PyPDF (local) â”‚ Gemini (per page + summary)
```

Two processes, one image. Redis is the only job state; the stream carries a `document_id`
and nothing else. Source PDFs live in S3, never in Redis.

## Infrastructure provisioning (Terraform)

The bucket is **provisioned by Terraform**, and you never install Terraform: a one-shot
`provisioner` service in `compose.yaml` runs `terraform init && terraform apply` against LocalStack
*before* the API and worker start. `docker compose up` is therefore the whole story — images,
LocalStack, and infrastructure.

```text
infra/
â”œâ”€â”€ versions.tf              required_version + provider pin
â”œâ”€â”€ modules/
â”‚   â””â”€â”€ s3/                  bucket, public-access block, versioning, optional lifecycle
â”œâ”€â”€ envs/
â”‚   â”œâ”€â”€ local/               same module, provider pointed at LocalStack  <- applied by compose
â”‚   â””â”€â”€ prod/                real AWS: S3 + EC2 app host + IAM + Elastic IP
```

**What Compose does on `up`:** `localstack` → `provisioner` (init + apply + print the bucket name)
→ `api`/`worker` (which wait for the provisioner to exit 0) → `frontend`.

The first run downloads the AWS provider (a minute or two, cached in `infra/envs/local/.terraform`
afterwards). State lives in `infra/envs/local/terraform.tfstate` and is gitignored.

**Why this beats the init-script it replaced.** `terraform apply` refreshes before planning, so if
the LocalStack volume is wiped (`docker compose down -v`) Terraform notices the bucket is gone and
re-creates it. An imperative hook cannot tell drift from success — it just runs and assumes. This
was verified by wiping the volume with state intact: `3 added, 0 changed, 0 destroyed`, and the app
came back healthy.

The LocalStack-only provider settings live in `infra/envs/local/main.tf`:

- `s3_use_path_style = true` — without it the provider builds virtual-hosted URLs
  (`bucket.localhost`) that LocalStack cannot resolve, and bucket creation hangs.
- `enable_lifecycle = false` — LocalStack never reports a lifecycle rule as applied; the provider
  polls for ~3 minutes and then fails the whole apply. Production keeps it enabled.

### Deploying to production

`infra/envs/prod` provisions real AWS:

| Resource | Purpose |
|---|---|
| S3 bucket | source PDFs (private, versioned, lifecycle-enabled, `force_destroy = false`) |
| EC2 instance | the compose host; Docker + compose plugin installed by `user_data` |
| IAM role + instance profile | S3 read/write with **no static keys on the box** |
| Security group | 80/443 public, SSH closed unless you pass `ssh_cidr_blocks` |
| Elastic IP | a stable address for DNS |

```bash
cd infra/envs/prod
cp terraform.tfvars.example terraform.tfvars     # set bucket_name (globally unique)
terraform init && terraform plan && terraform apply
terraform output next_steps                      # exact follow-up steps
```

Then on the host: copy the repo to `/opt/app`, write `.env` with `S3_BUCKET` from the output,
`AWS_REGION`, `AI_MODE=live`, `GEMINI_API_KEY`, and set `S3_ENDPOINT_URL=` **empty** so the SDK uses
real AWS and the instance role. Start it with the production topology:

```bash
docker compose -f compose.prod.yaml up -d --build
```

`compose.prod.yaml` exists because the development file is wrong for a host: it would start an
in-host LocalStack and run the *local* Terraform environment against it — harmless for the app,
which talks to real S3 through the instance role, but pointless work and a misleading log. The prod
file has no LocalStack and no provisioner: `infra/envs/prod` already created the bucket, and nginx
is the only published port (80). TLS terminates in front of it.

`docker compose -f compose.prod.yaml config -q` is validated in this repository; it has not been
deployed to AWS from here.

State note: `envs/prod` ships with local state so it runs with no extra setup; the commented
`backend "s3"` block in `main.tf` is the change to make before two people ever run `apply`.

What is deliberately out of scope: TLS termination and autoscaling. The output tells you where they
belong (ALB + ACM in front, or nginx + Let's Encrypt on the box).

## What each piece is responsible for

| Component | Responsibility | Never does |
|---|---|---|
| `api` | Validate input, store source, enqueue atomically, serve status | Parse a PDF, call a model, block on work |
| `worker` | Consume the stream, parse, checkpoint pages, summarize, commit | Serve HTTP |
| Redis | Authoritative job state + queue (Streams) | Hold PDF bytes |
| LocalStack S3 | Source PDF storage (`uploads/<id>/source.pdf`) | Anything else — S3 only |
| nginx | Static UI + `/api` reverse proxy | Business logic |

---

## Quickstart

Requires **Docker + Compose v2** and network access for the first image build. Terraform runs
inside a container, so you do not install it. No AWS account, no cloud credentials, no local
Python/Node install — everything runs in containers.

### Bring your own key (30 seconds)

The key never goes into the source tree — it lives only in your local `.env`, which is
`.gitignore`d and excluded from the delivered archive. Two ways, pick one.

**Option 1 — pass it to Compose, no file editing:**

```bash
# macOS / Linux
GEMINI_API_KEY=AIza... docker compose up --build -d

# Windows PowerShell
$env:GEMINI_API_KEY="AIza..."; docker compose up --build -d
```

**Option 2 — put it in `.env`:**

```bash
cp .env.example .env
```
Then set the one line (any editor: `notepad .env`, `nano .env`, VS Code):
```dotenv
GEMINI_API_KEY=AIza...
```
and start:
```bash
docker compose up --build -d
```

Either way, confirm:
```bash
curl -fsS http://localhost:8000/health/ready    # 200, summarizer: gemini:<model>
```

### No key? Everything still runs, labelled

```bash
cp .env.example .env      # then set:  AI_MODE=fake
docker compose up --build -d
```
Fake mode is deterministic, makes no network calls, and says so everywhere (API, UI banner,
document warnings, summary text). Nothing is hidden.

Then:

| URL | What |
|---|---|
| <http://localhost:8000/docs> | Swagger UI — the fastest way to try an upload |
| <http://localhost:3000> | React UI (upload, live status, summary, pages) |
| <http://localhost:8000/health/ready> | Dependency + AI-config readiness |

### Process a PDF with curl

```bash
# 1. Upload (one or more files, one parser for the batch)
curl -F 'parser=pypdf' -F 'files=@samples/sample-text.pdf' \
     http://localhost:8000/api/v1/documents
```

```json
{"documents":[{"id":"29957dd5-4a32-467e-9f78-bb5f199b6a8b","filename":"sample-text.pdf",
  "parser":"pypdf","status":"queued",
  "status_url":"/api/v1/documents/29957dd5-4a32-467e-9f78-bb5f199b6a8b"}],"rejected":[]}
```

```bash
# 2. Poll until status is completed (or failed)
curl http://localhost:8000/api/v1/documents/29957dd5-4a32-467e-9f78-bb5f199b6a8b
```

```json
{"id":"29957dd5-...","filename":"sample-text.pdf","parser":"pypdf","status":"completed",
 "stage":null,"attempt":1,"created_at":"2026-11-25T17:33:02Z","updated_at":"2026-11-25T17:33:03Z",
 "page_count":2,
 "pages":[{"page_number":1,"format":"text","content":"Piscina maintenance report — page 1\n..."},
          {"page_number":2,"format":"text","content":"... page 2 ..."}],
 "summary":"## Summary (fake mode)\n\n- pages: 2\n...","warnings":[],"error":null}
```

`curl -N http://localhost:8000/api/v1/documents/<id>` in a loop, or `scripts/smoke.sh`, does
the same end to end with assertions.

Sample files (generated by `scripts/make_samples.py`, deterministic):

| File | Purpose | Expected result |
|---|---|---|
| `samples/sample-text.pdf` | 2 pages of real text | `completed`, 2 ordered `text` pages |
| `samples/sample-blank.pdf` | 1 page, no text | `failed` with `OCR_REQUIRED` (PyPDF) |
| `samples/sample-corrupt.pdf` | not a PDF | rejected at upload with `NOT_A_PDF` |

---

## API

### `POST /api/v1/documents`
`multipart/form-data`: repeated `files` fields + one `parser` field for the whole batch.

| Status | When | Body |
|---|---|---|
| `202` | at least one file accepted | `{documents:[…], rejected:[…]}` |
| `422` | no files, unknown parser, or every file invalid | structured `rejected` list |
| `413` | a per-file or batch size/count limit was exceeded | `{"detail": "…"}` |
| `503` | Redis/S3 down, or the selected parser is not enabled | `{"detail": "…"}` |

Each accepted file gets an independent UUID. A batch is **not** atomic: a corrupt file is
reported in `rejected` while the valid ones are queued.

Uploads are admission-controlled (`MAX_CONCURRENT_UPLOADS`, default 16): each request buffers its
file in memory before the S3 write, so past that limit a request waits briefly and then gets a
truthful `503` with `Retry-After` instead of being buffered anyway. Behind nginx the proxy also
rate-limits (20 r/s on this path, 100 r/s on the rest, JSON `429`).

### `GET /api/v1/documents`
`200` with the most recent documents, newest first — this is what lets a page refresh recover the
cards instead of losing them:

```json
{"documents":[{"id":"…","filename":"sample-text.pdf","parser":"pypdf","status":"completed",
  "page_count":2,"created_at":"2026-11-25T20:08:26Z","status_url":"/api/v1/documents/…"}],
 "next_before": 1795628000.12}
```

`limit` is 1–100 (default 20; outside that range → `422`). Pass `next_before` back as `before` to
walk backwards; the last page returns `next_before: null`. `503` if Redis is unavailable.

### `GET /api/v1/documents/{id}`
`200` for any known state (`pages` empty and `summary` null until ready) · `404` unknown or
expired · `503` Redis unavailable.

### `GET /api/v1/parsers`
Which parsers would actually work right now, and why not when they wouldn't:

```json
{"ai_mode":"fake","parsers":[
  {"id":"pypdf","enabled":true,"output_format":"text","requires_api_key":false,
   "note":"Local extract_text(); returns plain text, not markdown."},
  {"id":"gemini","enabled":true,"output_format":"markdown","requires_api_key":true,
   "note":"fake mode: deterministic stand-in, no model call. Per-page markdown transcription."},
  {"id":"gpt-5-mini","enabled":true,"output_format":"markdown","requires_api_key":true,
   "note":"fake mode: deterministic stand-in, no model call. Per-page markdown transcription."}]}
```

In `live` mode without an `OPENAI_API_KEY`, that last entry flips to `enabled: false` with
`"OPENAI_API_KEY is not configured"` — implemented, simply not configured.

### Health
- `GET /health/live` — process is serving. No dependency checks. (Compose watches this.)
- `GET /health/ready` — Redis `PING`, S3 `head_bucket`, and in live mode a configured Gemini
  key. `200` or `503` with a `checks` map naming what failed.

---

## AI modes — read this before judging the output

There are no model credentials in the environment this was built in, so the demo runs in an
explicitly labelled stand-in mode. It is never a silent fallback:

| `AI_MODE` | Gemini key | Behaviour |
|---|---|---|
| `live` | set | Real `google-genai` calls for markdown extraction and the summary |
| `live` | missing | **Every** parser is disabled and uploads return `503` with the reason; `/health/ready` → `503`. A document is only complete with a summary, and the summary is Gemini-only, so accepting the upload would mean accepting work that can only fail |
| `fake` | ignored | `FakeParser` / `FakeSummarizer`: deterministic, no network, no model |

In `fake` mode the mode is visible in `/api/v1/parsers`, `/health/ready` (`checks.ai_mode`),
the API/worker startup logs, a persistent banner in the UI, the `warnings[]` of every AI-parsed
document, and the summary text itself says *"(fake mode) … no model was called"*.

To use the real thing: set `GEMINI_API_KEY` and `AI_MODE=live` in `.env`, then
`docker compose up -d api worker`.

**Live verification.** With a real key the opt-in test passes
(`pytest tests/integration/test_live_ai.py` → `1 passed`): per-page markdown from actual model
calls plus a genuine document summary. One practical note: the mandated
`gemini-3.1-flash-lite` was returning `503 UNAVAILABLE` ("high demand") during development —
a retry eventually succeeded but took ~7 minutes for one call. If you hit that, set
`GEMINI_MODEL=gemini-flash-lite-latest` in `.env` for the demo and delete the line afterwards;
the default in `app/config.py` and `.env.example` stays the brief's id. The client makes one
attempt per call and the worker owns the retry policy, so congestion surfaces as a bounded
transient failure rather than a hang.

## Parsers

| Parser | How it extracts | Output | Notes |
|---|---|---|---|
| `pypdf` | `PdfReader.extract_text()` per page, in a thread | `text` per page | Offline. Empty pages are kept so numbering stays contiguous. No extractable text at all → `OCR_REQUIRED` |
| `gemini` | Splits the PDF into single-page PDFs, one model call per page | `markdown` per page | Deterministic page attribution instead of trusting the model's page breaks. Capped by `MAX_AI_PAGES` (default 20), sequential to respect free-tier limits |
| `gpt-5-mini` | Same splitting, one Responses-API call per page with the page sent as an inline PDF | `markdown` per page | Implemented (`app/providers/openai_adapter.py`). Enabled when `OPENAI_API_KEY` is set; without it the API reports `enabled: false` with that reason. **Payload and error mapping are unit-tested against a stub; no live OpenAI call has been made**, because this environment has no OpenAI credentials |

All three sit behind the same `Parser` protocol (`app/parsers/base.py`), so the worker never learns
which provider ran: `registry.py` decides, and the splitter is shared (`app/parsers/pdf_pages.py`)
so page attribution is identical across providers.

The summary is **always** Gemini (`gemini-3.1-flash-lite`), one per document, built from the
ordered pages with explicit `[PAGE n]` delimiters. A failed summary keeps the pages
(`status: failed`, pages still readable).

---

## How processing works

1. `POST` validates, `PUT`s the source to S3, then `MULTI: HSET job + XADD` — a job can never
   exist without being queued, and an ambiguous Redis failure is never reported as accepted.
2. The worker joins the consumer group `document-workers`, reads with `XREADGROUP`, and takes a
   per-job lease (`SET NX PX`).
3. It parses, then **checkpoints** the pages before summarizing. The pages key *is* the
   resume point.
4. It summarizes, then commits terminal state + TTLs + `XACK` + `XDEL` in one fenced
   transaction. A worker whose lease expired cannot commit or acknowledge.
5. Crash recovery: the entry stays pending; after `LEASE_SECONDS` another worker takes it with
   `XAUTOCLAIM`, skips extraction if pages were checkpointed, and finishes.

**Delivery is at-least-once.** A crash after a provider call but before the checkpoint repeats
that call. That is stated as a property, not hidden: duplicate provider billing is possible,
duplicate *results* are not (the commit is fenced).

**S3 and Redis are not transactional.** If the process dies between the upload and the enqueue,
an unreferenced object can remain. On a definite Redis rejection the API deletes the object; the
residual crash window is documented rather than papered over. Sources are deleted after the
terminal state (best effort).

## Performance and fault tolerance

Measured on this project, not estimated — full method and tables in
[`docs/PERFORMANCE.md`](docs/PERFORMANCE.md).

| Scenario | Result |
|---|---|
| 200 concurrent uploads | 200/200 accepted, 0 HTTP errors, ~31 documents/s settled |
| Worker parallelism (`WORKER_CONCURRENCY` 1 → 4 → 8) | 4.2 → 16.3 → 23.0 documents/s |
| Worker SIGKILLed mid-job | reclaimed from the page checkpoint, completes |
| Redis restarted with a 30-document backlog | 30/30 completed, 0 lost |
| Stale worker resumes after losing its lease | fenced: detects the lost lease, cannot overwrite |

`./scripts/resilience.sh` runs the three fault scenarios; `scripts/stress.py` is the load harness and
exits non-zero on any failure, so both work as gates rather than reports.

Two knobs matter, and they trade against each other:

- **`WORKER_CONCURRENCY`** (default 4) — documents processed in parallel per worker.
- **`PROVIDER_MAX_REQUESTS_PER_MINUTE`** (default 0 = off) — client-side pacing. With the live
  provider, leaving this at 0 under a burst means the provider answers `429`, retries exhaust, and
  documents fail. Set it to your published RPM and throughput becomes `RPM / calls_per_document`
  instead of errors.

## Limits (demo defaults, all configurable in `.env`)

| Limit | Default |
|---|---|
| Per file | 20 MB |
| Per batch | 5 files / 50 MB |
| Pages | 50 (`pypdf`), 20 (`gemini`) |
| Extracted characters | 200 000 |
| Provider timeout | 60 s |
| Attempts per document | 3 |
| Whole-job deadline | 600 s |
| Result TTL in Redis | 24 h |

nginx caps request bodies at 25 MB, below the 50 MB batch limit — a multi-file batch over
25 MB total is refused by the proxy before it reaches the API. Raise `client_max_body_size` in
`frontend/nginx.conf` if you need the full batch size.

---

## Tests

```bash
# Unit tests, in the same image (no Redis/S3 needed)
docker compose --profile test run --rm test

# Integration tests against the running stack.
# The samples/ directory is dockerignored, so mount it into the test container.
docker compose up -d
docker compose --profile test run --rm \
  -e API_BASE_URL=http://api:8000 \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python -m pytest tests/integration -m integration -q

# The repository/lease suite needs only Redis, not the API
docker compose --profile test run --rm \
  -e REDIS_URL=redis://redis:6379/0 \
  test python -m pytest tests/integration/test_repository.py -m repository -q

# End-to-end through nginx - the path a browser actually takes
docker compose --profile test run --rm \
  -e FRONTEND_URL=http://frontend:80 \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python -m pytest tests/e2e -m e2e -q

# Manual walkthrough with assertions
bash scripts/smoke.sh
```

| Suite | Tests | Needs | What it proves |
|---|---|---|---|
| Unit (`tests/unit`) | 44 | nothing | models, settings validation, provider error mapping, retry/Retry-After policy, the GPT-5 Mini payload and parser contract |
| API integration (`test_api.py`) | 16 | stack | upload → poll → completed, ordered pages, batch independence, every rejection code, listing and pagination |
| Repository/lease (`test_repository.py`) | 14 | **Redis only** | the fenced-commit heart: exclusive leases, stale-token rejection, checkpoint/resume, ACK+delete on commit, failure keeps pages |
| Proxy e2e (`tests/e2e`) | 8 | stack incl. frontend | the shell and its hashed assets, `/api` proxying, JSON 404s that must not fall through to the SPA, upload through nginx, the 25 MB body limit |
| Concurrency (`test_concurrency.py`) | 3 | fake-mode worker | 20 simultaneous uploads, responsiveness under burst, mixed batch under load |
| Live AI (`test_live_ai.py`) | 1 | a real key | per-page markdown + a genuine summary from the provider |
| Frontend (`frontend/src/*.test.*`) | 29 | node only (`npm test`) | polling policy (terminal stop, capped backoff), status/label mapping, API error parsing, list mapping, and reload recovery rendered in jsdom |

The two provider/load suites are opt-in (`RUN_CONCURRENCY_TESTS=1`, `RUN_LIVE_AI_TESTS=1`) because
they cost real provider calls or need a disposable worker; they report as **skipped**, never as
passed.

Verified in this environment: **44 unit**, **16 API integration**, **14 repository/lease**,
**8 proxy e2e**, **29 frontend**, 3 concurrency (fake worker) and 1 live-AI test, plus `smoke.sh`.
That is 111 automated checks green. The ledger of what was actually executed, and anything left
unverified, is `docs/PROGRESS.md`.

---

## Layout

```text
app/
  config.py        validated settings (env var = field name upper-cased)
  models.py        API + internal models
  registry.py      decides which parsers are enabled, and why not
  main.py          FastAPI app, lifespan, health
  api/documents.py upload + list + status handlers
  storage/objects.py  S3 adapter (4 operations)
  storage/jobs.py     Redis repository, lease-fenced writes, list index
  queue.py         consumer group, read, claim, ack
  processing.py    parse → checkpoint → summarize → commit
  worker.py        worker entrypoint (concurrent dispatch, backpressure, drain)
  parsers/         base contract, pdf_pages splitter, pypdf/gemini/openai parsers
  providers/       gemini + openai clients, ProviderPacer, fake stand-ins
frontend/          React + Vite + nginx (proxies /api, rate-limits it)
infra/             Terraform: modules/ + envs/local (LocalStack) + envs/prod (AWS)
scripts/           make_samples.py, smoke.sh, stress.py, resilience.sh, check_encoding.py
samples/           generated fixtures
tests/             unit + integration (incl. repository/lease) + e2e
docs/              CONTRACT, DECISIONS, REQUIREMENTS, PROGRESS, PERFORMANCE, + the docs set
.github/workflows/ ci.yml — lint, types, encoding gate, unit, integration+e2e, frontend, terraform
requirements*.lock.txt   exact pins generated from the verified image (`pip freeze`)
```

Design notes worth reading: [`docs/DECISIONS.md`](docs/DECISIONS.md) (what I changed in the
plan and why), [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) (the brief traced line by line),
[`docs/CONTRACT.md`](docs/CONTRACT.md) (frozen interfaces),
[`docs/PROGRESS.md`](docs/PROGRESS.md) (what is actually verified).

## Quality gates

Every one of these runs in CI (`.github/workflows/ci.yml`) and can be run locally:

| Gate | Command | Why |
|---|---|---|
| Lint | `ruff check .` (in the test image) | One style, no dead imports, no shadowed names |
| Format | `ruff format --check .` | Diffs stay about behaviour |
| Types | `mypy` (24 modules, `check_untyped_defs`) | Catches the class of bug the Redis-blip fix came from |
| Encoding | `python scripts/check_encoding.py` | A cp1252 em dash once broke `mypy` and rendered as garbage |
| Secrets | `grep` for `AIza…` / `sk-…` patterns in CI | The zip ships source; no key may be in it |
| Dependencies | `requirements*.lock.txt` installed by the Dockerfile | Builds are reproducible; `requirements.txt` stays the human intent |
| Tests | unit · integration · repository · e2e | See the table above |

```bash
# everything CI runs, locally
docker run --rm -v "$PWD:/src" -w /src <test-image> ruff check .
docker run --rm -v "$PWD:/src:ro" -w /src <test-image> mypy --cache-dir=/tmp/mypy
python scripts/check_encoding.py
```

---

## Security scope — this is a trusted local demo

No authentication, no tenancy: anyone who can reach the API can upload and read any document
id, and **a UUID is not access control**. LocalStack credentials are literally `test`/`test`.
Redis and S3 are loopback-bound and not exposed on the network.

A production deployment would additionally need: auth + per-tenant ownership, request quotas,
malware scanning and sandboxed parsing (a PDF parser is an attack surface), TLS, private
storage, secret management, data-retention policy and orphan reconciliation. **Do not deploy
this as-is.**

## Known limitations

- **The live Gemini path is verified against a same-family alias, not the exact mandated id.**
  `gemini-3.1-flash-lite` answers `503 UNAVAILABLE ("high demand")` for this key: ten attempts over
  ~7 minutes all failed, while `gemini-flash-lite-latest` answered in seconds. The default remains
  the brief's id (`GEMINI_MODEL`), so pointing it back is one line; the live test
  (`tests/integration/test_live_ai.py`) is what proves whichever id is configured.
- **`gpt-5-mini` is implemented but not live-verified.** The adapter sends one page per Responses-API
  call as an inline PDF; the payload shape and error mapping are unit-tested against a stub
  (`tests/unit/test_openai_parser.py`), and the registry keeps it disabled until `OPENAI_API_KEY`
  exists. No live OpenAI call was possible here.
- **LocalStack persists nothing across container recreation** (persistence is a LocalStack Pro
  feature), so a wiped volume loses the bucket. Terraform repairs this on the next `up`: `apply`
  refreshes state, sees the bucket is gone and re-creates it. `docker compose down -v` also discards
  job state in Redis.
- **Single Redis instance.** The lease/commit transactions are not Redis Cluster compatible.
- Per-page Gemini calls are slow and costly by design (deterministic page boundaries). Large
  documents belong on the PyPDF parser.
- No auth and no quotas: per-IP rate limits at the proxy and per-process upload admission are the only abuse controls. No orphan-reconciliation job.

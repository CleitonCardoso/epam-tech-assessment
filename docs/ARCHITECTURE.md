# Architecture

How the async PDF-processing application is put together: processes, the request path, the job
state machine, the Redis and S3 layouts, and the reliability mechanisms that make a killed worker
recoverable without double-committing a result.

Sources for every claim here: `app/`, `compose.yaml`, `infra/`, `frontend/nginx.conf`, the test
suites under `tests/`, `.github/workflows/ci.yml`, and the verification ledger in
[PROGRESS.md](PROGRESS.md). Where something was checked by running it in this environment, it is
marked **verified**. Throughput, latency and fault-injection numbers are quoted from the measured
report [PERFORMANCE.md](PERFORMANCE.md), not re-derived here.

## 1. Infrastructure provisioning and components

### 1.1 What `docker compose up` does

Infrastructure is code: `infra/` is a Terraform tree applied by a one-shot Compose service, so a
reviewer never installs Terraform - `hashicorp/terraform:1.9` runs inside a container. Startup
order is enforced with `depends_on`:

```text
docker compose up --build -d
  |
  +-- redis         (healthy)
  +-- localstack    (healthy; S3 API only, no bucket yet)
  |       |
  |       v
  +-- provisioner   one-shot, working_dir /tf/envs/local, ./infra mounted at /tf
  |       |         terraform init -backend=false
  |       |         terraform apply -auto-approve -input=false
  |       |         terraform output -raw bucket_name
  |       |
  |       `-- api and worker wait for it with
  |           `condition: service_completed_successfully`
  v
  api  +  worker   (start only after the provisioner exits 0)
  |
  v
  frontend
```

- The provisioner is a one-shot service (`restart: "no"`, `TF_IN_AUTOMATION=1`). While it runs,
  `docker compose ps` shows `redis`, `localstack` and `provisioner` only - api, worker and
  frontend are not started yet. This was observed live during this documentation pass.
- **The first run needs network and is slower**: it downloads the AWS provider (a minute or two).
  The provider is cached in `infra/envs/local/.terraform/` afterwards (gitignored).
- **State** lives in `infra/envs/local/terraform.tfstate` (gitignored; it contains resource ids).
  `.terraform.lock.hcl` is committed deliberately, pinning the provider build for every machine.
  Observed in this environment: Terraform `1.9.8`, provider `hashicorp/aws v5.100.0`, and state
  holding exactly three managed resources - `aws_s3_bucket`,
  `aws_s3_bucket_public_access_block`, `aws_s3_bucket_versioning`.
- **Drift healing is the reason Terraform is here**: `apply` refreshes before planning, so after
  `docker compose down -v` (LocalStack state gone, Terraform state intact) it sees that the bucket
  is missing and re-creates it instead of reporting "no changes". Verified by the coordinator:
  refresh detected the vanished bucket, re-created it, and readiness returned `200`.
- **One bucket name feeds both sides.** `compose.yaml` passes
  `TF_VAR_bucket_name: ${S3_BUCKET:-documents}` to the provisioner and `S3_BUCKET` (among the
  passthrough vars) to `api` and `worker`, so Terraform provisions exactly the bucket the app
  reads. Changing `S3_BUCKET` therefore **renames** the bucket: because the name is the resource
  identity, `apply` destroys the old bucket and creates the new one (contents are lost). Verified
  end to end by the coordinator with `S3_BUCKET=pdf-docs-custom`
  (`3 added, 0 changed, 3 destroyed`, `bucket_name = "pdf-docs-custom"`, readiness
  `s3: bucket 'pdf-docs-custom' ok`) and back to the default in reverse. LocalStack itself never
  creates a bucket and receives no `S3_BUCKET`; Terraform owns that resource.
- With only this project's containers running, the documented defaults work unchanged: host port
  `4566`, readiness `200`, and an upload completes (verified by the coordinator after unrelated
  containers were removed; no volumes were touched).

### 1.2 The Terraform tree

| Path | What it defines |
|---|---|
| `infra/versions.tf` | `required_version >= 1.6`; `hashicorp/aws ~> 5.0` |
| `infra/modules/s3/` | Shared bucket module: bucket (3-63 char precondition), public-access block with all four flags, versioning enabled, optional lifecycle (`enable_lifecycle`) expiring non-current versions after 30 days, `force_destroy` flag, tags |
| `infra/modules/ec2-app/` | Production host module: AL2023 AMI lookup, security group (80/443 public; 22 only for listed `ssh_cidr_blocks`, empty by default), all egress, IAM role + inline S3 policy scoped to the bucket ARN + instance profile, EC2 instance (IMDSv2 required, encrypted gp3 root, `ignore_changes = [ami, user_data]`), Elastic IP + association, `user_data` installing Docker + compose plugin into `/opt/app` |
| `infra/envs/local/` | LocalStack provider (endpoint `http://localstack:4566`, `s3_use_path_style = true`, credential/STS/metadata checks skipped, `test`/`test` credentials), `enable_lifecycle = false`, `force_destroy = true`, tags; output `bucket_name`. Applied automatically by Compose |
| `infra/envs/prod/` | Real AWS: provider with default tags, S3 module with lifecycle on and `force_destroy = false`, EC2 host module, `variables.tf`, `terraform.tfvars.example`, outputs (`bucket_name`, `public_ip`, `instance_role_arn`, `next_steps`), commented `backend "s3"` block for remote state |
| `infra/envs/*/.terraform.lock.hcl` | Committed provider lock file (same pin in both envs) |

Why one module and two envs: local and production use the *same* S3 module; only the provider,
backend and module inputs differ. `enable_lifecycle` exists because LocalStack never confirms a
lifecycle configuration (the provider polls for about three minutes and then fails the apply), so
local disables it and production enables it. `force_destroy` is true only locally so
`terraform destroy` can clean up.

### 1.3 What is verified, and what is not

| Check | Status |
|---|---|
| `terraform fmt -check -recursive` | clean (verified in this documentation pass with the `hashicorp/terraform:1.9` image) |
| `terraform validate` for `infra/envs/local` | `Success! The configuration is valid.` (verified in this pass) |
| `terraform validate` for `infra/envs/prod` | `Success! The configuration is valid.` (verified in this pass) |
| Local apply through `docker compose up` | the provisioner added 3 resources, `0 changed, 0 destroyed`; api/worker start only after it exits 0 (verified by the coordinator) |
| Drift healing after `docker compose down -v` with state intact | Terraform refreshed, detected the missing bucket, re-created it (`3 added`), readiness returned `200` (verified by the coordinator) |
| Production environment | **validated only - never applied.** It is not deployed and nothing in AWS was observed |

Production topology: the host does **not** run `compose.yaml` (that would start an in-host
LocalStack and apply the *local* Terraform environment against it, which is pointless once
`infra/envs/prod` created the real bucket). It runs `compose.prod.yaml`: redis + api + worker +
frontend, nginx as the only published port (80), no LocalStack and no provisioner.
`docker compose -f compose.prod.yaml config -q` validates (verified in this pass, exit 0); it has
not been deployed - there is no AWS account here.

### 1.4 Components

| Component | What it is | Responsibility | Explicitly never does |
|---|---|---|---|
| `provisioner` | `hashicorp/terraform:1.9`, one-shot | `terraform init` + `apply` the local environment against LocalStack, then exit; api/worker gate on its exit status | Serve traffic, stay running |
| `api` | FastAPI app under uvicorn (`app.main:app`) | Validate upload, admission-control and store the source, write the job hash + stream entry + list index atomically, serve status/list/parsers/health | Parse a PDF, call a model, block on work |
| `worker` | Same image, different command (`python -m app.worker`) | Consume the stream with bounded concurrency, hold a per-job lease, parse (PyPDF, Gemini or GPT-5 Mini), checkpoint pages, summarize, commit terminal state, drain on SIGTERM | Serve HTTP |
| `redis` | Redis 7.4 with AOF (`--appendonly yes --appendfsync everysec`) | Authoritative job state, the queue (Streams) and the recent-documents index (sorted set) | Store PDF bytes |
| `localstack` | LocalStack 3.8, `SERVICES=s3` | The S3 API the app and Terraform talk to; it owns no bucket itself - Terraform creates that | Persist anything across recreation (community edition) |
| `frontend` | React + Vite build served by nginx | Static UI, same-origin `/api` and `/health` reverse proxy, per-IP request-rate limits | Business logic |
| nginx | Inside the `frontend` image | Proxy `/api/` and `/health/` to `api:8000`, SPA fallback, 25 MB body cap, two `limit_req` zones (http context) that answer `429` JSON | Parse, retry, transform |

The API and worker share one Dockerfile and the same code (`app/config.py`, `app/models.py`,
`app/storage/*`, `app/registry.py`). The API never imports the worker loop. Redis is the only job
state; S3 holds sources only. Both the OpenAI and Gemini SDK surfaces are confined to
`app/providers/`; the parsers share a page splitter (`app/parsers/pdf_pages.py`) and the provider
clients share a pacer (`app/providers/pacing.py`).

Ports are loopback-bound on the host (verified with `docker compose ps`):

```text
frontend   127.0.0.1:3000 -> 80
api        127.0.0.1:8000 -> 8000
localstack 127.0.0.1:${LOCALSTACK_PORT:-4566} -> 4566   (default 4566; override only if the host port is taken)
redis      no published port (Compose network only)
worker     no published port
provisioner no published port (one-shot)
```

## 2. Request path

```text
browser (React :3000)
  |
  | same-origin fetch: /api/v1/...
  v
nginx (frontend container :80) -- /api/ and /health/ --> api:8000
  |   limit_req: 20 r/s per IP on /api/v1/documents (uploads+list),
  |              100 r/s per IP on the rest of /api/; JSON 429 on excess
  |
  |  POST /api/v1/documents  (multipart: parser + repeated files)
  v
api (FastAPI :8000)
  |-- admission: wait up to 10 s for one of MAX_CONCURRENT_UPLOADS slots (else 503 + Retry-After)
  |-- validate: parser known + enabled, bounded read, %PDF- signature, limits
  |-- PUT s3://documents/uploads/<uuid>/source.pdf
  |-- MULTI { HSET docs:job:<uuid> ..., XADD docs:jobs * document_id <uuid>,
  |           ZADD docs:index <epoch> <uuid> }   (also prunes the index to the retention window)
  `-- 202 {documents:[...], rejected:[...]}

  GET /api/v1/documents?limit=&before=  -> ZREVRANGEBYSCORE docs:index + HGETALL per id
                                           -> 200 {documents:[...], next_before}
                                     |
                                     |  Redis Stream docs:jobs (field: document_id only)
                                     v
                              worker (same image, python -m app.worker)
                                |-- XREADGROUP group=document-workers count=WORKER_BATCH_SIZE block=2s
                                |-- dispatch up to WORKER_CONCURRENCY jobs; stop reading when full
                                |-- per job: SET NX PX docs:lease:<uuid>  (own execution token)
                                |-- GET s3://documents/uploads/<uuid>/source.pdf
                                |-- parse (PyPDF in a thread; Gemini or GPT-5 Mini, one call/page)
                                |-- SET docs:pages:<uuid> + status=summarizing   (checkpoint)
                                |-- summarize with Gemini (one call, whole document)
                                `-- WATCH/MULTI fenced commit:
                                      status=completed, TTLs on job/pages/summary,
                                      XACK + XDEL the stream entry
```

The API phase is bounded by upload size (max 20 MB per file) and by the admission semaphore, not by
parsing time. Parsing and model calls happen only in the worker process, so a slow document cannot
slow down `/health/live`, status reads, or another upload. That property was exercised in the ledger
(8 s job in flight: health 3.7 ms, status 6.1 ms, second POST 10.7 ms).

## 3. Job lifecycle state machine

Statuses are the `DocumentStatus` enum in `app/models.py`: `queued`, `parsing`, `summarizing`,
`retrying`, `completed`, `failed`. Only `completed` and `failed` are terminal
(`DocumentStatus.is_terminal`).

```mermaid
stateDiagram-v2
    [*] --> queued: POST accepted\nHSET + XADD in one MULTI
    queued --> parsing: worker owns the lease,\nextraction starts
    queued --> failed: deadline already exceeded\nbefore the first attempt
    parsing --> summarizing: pages validated\nand checkpointed
    parsing --> retrying: retryable error\n(attempt < MAX_JOB_ATTEMPTS)
    parsing --> failed: permanent error
    summarizing --> completed: summary non-empty,\nfenced commit + ACK + XDEL
    summarizing --> retrying: retryable error\n(attempt < MAX_JOB_ATTEMPTS)
    summarizing --> failed: permanent error or\nattempt budget exhausted
    retrying --> parsing: next attempt\n(fresh extraction)
    retrying --> summarizing: next attempt\n(resumed from pages checkpoint)
    retrying --> failed: attempt budget exhausted,\nor deadline exceeded
    completed --> [*]
    failed --> [*]
```

Transition table (what writes the state, and what triggers it):

| From | To | Trigger | Code |
|---|---|---|---|
| (none) | `queued` | Upload accepted; job hash and stream entry committed atomically | `api/documents.py` -> `JobRepository.create_and_enqueue` |
| `queued` | `failed` | The deadline had already passed before the first attempt started | `processing.process` raises `DEADLINE_EXCEEDED` before any stage write |
| `queued`, `retrying` (or a stale non-terminal state) | `parsing` | Worker owns the lease and starts the extraction stage | `processing.JobProcessor._extract` -> `set_stage(PARSING, "parsing")` |
| `parsing` | `summarizing` | Pages extracted, validated (`validate_pages`), and checkpointed | `save_pages` writes status `summarizing` |
| `summarizing` (or resumed) | `summarizing` | Resume path re-enters the summarize stage without extraction | `_summarize` -> `set_stage(SUMMARIZING, "summarizing")` |
| `parsing`/`summarizing` | `retrying` | A retryable error was raised, attempts remain, backoff scheduled | `worker._attempt_loop` -> `JobRepository.mark_retry` |
| `summarizing` | `completed` | Non-empty summary returned; fenced commit, TTLs, XACK, XDEL all succeed | `commit_completed` |
| any non-terminal | `failed` | Permanent error, or a retryable error on the final attempt, or deadline expired | `commit_failed` (via `worker._fail`) |
| any non-terminal | unchanged | Lease was lost mid-work | `LeaseLost` - nothing is written |
| terminal | unchanged | Duplicate delivery (job already terminal) | `worker._handle` acknowledges and drops |

Details that matter when reading a status:

- **`retrying` is a real state, not a stage.** `mark_retry` sets `status=retrying`, clears
  `stage`, increments `attempt`, and stores the last `error` record. The client can poll and see
  it; the next loop iteration writes `parsing` again. Status is therefore not monotonic.
- **`attempt` is incremented before each attempt**, via `HINCRBY`, and starts at `0` in the hash
  until the worker runs. A completed job therefore normally has `attempt` 1. `MAX_JOB_ATTEMPTS`
  is 3 by default, and the failure is `ATTEMPTS_EXHAUSTED` when the counter exceeds it.
- **A retryable error can still end as `failed` with `retryable: true`.** The error record on a
  failed document mirrors the last `ParserError`'s `retryable` flag; if it is `true`, the attempt
  budget or deadline ran out while the underlying error was considered transient.
- **`stage` is `null` or `"parsing"`/`"summarizing"`.** It is set alongside the status and cleared
  on terminal states and on retry. It exists so a stalled job is diagnosable (queued vs working).
- **A resumed job may briefly show a stage from the dead worker** (`parsing`/`summarizing`) before
  the new attempt rewrites it. That is expected, and it is exactly what makes `XPENDING` plus
  `stage` useful when diagnosing a stuck job (see [RUNBOOK.md](RUNBOOK.md)).
- There is a **deadline**: `deadline_at` is written at accept time
  (`time.time() + JOB_DEADLINE_SECONDS`) and checked at the start of each processing attempt. A
  job that starts after the deadline fails immediately with `DEADLINE_EXCEEDED`.

## 4. Redis key schema

Key names are constants in `app/storage/jobs.py`; stream and group names come from settings
(`STREAM_KEY`, `CONSUMER_GROUP`, defaults `docs:jobs` and `document-workers`). The schema is
frozen in [CONTRACT.md](CONTRACT.md) section 6.

| Key | Type | Written by | TTL | Contents |
|---|---|---|---|---|
| `docs:jobs` | Stream | `create_and_enqueue` (XADD); `commit_completed`/`commit_failed` (XACK+XDEL) | n/a | One field only: `document_id`. The stream is a pointer, never a payload. |
| `docs:job:<id>` | Hash | API at accept; repository on every stage/terminal write | `RESULT_TTL_SECONDS` set at terminal state | `id, filename, parser, status, stage, attempt, created_at, updated_at, source_key, page_count, warnings (JSON), error (JSON or empty), deadline_at` |
| `docs:pages:<id>` | String (JSON) | `save_pages` | `EXPIRE` at terminal state (completed and failed) | `{"pages":[{"page_number":1,"format":"text","content":"..."}],"warnings":[...]}` |
| `docs:summary:<id>` | String | `commit_completed` (`SET ... EX ttl`) | `RESULT_TTL_SECONDS` | The document summary markdown. Gemini-only. |
| `docs:lease:<id>` | String + TTL | `acquire_lease` (SET NX PX), `renew_lease` (Lua), `release_lease` (Lua DEL) | `LEASE_SECONDS` while held; removed on clean release | Unique execution token (uuid4 hex) for the worker that owns the job |
| `docs:index` | Sorted set | `create_and_enqueue` (`ZADD score=<enqueue epoch>` + `ZREMRANGEBYSCORE` prune, in the acceptance MULTI) | No per-key TTL; pruned to `RESULT_TTL_SECONDS` on every accepted upload | Member `document_id`, score = enqueue epoch. Backs `GET /api/v1/documents` (newest first, cursor = score). It is an index only: the hash is the source of truth and expired hashes are skipped when paging. |

Consumer group: `document-workers` on `docs:jobs`, created with `MKSTREAM` at id `0` so entries
already in the stream are consumed. Only `BUSYGROUP` is tolerated as "already created".

Reading the schema in a live stack (verified): an example job hash held exactly
`id, filename, parser, status=completed, stage=<empty>, attempt=1, created_at, updated_at,
source_key=uploads/<uuid>/source.pdf, page_count=2, warnings=[], error=<empty>,
deadline_at=<epoch>`, and `TTL docs:job:<id>` / `TTL docs:summary:<id>` were ~84,000 s with a
24 h result TTL - the terminal TTL was applied. See [RUNBOOK.md](RUNBOOK.md) for the inspection
commands.

Notes:

- Missing/empty fields are the encoding for `None`: `stage=""`, `page_count=""`, `error=""`.
- The job hash is the authoritative record; `docs:pages` and `docs:summary` are the result
  payloads, written through the same lease fence.
- No PDF bytes ever go into Redis, and stream entries never carry the file or its content.
- `docs:index` exists only so a page refresh can rebuild the UI (`GET /api/v1/documents`). It has
  no per-key TTL: it is pruned to `RESULT_TTL_SECONDS` on every accepted upload, and when a job
  hash has already expired the list simply skips that id. The cursor is the zset **score**
  (enqueue epoch), so pagination is `ZREVRANGEBYSCORE docs:index (before -inf LIMIT`. The index is
  written in the same acceptance `MULTI` as the hash and stream entry, so a job can never be
  visible in the list without being queued.

## 5. S3 object layout

| Item | Value |
|---|---|
| Bucket | `S3_BUCKET` (default `documents`), fed to Terraform as `TF_VAR_bucket_name`; owned by Terraform (`infra/modules/s3`) and created by the `provisioner` service before api/worker start in the local environment. Changing the name destroys and re-creates the bucket. |
| Key | `uploads/<document_id>/source.pdf` (constant template `SOURCE_KEY_TEMPLATE` in `app/storage/objects.py`) |
| Key source | **Server-generated UUID only.** The client filename is never used as a key, so path traversal is impossible by construction. |
| Object metadata | `ContentType: application/pdf`, `ContentLength` = uploaded byte count |
| Client config | boto3 with path-style addressing (required by LocalStack), SigV4, `max_attempts=3` standard retries, 5 s connect / 30 s read timeouts; all calls pushed to a thread via `asyncio.to_thread` |
| Versioning | Enabled by the module in both environments. In production a delete keeps previous non-current versions until the lifecycle rule expires them after 30 days (`enable_lifecycle = true`); locally lifecycle is disabled (LocalStack cannot confirm it) and `force_destroy = true` lets destroy clean up. |
| Deletion | Best effort, after a terminal state: after the completed commit, and after a failed commit. A leftover object is harmless; a job pointing at a deleted object is not, hence "only after durable terminal state". |
| Persistence / drift | LocalStack community keeps no state across container recreation. On the next `up` the provisioner applies again; `terraform apply` refreshes first, so a wiped bucket is detected and re-created (drift healing, verified by the coordinator). |

Because S3 and Redis are not transactional, one window is documented rather than hidden: if the
API process dies between `put_source` and the Redis `MULTI`, an unreferenced object can remain
(the API deletes the object only on a definite Redis rejection). There is no orphan-reconciler in
this delivery.

Verified in the live stack: `awslocal s3api head-bucket --bucket documents` returned the bucket
region, and `awslocal s3 ls s3://documents/uploads/` listed nothing after terminal documents,
consistent with source deletion after commit.

## 6. Why the lease, the heartbeat, and the fenced commit exist

Redis Streams with a consumer group give **at-least-once delivery**, not exactly-once. Two
failure modes are inherent, and this design neutralises both:

**Failure mode 1 - reclamation can run two workers at once.** A pending entry whose consumer died
is taken over by `XAUTOCLAIM` after the idle threshold. But "no heartbeat for a while" is not the
same as "dead": a worker can be paused (GC, CPU starvation, container freeze, network partition)
past the threshold while still alive and still working. Without per-job mutual exclusion, both
workers parse and summarize the same document and both try to commit. You also cannot simply
never reclaim, because then a genuinely killed worker strands the job forever.

**Failure mode 2 - the stale worker can win.** Even with a claim, a worker that lost ownership can
wake up later and write. If its write is a plain `HSET`, a completed result can be overwritten by
an older, worse one, or a `failed` can replace a `completed`. If it can `XACK` freely, it can
acknowledge a stream entry that another worker is still responsible for.

The mechanisms, and what each one prevents if removed:

| Mechanism | Implementation | What breaks without it |
|---|---|---|
| Per-job lease | `SET docs:lease:<id> <token> NX PX <LEASE_SECONDS*1000>`; one token per execution; worker logs `job_leased_elsewhere` and leaves the entry pending when it cannot acquire | Any claim can start duplicate work on a live job |
| Heartbeat | Background task calls a Lua compare-and-`PEXPIRE` every `HEARTBEAT_SECONDS`; stops renewing when the token no longer matches | A long parse or model call silently loses the lease mid-flight, then commits nothing (or races another worker) |
| Fenced commit | `WATCH docs:lease:<id>`, compare the token, then `MULTI` the terminal write; new token (or missing key) means the write is rejected and `LeaseLost` is raised | A worker whose lease expired can overwrite a completed result or ack another worker's entry |
| ACK+XDEL inside the terminal transaction | `commit_completed`/`commit_failed` pipeline `XACK` and `XDEL` in the same `MULTI` as the state write | A crash between state write and ACK leaves a completed job pending forever and reprocessed; ACK before state write can lose a job |
| Reclaim threshold = lease length | `effective_reclaim_idle_ms` defaults to `LEASE_SECONDS * 1000`, and `XAUTOCLAIM` runs only when `XREADGROUP` returned nothing | A live worker that renews every heartbeat is never stolen; the two numbers must agree |

The cost of the design is stated, not hidden:

- Delivery remains at-least-once. A crash after a provider call but before the checkpoint repeats
  that provider call, so duplicate provider billing is possible. Duplicate *results* are not: the
  terminal commit is fenced and atomic.
- The lease and commit transactions use `WATCH`/`MULTI` and Lua on a single Redis instance. This
  is **not Redis Cluster compatible**.
- `XAUTOCLAIM` is deliberately a simple sweep (no persisted cursor state machine): one sweep per
  idle read iteration, `START 0-0`, `COUNT 10`. The contract change is documented in
  [DECISIONS.md](DECISIONS.md) item 3.
- The design assumes exactly **one** consumer group, which is why `XDEL` after `XACK` is safe; a
  second group would have had the entry deleted out from under it.

## 7. Parsers: three real implementations, one page per model call

There are three parsers, and all three are real implementations:

| Parser | Implementation | Output | How it extracts |
|---|---|---|---|
| `pypdf` | `app/parsers/pypdf_parser.py` | `text` per page | `PdfReader.extract_text()` in a thread; empty pages kept so numbering stays contiguous; no text at all -> `OCR_REQUIRED` |
| `gemini` | `app/parsers/gemini_parser.py` + `app/providers/gemini.py` | `markdown` per page | Split into single-page PDFs (shared `pdf_pages.split_pdf_pages`), then one `google-genai` call per page, sequential within the document; Gemini-only summary afterwards |
| `gpt-5-mini` | `app/parsers/openai_parser.py` + `app/providers/openai_adapter.py` | `markdown` per page | Same shared splitter, then one Responses-API call per page with the page sent as an inline PDF (`input_file` with a base64 `data:application/pdf` URL + `input_text`); sequential within the document |

Both AI parsers share `app/parsers/pdf_pages.py` (split/validate/limit) and both provider clients
share `app/providers/pacing.py` (`ProviderPacer`: semaphore + token bucket) and the
`TransientError`/`PermanentError` vocabulary, so the worker cannot tell which provider ran. The
registry is the only place that decides which are enabled and why not (see
[API.md](API.md#get-apiv1parsers)).

Why split first instead of sending the whole document: correctness of attribution, not cost
efficiency. A whole-document call returns one blob, and the model decides where page boundaries
are; when a document has no visible page breaks that silently mis-attributes content, while the API
promises ordered per-page content. Splitting first makes `page_number` deterministic: page N's
markdown is the result of page N's call (one request per page, so one page's content cannot bleed
into another's).

The costs and bounds are explicit:

- N calls for an N-page document per AI parser, sequential **within** a document - several
  documents can be in flight at once, bounded by `WORKER_CONCURRENCY` and paced per client
  (section 10.3).
- The AI parsers are capped by `MAX_AI_PAGES` (default 20) by the shared splitter and fail with
  `TOO_MANY_PAGES_FOR_AI` before any provider call; the page-level text cap is
  `MAX_EXTRACTED_CHARS`.
- A page that yields the literal marker `(no extractable text)` is stored as an empty page with a
  warning, keeping page numbers contiguous. If every page is empty, the parser fails with
  `OCR_REQUIRED`; a PDF with zero pages also raises `OCR_REQUIRED` from the shared splitter.
- The **summary is separate and always document-level and Gemini-only**, whichever parser extracted
  the pages: after all pages are checkpointed, `GeminiSummarizer` is called once with the pages
  delimited as `[PAGE n] ... [END PAGE n]`. A failed summary keeps the extracted pages (a `failed`
  document can still return readable pages).
- The GPT-5 Mini path is **implemented but not live-verified**: no OpenAI credential exists here,
  so its payload, ordering, cap and error mapping are pinned against a stub
  (`tests/unit/test_openai_parser.py`). The Gemini path has been live-verified.

## 8. Upload to completed, step by step

```text
1. Client POSTs multipart (parser field + repeated files fields) to /api/v1/documents.
   Admission control runs first: wait up to UPLOAD_SLOT_TIMEOUT_SECONDS (10 s) for one of
   MAX_CONCURRENT_UPLOADS (16) slots; on timeout -> 503 + Retry-After: 1 (before any validation).
2. API parses the parser name against ParserId: unknown -> 422; disabled -> 503 (before reading files).
3. API checks file count <= MAX_BATCH_FILES: else 413.
4. For each file, in order:
   a. sanitize the filename (basename, printable only, max 200 chars);
   b. bounded chunked read (1 MB chunks, stop once MAX_UPLOAD_BYTES is exceeded) -> 413 if exceeded;
   c. running batch total > MAX_BATCH_BYTES -> 413;
   d. empty -> rejected[] EMPTY_FILE; missing %PDF- signature -> rejected[] NOT_A_PDF;
   e. PUT uploads/<new-uuid>/source.pdf in a thread; object-store failure -> rejected[] STORAGE_UNAVAILABLE;
   f. build DocumentJob (status queued, deadline_at = now + 600 s);
   g. MULTI { HSET docs:job:<id>; XADD docs:jobs * document_id <id>;
              ZADD docs:index <epoch> <id>; ZREMRANGEBYSCORE docs:index -inf <now - TTL> };
      on RedisError delete the just-uploaded object and reject with QUEUE_UNAVAILABLE.
5. Respond 202 {documents, rejected} if at least one file was accepted; else 422 (or 503 when the
   only failures were storage/queue failures). The admission slot is released after the response.
6. Worker loop: XREADGROUP (count WORKER_BATCH_SIZE=8, block 2 s); if empty, XAUTOCLAIM stale
   entries (same count). One asyncio task is dispatched per message, bounded by a semaphore of
   WORKER_CONCURRENCY=4; while every slot is busy the loop stops reading (backpressure).
7. Worker loads the job hash. Missing hash -> ack the orphan entry and drop. Already terminal -> ack and drop.
8. Worker acquires that job's own lease (SET NX PX). Held elsewhere -> log and leave the entry pending.
9. Start that job's heartbeat task (renew every 15 s, fenced by token). Concurrent jobs share no
   mutable state; each has its own lease, token, heartbeat, retry budget and pacer position.
10. Attempt loop: HINCRBY attempt; if > MAX_JOB_ATTEMPTS -> fail ATTEMPTS_EXHAUSTED.
11. Processor checks deadline_at; expired -> fail DEADLINE_EXCEEDED.
12. If docs:pages:<id> exists, resume: skip extraction (log job_resumed).
13. Otherwise: status parsing; GET the source from S3; parse (PyPDF in a thread, or Gemini / GPT-5
    Mini via the shared page splitter with one model call per page);
    validate pages (1-based, contiguous); save_pages writes docs:pages:<id> JSON, sets
    status=summarizing, page_count and warnings - still fenced by the lease token.
14. Status summarizing; get the summarizer; one Gemini call over the delimited pages.
15. Empty summary -> fail SUMMARY_EMPTY. Otherwise the fenced MULTI: status=completed, clear
    stage/error, TTL on job/pages/summary, SET summary with TTL, XACK and XDEL.
16. On success: delete the source object (best effort) and release the lease.
17. On failure: commit_failed (fenced, same XACK/XDEL pattern), delete the source object, release
    the lease.
18. A retryable failure with attempts left instead marks retrying, sleeps the backoff from
    RETRY_BACKOFF_SECONDS, and loops from step 10 with the same lease and token.
```

Client-visible consequence: `GET /api/v1/documents/{id}` returns `200` for every known state.
`pages` is `[]` and `summary` is `null` until they are produced; `error` is `null` until a
terminal failure. Full field and status-code detail is in [API.md](API.md).

## 9. Crash recovery

The recovery story follows directly from the mechanisms above.

| Crash point | State left behind | What happens next |
|---|---|---|
| API dies between S3 PUT and the Redis MULTI | Orphan S3 object, no job | Documented orphan window; no reconciler. Nothing is returned to a client because the request died. |
| Worker dies before extraction finishes | Stream entry pending; `docs:job` shows `parsing`; no `docs:pages` | After `LEASE_SECONDS` of idle, another worker's `XAUTOCLAIM` claims the entry, acquires the lease (the old lease has expired or is deleted by TTL), and re-runs extraction from scratch. Duplicate provider calls are possible here. With `WORKER_CONCURRENCY > 1`, a SIGKILL takes all of that process's in-flight jobs and each is reclaimed independently. |
| Worker dies after `save_pages`, before the summary | Stream entry pending; `docs:pages` exists; status `summarizing` | Reclaimed worker sees the checkpoint, logs `job_resumed`, and skips extraction. Only the summary is repeated. |
| Worker dies after the terminal `MULTI` | Stream entry already ACKed+XDELed; terminal state durable | Nothing to recover. The job is terminal; a duplicate delivery would be acked and dropped at step 7. |
| Worker loses its lease but keeps running | Lease token changed or expired; entry may have been claimed | Every subsequent fenced write returns `False`; `LeaseLost` propagates; the worker logs `lease_lost (no state written)` and exits that job without writing or acking. |
| Worker is SIGTERM'd (deploy, restart) | Reading stops immediately; in-flight jobs still hold their leases | The worker logs `worker_draining`, gives jobs up to `WORKER_SHUTDOWN_GRACE_SECONDS` (30 s), then cancels the rest (`worker_shutdown_incomplete`). Nothing is acknowledged because shutdown began; unfinished entries stay pending and a replacement reclaims them from the checkpoint. |
| A job hits a Redis error mid-processing (for example during `commit_completed`) | The entry is not acknowledged and the document is not terminal | The attempt loop re-raises `RedisError` (`job_deferred_redis_unavailable`) instead of classifying it as a document failure. The message stays pending and a reclaim resumes from the checkpoint. This is the fix for the bug where a transient store blip became a permanent `INTERNAL_ERROR` failure. |
| One concurrent job task raises unexpectedly | The other jobs are unaffected | The task callback logs `job_task_crashed`, releases the slot, and the worker keeps consuming. |
| Job hash expired while the entry is still pending | Orphan stream entry | The worker acks and deletes it (`orphan_stream_entry`), so it cannot feed a claim loop forever. |

Recovery is not instantaneous: the observe-to-recovery latency is bounded by the worker's read
block (2 s) plus the reclaim idle threshold (`LEASE_SECONDS`, 90 s by default) plus the next
`XAUTOCLAIM` sweep. See the stuck-job and manual-reclaim procedures in [RUNBOOK.md](RUNBOOK.md).

## 10. Worker concurrency, backpressure, and provider pacing

### 10.1 Dispatch

- One worker process runs `WORKER_CONCURRENCY` jobs at once (default 4). The cap is an
  `asyncio.Semaphore`: each received stream entry becomes one task that holds a slot for its whole
  lifetime.
- Reading and processing are coupled on purpose. `XREADGROUP` asks for `WORKER_BATCH_SIZE` entries
  (default 8; keep it >= concurrency so slots are never idle); the loop then waits for a free slot
  before dispatching each entry. When every slot is busy it stops issuing reads, and at most the
  current batch is held undelivered, so it can never buffer unbounded work in memory. That is the
  backpressure mechanism. Saturation appears as `worker_saturated` in the log at DEBUG level (so
  `LOG_LEVEL=DEBUG` to see it) and externally as growing stream lag / pending entries.
- Jobs share nothing mutable: each has its own lease token, heartbeat task, attempt counter and
  (for the AI parsers) its own position in the provider pacer. The lease/fencing guarantees are
  per job, so concurrency does not weaken them - two jobs cannot collide, and two workers cannot
  double-commit one job.
- One job cannot crash the worker: the task callback logs `job_task_crashed` and releases the slot,
  and `_handle` failures are caught per task.
- This is I/O concurrency inside one process, not multiprocessing. PyPDF work still runs in threads
  (`asyncio.to_thread`), so CPU-bound parsing stops scaling around the number of cores; the
  measured curve is 4.22 -> 16.32 -> 22.99 settled docs/s at concurrency 1 -> 4 -> 8
  ([PERFORMANCE.md](PERFORMANCE.md)).
- Backlog is accepted, not shed at the queue level: admission bounds how many uploads are buffered
  at once (16) and the proxy bounds browser request rate, but neither caps queue depth. Uploads keep
  returning `202` while the queue grows and the worker stays saturated. Memory during the upload
  phase is bounded by in-flight uploads x `MAX_BATCH_BYTES`, not by the queue.

### 10.2 Shutdown and draining

- SIGTERM/SIGINT only sets a stop flag: the loop stops reading the stream immediately, then
  `_drain` waits up to `WORKER_SHUTDOWN_GRACE_SECONDS` (default 30) for in-flight jobs.
- Jobs that finish within the grace period commit and ACK normally. Jobs still running are
  cancelled and logged as `worker_shutdown_incomplete`; their entries stay pending and are
  reclaimed by a replacement worker after the lease expires, resuming from the pages checkpoint.
- Nothing is ACKed merely because shutdown started. A `SIGKILL` has no drain at all and takes all
  in-flight jobs with it; each is reclaimed the same way.

### 10.3 Provider pacing

One shared class, `app/providers/pacing.py::ProviderPacer`, is used by every provider client
(Gemini parser, Gemini summarizer, GPT-5 Mini parser). Each client instance owns its own pacer:

1. `PROVIDER_MAX_CONCURRENCY` (default 4) - a semaphore around each provider request.
2. `PROVIDER_MAX_REQUESTS_PER_MINUTE` (default 0 = disabled) - a continuously refilled token
   bucket; calls wait before reaching the provider instead of discovering the limit as a 429.
3. `Retry-After` (clamped by `RETRY_AFTER_MAX_SECONDS`, default 30 s) and jittered backoff - when a
   429/503 does arrive, the worker honours the provider's delay within that cap and adds jitter so
   parallel jobs do not retry in lockstep. Both adapters parse the header (`gemini.py` and
   `openai_adapter.py`).

Pacing is **per client instance, not per process**. In a live Gemini configuration the worker holds
two Gemini clients (the Gemini parser and the Gemini summarizer, when that parser is selected) and
one OpenAI client, each with its own semaphore and bucket; if `PROVIDER_MAX_REQUESTS_PER_MINUTE` is
set, the effective request rate can be a small multiple of that number depending on which clients
are active. Set the RPM from the provider's published limit and leave headroom for the summarizer.
Multiple worker processes multiply it again.

Measured effect on the Gemini path: a 20-document live burst with no pacing failed documents with
`PROVIDER_ERROR` after 429/503 responses exhausted the retry budget; paced at 20 RPM it completed
20/20 (0.76 docs/s, almost exactly 20 RPM / 3 calls per document). Tuning rule:
`throughput ~= RPM / calls_per_document`. Details and caveats: [PERFORMANCE.md](PERFORMANCE.md).

### 10.4 Admission control and proxy rate limiting

Two protective layers sit in front of the expensive path. Neither is authentication; both are
blast-radius controls.

- **Admission control (application, always on).** Uploads buffer the whole file in memory before the
  S3 write, so the API holds a semaphore of `MAX_CONCURRENT_UPLOADS` (default 16). A request waits up
  to `UPLOAD_SLOT_TIMEOUT_SECONDS` (default 10 s) for a slot and otherwise gets a truthful `503`
  with `Retry-After: 1` - it is not buffered anyway. The limit is per API process. The list endpoint
  is not admission-controlled.
- **Proxy rate limiting (nginx, UI origin only).** `frontend/nginx.conf` declares two
  `limit_req_zone`s keyed on `$binary_remote_addr` (per client IP): `uploads` at 20 r/s with burst
  40 on exactly `/api/v1/documents` (both upload and list), and `api` at 100 r/s with burst 200 on
  the rest of `/api/`. Excess requests get `429` with a JSON body
  (`{"detail": "...", "code": "RATE_LIMITED"}`) generated by an `error_page` named location.
  `limit_req_zone` is only legal in the http context, so the directives sit **outside** the `server`
  block of that file (it is included from `conf.d/`, inside `http {}`); moving them inside `server`
  makes nginx exit with `"limit_req_zone" directive is not allowed here` and the container
  crash-loops.
- The proxy limits do not apply to a direct call on `:8000`; they are a browser-traffic guard, not
  a security boundary. Per-IP means clients behind one NAT share a bucket, and there are no
  per-account quotas. Measured: 150 rapid requests from one IP through nginx produced 64 `429`s
  with the documented body (coordinator-run); a 200-request repeat during this pass produced 90
  `429` / 110 `200`.

## 11. API responsiveness, retries, and bounds

- The API event loop never runs blocking I/O directly: boto3 calls are wrapped in
  `asyncio.to_thread`, PyPDF extraction runs in a thread, both AI SDKs are awaited (async clients).
- `/health/ready` runs each dependency check under a 5 s `asyncio.wait_for`.
- Measured API responsiveness: 200 concurrent uploads were accepted with 0 HTTP errors (p95 upload
  1555 ms) while the worker was the bottleneck, and 100-document runs at client concurrency
  1/10/50/100 had 0 errors and p95 upload latency 9/88/651/802 ms. Since admission control and the
  proxy rate limits landed, those numbers describe the unbounded past; they remain valid as a
  measurement of the API's own capacity. See [PERFORMANCE.md](PERFORMANCE.md).
- Bounds (defaults in `app/config.py`): 20 MB per file, 5 files / 50 MB per batch, 16 simultaneous
  uploads, 50 pages (PyPDF), 20 pages (AI parsers), 200,000 extracted characters, 60 s provider
  timeout, 3 attempts, 600 s job deadline, 90 s lease / 15 s heartbeat, 24 h result TTL. The full
  table with the exact enforcement point is in [API.md](API.md#limits); the concurrency and pacing
  knobs are tabled in [DEVELOPING.md](DEVELOPING.md#tuning-knobs); the per-IP nginx limits are in
  section 10.4.
- Retry policy lives in the worker: a `TransientError` gets `RETRY_BACKOFF_SECONDS` (2 s, 5 s,
  10 s, clamped to the last value) until `MAX_JOB_ATTEMPTS`, preferring the provider's
  `Retry-After` when the error carries one (clamped by `RETRY_AFTER_MAX_SECONDS`) and otherwise
  adding +/-20% jitter. A `PermanentError` fails immediately. A `RedisError` is neither: it is a
  dependency blip, so it is re-raised and the job is deferred, never failed. Both provider SDKs are
  configured to make **one** attempt (Gemini `HttpRetryOptions(attempts=1)`, OpenAI
  `max_retries=0`) so the worker's budget and the 60 s timeout stay authoritative.
- Both provider adapters map failures into that vocabulary: 429/5xx/timeouts ->
  `TransientError` (carrying `retry_after` when the provider sent one); configuration/permanent
  4xx (Gemini: other 4xx, safety block; OpenAI: 401/403/other 4xx such as an unknown model) ->
  `PermanentError`. Every unknown failure is transient, so it is retried a bounded number of times
  and then fails with a stable code.
- Single Redis instance is an explicit assumption; see section 6.

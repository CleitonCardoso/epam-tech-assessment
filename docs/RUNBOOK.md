# Runbook

Operating the async PDF-processing stack: start/stop, health, logs, queue inspection, diagnosing a
stalled job, manual recovery, resets, and a troubleshooting table.

**Provenance rule for this document.** Every command is marked either **[executed]** (run in this
environment on 2026-11-25, read-only against the live stack) or **[from source]** (derived from
`compose.yaml`, `app/`, or `infra/`, not run during this documentation pass). Mutating commands
were deliberately not executed because a demo stack was live; this is stated where it matters.
Host and container names assume you run from the repository root.

## 1. Environment assumptions

- Docker Engine with Compose v2. Verified here: Docker server `28.3.2`, Compose
  `v2.38.2-desktop.1`.
- **Provisioning is Terraform, executed inside a container.** `docker compose up` first runs the
  one-shot `provisioner` service (`hashicorp/terraform:1.9`), which runs
  `terraform init -backend=false && terraform apply -auto-approve` for `infra/envs/local` against
  LocalStack. `api` and `worker` wait for it with `condition: service_completed_successfully`.
  You do **not** install Terraform on the host, and no AWS account or cloud credentials are needed
  for the local demo. State lives in `infra/envs/local/terraform.tfstate` (gitignored); the
  provider lock file is committed. The **first** `up` needs network and takes a minute or two
  longer because the AWS provider is downloaded into `infra/envs/local/.terraform/`.
- **LocalStack host-port knob.** `LOCALSTACK_PORT` (default `4566`) is the host port published by
  the `localstack` container. On a clean host the default is correct and was verified: with only
  this project's containers running, the mapping is `127.0.0.1:4566->4566`, readiness is `200`, and
  an upload completes. If another process already owns 4566, Compose refuses to start the container
  with an error beginning `ports are not available: ... 127.0.0.1:4566`; set
  `LOCALSTACK_PORT=<free port>` in `.env` (documented in `.env.example`) and start again.
  Container-internal addressing is unaffected: the app and the provisioner always use
  `localstack:4566` inside the Compose network.
- Services: `redis`, `localstack`, `provisioner` (one-shot), `api`, `worker`, `frontend`, plus an
  optional `test` profile.
- **Worker throughput and pacing knobs.** The worker processes `WORKER_CONCURRENCY` jobs at once
  (default 4), reads up to `WORKER_BATCH_SIZE` entries per read (8) and drains in-flight work for
  `WORKER_SHUTDOWN_GRACE_SECONDS` (30) on SIGTERM. AI calls are paced by
  `PROVIDER_MAX_CONCURRENCY` (4) and an optional requests-per-minute token bucket
  (`PROVIDER_MAX_REQUESTS_PER_MINUTE`, `0` = disabled) - **per provider client instance**, not per
  process, so a Gemini parse plus a Gemini summary use two pacers. `Retry-After` is honoured and
  clamped by `RETRY_AFTER_MAX_SECONDS` (30). The full tuning table is in
  [DEVELOPING.md](DEVELOPING.md#tuning-knobs); measured effects are in
  [PERFORMANCE.md](PERFORMANCE.md).
- **Upload protection knobs.** `MAX_CONCURRENT_UPLOADS` (default 16) bounds simultaneous uploads per
  API process; a request waits up to `UPLOAD_SLOT_TIMEOUT_SECONDS` (10) for a slot and then gets a
  `503` with `Retry-After: 1`. The UI proxy additionally rate-limits per client IP: 20 r/s (burst
  40) on `/api/v1/documents` and 100 r/s (burst 200) on the rest of `/api/`, answering `429` with
  `{"detail": "...", "code": "RATE_LIMITED"}`. Neither is authentication, and the nginx limits do
  not apply to a direct call on `:8000`. Details: [API.md](API.md#limits).
- **Providers and keys.** Three real parsers: `pypdf` (local), `gemini` (needs `GEMINI_API_KEY`),
  `gpt-5-mini` (needs `OPENAI_API_KEY`). The document summary is always Gemini, so live mode needs
  `GEMINI_API_KEY` even for pypdf and gpt-5-mini; an OpenAI-only live setup enables nothing
  (`/api/v1/parsers` explains why). The OpenAI path is implemented but **not live-verified** here -
  no OpenAI credentials exist in this environment.
- Host ports are loopback-bound: API `127.0.0.1:8000`, UI `127.0.0.1:3000`, LocalStack S3
  `127.0.0.1:${LOCALSTACK_PORT:-4566}` (4566 on a clean host). Inside the Compose network the app
  and Terraform always use `localstack:4566` and `redis:6379`; the host port only matters to
  host-side tools. Read the actual mapping from `docker compose ps`.
- Redis is **not** published to the host. All Redis inspection goes through
  `docker compose exec`.
- `REDIS_URL`, `S3_ENDPOINT_URL`, `S3_BUCKET`, `AI_MODE`, `GEMINI_API_KEY`, `OPENAI_API_KEY`,
  `MAX_CONCURRENT_UPLOADS` and the limits come from `.env` when present, from Compose environment
  passthrough, or from the defaults in `app/config.py`. Do not read or print `.env`; ask the stack
  what it is doing via `/health/ready` and `/api/v1/parsers` instead.

## 2. Start, stop, restart

All of these are **[from source]** (compose.yaml/README) and were not run during this pass; a live
demo was running and recreating containers was out of scope.

```bash
# Start everything, built. Also provisions the local infrastructure first (see below).
docker compose up --build -d

# Start only the backend pieces after a code change
docker compose up -d api worker

# Recreate api+worker after changing .env (env_file is only read at container creation)
docker compose up -d --force-recreate api worker

# Stop without deleting anything
docker compose stop

# Stop and remove containers/networks, keep named volumes (Redis AOF, LocalStack data dir)
docker compose down

# Stop and delete the volumes too. Job state is lost; the Terraform state file survives, so the
# next `up` refreshes, notices the bucket is gone and re-creates it (drift healing, verified).
docker compose down -v
```

Notes:

- `api` and `worker` are the **same image**; only the command differs
  (`uvicorn app.main:app ...` vs `python -m app.worker`).
- `api` and `worker` wait for `redis` and `localstack` to be healthy **and** for `provisioner` to
  exit `0`. While the provisioner is running, `docker compose ps` shows only
  redis/localstack/provisioner - this was observed live during this pass.
- `.env` is optional (`env_file.required: false`) and is read when the container is created. An
  environment change therefore needs `--force-recreate`, not just `restart`.
- `redis` and `localstack` use `restart: unless-stopped`; so do `api`, `worker`, `frontend`. The
  provisioner is `restart: "no"` by design - it is a one-shot apply.
- **Shutdown is graceful but bounded.** On SIGTERM (which includes `docker compose stop/restart`)
  the worker stops reading the stream, drains in-flight jobs for up to
  `WORKER_SHUTDOWN_GRACE_SECONDS` (30 s) and then cancels whatever is left
  (`worker_shutdown_incomplete`). Nothing is acknowledged because shutdown began; unfinished
  entries stay pending and a replacement reclaims them from the checkpoint. A restart can therefore
  take up to the grace period when jobs are in flight. A `SIGKILL` has no drain at all.

### Provisioning: what runs, where state lives, how to re-apply

```bash
# Watch the Terraform run (first time: provider download; later: init + apply + output)
docker compose logs -f provisioner

# Re-apply the local environment without touching the rest of the stack
docker compose run --rm provisioner

# Ad-hoc Terraform commands in the pinned image, same mount/workdir as the service
docker compose run --rm --entrypoint terraform provisioner fmt -check -recursive /tf
docker compose run --rm --entrypoint terraform provisioner validate
docker compose run --rm --entrypoint terraform provisioner output bucket_name
docker compose run --rm --entrypoint terraform --workdir /tf/envs/prod provisioner validate

# Destroy the local infrastructure (the local env sets force_destroy = true; prod does not)
docker compose run --rm --entrypoint terraform provisioner destroy -auto-approve -input=false
```

The `output` variant of the ad-hoc commands was **[executed]** in this pass through
`docker compose run --rm --no-deps --entrypoint terraform provisioner output -raw bucket_name`
(printed `documents`); `fmt`/`validate` were verified with a direct `docker run` of the same image
(see the standalone check below). Everything else here is **[from source]** (compose.yaml + the
Terraform tree). What they operate on:

- The service is defined in `compose.yaml` (`provisioner`): image `hashicorp/terraform:1.9`,
  `working_dir: /tf/envs/local`, `./infra` mounted at `/tf`, entrypoint `/bin/sh -c` running
  `terraform init -input=false -backend=false && terraform apply -auto-approve -input=false &&
  terraform output -raw bucket_name`.
- **The bucket name is shared**: `compose.yaml` passes
  `TF_VAR_bucket_name: ${S3_BUCKET:-documents}` to the provisioner and `S3_BUCKET` to `api`/`worker`,
  so Terraform provisions exactly the bucket the app reads. Changing `S3_BUCKET` renames the bucket
  (destroy + recreate because the name is the resource identity) - see section 9.
- **State**: `infra/envs/local/terraform.tfstate` plus `.backup`, both gitignored (they contain
  resource ids). **Provider cache**: `infra/envs/local/.terraform/` (gitignored, hundreds of MB).
  The committed `infra/envs/local/.terraform.lock.hcl` pins the provider (observed
  `hashicorp/aws v5.100.0`, Terraform `1.9.8`).
- `docker compose down` keeps state and volumes. `down -v` keeps the state file but deletes the
  LocalStack volume - exactly the drift case Terraform heals on the next `up`.
- **Production (`infra/envs/prod`) is validated but never applied** in this delivery. Deploying it
  is a normal host-side Terraform run from that directory, described in its `main.tf` comments,
  `terraform.tfvars.example`, and the repository README; it is outside the local demo.

### Standalone start from a clean extraction - verified

Verified by the coordinator in this session from a **clean extraction of the deliverable archive**
into a temporary directory outside the repository, with **no `.env` file** and `AI_MODE=fake`
supplied through the Compose environment passthrough:

| Check | Result |
|---|---|
| `docker compose up --build -d` | the `provisioner` ran first and applied the local environment: `3 added, 0 changed, 0 destroyed`; then all five long-running services up (api/worker/frontend/redis/localstack) and `api` healthy |
| `GET /health/ready` | `200`, `{"redis":"ok","s3":"bucket 'documents' ok","summarizer":"fake summarizer","ai_mode":"fake"}` |
| bucket creation | Terraform created the bucket, its public-access block and its versioning before api/worker started |
| `parser=pypdf` upload | `completed`, 2 pages, `format: text` |
| `parser=gemini` upload | `completed`, 2 pages, `format: markdown`, fake warning present |
| UI | `GET :3000/` -> `200`; `GET :3000/api/v1/parsers` -> `200` |

What this proves: the deliverable runs on a machine with only Docker + Compose v2. Terraform runs
inside a container, so there is no Terraform install, no AWS account and no cloud credentials for
the local demo, and no local Python/Node install or `.env` is required.

Terraform checks verified in this documentation pass with a direct `docker run` of the pinned
image against the working tree: `terraform fmt -check -recursive` -> clean (exit 0);
`terraform validate` for `infra/envs/local` and `infra/envs/prod` -> `Success! The configuration
is valid.` The local apply and the drift-healing run were verified by the coordinator (`down -v`
with state intact, Terraform refreshed, detected the missing bucket, re-created it, readiness back
to `200`). The production environment has **not** been applied.

One nuance, so the result is reproducible: with **no `.env` and no `AI_MODE` exported**, the
`app/config.py` default is `AI_MODE=live`, which has no key, so `/health/ready` is `503` and every
parser is disabled by design. The clean-extraction run set `AI_MODE=fake` through the shell
passthrough (`compose.yaml` forwards `AI_MODE` for `api` and `worker` when it is set), which is why
readiness reports `ai_mode: fake` and `summarizer: fake summarizer`.

**Documented defaults on a clean host - verified.** With only this project's containers running
(18 stale containers from unrelated projects were removed at the user's request; no volumes were
touched), the stack ran on its documented defaults: LocalStack published on host port `4566`,
readiness `200`, and an upload completed. That is the baseline a reviewer should see;
`LOCALSTACK_PORT` exists for machines where 4566 is already occupied.

## 3. Health checks

### Endpoints

```bash
curl -fsS http://localhost:8000/health/live     # [executed] 200 {"status":"ok"}
curl -fsS http://localhost:8000/health/ready    # [executed] 200 healthy / 503 degraded
```

`/health/live` answers only "is this process serving HTTP". It has no dependency checks and is
what the Docker healthcheck watches (`compose.yaml`, api service). `worker` has no HTTP endpoint
and no healthcheck; it reports through logs.

`/health/ready` is the **human-facing work-availability signal**. It checks, each with a 5 s
timeout (`READY_TIMEOUT_SECONDS`):

| `checks` key | Meant by `ok` | Failure example |
|---|---|---|
| `redis` | Redis `PING` answered | `unavailable: ConnectionError` |
| `s3` | `head_bucket` succeeded on the configured bucket | `bucket 'documents' unreachable (HTTP 404)` |
| `summarizer` | A summarizer exists: `gemini:<model>` in live+key mode, `fake summarizer` in fake mode | `gemini_api_key not configured` |
| `ai_mode` | `live` or `fake` | n/a |

In `AI_MODE=live` without a Gemini key the endpoint is deliberately `503` even though the process
is fine: it must not accept uploads that can only fail. In `AI_MODE=fake` the deterministic
summarizer counts as ready and readiness is `200`.

Observed healthy payload in this environment (**[executed]**; the demo ran in live mode with a
real key and a configured model alias):

```json
{"status":"ok","checks":{"redis":"ok","s3":"bucket 'documents' ok",
 "summarizer":"gemini:gemini-flash-lite-latest","ai_mode":"live"},"detail":null}
```

### Container health

```bash
docker compose ps          # [executed]
```

Observed (**[executed]**, before and during a Terraform-era start):

```text
epam-tech-assessment-api-1         api         Up ... (healthy)  127.0.0.1:8000->8000/tcp
epam-tech-assessment-frontend-1    frontend    Up ...            127.0.0.1:3000->80/tcp
epam-tech-assessment-localstack-1  localstack  Up ... (healthy)  127.0.0.1:4566->4566/tcp
epam-tech-assessment-redis-1       redis       Up ... (healthy)  6379/tcp
epam-tech-assessment-worker-1      worker      Up ...            8000/tcp
```

The `provisioner` is a one-shot service: while it is applying it appears as `Up` and the other
services are not started yet; after it exits 0 it is no longer in a plain `docker compose ps`.
Use `docker compose ps -a` to see its exit code, and `docker compose logs provisioner` to see the
Terraform output.

`worker` showing `Up` is not proof it is consuming; check its logs and the Redis group (sections 4
and 5). The worker's missing healthcheck is intentional (it exposes nothing).

## 4. Logs

```bash
docker compose logs -f api             # follow API logs
docker compose logs -f worker          # follow worker logs (the important one for jobs)
docker compose logs --tail=100 api worker
docker compose logs --since=10m worker
docker compose logs -f provisioner     # Terraform init/apply output; runs before api/worker
docker compose logs -f localstack      # S3 API logs (Terraform creates the bucket, no init hook)
docker compose logs -f frontend        # nginx access/error logs
```

**[executed]**: `docker compose logs --tail=6 api`, `--tail=10 worker` and `docker compose logs
provisioner` returned the expected streams (access lines on the API; `worker_started consumer=...
ai_mode=live stream=docs:jobs group=document-workers` on the worker; `Initializing provider
plugins...` / `Terraform has been successfully initialized!` on the provisioner while it ran).

Log format is `%(asctime)s %(levelname)s %(name)s %(message)s` (`basicConfig` in `app/main.py` and
`app/worker.py`); uvicorn access lines are appended in the same stream. Startup lines worth
knowing:

- `api_started ai_mode=... gemini_model=... s3_bucket=... s3_endpoint=... redis_host=...`
  (no secrets; only the Redis host part of the URL).
- `parser parser=<id> enabled=<bool> note=<reason>` once per parser at startup.
- `ai_mode=fake: ... markdown extraction and summaries are deterministic stand-ins.`
- `worker_started consumer=<hostname-pid> ai_mode=... stream=... group=... concurrency=<n>`
  (observed live: `concurrency=4`).
- `consumer_group_created stream=docs:jobs group=document-workers` (first boot only).
- `worker_draining in_flight=<n> grace_s=30` on SIGTERM, then `worker_stopped consumer=...`.
- `worker_shutdown_incomplete abandoned=<n> entries remain pending until the lease expires` when
  the grace period ran out.

Provisioner lines worth knowing (Terraform output, not app logs):

- `Initializing modules...` / `Installing hashicorp/aws v5.100.0...` on the first run (network);
  later runs reuse the cached provider and the lock file.
- `Terraform has been successfully initialized!`
- `Apply complete! Resources: 3 added, 0 changed, 0 destroyed.`
- On drift healing you see it re-create the three resources after refresh detects the bucket is
  gone; the bucket name is printed by `terraform output -raw bucket_name` at the end.

Per-job events to grep for:

| Log token | Means |
|---|---|
| `document_accepted` | Upload accepted; includes `document_id`, parser, bytes (not the filename) |
| `source_upload_failed` | S3 PUT failed; file rejected with `STORAGE_UNAVAILABLE` |
| `enqueue_failed` | Redis MULTI failed; object deleted, file rejected with `QUEUE_UNAVAILABLE` |
| `job_resumed` | Recovered job found a pages checkpoint; extraction skipped |
| `pages_checkpointed` | Pages persisted; the resume point exists |
| `job_completed` | Terminal success with duration |
| `job_attempt_failed` | A failed attempt with code, retryable flag, duration |
| `job_failed` | Terminal failure committed; includes the error code |
| `job_leased_elsewhere` | Another worker holds this job's lease; entry left pending |
| `lease_lost` | This worker lost ownership; nothing was written |
| `lease_renewal_rejected` / `lease_renewal_error` | Heartbeat could not renew (token changed / Redis error) |
| `orphan_stream_entry` | Stream entry whose job hash is gone; acked and dropped |
| `waiting_for_redis` / `redis_unavailable` | Worker cannot reach Redis; it retries with backoff |
| `job_unexpected_error` / `unexpected_processing_error` | Exception with a traceback in the log only |
| `job_deferred_redis_unavailable` | A Redis error interrupted the job; the entry is **not** acked and stays pending for a reclaim. This is a dependency blip, never a document failure - the fix for the old `INTERNAL_ERROR` misclassification |
| `worker_saturated` | All `WORKER_CONCURRENCY` slots are busy; the worker stops reading (backpressure). Logged at DEBUG, so set `LOG_LEVEL=DEBUG` to see it |
| `job_task_crashed` | One concurrent job task raised unexpectedly; the other jobs and the worker survive |
| `worker_stopped` | SIGTERM/SIGINT handled; pending entries are left for a replacement |

Logs intentionally contain no PDF bytes, no extracted page text, no prompts, and no API keys. A
document filename may appear in the job hash but not in these log lines.

Proxy-level rejections never reach the API, so they appear only in the **frontend** container log:
nginx logs rate-limit events at error level (`limiting requests, excess: ... by zone "uploads"`),
and the access log shows the `429`. A `503` from admission control or a disabled parser does appear
as an API access-log line with status 503, but there is no application log line for the admission
timeout itself - the response is the record.

## 5. Inspecting the queue and job state

All commands below were **[executed]** against the live stack, read-only. Stream and group names
are the defaults from `app/config.py`; if `STREAM_KEY`/`CONSUMER_GROUP` are overridden in `.env`,
substitute them (discover the real names with `docker compose exec -T redis redis-cli --scan
--pattern 'docs:*'`).

```bash
# Stream length and metadata
docker compose exec -T redis redis-cli XLEN docs:jobs
docker compose exec -T redis redis-cli XINFO STREAM docs:jobs

# Consumer group: pending count, consumers, lag, last delivered id
docker compose exec -T redis redis-cli XINFO GROUPS docs:jobs
docker compose exec -T redis redis-cli XINFO CONSUMERS docs:jobs document-workers

# Pending summary, then the actual pending entries with idle time and delivery count
docker compose exec -T redis redis-cli XPENDING docs:jobs document-workers
docker compose exec -T redis redis-cli XPENDING docs:jobs document-workers - + 10

# Read entries without claiming them (normally empty: XDEL removes them at terminal state)
docker compose exec -T redis redis-cli XRANGE docs:jobs - + COUNT 10
```

Observed in a healthy, idle stack (**[executed]**):

- `XLEN docs:jobs` -> `0` (terminal entries are XACK+XDEL'd; the stream is drained).
- `XINFO GROUPS docs:jobs` -> one group `document-workers`, `pending 0`, `consumers 11`,
  `lag 0`, `entries-read 24`, a `last-delivered-id`.
- `XINFO STREAM docs:jobs` -> `length 0`, `entries-added 24`, one group.
- `XPENDING docs:jobs document-workers` -> `0`.

The consumer count (11) is larger than the number of worker containers: every container/process
start registers a consumer named `<hostname>-<pid>` and nothing removes the old names
(`XGROUP DELCONSUMER` is never called). Stale consumer names are harmless for correctness; the
`pending` count and `lag` are the numbers that matter. With concurrent dispatch, one worker process
can legitimately hold several pending entries at once (one per in-flight job), so `pending > 0` is
normal while work is running - check the entries' idle times against the lease before treating it as
stuck.

### Per-document state

```bash
ID=<document uuid>

docker compose exec -T redis redis-cli HGETALL "docs:job:$ID"     # status, stage, attempt, error, ...
docker compose exec -T redis redis-cli GET     "docs:pages:$ID"   # JSON pages checkpoint, absent if not reached
docker compose exec -T redis redis-cli GET     "docs:summary:$ID" # summary markdown, only when completed
docker compose exec -T redis redis-cli TTL     "docs:lease:$ID"   # >0 and renewing = worker alive
docker compose exec -T redis redis-cli TTL     "docs:job:$ID"     # after terminal: up to RESULT_TTL_SECONDS
docker compose exec -T redis redis-cli TTL     "docs:pages:$ID"
docker compose exec -T redis redis-cli TTL     "docs:summary:$ID"

# Discover existing keys
docker compose exec -T redis redis-cli --scan --pattern 'docs:*'
```

Observed on a completed job (**[executed]**): the hash held the 13 fields, `status completed`,
`attempt 1`, `page_count 2`, `error` empty; `TTL docs:job:<id>` and `TTL docs:summary:<id>` were
both about 84,000 s, i.e. the 24 h result TTL was applied at commit.

Remember that missing fields encode nulls: `stage=""`, `page_count=""`, `error=""`.

### Recent-documents index (`docs:index`)

The list endpoint (`GET /api/v1/documents`) is backed by this sorted set: member = document id,
score = enqueue epoch. It has **no per-key TTL**; every accepted upload prunes entries older than
`RESULT_TTL_SECONDS`, and ids whose hash has expired are skipped when listing.

```bash
# How many documents are indexed
docker compose exec -T redis redis-cli ZCARD docs:index

# Newest 10 ids with their epoch scores
docker compose exec -T redis redis-cli ZREVRANGE docs:index 0 9 WITHSCORES

# What the list endpoint's cursor would return (strictly older than a score)
docker compose exec -T redis redis-cli ZREVRANGEBYSCORE docs:index "(1795637732.6776729" -inf LIMIT 0 20
```

If the list looks empty or short, check `ZCARD docs:index` first, then whether the job hashes still
exist (`HGETALL docs:job:<id>`): an index entry with an expired hash is intentionally skipped.

### S3 state

```bash
docker compose exec -T localstack awslocal s3api head-bucket --bucket documents
docker compose exec -T localstack awslocal s3 ls s3://documents/uploads/
```

Observed (**[executed]**): `head-bucket` returned the bucket region; `s3 ls s3://documents/uploads/`
listed nothing, consistent with sources being deleted after terminal state. If you use a different
`S3_BUCKET`, substitute it.

## 6. Stuck job or slow job?

Use `HGETALL docs:job:<id>` plus `XPENDING` plus the worker logs. The deadline makes "forever"
impossible: every job has `deadline_at = created_at + JOB_DEADLINE_SECONDS` (600 s by default),
and an attempt that starts after it fails immediately with `DEADLINE_EXCEEDED`.

| Observation | Likely meaning | Check next |
|---|---|---|
| `docker compose up` returned but the API is not reachable / the UI is 502 | The stack is gated on the one-shot `provisioner`: if Terraform failed, api/worker never start | `docker compose ps -a`; `docker compose logs provisioner`; first run needs network for the provider download |
| `status: queued` for more than a few seconds; `lag > 0` or `pending > 0` | The worker is not consuming (down, crash-looping, cannot reach Redis) **or** it is saturated with other work | `docker compose ps worker`; `docker compose logs --tail=50 worker`; look for `waiting_for_redis` / `redis_unavailable`; check `XINFO GROUPS` lag and `worker_started ... concurrency=` |
| Backlog grows but every document eventually completes; throughput is flat | The worker is **saturated**, not stuck. Throughput ceiling is roughly `WORKER_CONCURRENCY / job-duration` | `XLEN`/lag; `worker_saturated` with `LOG_LEVEL=DEBUG`; [PERFORMANCE.md](PERFORMANCE.md). Fix: raise `WORKER_CONCURRENCY` (I/O-bound work; default 4), add workers, or accept queue depth - do not raise it on live AI without pacing |
| Live AI jobs are slow (sub-1 doc/s) but complete | Provider pacing is doing its job: throughput is about `PROVIDER_MAX_REQUESTS_PER_MINUTE / calls-per-document` | `PROVIDER_MAX_REQUESTS_PER_MINUTE`, `PROVIDER_MAX_CONCURRENCY`; `worker_started`; [PERFORMANCE.md](PERFORMANCE.md) section 3. Expected with a free-tier RPM; raise the RPM only to the provider's published limit |
| Several documents fail with `PROVIDER_ERROR`/`PROVIDER_TIMEOUT` after 429/503 | The provider is being hit faster than allowed (no client-side pacing, or too many workers/concurrency for the RPM) | worker `job_attempt_failed` codes; provider status; `PROVIDER_MAX_REQUESTS_PER_MINUTE`. Fix: set the RPM to the published limit and re-upload; `Retry-After` is honoured (clamped) and backoff is jittered |
| `job_deferred_redis_unavailable` in the worker log | A Redis error interrupted a job; it is **deferred, not failed** - entry pending, lease released, reclaim resumes from the checkpoint | Redis health/restart timeline; `XPENDING`. Fix Redis; the job completes on a reclaim - do not re-upload (that creates a second document) |
| `status: parsing` or `summarizing`, `updated_at` moving | Slow but alive; long Gemini calls are normal in live mode (one call per page plus a summary) | `job_attempt_failed` / `job_completed` duration lines; provider timeouts are bounded. Wait, or lower `WORKER_CONCURRENCY`/RPM if it saturates the provider |
| `status: parsing`/`summarizing`, `updated_at` frozen for minutes, `TTL docs:lease:<id>` at 0 or absent | The worker died or lost ownership mid-job; the pending entry should be reclaimed after the lease idle threshold | `XPENDING` entry idle time vs `LEASE_SECONDS` (90 s); worker restart timeline |
| `status: retrying` with a recent `updated_at` | Working as designed: backoff is 2 s, 5 s, 10 s between attempts | The `error.code` on the job tells you the transient cause |
| `pending` entry with idle time > lease threshold and it stays pending | No worker is performing the idle-read sweep (worker down) or a claim keeps failing | Worker logs; reclaim manually (section 7) |
| `status: failed` | Terminal; nothing is stuck | `error.code` / `error.message` in the hash or the GET response |
| `GET` returns `404` | Unknown id or the 24 h result TTL expired | Expected for old documents |

A healthy worker renews the lease every `HEARTBEAT_SECONDS` (15 s), so `TTL docs:lease:<id>`
should sit between 0 and 90 and keep moving for a live job. A frozen TTL with a non-terminal job
is the clearest "stuck" signal. Separate "slow" from "stuck" by the ceiling, not by impatience:
with concurrency C, a burst of N documents at D seconds each settles in roughly `N * D / C`
seconds plus queueing; the [PERFORMANCE.md](PERFORMANCE.md) tables give the measured baselines
(4.22 / 16.32 / 22.99 settled docs/s at concurrency 1 / 4 / 8 in fake mode).

## 7. Manual reclaim of a pending entry

**[from source], not executed** (mutating; no pending entries existed). The worker does this
automatically on every idle read iteration: one `XAUTOCLAIM` sweep with
`min_idle_time = LEASE_SECONDS * 1000` (90,000 ms by default), `start 0-0`, `count 10`. A manual
reclaim is the same operation with a consumer name you choose:

```bash
# Claim the oldest entry idle for at least 90 s
docker compose exec -T redis redis-cli XAUTOCLAIM docs:jobs document-workers manual-reclaim 90000 0-0 COUNT 1

# Then inspect what was claimed
docker compose exec -T redis redis-cli XPENDING docs:jobs document-workers - + 10
```

Safety notes:

- Only reclaim while the owning worker is known dead, or after the pending idle time exceeds
  `LEASE_SECONDS`. Claiming a job that is still running gives you two executions: the fenced
  commit prevents duplicate *results*, but provider calls can be billed twice (at-least-once).
- The reclaim does not create a job hash or repair state; it only moves stream-entry ownership to
  a live consumer. If no worker is running, the claimed entry just sits with that consumer name
  until a worker sweeps again.
- Restarting the worker is equally valid and simpler: pending entries survive, a replacement
  sweeps them after the lease threshold, and checkpointed pages are reused
  (`job_resumed` in the log).
- To drop a poisonous entry permanently (documented, mutating, use with intent): `XACK
  docs:jobs document-workers <stream-id>` followed by `XDEL docs:jobs <stream-id>`, and decide
  separately what to do with the `docs:job:<id>` hash (delete it or leave it to expire).
- Concurrent dispatch does not change reclaim semantics: each job has its own lease, so reclaim is
  per stream entry, not per process. Pausing a worker process (`docker pause`) pauses **all** of
  its in-flight jobs at once - that is exactly what the fencing fault scenario does to prove a
  paused worker cannot overwrite a result once its leases have expired.

## 8. Reset paths

Pick the narrowest one that solves the problem.

| Goal | Command | Status |
|---|---|---|
| Let results expire naturally | do nothing; terminal keys carry `RESULT_TTL_SECONDS` (24 h) | [from source] |
| Remove one document's state | `docker compose exec -T redis redis-cli DEL "docs:job:<id>" "docs:pages:<id>" "docs:summary:<id>" "docs:lease:<id>"` | [from source], mutating |
| Clear all app state but keep the stack running | `docker compose exec -T redis redis-cli FLUSHDB` (drops the queue too; running workers will log `redis_unavailable` or see `orphan_stream_entry`) | [from source], destructive |
| Wipe Redis and LocalStack volumes (and therefore the bucket) | `docker compose down -v` then `docker compose up --build -d` - Terraform state survives, so the provisioner refreshes, detects the missing bucket and re-creates it | [from source], destructive; job state is lost, infrastructure is healed |
| Destroy only the local infrastructure | `docker compose run --rm --entrypoint terraform provisioner destroy -auto-approve -input=false`, then re-apply with `docker compose run --rm provisioner` | [from source], destructive to the bucket |
| Re-provision a missing bucket without wiping anything | re-apply the provisioner (section 9) | [from source] |
| Restart only the worker | `docker compose restart worker` | [from source] |

Redis runs with `--appendonly yes --appendfsync everysec` on the `redis-data` volume, so a plain
container restart does **not** lose job state. LocalStack community retains nothing across a
recreated container, which is exactly why the bucket is owned by Terraform instead of an
imperative hook: the next `up` detects the drift and re-creates it.

## 9. Re-provision the S3 bucket if it is missing

Symptom: `/health/ready` reports `"s3": "bucket 'documents' unreachable (HTTP 404)"`, uploads are
rejected with `STORAGE_UNAVAILABLE`, and the worker's S3 reads fail. This happens after LocalStack
loses the bucket (`docker compose down -v`, a recreated/restarted container, a manual volume wipe)
if the provisioner has not re-run since.

The bucket is owned by Terraform - there is no init hook any more, because two mechanisms for one
resource is how drift starts - so "re-provision" means re-apply the local environment:

```bash
# 1. Check that the bucket really is absent, and see what Terraform last did
docker compose exec -T localstack awslocal s3api head-bucket --bucket documents
docker compose logs --tail=20 provisioner

# 2. Re-run the one-shot provisioner (init + apply + output). `apply` refreshes first, so it
#    re-creates whatever vanished.
docker compose run --rm provisioner

# 3. Verify
docker compose exec -T localstack awslocal s3api head-bucket --bucket documents
curl -fsS http://localhost:8000/health/ready
```

**[executed]** the `head-bucket` and `docker compose logs provisioner` checks were run in this
environment (the bucket existed and the provisioner had been applied). Re-applying to heal a wiped
bucket was verified by the coordinator: `down -v` with state intact, Terraform refreshed, detected
the missing bucket, re-created it (`3 added`), and readiness returned `200`.

Do **not** fix this with `awslocal s3 mb`: Terraform would then be managing a resource it did not
create, and state and reality would be diverged by construction. Re-apply instead.

**Changing `S3_BUCKET` renames the bucket - it is a migration, not a free edit.** The name is the
Terraform resource identity, so re-applying with a different value destroys the old bucket and
creates the new one. The wiring makes both sides agree: `compose.yaml` passes
`TF_VAR_bucket_name: ${S3_BUCKET:-documents}` to the provisioner and `S3_BUCKET` to `api`/`worker`.
Verified end to end by the coordinator: with `S3_BUCKET=pdf-docs-custom`,
`docker compose run --rm --no-deps provisioner` produced
`Apply complete! Resources: 3 added, 0 changed, 3 destroyed` and `bucket_name = "pdf-docs-custom"`;
recreating api/worker gave readiness `200, s3: bucket 'pdf-docs-custom' ok`; reverting to the
default did the same in reverse and readiness reported `bucket 'documents' ok`. If you only want to
verify wiring, use `docker compose run --rm --no-deps --entrypoint terraform provisioner output
-raw bucket_name` (executed in this pass: printed `documents`).

## 10. Troubleshooting

| Symptom | Likely cause | What to check | Fix |
|---|---|---|---|
| UI shows `502 Bad Gateway` | API down or being recreated; historically nginx pinned the API container IP at startup | `docker compose ps api`; `curl http://localhost:8000/health/live`; `docker compose logs --tail=30 frontend` | nginx re-resolves `api:8000` through Docker DNS (`resolver 127.0.0.11 valid=10s` + variable upstream), so it recovers by itself; if not, restart `frontend` |
| `GET /health/ready` -> `503`, `checks.redis: unavailable: ...` | Redis down or unreachable | `docker compose ps redis`; `docker compose logs --tail=30 redis` | start/wait for Redis healthcheck (`redis-cli ping`) |
| `checks.s3: bucket 'documents' unreachable` | Bucket missing (LocalStack volume wiped/restarted and the provisioner has not re-run), LocalStack down, or a name mismatch caused by setting an env var for only one side | `docker compose ps localstack`; `docker compose logs --tail=20 provisioner`; section 9 | start localstack; re-apply the provisioner (`docker compose run --rm provisioner`); `S3_BUCKET` is wired to Terraform automatically, so make sure it is set consistently (e.g. exported in the same shell that runs Compose) |
| `docker compose up` leaves api/worker unstarted; `docker compose ps -a` shows `provisioner` exited `1` | Terraform failed, and api/worker are gated on `service_completed_successfully` | `docker compose logs provisioner` | fix the cause in the log (usually network for the first provider download, a bad `TF_VAR_*`, or a stale state lock) and `docker compose up -d` again |
| First `up` takes a minute or two and logs `Installing hashicorp/aws ...` | Expected: the provider is downloaded into `infra/envs/local/.terraform/` on first run | provisioner log | wait; later runs reuse the cache. The first run needs network |
| `Error acquiring the state lock` / `.terraform.tfstate.lock.info` exists | A previous provisioner was killed mid-apply | `infra/envs/local/` directory | if no Terraform run is in flight, remove `infra/envs/local/.terraform.tfstate.lock.info` and re-apply; never remove it while an apply is running |
| `terraform fmt`/`validate` fails | Terraform tree edited without formatting or a module call broken | section 2 commands | fix and re-run; both are clean in this delivery (verified) |
| Apply hangs for ~3 minutes then fails on a lifecycle configuration | LocalStack never confirms S3 lifecycle rules | `infra/envs/local/main.tf` | keep `enable_lifecycle = false` locally (already set); production enables it |
| Bucket still absent after re-applying | Provisioner failed, or it manages a different bucket name | provisioner log; `S3_BUCKET` | fix and re-apply; do not create it with `awslocal s3 mb` |
| `503` upload: `summarization is unavailable (gemini_api_key not configured)` | `AI_MODE=live` without a Gemini key; every parser is disabled by design | `curl /health/ready`; `curl /api/v1/parsers` | set `GEMINI_API_KEY` (and optionally `AI_MODE=live`) in `.env`, or `AI_MODE=fake`; then `docker compose up -d --force-recreate api worker` |
| `503` upload: `Parser 'gpt-5-mini' is not available right now. ... OPENAI_API_KEY is not configured` | Expected in live mode without an OpenAI key: the parser is implemented but not configured | `GET /api/v1/parsers`; is `OPENAI_API_KEY` set? | set `OPENAI_API_KEY` (and `GEMINI_API_KEY`, because the summary is Gemini-only) and recreate api/worker. In fake mode the parser is a labelled stand-in and is enabled |
| `503` upload: `summarization is unavailable (gemini_api_key not configured)` **even with `OPENAI_API_KEY` set** | Live mode needs Gemini for the summary; without it every parser is disabled by design | `curl /health/ready`; `GET /api/v1/parsers` | set `GEMINI_API_KEY`, or run `AI_MODE=fake` |
| `429` through the UI with `{"code":"RATE_LIMITED"}` | nginx per-IP `limit_req` exceeded (20 r/s uploads / 100 r/s api, burst 40/200) | `docker compose logs --tail=50 frontend` (rate-limit messages + access log); `grep -n limit_req frontend/nginx.conf` | back off / retry; for a legitimate client raise the zone rate or burst in the **http context** of `frontend/nginx.conf`; direct `:8000` is not proxy-limited |
| `503` upload: `The server is at its concurrent-upload limit (MAX_CONCURRENT_UPLOADS=16). Retry shortly.` | Admission control: 16 uploads already buffering; the request waited 10 s for a slot | response headers (`Retry-After: 1`); `docker compose logs --tail=50 api` | retry; raise `MAX_CONCURRENT_UPLOADS`/`UPLOAD_SLOT_TIMEOUT_SECONDS` in `.env` only if the host has memory headroom (uploads are buffered before the S3 write) |
| `GET /api/v1/documents` returns `503` | Redis unavailable (the only dependency of the list endpoint) | `curl /health/ready`; `docker compose ps redis` | restart/wait for Redis; the list is an index, the detail endpoint has the same dependency |
| List is empty or shorter than expected although documents exist | `docs:index` pruned/absent, or the job hashes expired | `ZCARD docs:index`; `HGETALL docs:job:<id>`; [section 5](#5-inspecting-the-queue-and-job-state) | expected after the 24 h retention; index entries with expired hashes are skipped. Old lists may look short until new uploads prune the index |
| Frontend container crash-loops with `"limit_req_zone" directive is not allowed here` | `limit_req_zone` was moved inside `server {}`; it is only legal in the http context | `docker compose logs frontend`; `frontend/nginx.conf` | keep the two `limit_req_zone` lines and `limit_req_status` outside the `server` block (the file is included inside `http {}`) |
| CI quality job fails on `ruff` / `ruff format --check` / `mypy` / encoding | A style, type or non-UTF-8 regression | run the same commands locally (section 12); `python scripts/check_encoding.py` | fix the code or the encoding; do not disable the gate. `ruff format` is the authority for formatting |
| `pip` complains that a lock and intent disagree, or CI resolves different versions | `requirements*.txt` changed without regenerating the lock files | `git diff requirements*.txt requirements*.lock.txt`; Dockerfile comment | regenerate both lock files from the verified image (section 12) and rebuild |
| Gemini jobs fail `PROVIDER_TIMEOUT` / `PROVIDER_ERROR` after retries | Provider congestion (the mandated `gemini-3.1-flash-lite` returned `503 UNAVAILABLE` during development), wrong model id, or exhausted quota | worker logs `job_attempt_failed`; `PROVIDER_CONFIG` vs transient code | for a demo, set `GEMINI_MODEL=gemini-flash-lite-latest` in `.env` and recreate; leave the default as the brief's id otherwise |
| `PROVIDER_CONFIG` | Wrong/renamed model or bad key (Gemini 4xx, or OpenAI 401/403/unknown model) | worker log for the attempt; provider status | correct `GEMINI_MODEL`/`GEMINI_API_KEY` or `OPENAI_MODEL`/`OPENAI_API_KEY`; the code never silently substitutes a model |
| Upload rejected `422 NOT_A_PDF` although the file opens in a reader | The uploaded bytes do not start with `%PDF-` (the check is on raw bytes, not extension/MIME) | `Format-Hex -Path file.pdf -Count 8` (PowerShell) or `head -c 5 file.pdf` | re-export/download the original file; do not send a proxy-generated preview |
| Upload `422 EMPTY_FILE` | Zero bytes were received | file size locally | re-upload the file |
| `413` through `:3000` for a large batch, but `:8000` accepts each file | nginx caps bodies at 25 MB | `frontend/nginx.conf` `client_max_body_size` | raise the nginx cap, or upload directly to the API, or split the batch |
| Job stays `queued`, worker logs `waiting_for_redis` | Worker started before Redis was reachable; it retries with backoff | `docker compose logs worker` | it should recover on its own; otherwise restart `worker` |
| Backlog grows, all jobs still complete, no errors | Worker saturated (throughput ceiling), not stuck | `XINFO GROUPS` lag; `worker_started ... concurrency=`; `worker_saturated` at `LOG_LEVEL=DEBUG` | raise `WORKER_CONCURRENCY` / add workers for I/O-bound loads; keep provider RPM in step - see section 6 and [PERFORMANCE.md](PERFORMANCE.md) |
| Many documents failed with `PROVIDER_ERROR` after 429/503 | Provider rate limit hit because there is no client-side pacing configured | worker `job_attempt_failed`; `PROVIDER_MAX_REQUESTS_PER_MINUTE` | set it to the provider's published RPM and re-upload; `Retry-After` is honoured (clamped by `RETRY_AFTER_MAX_SECONDS`) and backoff is jittered |
| Worker restart takes up to 30 s | Graceful drain of in-flight jobs (`worker_draining`) | `docker compose logs worker` | expected; unfinished jobs are reclaimed after the lease expires, nothing is lost |
| `job_deferred_redis_unavailable` in the worker log | A Redis blip interrupted a job; it is deferred, not failed | Redis health; `XPENDING` | fix Redis; the job resumes from the checkpoint on a reclaim - do not re-upload |
| Job stays `parsing`/`summarizing` | Worker died mid-job (lease expiry) or a provider call is genuinely slow | `XPENDING`, `TTL docs:lease:<id>`, `deadline_at` | wait for `XAUTOCLAIM` (>= 90 s idle), restart the worker, or reclaim manually (section 7) |
| Every job fails `ATTEMPTS_EXHAUSTED` | Provider/model problem with a transient classification, so the budget is consumed before succeeding | `error.message` of the last attempt in the job hash | fix the provider config; re-upload |
| Document `404` after previously working | Result TTL expired (`RESULT_TTL_SECONDS`, 24 h) | `TTL docs:job:<id>` (already gone) | expected; re-upload |
| `blank` sample fails `OCR_REQUIRED` | Expected: PyPDF found no extractable text | job `error` | use the Gemini parser (in fake mode a deterministic stand-in; in live mode the real model) |
| `docker compose up` fails with `ports are not available: ... 127.0.0.1:4566` | Another process on the host already owns port 4566 | `docker compose ps`; `netstat -ano \| findstr 4566` | set `LOCALSTACK_PORT=<free port>` in `.env` (documented in `.env.example`) and run `docker compose up -d localstack`; container-internal addressing is unaffected. On a clean host the default 4566 works (verified) |
| Shell script fails with `bad interpreter: /bin/sh^M` | CRLF line endings in a shell script | file bytes | keep LF; `.gitattributes` enforces it, re-checkout if needed |
| `docker compose build` produces an image with stale code on a OneDrive path | BuildKit snapshotting a stale context (documented environment caveat in `PROGRESS.md`) | compare `grep -c HttpRetryOptions app/providers/gemini.py` inside the image | build from a directory outside OneDrive, or use the documented workaround |
| `docker` commands fail with "Access is denied" on the named pipe | A restricted sandbox denies the Docker Desktop pipe | error text | run from an unrestricted terminal; not an engine fault |
| `scripts/smoke.sh` fails with `curl: (26)` on Windows | Git Bash POSIX path passed to native curl | script path handling | the script already uses `pwd -W`; run it from Git Bash with the repo checked out at a native path |

## 11. Load and fault testing

Two harnesses cover what the pytest suite deliberately does not. Neither was re-run during this
documentation pass; the results quoted here are the coordinator's, recorded in
[PERFORMANCE.md](PERFORMANCE.md) and [PROGRESS.md](PROGRESS.md).

### Load harness - `scripts/stress.py`

Bounded concurrent uploads, then every accepted document polled to terminal. It exits non-zero if
any upload was not `202`, any 5xx occurred, any file was rejected, or any document failed or timed
out, so it works as a gate. It needs `httpx` (test image) and both `scripts/` and `samples/` are
dockerignored, so mount them:

```bash
docker compose --profile test run --rm \
  -v "$(pwd)/scripts:/app/scripts:ro" \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python /app/scripts/stress.py --docs 200 --concurrency 200 --label burst
```

Key flags: `--docs` (exact upload count), `--concurrency` (parallel in-flight uploads), `--parser`,
`--file`, `--poll-timeout` (default 120 s, for the settle phase as a whole), `--request-timeout`,
`--json-out PATH`, `--label`. Exit codes: `0` clean, `1` failed run (reason on the `RESULT:` line),
`2` CLI misuse. A machine-readable summary is printed as one `JSON ` line.

For repeatable throughput numbers, point it at a disposable fake-mode worker with a fixed delay
(measured method in [PERFORMANCE.md](PERFORMANCE.md) section 1):

```bash
docker compose stop worker
docker compose run -d --name stress-w --no-deps \
  -e AI_MODE=fake -e FAKE_DELAY_SECONDS=0.2 -e WORKER_CONCURRENCY=8 \
  worker python -m app.worker
# ... run stress.py ...
docker rm -f stress-w && docker compose start worker
```

Recorded results: worker parallelism 1/4/8 -> 4.22/16.32/22.99 settled docs/s; 100 docs at client
concurrency 1/10/50/100 -> 0 errors, p95 upload 9/88/651/802 ms; 200 concurrent uploads ->
200/200 accepted, 0 errors, 31.0 docs/s; 50 x 200 KB completed, 10 x 1 MB failed cleanly with
`TOO_MUCH_TEXT` and 0 HTTP errors.

### Fault injection - `scripts/resilience.sh`

**Destructive**: it kills worker processes, restarts Redis and pauses a worker. Run it against a
stack you can recreate, and prefer `WORKER_MODE=fake` so it costs no provider quota:

```bash
WORKER_MODE=fake ./scripts/resilience.sh all
./scripts/resilience.sh crash      # one scenario: SIGKILL recovery
./scripts/resilience.sh redis      # one scenario: Redis restart with a backlog
./scripts/resilience.sh fencing    # one scenario: paused stale writer must not commit
```

It uses a short lease by default (`LEASE_SECONDS=15`, `HEARTBEAT_SECONDS=5`) so recovery is
observable in seconds, restores the normal worker on exit via a trap, and needs docker + curl only.
It exits non-zero if any check fails, so it can also be used as a gate. Recorded results: crash
5/5, redis 3/3 (30/30 completed, 0 lost), fencing 6/6. Scenario B is what found the
transient-Redis-error misclassification that is now fixed and logged as
`job_deferred_redis_unavailable`.

## 12. Test suites and quality gates

Full inventory and per-test descriptions: [TESTING.md](TESTING.md). Current counts: **82 backend
checks** run by default (44 unit, 16 API integration, 14 repository/lease, 8 proxy e2e) plus **4
opt-in** (3 concurrency, 1 live AI), plus 29 frontend vitest tests.

```bash
# 44 unit tests, offline (no Redis/S3/API)
docker compose --profile test run --rm test

# 16 API integration tests (needs the stack; samples mount required)
docker compose --profile test run --rm \
  -e API_BASE_URL=http://api:8000 \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python -m pytest tests/integration/test_api.py -m integration -q

# 14 repository/lease tests (needs Redis only; the readiness wait is not autouse any more)
docker compose --profile test run --rm \
  -e REDIS_URL=redis://redis:6379/0 \
  test python -m pytest tests/integration/test_repository.py -m repository -q

# 8 proxy e2e tests (needs the frontend service too)
docker compose --profile test run --rm \
  -e FRONTEND_URL=http://frontend:80 \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python -m pytest tests/e2e -m e2e -q

# Opt-in load suites (3 concurrency tests need a disposable fake-mode worker, see section 11;
# the 1 live-AI test needs AI_MODE=live + a real GEMINI_API_KEY + RUN_LIVE_AI_TESTS=1)
docker compose --profile test run --rm \
  -e API_BASE_URL=http://api:8000 -e RUN_CONCURRENCY_TESTS=1 \
  -v "$(pwd)/samples:/app/samples:ro" \
  test python -m pytest tests/integration/test_concurrency.py -q
```

`asyncio_mode = "auto"` is set in `pyproject.toml` (the repository suite is async); markers are
`integration`, `e2e` and `repository`. The Compose `test` profile's baked-in command is still
`pytest -q -m "not integration"`, which excludes repository and e2e because both also carry the
`integration` marker.

Quality gates (also the six CI jobs in `.github/workflows/ci.yml`: quality, unit, integration+e2e,
frontend, terraform; the quality job runs lint, format, types, secret grep and encoding):

```bash
# In the test image (ruff + mypy are dev dependencies)
docker run --rm -v "$PWD:/src" -w /src <test-image> ruff check .
docker run --rm -v "$PWD:/src" -w /src <test-image> ruff format --check .
docker run --rm -v "$PWD:/src:ro" -w /src <test-image> mypy --cache-dir=/tmp/mypy

# On the host (stdlib only)
python scripts/check_encoding.py        # fails on any non-UTF-8 text file
```

Lock files: the Dockerfile installs `requirements.lock.txt` (and
`requirements-dev.lock.txt` for the test profile); `requirements*.txt` are the human-edited intent.
Regenerate after changing the intent, from the verified image (the command is in the Dockerfile
comment):

```bash
docker run --rm --entrypoint pip <image> freeze | grep '==' | sort > requirements.lock.txt
# and the dev variant from an INSTALL_DEV=1 image -> requirements-dev.lock.txt
```

CI runs with `AI_MODE=fake` and no provider credentials, so nothing in the pipeline claims to
exercise a live model.

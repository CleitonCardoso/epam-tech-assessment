# Performance and fault tolerance — measured, not estimated

Every number here was produced by running the stack in this repository. Commands are given so a
reviewer can reproduce or contradict them. Where a run used the deterministic `AI_MODE=fake`
stand-ins that is stated explicitly; live-provider runs are marked as such and cost API quota.

Environment: Docker Desktop 28.3.2, Compose v2.38, 16 GB RAM, Python 3.12 image, Redis 7.4-alpine,
LocalStack 3.8, worker/API from the same image. Sample payloads: `samples/sample-text.pdf`
(1.2 KB, 2 pages) for throughput runs, plus generated 200 KB and 1 MB PDFs for payload runs.

## TL;DR

| Question | Answer |
|---|---|
| Does it survive a worker being SIGKILLed mid-job? | Yes — reclaimed from the page checkpoint, job completes |
| Does it survive a Redis restart with a 30-document backlog? | Yes — 30/30 completed, 0 lost |
| Can a stale worker overwrite a finished result? | No — fenced; it detects the lost lease instead |
| How many concurrent uploads before errors? | 200 concurrent: 0 errors (not the limit — the API is not the bottleneck) |
| What actually limits throughput? | The AI provider's rate limit, then worker concurrency. Never the API |
| What happens when the provider rate-limits? | Unpaced: documents **fail**. Paced: throughput drops, documents **complete** |

## 1. Worker parallelism is the throughput knob

40 documents, client concurrency 20, fake AI with a fixed 200 ms delay per document, so the only
variable is `WORKER_CONCURRENCY`:

| WORKER_CONCURRENCY | settled docs/s | p50 end-to-end | p95 end-to-end | completed |
|---|---|---|---|---|
| 1 | 4.22 | 4447 ms | 8276 ms | 40/40 |
| 4 | 16.32 | 1523 ms | 2268 ms | 40/40 |
| 8 | 22.99 | 819 ms | 1523 ms | 40/40 |

Near-linear to 4, then sublinear at 8 (PyPDF is CPU work and the host has finite cores). This is
why the default is 4 and 8 is documented as "for I/O-bound loads".

Reproduce:

```bash
docker compose stop worker
docker compose run -d --name stress-w --no-deps -e AI_MODE=fake -e FAKE_DELAY_SECONDS=0.2 \
  -e WORKER_CONCURRENCY=8 worker python -m app.worker
docker compose --profile test run --rm -v "$(pwd)/scripts:/app/scripts:ro" \
  -v "$(pwd)/samples:/app/samples:ro" test \
  python /app/scripts/stress.py --docs 40 --concurrency 20
```

## 2. The API is not the bottleneck

100 documents per run, worker concurrency 8, fake AI:

| Client concurrency | accepted | errors | peak in-flight | upload p95 | settled docs/s |
|---|---|---|---|---|---|
| 1 | 100 | 0 | 1 | 9 ms | 25.32 |
| 10 | 100 | 0 | 10 | 88 ms | 25.67 |
| 50 | 100 | 0 | 50 | 651 ms | 27.13 |
| 100 | 100 | 0 | 100 | 802 ms | 29.43 |

Throughput is flat from 1 to 100 clients because the worker is saturated; only latency grows.
At **200 concurrent** uploads and 200 documents: 200/200 accepted, **0 HTTP errors, 0 5xx**, peak
in-flight 200, upload p50 1070 ms / p95 1555 ms, 31.0 docs/s settled, all 200 completed.

The API does no parsing and no model calls: it validates, writes one S3 object, and does one
`MULTI` (HSET + XADD). That is why it scales this far without tuning.

## 3. With the real provider, pacing decides success vs failure

Live Gemini, 3 provider calls per 2-page document (one per page + one summary).

Baseline, 10 documents, concurrency 5: uploads at 162 docs/s, but the settle phase managed
**1.4 docs/s** (p50 2.1 s, max 7.2 s). Provider latency, not our code, is the wall.

Then a 20-document burst:

| Configuration | Result |
|---|---|
| No client-side pacing, `MAX_JOB_ATTEMPTS=3` | Several documents **failed** with `PROVIDER_ERROR`. Logs show `HTTP/1.1 429 Too Many Requests` and `503`, and the fixed 2/5/10 s backoff exhausted all three attempts |
| `PROVIDER_MAX_REQUESTS_PER_MINUTE=20`, `MAX_JOB_ATTEMPTS=4` | **20/20 completed, 0 failed, 0 error codes**, p50 3.3 s / p95 22.4 s, 0.76 docs/s |

0.76 docs/s is almost exactly 20 RPM ÷ 3 calls per document. That is the intended trade: pacing
converts a rate limit into *slower but successful* work. It is the difference between an honest
throughput ceiling and documents that fail for no reason the user can act on.

Three mechanisms do that, all fixed as a result of this testing:

1. `Retry-After` from the provider is honoured (clamped by `RETRY_AFTER_MAX_SECONDS`).
2. Backoff is jittered, so workers do not retry in lockstep after a shared outage.
3. A token bucket (`PROVIDER_MAX_REQUESTS_PER_MINUTE`) and a semaphore (`PROVIDER_MAX_CONCURRENCY`)
   pace calls *before* they hit the provider.

**Tuning rule for live mode:** set `PROVIDER_MAX_REQUESTS_PER_MINUTE` to your provider's published
RPM and expect `throughput ≈ RPM / calls_per_document`. Raising `WORKER_CONCURRENCY` without pacing
just manufactures 429s.

## 4. Payload size

| Run | Result |
|---|---|
| 50 concurrent × 200 KB (≈180 k chars, under the limit) | 50/50 completed, 0 HTTP errors, 3.59 docs/s, upload p95 379 ms |
| 10 concurrent × 1 MB (≈897 k chars, over `MAX_EXTRACTED_CHARS`) | 10/10 accepted, then **10/10 failed cleanly with `TOO_MUCH_TEXT`**, 0 HTTP errors |

The second row is the intended behaviour: oversized *content* is a per-document failure with a
stable code, not a crash and not a rejected request. Container memory after both runs:
api 123 MiB, localstack 147 MiB, redis 17 MiB.

Memory is bounded by `MAX_BATCH_BYTES × in-flight requests`, because an upload is buffered before it
is written to S3. With the defaults (50 MB batch) and nginx's 25 MB body cap, a sustained flood of
large batches is the one way to exhaust memory — it is documented rather than hidden.

## 5. Fault injection — `scripts/resilience.sh`

Run with `WORKER_MODE=fake` so fault testing costs no API quota (the script also supports `live`).

| Scenario | Checks | Result |
|---|---|---|
| **A. Worker SIGKILLed mid-summary** | 5 | **passed** — entry stayed pending while the lease was held, the killed job did not complete, a replacement worker reclaimed it and finished it, pending returned to 0 |
| **B. Redis restarted with a 30-document backlog** | 3 | **passed** — accepted 30/30, after recovery **completed 30, failed 0, lost 0, unresolved 0** |
| **C. Stale writer (fencing)** | 6 | **passed** — worker A was `docker pause`d, its lease expired, worker B finished the job; on unpause A detected the lost lease, never reported completion, and the document stayed completed |

Scenario C is the one worth reading the code for: every state write goes through a WATCH/MULTI
compare against the lease token, so a resumed zombie cannot commit or acknowledge.

### The bug these tests found

Scenario B originally reported `completed 29, failed 1`, with the failure recorded as
`INTERNAL_ERROR`. The traceback showed `redis.exceptions.ConnectionError` raised inside
`commit_completed` — a **transient** Redis blip during the restart — being caught by the generic
handler and written to the document as a *permanent* failure.

That is exactly the wrong classification: the document was fine, the store was briefly down. The
worker now re-raises `RedisError` from the attempt loop, so nothing is acknowledged, the message
stays pending, and a reclaim resumes from the checkpoint. Re-run: **30/30 completed, 0 failed**.

## 6. Known limits (honest list)

- **Single Redis instance.** The lease/commit transactions are not Redis Cluster compatible.
- **Uploads are buffered in memory** before the S3 write; see the memory note above.
- **LocalStack S3** is a stand-in: it was not stressed as a storage backend, only as an
  AWS-compatible target. Real S3 latency/throughput will differ.
- **`WORKER_CONCURRENCY` > 8 is untested** here; it is CPU-bound on PyPDF and I/O-bound on
  providers, so the useful ceiling depends on which parser you use.
- **No load test against real AWS.** The production topology (`compose.prod.yaml` +
  `infra/envs/prod`) is validated, not benchmarked.
- Numbers are from one machine with Docker Desktop; treat the *ratios* as portable and the absolute
  values as indicative.

## 7. Reproducing anything here

```bash
# throughput / concurrency (no API cost)
docker compose --profile test run --rm -v "$(pwd)/scripts:/app/scripts:ro" \
  -v "$(pwd)/samples:/app/samples:ro" test \
  python /app/scripts/stress.py --docs 200 --concurrency 200

# fault injection (no API cost)
WORKER_MODE=fake ./scripts/resilience.sh all

# live provider, paced
docker compose stop worker
docker compose run -d --name paced --no-deps -e PROVIDER_MAX_REQUESTS_PER_MINUTE=20 \
  worker python -m app.worker
```

`scripts/stress.py` exits non-zero if any upload is not 202, any 5xx occurs, or any document fails
to reach a terminal state inside `--poll-timeout`, so it is usable as a gate rather than a report.

## 8. Real-world corpus (10 documents from a personal Downloads folder)

Synthetic fixtures prove the plumbing; these prove it against documents nobody designed for it:
invoices, contracts, lecture slide decks, a book extract, tax statements and a scanned power of
attorney. Uploaded from the host with `curl`, one at a time, `parser=pypdf` unless stated.

| Document type | Size | Pages | Extracted chars | Result | Wall time |
|---|---|---|---|---|---|
| Utility invoice | 0.02 MB | 1 | 2 437 | completed | 1.6 s |
| Resume | <0.1 MB | 2 | 2 195 | completed | 5.1 s |
| Articles of association | 0.28 MB | 4 | 10 938 | completed | 3.0 s |
| Signed contract | 0.32 MB | 7 | 25 837 | completed | 4.4 s |
| **Scanned power of attorney** | 0.43 MB | 1 (image) | 0 | **failed - `OCR_REQUIRED`** | 0.8 s |
| Tax statement | 1.13 MB | 29 | 43 681 | completed | 6.6 s |
| Lecture deck | 1.53 MB | 24 | 27 702 | completed | 35.1 s |
| Book extract | 1.24 MB | 34 | 78 918 | completed | 8.8 s |
| Scanned-looking report | 4.37 MB | 13 | 24 341 | completed | 5.2 s |
| Small data dictionary | <0.1 MB | 1 | 1 982 | completed | 1.5 s |

**The scan is the interesting row.** `parser=pypdf` cannot read an image-only page, so the document
fails with a stable, actionable code (`OCR_REQUIRED`, message tells the user to choose Gemini). The
same file re-uploaded with `parser=gemini` completed in 5.9 s and returned a faithful transcription
of the page plus a correct document summary � that is the whole reason the two parsers exist.

Also verified on this corpus:

- **AI page cap**: a 24-page deck selected with `parser=gemini` fails fast with
  `TOO_MANY_PAGES_FOR_AI` and the message "Use the PyPDF parser for large documents" � before any
  provider call is made, so a mis-selection costs no quota. The same file succeeds with `pypdf`.
- **Accents and Portuguese** survive extraction (invoice text came back with correct encoding).
- **Text-heavy slide decks are slow**: 24 pages took 35 s with PyPDF while a 34-page book took 8.8 s,
  because extraction cost tracks text and layout, not file size. The whole-job deadline (600 s) and
  the asynchronous API are what keep that from being a user-visible hang.

These are real personal documents, so they are **not** in the repository, the archive, or this
report's fixtures: uploads go to LocalStack, the source object is deleted at terminal state, and the
Redis records expire with `RESULT_TTL_SECONDS`.

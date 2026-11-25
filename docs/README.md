# Documentation index

Reviewer-facing documentation for the async PDF-processing application (BWAI 2.1 technical
assessment). The repository root [README.md](../README.md) is the entry point for *running* the
stack; this index says which document answers which question and which documents are
authoritative.

## 5 minutes if you are reviewing

A minimal path through the evidence, in this order:

1. [../README.md](../README.md) - what the system does, the 30-second quickstart, the AI modes,
   and the documented limits. Note that `docker compose up` is the whole story: it also runs a
   one-shot Terraform `provisioner` that creates the LocalStack infrastructure, so there is no
   separate setup step and no Terraform install. (2-3 min)
2. [PROGRESS.md](PROGRESS.md) - the verification ledger: every command that was actually run,
   its result, and what was *not* verified and why - including the Terraform apply and drift-heal
   runs. (1 min)
3. [DECISIONS.md](DECISIONS.md) - what was changed from the original plan and the reason for
   each change. (1 min)
4. [REQUIREMENTS.md](REQUIREMENTS.md) - the brief traced line by line, including the named gaps
   (`gpt-5-mini` implemented but not live-verified; UI verified at component/proxy level, not in a
   browser). (2 min)

If you want depth instead of breadth after that, pick one: [API.md](API.md) for the HTTP
contract, [ARCHITECTURE.md](ARCHITECTURE.md) for the queue/lease design, provider matrix and the
Terraform tree, [TESTING.md](TESTING.md) for what the test suites do and do not prove, or
[PERFORMANCE.md](PERFORMANCE.md) for the measured load and fault-injection campaign (throughput,
latency, pacing, crash recovery, fencing) with reproduction commands.

## Infrastructure (Terraform)

Infrastructure as code lives in [`../infra`](../infra), and Compose applies it: `docker compose up`
starts `localstack`, then a one-shot `provisioner` service runs
`terraform init -backend=false && terraform apply -auto-approve` against LocalStack, and only then
do `api`/`worker` start. Terraform runs inside a container (`hashicorp/terraform:1.9`), so a
reviewer installs nothing. The first run downloads the AWS provider (a minute or two); later runs
reuse the cache. State lives in `infra/envs/local/terraform.tfstate` (gitignored).

| Path | Purpose |
|---|---|
| [`../infra/versions.tf`](../infra/versions.tf) | Terraform and provider version pins |
| [`../infra/modules/s3`](../infra/modules/s3) | Shared bucket module: private public-access block, versioning, optional lifecycle |
| [`../infra/modules/ec2-app`](../infra/modules/ec2-app) | Production host module: EC2, security group, IAM instance profile, Elastic IP, user_data |
| [`../infra/envs/local`](../infra/envs/local) | LocalStack environment - applied automatically by Compose |
| [`../infra/envs/prod`](../infra/envs/prod) | Real-AWS environment - **validated, not applied** in this delivery |
| [`../infra/envs/prod/terraform.tfvars.example`](../infra/envs/prod/terraform.tfvars.example) | Template for the production variables |

Design, startup order and what is verified vs not: [ARCHITECTURE.md](ARCHITECTURE.md).
Operating it (provisioner logs, re-apply, state, destroy, troubleshooting):
[RUNBOOK.md](RUNBOOK.md).

## Authoritative vs derived

The four documents below are the coordinator's authoritative record. They are written first,
updated last, and everything else must agree with them. If a derived document contradicts one of
them, the authoritative document wins and the contradiction is a bug worth reporting.

| Document | Why it is authoritative |
|---|---|
| [CONTRACT.md](CONTRACT.md) | Frozen interfaces: settings, provider signatures, HTTP API, Redis schema. Another agent built against this file. |
| [DECISIONS.md](DECISIONS.md) | What was kept from the original plan, what was cut, and the accepted risks. |
| [REQUIREMENTS.md](REQUIREMENTS.md) | Line-by-line traceability to the assessment brief, with named gaps. |
| [PROGRESS.md](PROGRESS.md) | Verification ledger: executed commands, results, conflicts, environment facts. |

The documents below are **derived**: written from the source code and from the authoritative
docs, for reviewers and future contributors. They describe the code as it is, not as it was
planned, and they are the documents to update when the code moves. [PERFORMANCE.md](PERFORMANCE.md)
is the exception in origin - it is a coordinator-authored **measured report**, and it is the source
every other document quotes for throughput, latency and fault-tolerance numbers.

| Document | One-line purpose |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, provisioning/startup order, request path, job state machine, Redis keys (including `docs:index`), S3 layout, lease/fencing rationale, worker concurrency, provider matrix (PyPDF + Gemini + GPT-5 Mini) and pacing, admission control and proxy rate limits. |
| [API.md](API.md) | Every endpoint (upload, list/pagination, detail, parsers, health), exact JSON shapes, status codes incl. `429`/admission `503`, the real error-code table, curl examples, limits and protection layers. |
| [RUNBOOK.md](RUNBOOK.md) | Operating the stack: health, logs, queue/index inspection, stuck-vs-saturated diagnosis, load/fault harnesses, test commands, lock regeneration, reset paths, troubleshooting. |
| [TESTING.md](TESTING.md) | Test inventory (44 unit, 16 API integration, 14 repository/lease, 8 proxy e2e, 4 opt-in, 29 frontend), what each layer proves, exact commands, fixtures, and what is not covered. |
| [PERFORMANCE.md](PERFORMANCE.md) | Measured throughput/latency under load, worker-concurrency scaling, provider pacing, and the results of the three fault-injection scenarios - with the commands to reproduce them. |
| [SECURITY.md](SECURITY.md) | Honest threat model for a trusted local demo, including the listing endpoint's enumeration surface, per-IP rate limits/admission as blast-radius controls, cost/abuse, and what production would require. |
| [DEVELOPING.md](DEVELOPING.md) | How to add a parser (two worked provider examples) or endpoint, how to change limits/tuning knobs safely, module responsibilities, quality gates and lock regeneration, conventions. |

## Optional background (now genuinely related to the infra layer)

[infra-playbook.md](infra-playbook.md) is **not authoritative documentation for this
application**: it is background research distilled from a different, earlier project
(a Spring Boot + Vue app with Terraform + LocalStack + Docker). It is now directly relevant to
the `infra/` Terraform layer - several of its lessons are deliberately followed here, including the
`enable_lifecycle` flag for LocalStack's lifecycle-configuration trap, a committed
`.terraform.lock.hcl`, committed `terraform.tfvars.example`, `fmt -check` + `validate` per env, and
one module shared by local and production. Read it as optional background on *why* the infra is
shaped this way; the source of truth for this application is [`../infra`](../infra) together with
[ARCHITECTURE.md](ARCHITECTURE.md) and [RUNBOOK.md](RUNBOOK.md), and nothing in the playbook was
re-verified against this repository.

## Scripts and harnesses

| Script | What it is | Safe against a live stack? |
|---|---|---|
| `scripts/smoke.sh` | Functional end-to-end walkthrough with assertions (HTTP only) | yes - makes HTTP calls only |
| `scripts/stress.py` | Load harness: bounded concurrent uploads, polls to terminal, exits non-zero on any failure. Needs the test image plus `scripts/` and `samples/` mounts | yes - creates documents (they expire) |
| `scripts/resilience.sh` | Destructive fault injection: SIGKILLs workers, restarts Redis, pauses a worker to test fencing | **no** - run it against a stack you can recreate |
| `scripts/make_samples.py` | Generates the deterministic PDF fixtures | yes - no state touched |
| `scripts/check_encoding.py` | CI quality gate: every text file must be valid UTF-8 | yes - read-only |
| `scripts/package.py` | Builds and verifies the delivery archive | yes - writes `dist/` |

Usage for the harnesses is in [RUNBOOK.md](RUNBOOK.md) (operating) and
[PERFORMANCE.md](PERFORMANCE.md) (method and measured results).

## Quality gates and CI

Five CI jobs in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml): **quality** (ruff lint,
ruff format check, mypy, credential-shaped-secret grep, UTF-8 encoding gate), **unit** (44 offline
tests), **integration** (16 API + 14 repository/lease + 8 proxy e2e against a fake-mode stack),
**frontend** (`npm ci`, `tsc`, `npm run build`, 29 vitest tests), and **terraform**
(`fmt -check` + `validate` for both environments). CI runs with `AI_MODE=fake` and no provider
credentials, so nothing in the pipeline pretends to exercise a live model. Dependency builds use
the committed `requirements*.lock.txt` files, generated from the verified images with `pip freeze`.

Local commands for every gate are in [RUNBOOK.md](RUNBOOK.md) section 12 and
[DEVELOPING.md](DEVELOPING.md). The repository root README has the same table under "Quality gates".

## How to read the claims in these documents

- A statement is traceable to one of: source code under `app/`, the Terraform tree under `infra/`,
  the test suites, the CI workflow, or a command executed in this environment on 2026-11-25.
- Test counts are the current suite: **82 backend checks run by default** (44 unit + 16 API + 14
  repository + 8 e2e), **4 opt-in** (3 concurrency + 1 live), and **29 frontend vitest tests**.
  Counts were verified by collection in this pass and are stated per suite in
  [TESTING.md](TESTING.md).
- Throughput, latency, memory and fault-tolerance numbers are quoted from
  [PERFORMANCE.md](PERFORMANCE.md), which states the method and reproduction commands; they are not
  re-derived in the derived documents.
- Commands are marked as **executed** or **documented from source, not executed** where the
  distinction matters (mainly in [RUNBOOK.md](RUNBOOK.md)). Nothing was re-run that would
  disturb the live demo stack.
- Where the code disagrees with a prose document, the disagreement is reported to the
  coordinator rather than silently smoothed over. Known open discrepancies are listed in
  [PROGRESS.md](PROGRESS.md#conflicts-and-fixes-made-during-integration).

## Repository layout relative to this directory

```text
README.md          repository entry point (quickstart, API summary, limits, performance, quality gates, production path)
docs/              this directory (authoritative + derived documents + measured report)
app/               FastAPI API and worker processes (shared code; concurrent worker; 3 parsers)
frontend/          React UI + nginx (proxies /api, per-IP rate limits) + vitest tests
infra/             Terraform: versions.tf, modules/ (s3, ec2-app), envs/ (local, prod)
tests/             unit (contract + hardening + openai) + integration (API + repository/lease +
                   opt-in concurrency/live) + e2e (proxy)
samples/           three deterministic PDF fixtures
scripts/           make_samples.py, smoke.sh, stress.py, resilience.sh, check_encoding.py, package.py
.github/workflows/ ci.yml: quality, unit, integration, frontend, terraform (5 jobs)
requirements*.txt  human-edited intent; requirements*.lock.txt exact pins from the verified images
compose.yaml       redis, localstack, provisioner (one-shot Terraform), api, worker, frontend, test profile
compose.prod.yaml  production host topology: redis + api + worker + frontend, no LocalStack/provisioner
```

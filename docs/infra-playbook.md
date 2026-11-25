> **Status: background research from an unrelated earlier project - not documentation for this
> application, but now relevant to its `infra/` layer.**
>
> This file was produced for a previous project (Spring Boot + Vue, Terraform + LocalStack + Docker). It
> is kept because the Terraform/local-infrastructure layer of this repository deliberately follows
> several of its lessons: the LocalStack lifecycle-configuration trap and the `enable_lifecycle`
> switch, a committed provider lock file, `fmt -check` + `validate` per environment, and one module
> shared by local and production. It does **not** describe this application, and nothing in it was
> re-verified against this codebase. The source of truth is `infra/` together with
> [ARCHITECTURE.md](ARCHITECTURE.md) section 1 and [RUNBOOK.md](RUNBOOK.md).
>
> Start with [README.md](README.md) instead. Read this only as optional background on why the
> infrastructure is shaped the way it is.

---

# Infrastructure playbook — Terraform + Docker + LocalStack

Distilled from a previous project (Spring Boot + Vue monolith, Terraform + LocalStack + Docker in
dev, one EC2 host in production). Every item below is either a pattern that worked there or a bug that
cost that project real time. That project's source is not part of this repository; the patterns it
teaches are applied here under `infra/` and `compose.yaml`, which is where to look when you want the
working version.

Scope: how to stand up a local stack, how to structure Terraform, how to shape a compose file, and
which failures are guaranteed if you skip a step.

---

## 0. The five rules that carry the most weight

1. **One code path for local and prod.** The app never knows whether it is talking to LocalStack or
   AWS — only an endpoint override changes. No `if (local)` branches in the application code.
2. **Re-provision LocalStack whenever its container is recreated.** It persists nothing, and Terraform
   *cannot detect the loss*. `terraform apply` will happily report "no changes" over a wiped instance.
3. **Pin third-party images by digest, cap memory, cap logs, and make health checks real.** A 1 GB host
   with an uncapped JVM and unbounded `json-file` logs goes down in a way that looks like an app bug.
4. **Compose resolves `${...}` from the shell or `.env` — never from `.env.prod`.** Wrap every compose
   command in a script that adds `--env-file`, and mark required variables with `:?`.
5. **Rehearse the production stack locally before the first real deploy**, and assert it with a script.
   That is where nginx body limits, CSP mistakes, collapsed cron lines and wrong JVM heap sizes are
   cheap to find.

---

## 1. Directory shape

```
infra/
├── versions.tf          required_version + provider pin, declared once
├── bootstrap/           state bucket + DynamoDB lock table — applied once, by hand
├── modules/             pure modules: s3, ses, secrets-manager, ec2, dns, alarms…
└── envs/
    ├── local/           same modules, LocalStack endpoints, no remote state
    └── prod/            same modules, real AWS, S3 backend + native state locking
```

Rules that kept that project sane:

- A module takes variables and returns outputs. It never reads an env-specific value, never hardcodes a
  region, never assumes a bucket name.
- An `envs/*` file is **only composition** plus the backend/provider block.
- The same module must be usable from both envs. That is exactly what makes local practice meaningful.
- **Delete dead modules.** An unused `ssm/` module stayed in the tree long after secrets moved to
  Secrets Manager; it still wastes a reader's attention and gets `validate`d in CI for nothing.

`infra/versions.tf`:

```hcl
terraform {
  required_version = ">= 1.7"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
```

Pin the provider in one place, the way `required_version` is pinned in one place. Per-env duplication
drifts.

---

## 2. The LocalStack provider block

This is the single most copied snippet of the whole project. Verbatim shape:

```hcl
# infra/envs/local/main.tf
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

# Points every AWS call to LocalStack
provider "aws" {
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  s3_use_path_style           = true

  endpoints {
    s3             = "http://localhost:4566"
    ses            = "http://localhost:4566"
    secretsmanager = "http://localhost:4566"
  }
}
```

Why each line exists:

| Setting | What breaks without it |
|---|---|
| `access_key` / `secret_key = "test"` | Provider fails before any call: no credentials to sign with. |
| `skip_credentials_validation` | Terraform calls STS to validate; LocalStack answers, but the round trip is pointless and slow. |
| `skip_metadata_api_check` | On EC2/containers the provider probes IMDS (169.254.169.254) and stalls. |
| `skip_requesting_account_id` | Provider resolves the account id through IAM; noise and latency. |
| `s3_use_path_style` | **Provider switches to virtual-hosted URLs (`bucket.localhost`), LocalStack never resolves them, and `aws_s3_bucket` creation hangs forever.** This is the classic one. |
| `endpoints` | Anything not listed goes to real AWS. List only the services you actually use. |

Local env composition stays tiny and readable — three modules, three inputs:

```hcl
module "s3" {
  source      = "../../modules/s3"
  bucket_name = "app-local"
  # LocalStack never confirms lifecycle rules; skipping it lets apply finish.
  enable_lifecycle = false
}

module "ses" {
  source     = "../../modules/ses"
  from_email = "noreply@app.local"
}

module "secrets_manager" {
  source      = "../../modules/secrets-manager"
  env         = "local"
  db_url      = "jdbc:postgresql://localhost:5432/app"
  db_username = "app"
  db_password = "app_dev"
  jwt_secret  = "<base64 32 bytes>"
}

output "local_secret_name" {
  value = module.secrets_manager.secret_name
}
```

Note `enable_lifecycle = false` — see §3.

---

## 3. LocalStack: seven behaviours that bite

| # | Behaviour | Consequence | Fix |
|---|---|---|---|
| 1 | **No persistence across restarts** (that's a Pro feature). A recreated container starts empty — even with a Docker volume for `/var/lib/localstack`. | Buckets, SES identity and secrets vanish. | Re-run the provisioning step after any recreation. Treat "infra up" as two steps, always: `docker compose up -d` **then** `terraform apply`. |
| 2 | **LocalStack reports any SES address as verified** when asked. | `terraform apply` sees no drift and does nothing, while every send fails with `MessageRejected`. Terraform is lying to you and it isn't Terraform's fault. | Register the identity **on every container start** via `localstack/init/ready.d/*.sh`, and re-run apply too. |
| 3 | `aws_s3_bucket_lifecycle_configuration` **never converges**. | Provider polls for ~3 minutes, then fails the whole apply — and because the apply aborted, unrelated resources (e.g. the SES sender) are left un-recreated. Drift with a confusing error. | `count = var.enable_lifecycle ? 1 : 0`, `false` locally. When you add the `count`, add a `moved` block so existing states aren't destroyed. |
| 4 | CloudWatch JSON protocol | A recent AWS CLI sends JSON-protocol calls that LocalStack 3 cannot parse. | Use the Query protocol via `curl --aws-sigv4 …` (that project's `heartbeat.sh` does) or test through `awslocal` from inside the container. |
| 5 | Only enabled services run. | `SERVICES=s3,ses,secretsmanager` means `dynamodb` calls fail confusingly. | Keep the `SERVICES` list and the `endpoints` block in sync; `EAGER_SERVICE_LOADING=1` avoids cold-start flakiness in tests. |
| 6 | It is a *stand-in*, not a simulator. IAM policy evaluation, SES real delivery, Let's Encrypt, DNS and "does it fit in a 1 GB box" are **not** provable locally. | A green local run is not a green production run. | Say so in writing (that project's rehearsal header lists exactly what is real and what is simulated). |
| 7 | It carries real utility: **sent e-mails are readable** at `http://localhost:4566/_aws/ses`. | — | Use it to test flows end to end: register, read the message body, extract the confirmation token, hit the endpoint. |

The SES re-seed, as actually shipped:

```bash
# localstack/init/ready.d/10-ses-sender.sh — runs on every container start
#!/bin/bash
# LocalStack keeps no state across restarts, and Terraform cannot tell the identity is gone.
# A missing sender makes every local email fail with MessageRejected.
# Must match the app's `ses.from-email` for the local profile.
awslocal ses verify-email-identity --email-address noreply@app.local
```

Mounted read-only:

```yaml
localstack:
  volumes:
    - localstack_data:/var/lib/localstack
    - ./localstack/init/ready.d:/etc/localstack/init/ready.d:ro
```

---

## 4. Docker: keep dev and prod as two different files

Never one compose file with profile gymnastics. Dev runs two containers and the app on the host; prod
runs everything in containers. Their requirements genuinely differ.

### Dev (`docker-compose.yml`) — two services, nothing else

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: app
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app_dev
    ports: ["5432:5432"]
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app"]
      interval: 5s
      timeout: 5s
      retries: 5

  localstack:
    image: localstack/localstack:3
    ports: ["4566:4566"]
    environment:
      SERVICES: s3,ses,secretsmanager
      DEFAULT_REGION: us-east-1
      EAGER_SERVICE_LOADING: 1
    volumes:
      - localstack_data:/var/lib/localstack
      - ./localstack/init/ready.d:/etc/localstack/init/ready.d:ro

volumes:
  pgdata:
  localstack_data:
```

The app itself runs from the host (`mvn spring-boot:run`, `npm run dev`) — hot reload, a debugger, no
image rebuild per change. `docker compose down -v` is the documented "wipe everything and re-seed" move.

### Prod (`docker-compose.prod.yml`) — settings that earn their place

| Setting | Why |
|---|---|
| **Digest pinning** — `image: postgres@sha256:…` with the tag in a comment | A rebuild can never silently pull an image nobody reviewed. Re-resolve with `docker manifest inspect <image>:<tag> --verbose \| head -3` and update digest **and** comment together. Your own app image is the only tag-based exception — the CD sets it to the deployed commit. |
| `mem_limit` + `JAVA_TOOL_OPTIONS: "-XX:MaxRAMPercentage=50 -XX:+ExitOnOutOfMemoryError"` | Without the limit the JVM sizes its heap from the **host** (25% by default). With it, it sizes from the cgroup; a leak ends in `OutOfMemoryError` and a container restart instead of the kernel picking a victim. The two numbers must be raised together. |
| `healthcheck.start_period: 120s` | Framework + migrations on a small instance takes over a minute cold. Without it a slow boot looks like a broken release and the deploy rolls back a healthy build. |
| Health check via `wget -qO-`, not `curl` | A JRE-alpine image ships busybox `wget` and no `curl`; a `curl` probe can never pass. |
| `depends_on: { condition: service_healthy }` | The app must not start against a database that hasn't finished initialising. |
| Log caps via one YAML anchor (`json-file`, `10m` × `3`) | The default driver is **unbounded**. On a small disk that also holds database data and images, a chatty service fills the disk and takes the database with it. |
| Fixed `name:` at the top | Container and volume names stop depending on the directory the stack happens to live in. |
| No `user:` on postgres/nginx/certbot | Their entrypoints deliberately start as root, fix volume ownership / bind 80+443, then drop privileges. Adding `user:` breaks exactly that step. |
| `client_max_body_size 10m` in nginx | The default is 1 MB: any photo upload gets a 413 *before the app sees it*. |
| `restart: unless-stopped` on everything | Survives a host reboot without a human. |

### Cron inside a container

One `echo` per job, appended, then `exec crond -f`:

```yaml
entrypoint: >
  sh -c "echo '0 2 * * * bash /backup.sh > /proc/1/fd/1 2>&1' > /etc/crontabs/root
  && echo '17 * * * * bash /guards.sh > /proc/1/fd/1 2>&1' >> /etc/crontabs/root
  && exec crond -f -d 8"
```

**Never `printf '...\n...'`.** Compose shell-splits this string itself and drops the backslash, so `\n`
arrives as a literal `n` and every job collapses into one broken line. That bug shipped, and the test
that missed it used a loose `grep -q backup.sh` which matched the broken line anyway. Assert with
`grep -qxF '<exact expected line>'` — whole-line, fixed-string.

---

## 5. Wrap compose: `scripts/compose.sh`

`env_file:` only feeds variables **into containers**. Compose resolves `${DOMAIN}` and friends in the
compose file from the shell or from a file literally named `.env` — never from `.env.prod`. A bare
`docker compose up` therefore silently resolves `${DOMAIN}` to a default and `${BACKUP_BUCKET}` to
nothing, and the stack half-works. The wrapper removes the failure mode:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Git Bash on Windows rewrites arguments that look like POSIX paths (/etc/..., /backup.sh) into
# Windows paths before they reach docker.exe, which breaks `exec … cat /etc/...`. No-op on Linux.
export MSYS_NO_PATHCONV=1

cd "$(dirname "${BASH_SOURCE[0]}")/.."

export PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-.env.prod}"
if [ ! -f "$PROJECT_ENV_FILE" ]; then
  echo "✗ $(pwd)/$PROJECT_ENV_FILE not found. Create it from .env.prod.example first." >&2
  exit 1
fi

files=(-f docker-compose.prod.yml)
if [ -n "${PROJECT_COMPOSE_OVERRIDE:-}" ]; then
  files+=(-f "$PROJECT_COMPOSE_OVERRIDE")
fi

exec docker compose --env-file "$PROJECT_ENV_FILE" "${files[@]}" "$@"
```

Two variables (`…_ENV_FILE`, `…_COMPOSE_OVERRIDE`) exist only so the rehearsal can reuse the exact same
compose file with a different env file and one small override. Keep that escape hatch; it is what makes
the rehearsal honest.

Then make the mistake impossible: mark every variable the stack needs as required in the compose file,
so an unwrapped `docker compose` fails loudly instead of half-working.

```yaml
environment:
  DOMAIN: ${DOMAIN:?DOMAIN is not set. Run compose through scripts/compose.sh, which reads .env.prod}
  S3_BUCKET: ${S3_BUCKET:?S3_BUCKET is not set. Run compose through scripts/compose.sh, which reads .env.prod}
```

---

## 6. Rehearse production locally before the first deploy

The highest-value habit of that project, and the one most worth copying. One script
(`scripts/prod-rehearsal.sh`, ~340 lines) that:

1. Asserts its own prerequisites and takes `--keep`, `--skip-build`, `--down`.
2. Builds a **fake host directory** (`.rehearsal/`) laid out exactly like the server's `/opt/app`,
   containing only the files the deploy actually ships (`tar` of compose file + nginx + scripts).
3. Generates throwaway secrets (`head -c 24 /dev/urandom | base64`) — no credential is in the script.
4. Runs in its own compose project: `COMPOSE_PROJECT_NAME=app-rehearsal`, so it can **never** touch a
   stack with the real name.
5. Stands up LocalStack and creates the same resources Terraform creates in prod (buckets, SES identity,
   app secret) — because the point is to exercise the real code paths, not a simplified stack.
6. Deploys the way CI/CD does, then issues a **self-signed** certificate so TLS is real.
7. Runs a flat checklist of ~40 assertions via `curl` and `docker inspect`, each printing ✓/✗, and exits
   non-zero if any failed.
8. Deploys a **deliberately broken release** and asserts it is rejected, the previous image is restored,
   and the site serves again.
9. Runs a backup, then a **restore drill** on that backup.
10. Always tears down (trap on `EXIT`), dumping the app's last 60 log lines if it failed.

The checklist categories are the transferable part:

- HTTP redirects to HTTPS; HSTS; CSP present
- health endpoint 200 **through nginx**
- SPA shell and a deep link both served
- data readable (proves secrets loading + migrations + seed)
- register, login, and the e-mail confirmation link **read back from LocalStack's SES inbox**
- a 3 MB upload passes nginx (catches the 1 MB default)
- no third-party font/CDN hosts in page or CSP
- management/actuator endpoints **not** reachable through the proxy
- cron file contains each job as its own exact line
- rollback works and the proxy follows the new container
- backup object lands in the bucket and restores
- metrics served on the private port only
- container memory caps applied, and JVM `MaxHeapSize` ≈ half the container limit
- log line format and "no token in logs"

Then wire it into CI for PRs that touch the deploy path, so it cannot rot.

---

## 7. Error → cause → fix

The table worth pasting into the next project's `CLAUDE.md` on day one.

| Symptom | Cause | Fix |
|---|---|---|
| `aws_s3_bucket` creation hangs forever | Provider used virtual-hosted S3 URLs | `s3_use_path_style = true` |
| `terraform apply` hangs ~3 min then fails on a lifecycle configuration | LocalStack never confirms the rule | `count = var.enable_lifecycle ? 1 : 0`, `false` locally |
| `terraform apply` reports "no changes" but buckets/identities are gone | LocalStack lost state; it answers "verified" for any SES address | Re-provision after every container recreation; re-seed via `init/ready.d` |
| Every local e-mail fails `MessageRejected` | SES sender not registered in this container's instance | `docker compose restart localstack` (the ready hook re-registers) or `awslocal ses verify-email-identity` |
| Mail flows work but the confirmation link 404s | You're reading a stale token | Read the *latest* message: `http://localhost:4566/_aws/ses` |
| `NoSuchBucket` on upload | LocalStack was recreated | Re-run provisioning |
| 413 on a photo upload | nginx `client_max_body_size` default is 1 MB | Set 10 MB explicitly |
| Deploy rolls back a healthy build | Health check `start_period` too short for a cold boot | `start_period: 120s` |
| Health check never passes although the app is up | Probe uses `curl` on a JRE-alpine image | Probe with busybox `wget` |
| Container restarted by the kernel / host OOM | Uncapped JVM sizing from the host, unbounded logs | `mem_limit` + `MaxRAMPercentage` + log caps |
| Cron jobs never run | `printf '\n'` collapsed all lines into one | One `echo` per line; assert whole lines with `grep -qxF` |
| Compose resolves variables to defaults/empty | `${...}` is not read from `.env.prod` | `compose.sh` wrapper with `--env-file` + `:?` |
| Terraform CI green over a parse error | `setup-terraform`'s wrapper swallows the exit code | `terraform_wrapper: false` |
| `fmt -check` passes but module calls are broken | Formatting ≠ type checking | Also `terraform -chdir=<env> init -backend=false && terraform validate` per env |
| Plan wants to replace the stateful instance | `ami`/`user_data` changed | `lifecycle { ignore_changes = [ami, user_data] }` — the data lives on the root volume |

---

## 8. Windows specifics (this machine)

| Symptom | Cause | Fix |
|---|---|---|
| `docker exec … cat /etc/...` fails | Git Bash rewrites `/etc/...` into a Windows path before `docker.exe` sees it | `export MSYS_NO_PATHCONV=1` in wrapper scripts |
| `Could not find or load main class MavenWrapperMain` | The opposite: with path conversion disabled, `java.exe` gets a POSIX classpath | `unset MSYS_NO_PATHCONV` around `mvnw`/`npm` calls |
| `pkill -f spring-boot:run` leaves the app running | It matches the Maven wrapper, not the child JVM holding the port | Find the PID by port: `Get-NetTCPConnection -LocalPort 8080 -State Listen`, then `Stop-Process` |
| "I fixed it but the old behaviour persists" | A days-old JVM is still bound to the port; the new one failed to bind silently | Check the PID's `StartTime`; kill by port; `clean compile` |
| `lsof`, `python3` missing | Not installed | Use `Get-NetTCPConnection`, `node -e`, or plain `curl` |
| Recursive directory listings hang | File-synced workspace + build caches | Exclude `.build-cache`, `node_modules`, `.git`, `.terraform`; list shallow first |

---

## 9. Where these patterns live in this repository

The source project this playbook was distilled from is not part of this repository. The working
versions of the patterns are here:

| What the playbook describes | Where it is implemented |
|---|---|
| Local provider + environment composition | [infra/envs/local/main.tf](../infra/envs/local/main.tf) |
| One module shared by local and production | [infra/modules/s3/main.tf](../infra/modules/s3/main.tf), [infra/modules/ec2-app/main.tf](../infra/modules/ec2-app/main.tf) |
| S3 module with the lifecycle `count` + `moved` | [infra/modules/s3/main.tf](../infra/modules/s3/main.tf) |
| Production environment and its tfvars example | [infra/envs/prod/main.tf](../infra/envs/prod/main.tf), [infra/envs/prod/terraform.tfvars.example](../infra/envs/prod/terraform.tfvars.example) |
| Committed provider lock files | `infra/envs/*/.terraform.lock.hcl` |
| Dev compose | [compose.yaml](../compose.yaml) |
| Prod compose (no LocalStack, no provisioner) | [compose.prod.yaml](../compose.prod.yaml) |
| Fault injection and production rehearsal | [scripts/resilience.sh](../scripts/resilience.sh), [scripts/stress.py](../scripts/stress.py) |
| Terraform CI job | [.github/workflows/ci.yml](../.github/workflows/ci.yml) |
| Architecture and module map | [docs/ARCHITECTURE.md](ARCHITECTURE.md) |
| Day-one runbook and the symptom → fix table | [docs/RUNBOOK.md](RUNBOOK.md) |

The Terraform lock files **are** committed here, which is the one deviation this playbook flags as
worth keeping: they pin provider versions across machines and CI.

---

## 10. Bootstrap checklist for a new project

Ordered, shortest path to a stack you can trust.

1. `infra/versions.tf` with `required_version` + provider pin.
2. `infra/modules/` — start with only the modules the app actually calls (typically `s3`, `secrets-manager`).
3. `infra/envs/local/main.tf` — provider with all six flags, `s3_use_path_style = true`, endpoint per service.
4. `docker-compose.yml` — database + LocalStack, health checks, named volumes.
5. `localstack/init/ready.d/` — re-seed whatever Terraform can't detect (at minimum the SES identity).
6. `scripts/compose.sh` wrapper; required-variable syntax in the prod compose file.
7. `.env.example` **and** `.env.prod.example`, both tracked; real files gitignored.
8. App config: a single `aws.endpoint` property; when set, use `endpointOverride` + static dev
   credentials. No branching anywhere else.
9. `infra/bootstrap/` — state bucket (`prevent_destroy`, versioning, SSE, public-access block) +
   DynamoDB lock table; then the S3 backend block in `infra/envs/prod`.
10. `.gitignore`: `.terraform/`, `*.tfstate*`, `.env*` (with `!*.example`), build outputs.
11. CI: `terraform fmt -check -recursive` + `validate` per env with `-backend=false` and
    `terraform_wrapper: false`.
12. `docker-compose.prod.yml` — pinned digests, `mem_limit`, log caps, real health checks, fixed `name:`.
13. Rehearsal script + a CI job that runs it on PRs touching the deploy path.
14. A `CLAUDE.md` at the repo root with: start commands in order, test accounts, the symptom → fix table
    from §7, and the "no hot reload / restart after changes" note if it applies.

---

## 11. Working with an agent on infrastructure

What that project learned across Claude and Codex sessions:

- **Keep a symptom → cause → fix table in the repo, not in the chat.** It was the most reused artifact
  of the whole project. Every pain that took more than ten minutes to diagnose went into it the same day.
- **Write the runbook as a skill/command, not as prose in a conversation.** "How to bring this stack up
  on this machine, including the three things that are broken here" is what a fresh session actually
  needs.
- **Gate anything that touches real AWS behind a human.** In that project, permission hooks blocked
  `terraform`/`aws` in autonomous mode and the human ran them explicitly. Keep that gate: plans and
  applies against real accounts are a human decision, not an agent decision.
- **One worktree per feature, one PR, no direct pushes.** Worktrees also collide less when two agents
  work in parallel.
- **A fresh session cannot tell a stale process from stale code.** Put the "kill by port, check the
  process start time" procedure in the runbook, in the imperative, with the exact commands.
- **Record what is *not* verified.** A header saying "simulated: AWS, certificate, host layout" prevents
  the next session from reading a green run as production-ready.

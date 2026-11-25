# Review of the ChatGPT implementation plan — and the decisions I made

Reviewed against `BWAI2.1 test task.docx` (the actual brief; kept in the private notes repo, not
shipped) and the 2-hour constraint.

## Verdict

The plan is a **good specification and a poor 2-hour plan**. Its contract work is excellent and I
adopted it almost verbatim. Its reliability chapter is a two-day distributed-systems design
(fenced leases, heartbeats, `XAUTOCLAIM` cursor sweeps, checkpoint journals, TTL alignment,
tri-state batch acceptance, 16 acceptance tests, 3 providers and a UI), and §14 of the plan
admits exactly that: *"the principal implementation risk is excessive reliability complexity
within 120 minutes"*. It then schedules that complexity into 120 minutes anyway.

So: **keep the contract, keep the failure taxonomy, implement the thin version of each
mechanism, and cut what cannot be verified in the budget.** Every cut below is a documented
limitation in the README, not a silent gap.

## What I kept, because it is load-bearing

| Kept | Why it matters |
|---|---|
| Frozen API contract + Pydantic models first | It is what makes parallel agents possible at all. Contract before code. |
| Redis as the *only* job state; Streams as the *only* queue | Mandated by the brief. No second source of truth. |
| Explicit `live` / `fake` AI modes, never a silent fallback | I have no Gemini key. Without this, every demo would be a lie. The mode is visible in `/api/v1/parsers`, `/health/ready`, worker logs and README. |
| Per-page Gemini calls with deterministic page attribution | A whole-document call cannot be trusted to report page boundaries. Reliability of the *output* beats call efficiency here. |
| `PermanentError` vs `TransientError` on every provider failure | Turns "bounded retry" from a wish into ~15 lines of worker code. |
| Idempotent LocalStack bucket init on every container start | Direct lesson from the `previous-project` project: LocalStack persists nothing and Terraform cannot detect it, so init must run at boot, not "once". |
| Bounded reads, PDF signature check, explicit limits | Cheap, and the difference between a demo and a liability. |
| `GET /api/v1/parsers` | Lets the UI avoid advertising a provider that cannot work. |
| Honest `503`/`422`/`413` semantics | Nothing is accepted that cannot be processed. |

## What I changed, and why

| # | Plan says | I do | Reason |
|---|---|---|---|
| 1 | Separate stage journal for checkpoint resume | The existence of `docs:pages:<id>` **is** the checkpoint | One key, same guarantee. A recovered job that finds pages skips extraction. ~0 extra code. |
| 2 | `unknown` outcome in the POST response | Definite Redis failure → `503` + best-effort object delete; the S3/Redis orphan window is documented | Tri-state acceptance is correct distributed-systems advice but adds API surface and client handling for a failure that a local demo cannot distinguish anyway. Documented instead of over-engineered. |
| 3 | `XAUTOCLAIM` with cursor state machine | One `XAUTOCLAIM` sweep per idle read iteration, `min-idle-time = LEASE_SECONDS` | Same recovery property, no cursor persistence. A live consumer never gets stolen because the lease TTL and the claim threshold are the same number. |
| 4 | Heartbeat + lease + fenced commit via Lua | **Kept, all three** — ~40 lines total | Cutting this would mean `XAUTOCLAIM` can double-process and two workers can overwrite each other. It is the one piece of the reliability chapter that pays for itself. |
| 5 | `XDEL` only after ACK in a single-group design | Kept | We have exactly one group. Documented as an assumption. |
| 6 | TTL applied to all terminal keys atomically | Kept — `EXPIRE` for job/pages/summary inside the same `MULTI` as the terminal write | Same cost as doing it sloppily. |
| 7 | UI **only after** backend gates pass | Built in parallel by a separate agent, integrated last | With agents, parallelism is free and the integration risk is mine to absorb. If the UI is broken at review time it simply does not ship; the API stands alone. |
| 8 | Lockfile from one dependency manager (uv/poetry) | `requirements.txt` with bounded ranges, frozen to exact versions after the first verified build, recorded in `docs/PROGRESS.md` | One less tool to install on a Windows host. The frozen list *is* the lockfile, and it is honest because it comes from the image we actually built. |
| 9 | `parsers/pypdf.py` | `app/parsers/pypdf_parser.py` | `pypdf.py` inside a package shadows the `pypdf` dependency and produces confusing import errors. |
| 10 | 16 acceptance tests | T01–T05, T07, T15 as the committed set; T09/T10 (crash recovery) if the budget holds; the rest reported as **not implemented** | A test that cannot be executed must not be listed as passing. |
| 11 | LocalStack as an optional extra | Kept, per your direction, behind a 4-method adapter (`put/get/delete/healthy`) | If LocalStack misbehaves at review time, swapping to a shared volume is a ~20-line change in one file. |

## Risks I am accepting, stated up front

1. **No Gemini credentials in this environment.** The live path is implemented to the current SDK
   shape but will be reported as *implemented, not live-verified*. The fake mode exists precisely
   so the vertical slice is still demonstrable.
2. **Model IDs are unverifiable offline.** `gemini-3.1-flash-lite` and `gpt-5-mini` come from the
   brief. Both are configurable, and a wrong/renamed model surfaces as a clear
   `PROVIDER_CONFIG` permanent error — never as a silent substitution to another model.
3. **`gpt-5-mini` may ship disabled.** The brief lists it below Gemini in priority and the plan
   allows omission. Advertising it as working without a verified SDK path would be the exact
   dishonesty this review is trying to avoid.
4. **Windows host.** Compose init scripts must be LF-only; a CRLF shebang fails silently on Linux.
   Verified byte-wise, not by intent.
5. **Two-hour budget.** That budget is for a human in an AI IDE. I will report the wall-clock time
   actually spent, not a claim that it fit in 120 minutes.

## What the plan got right that most submissions get wrong

- Separating **per-page content** from **one document-level summary**, and never conflating them.
- `PyPDF` returning text and never pretending it is markdown.
- Refusing to invent a progress percentage.
- Treating the provider list inconsistency across the brief's pages as a *documentation* problem
  rather than guessing.
- At-least-once semantics stated as at-least-once, including duplicate provider billing.
- "Never advertise an unavailable provider as working" — the single most useful line in the plan.

---

## Superseded decision (2026-11-25, later in the same session)

Row 12 above ("LocalStack as an optional extra... behind a 4-method adapter") and the
"idempotent LocalStack bucket init" note were both written when provisioning was an imperative
ready-hook. The user asked for full provisioning and an easy production path, so Terraform replaced
the hook: infra/modules/s3 + infra/envs/local (applied automatically by a one-shot provisioner
Compose service) and infra/envs/prod (S3 + EC2 + IAM + Elastic IP). The hook file was deleted on
purpose - two mechanisms for one resource is how drift starts - and the reasoning is recorded in
README.md and docs/PROGRESS.md. This section is left in place rather than edited so the decision
history stays honest.

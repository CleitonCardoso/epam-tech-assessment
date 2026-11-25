#!/usr/bin/env python3
"""Load test for the async PDF-processing API.

Fires N uploads through a bounded pool of C concurrent requests against
POST /api/v1/documents, then polls every accepted document until it is terminal, measuring:

  * upload latency      - per-request wall time of the multipart POST
  * end-to-end latency  - from the instant the upload was sent until `completed`/`failed`,
                          so server-side queueing and worker processing are both included
  * throughput          - accepted documents/second during the upload phase, and settled
                          documents/second over the whole run
  * peak in-flight      - how many uploads were genuinely in flight at the same time

Concurrency 1 vs 50 is the point of the harness: at 1 the upload phase is serial (peak
in-flight 1), at 50 it is parallel. The report also shows the settled rate over the whole
run, which is the signal for the *worker's* own parallelism (WORKER_CONCURRENCY), independent
of how fast the client could upload.

The script needs httpx (a dev dependency, see requirements-dev.txt), so it is intended to run
inside the test profile container:

    docker compose --profile test run --rm \
        -v "$(pwd)/samples:/app/samples:ro" \
        -v "$(pwd)/scripts:/app/scripts:ro" \
        test python /app/scripts/stress.py --concurrency 20 --docs 200 --label pypdf-c20

Exit code is 0 only when every upload returned 202, no 5xx was seen, every uploaded file was
accepted, every accepted document reached `completed`|`failed` within --poll-timeout, and no
document finished in the `failed` state. Otherwise it prints a one-line reason and exits 1
(argparse usage errors exit 2). Nothing is retried and no number is fabricated: transport
errors, non-202 statuses and timeouts are counted and make the run fail.

The machine-readable summary is printed as ONE line prefixed with "JSON " (cheap to grep) and,
with --json-out, written to a file. Response bodies quoted in the report are truncated to 300
characters. Every timing uses time.monotonic().

End-to-end latency is an upper bound on real processing time: the status is observed by
polling, and per-document polling backs off to at most one GET per second, so each sample can
include up to ~1s of detection lag (the same lag applies to every document, so comparisons
between runs and between --concurrency values remain meaningful).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = "samples/sample-text.pdf"
TERMINAL_STATUSES = frozenset({"completed", "failed"})

# Response bodies are never printed beyond this many characters (requirement: <= 300).
MAX_PREVIEW = 300
# Settle-phase polling backoff: start eager (documents finish fast) and cap the poll rate so a
# long poll timeout does not turn the harness itself into the load.
INITIAL_POLL_DELAY = 0.1
POLL_BACKOFF = 1.5
MAX_POLL_DELAY = 1.0
# How many individual failure details are echoed in the report/JSON (all are counted).
MAX_DETAILS = 10


# ---------------------------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _UploadOutcome:
    """One multipart POST and what the API said about it."""

    index: int
    sent_at: float  # monotonic, right before the request is issued (semaphore already held)
    finished_at: float
    duration: float
    status_code: int | None  # None means the request raised at the transport level
    document_ids: list[str] = field(default_factory=list)
    rejected_codes: list[str] = field(default_factory=list)
    detail: str = ""
    transport_error: str | None = None


@dataclass(slots=True)
class _SettleOutcome:
    """One accepted document followed to a terminal state (or to the poll timeout)."""

    document_id: str
    uploaded_at: float  # monotonic instant the upload was sent; end-to-end starts here
    status: str | None = None
    error_code: str | None = None
    end_to_end: float | None = None
    timed_out: bool = False
    polls: int = 0
    detail: str = ""


class _InFlight:
    """Counts uploads between request-send and response-received, and remembers the peak."""

    __slots__ = ("current", "peak")

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0

    def enter(self) -> None:
        self.current += 1
        if self.current > self.peak:
            self.peak = self.current

    def exit(self) -> None:
        self.current -= 1


# ---------------------------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------------------------


def _preview(value: str, limit: int = MAX_PREVIEW) -> str:
    """Collapse whitespace and truncate so no quoted body exceeds `limit` characters."""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _percentile(ordered: list[float], percentile_value: float) -> float:
    """Nearest-rank percentile over an already sorted, non-empty list."""
    rank = math.ceil(percentile_value / 100.0 * len(ordered))
    index = min(max(rank - 1, 0), len(ordered) - 1)
    return ordered[index]


def _latency_block(values_ms: list[float]) -> dict[str, float | None]:
    if not values_ms:
        return {"min": None, "p50": None, "p95": None, "p99": None, "max": None, "mean": None}
    ordered = sorted(values_ms)
    return {
        "min": round(ordered[0], 3),
        "p50": round(_percentile(ordered, 50.0), 3),
        "p95": round(_percentile(ordered, 95.0), 3),
        "p99": round(_percentile(ordered, 99.0), 3),
        "max": round(ordered[-1], 3),
        "mean": round(sum(ordered) / len(ordered), 3),
    }


def _format_latency(block: dict[str, float | None]) -> str:
    if block.get("p50") is None:
        return "n/a (no samples)"
    return (
        f"p50={block['p50']} p95={block['p95']} p99={block['p99']} max={block['max']}"
        f" (mean={block['mean']}, min={block['min']})"
    )


def _format_counter(counts: dict[str, int]) -> str:
    if not counts:
        return "(none)"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _rate(count: int, seconds: float) -> float:
    return round(count / seconds, 3) if seconds > 0 else 0.0


def _kv(label: str, value: object) -> None:
    print(f"  {label:<26}: {value}")


# ---------------------------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------------------------


def _parse_json(response: object) -> object | None:
    """response.json() with a guard: an error page is not JSON, and that must not crash a run."""
    try:
        return response.json()  # type: ignore[attr-defined]
    except (ValueError, json.JSONDecodeError):
        return None


def _rejected_codes(payload: object) -> list[str]:
    """Per-file rejection codes from an UploadResponse-shaped body."""
    if not isinstance(payload, dict):
        return []
    rejected = payload.get("rejected")
    if not isinstance(rejected, list):
        return []
    codes: list[str] = []
    for entry in rejected:
        if isinstance(entry, dict):
            codes.append(str(entry.get("code") or "UNKNOWN"))
    return codes


def _describe_failure(response: object, payload: object) -> str:
    """One-line, <=300-char description of a non-202 upload response."""
    status = getattr(response, "status_code", "?")
    if isinstance(payload, dict) and payload.get("detail") is not None:
        text = f"HTTP {status}: {payload['detail']}"
    elif isinstance(payload, dict):
        text = f"HTTP {status}: {json.dumps(payload, separators=(',', ':'))}"
    else:
        text = f"HTTP {status}: {getattr(response, 'text', '')}"
    codes = _rejected_codes(payload)
    if codes:
        text += f" [rejected codes: {', '.join(codes)}]"
    return _preview(text)


async def _upload_one(
    httpx_module: object,
    client: object,
    semaphore: asyncio.Semaphore,
    in_flight: _InFlight,
    index: int,
    file_name: str,
    file_bytes: bytes,
    parser: str,
) -> _UploadOutcome:
    """One upload, bounded by the shared semaphore. Only one file per request."""
    async with semaphore:
        in_flight.enter()
        sent_at = time.monotonic()
        try:
            response = await client.post(  # type: ignore[attr-defined]
                "/api/v1/documents",
                files=[("files", (file_name, file_bytes, "application/pdf"))],
                data={"parser": parser},
            )
        except httpx_module.HTTPError as exc:  # type: ignore[attr-defined]
            finished_at = time.monotonic()
            return _UploadOutcome(
                index=index,
                sent_at=sent_at,
                finished_at=finished_at,
                duration=finished_at - sent_at,
                status_code=None,
                detail=_preview(f"transport error: {type(exc).__name__}: {exc}"),
                transport_error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            in_flight.exit()

    finished_at = time.monotonic()
    payload = _parse_json(response)
    document_ids: list[str] = []
    if isinstance(payload, dict):
        documents = payload.get("documents")
        if isinstance(documents, list):
            document_ids = [
                str(entry["id"])
                for entry in documents
                if isinstance(entry, dict) and entry.get("id")
            ]
    rejected_codes = _rejected_codes(payload)
    detail = ""
    if response.status_code != 202:
        detail = _describe_failure(response, payload)
    elif rejected_codes:
        detail = _preview(f"HTTP 202 but rejected: {', '.join(rejected_codes)}")
    return _UploadOutcome(
        index=index,
        sent_at=sent_at,
        finished_at=finished_at,
        duration=finished_at - sent_at,
        status_code=response.status_code,
        document_ids=document_ids,
        rejected_codes=rejected_codes,
        detail=detail,
    )


async def _settle_one(
    httpx_module: object,
    client: object,
    poll_semaphore: asyncio.Semaphore,
    document_id: str,
    uploaded_at: float,
    deadline: float,
) -> _SettleOutcome:
    """Poll one document until terminal or `deadline` (monotonic). Bounded by the semaphore."""
    outcome = _SettleOutcome(document_id=document_id, uploaded_at=uploaded_at)
    delay = INITIAL_POLL_DELAY

    while True:
        if time.monotonic() >= deadline:
            outcome.timed_out = True
            outcome.detail = "no terminal status before the poll deadline"
            return outcome

        async with poll_semaphore:
            if time.monotonic() >= deadline:
                outcome.timed_out = True
                outcome.detail = "no terminal status before the poll deadline"
                return outcome
            try:
                response = await client.get(  # type: ignore[attr-defined]
                    f"/api/v1/documents/{document_id}"
                )
            except httpx_module.HTTPError as exc:  # type: ignore[attr-defined]
                outcome.detail = _preview(
                    f"poll transport error: {type(exc).__name__}: {exc}"
                )
            else:
                outcome.polls += 1
                if response.status_code == 404:
                    # Expired or unknown: it will never become terminal. Report, do not spin.
                    outcome.detail = _preview(f"poll HTTP 404: {response.text}")
                    return outcome
                if response.status_code != 200:
                    outcome.detail = _preview(
                        f"poll HTTP {response.status_code}: {response.text}"
                    )
                else:
                    body = _parse_json(response)
                    if not isinstance(body, dict):
                        outcome.detail = _preview(f"poll returned non-JSON: {response.text}")
                    else:
                        status = body.get("status")
                        if status in TERMINAL_STATUSES:
                            outcome.status = str(status)
                            error = body.get("error")
                            if isinstance(error, dict) and error.get("code"):
                                outcome.error_code = str(error["code"])
                            outcome.end_to_end = time.monotonic() - uploaded_at
                            return outcome

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            outcome.timed_out = True
            outcome.detail = "no terminal status before the poll deadline"
            return outcome
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * POLL_BACKOFF, MAX_POLL_DELAY)


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a number") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stress.py",
        description=(
            "Load test for the async PDF-processing API: bounded concurrent uploads, then "
            "poll every accepted document to a terminal state."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python /app/scripts/stress.py --concurrency 1  --docs 50  --label serial\n"
            "  python /app/scripts/stress.py --concurrency 50 --docs 200 --label burst\n"
            "  python /app/scripts/stress.py --parser gemini --docs 20 --json-out /app/run.json\n"
            "\n"
            "exit codes: 0 = clean run, 1 = failed run (reason on the RESULT line), "
            "2 = bad CLI usage."
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://api:8000",
        help="API root (default: http://api:8000, the Compose network name)",
    )
    parser.add_argument(
        "--concurrency",
        type=_positive_int,
        default=10,
        help="maximum simultaneous in-flight uploads (default: 10)",
    )
    parser.add_argument(
        "--docs",
        type=_positive_int,
        default=50,
        help="exact number of upload requests to send (default: 50)",
    )
    parser.add_argument(
        "--parser",
        choices=("pypdf", "gemini"),
        default="pypdf",
        help="parser sent with every upload; pypdf is real local CPU work (default: pypdf)",
    )
    parser.add_argument(
        "--file",
        default=DEFAULT_FILE,
        help=(
            "PDF to upload; relative paths resolve against the repository root, i.e. the "
            "parent of this script's directory (default: samples/sample-text.pdf)"
        ),
    )
    parser.add_argument(
        "--poll-timeout",
        type=_positive_float,
        default=120.0,
        help=(
            "seconds allowed for the settle phase as a whole, measured from the end of the "
            "upload phase (default: 120)"
        ),
    )
    parser.add_argument(
        "--request-timeout",
        type=_positive_float,
        default=60.0,
        help="per-request HTTP timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        metavar="PATH",
        help="also write the JSON summary to this path (the 'JSON ' stdout line is always printed)",
    )
    parser.add_argument(
        "--label",
        default="stress",
        help="name for this run, echoed on stdout and in the JSON (default: stress)",
    )
    return parser


def _resolve_file(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def _import_httpx() -> object:
    try:
        import httpx
    except ModuleNotFoundError:
        print(
            "FAIL: stress.py needs httpx; run it inside the test image "
            "(requirements-dev.txt) or: pip install httpx",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    return httpx


def _early_failure(args: argparse.Namespace, message: str) -> int:
    """Fail before any measurement, but still emit the grep-friendly JSON contract."""
    report: dict = {
        "label": args.label,
        "base_url": args.base_url.rstrip("/"),
        "concurrency": args.concurrency,
        "docs": args.docs,
        "parser": args.parser,
        "file": str(_resolve_file(args.file)),
        "started_at": _utc_now(),
        "problems": [message],
        "success": False,
        "exit_code": 1,
    }
    print(f"FAIL: {message}", file=sys.stderr)
    if args.json_out:
        out_path = Path(args.json_out)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            report["problems"] = [f"{message}; could not write --json-out {out_path}: {exc}"]
    print("JSON " + json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 1


# ---------------------------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------------------------


def _print_report(report: dict) -> None:
    upload = report["upload"]
    settle = report["settle"]
    throughput = report["throughput"]

    print(f"load test: {report['label']}")
    _kv("base_url", report["base_url"])
    _kv("concurrency", report["concurrency"])
    _kv("docs", report["docs"])
    _kv("parser", report["parser"])
    _kv("file", f"{report['file']} ({report['file_bytes']} bytes)")
    _kv("request_timeout_s", report["request_timeout_seconds"])
    _kv("poll_timeout_s", report["poll_timeout_seconds"])

    print()
    print("upload phase")
    _kv("requests", upload["requests"])
    _kv("accepted", upload["accepted"])
    _kv("rejected files", upload["rejected_files"])
    _kv("http errors", upload["http_errors"])
    _kv("5xx responses", upload["responses_5xx"])
    _kv("transport errors", upload["transport_errors"])
    _kv("peak in-flight", upload["peak_in_flight"])
    _kv("upload latency ms", _format_latency(upload["latency_ms"]))
    _kv("phase duration s", upload["phase_seconds"])
    _kv("accepted per second", throughput["accepted_per_second_upload_phase"])

    print()
    print("settle phase")
    _kv("settled", f"{settle['settled']}/{settle['expected']}")
    _kv("timed out", settle["timed_out"])
    _kv("end-to-end latency ms", _format_latency(settle["end_to_end_ms"]))
    _kv("phase duration s", settle["phase_seconds"])
    _kv(
        "settled per second",
        f"{throughput['settled_per_second_whole_run']} "
        f"(whole run {throughput['total_run_seconds']}s)",
    )

    print()
    _kv("terminal statuses", _format_counter(report["terminal_statuses"]))
    _kv("error codes", _format_counter(report["error_codes"]))
    _kv("rejected codes", _format_counter(report["rejected_codes"]))
    _kv("http errors by status", _format_counter(report["http_errors_by_status"]))
    _kv("started at (UTC)", report["started_at"])

    if report["upload_error_details"]:
        print()
        print("upload error details (first 10)")
        for item in report["upload_error_details"]:
            print(f"  [#{item['index']}] {item['detail']}")

    if report["unsettled_details"]:
        print()
        print("unsettled document details (first 10)")
        for item in report["unsettled_details"]:
            print(f"  [id={item['id']}] {item['detail'] or 'no terminal status before deadline'}")

    print()
    if report["success"]:
        print("RESULT: PASS")
    else:
        print(f"RESULT: FAIL - {'; '.join(report['problems'])}")


# ---------------------------------------------------------------------------------------------
# Main async flow
# ---------------------------------------------------------------------------------------------


async def _run_async(args: argparse.Namespace, httpx: object) -> int:
    file_path = _resolve_file(args.file)
    if not file_path.is_file():
        return _early_failure(args, f"PDF not found: {file_path}")
    file_bytes = file_path.read_bytes()
    if not file_bytes:
        return _early_failure(args, f"PDF is empty: {file_path}")

    base_url = args.base_url.rstrip("/")
    timeout = httpx.Timeout(  # type: ignore[attr-defined]
        args.request_timeout, connect=min(10.0, args.request_timeout)
    )
    limits = httpx.Limits(  # type: ignore[attr-defined]
        max_connections=max(10, args.concurrency * 2),
        max_keepalive_connections=max(10, args.concurrency * 2),
        keepalive_expiry=30.0,
    )
    started_at = _utc_now()

    # One shared client for both phases: uploads and status polls reuse the same connections.
    async with httpx.AsyncClient(  # type: ignore[attr-defined]
        base_url=base_url,
        timeout=timeout,
        limits=limits,
        headers={"User-Agent": "stress.py"},
    ) as client:
        # Single cheap preflight so a down stack fails with one clear line, not N tracebacks.
        try:
            live = await client.get("/health/live")
        except httpx.HTTPError as exc:  # type: ignore[attr-defined]
            return _early_failure(
                args, f"cannot reach {base_url}/health/live: {type(exc).__name__}: {exc}"
            )
        if live.status_code != 200:
            return _early_failure(
                args,
                f"{base_url}/health/live returned HTTP {live.status_code}: "
                f"{_preview(live.text)}",
            )

        run_start = time.monotonic()

        # -- upload phase: exactly --docs requests, at most --concurrency in flight -----------
        upload_start = time.monotonic()
        semaphore = asyncio.Semaphore(args.concurrency)
        in_flight = _InFlight()
        uploads: list[_UploadOutcome] = list(
            await asyncio.gather(
                *(
                    _upload_one(
                        httpx,
                        client,
                        semaphore,
                        in_flight,
                        index,
                        file_path.name,
                        file_bytes,
                        args.parser,
                    )
                    for index in range(args.docs)
                )
            )
        )
        upload_end = time.monotonic()

        # -- settle phase: poll every accepted id, bounded, until terminal or timeout --------
        accepted_refs: list[tuple[float, str]] = [
            (outcome.sent_at, document_id)
            for outcome in uploads
            for document_id in outcome.document_ids
        ]
        settle_start = time.monotonic()
        settle_deadline = settle_start + args.poll_timeout
        # Polling is bounded separately from uploads: enough parallelism to observe completion
        # promptly, capped so a 200-document run is not 200 concurrent Redis reads.
        poll_parallelism = max(4, min(args.concurrency, 64))
        poll_semaphore = asyncio.Semaphore(poll_parallelism)
        settlements: list[_SettleOutcome] = []
        if accepted_refs:
            settlements = list(
                await asyncio.gather(
                    *(
                        _settle_one(
                            httpx,
                            client,
                            poll_semaphore,
                            document_id,
                            uploaded_at,
                            settle_deadline,
                        )
                        for uploaded_at, document_id in accepted_refs
                    )
                )
            )
        settle_end = time.monotonic()

    # -- derive the numbers from what actually happened --------------------------------------
    upload_phase_seconds = max(upload_end - upload_start, 1e-9)
    settle_phase_seconds = max(settle_end - settle_start, 1e-9) if accepted_refs else 0.0
    total_run_seconds = max(settle_end - run_start, 1e-9)

    upload_latency_ms = [outcome.duration * 1000.0 for outcome in uploads]
    accepted = sum(len(outcome.document_ids) for outcome in uploads)
    rejected_values = [code for outcome in uploads for code in outcome.rejected_codes]
    rejected_histogram: dict[str, int] = {}
    for code in rejected_values:
        rejected_histogram[code] = rejected_histogram.get(code, 0) + 1

    upload_errors = [outcome for outcome in uploads if outcome.status_code != 202]
    five_xx = [
        outcome
        for outcome in uploads
        if outcome.status_code is not None and 500 <= outcome.status_code < 600
    ]
    transport_errors = [outcome for outcome in uploads if outcome.transport_error is not None]
    http_errors_by_status: dict[str, int] = {}
    for outcome in upload_errors:
        key = "transport" if outcome.status_code is None else str(outcome.status_code)
        http_errors_by_status[key] = http_errors_by_status.get(key, 0) + 1

    settled = [outcome for outcome in settlements if outcome.status is not None]
    unsettled = [outcome for outcome in settlements if outcome.status is None]
    timed_out = [outcome for outcome in settlements if outcome.timed_out]
    end_to_end_ms = [
        outcome.end_to_end * 1000.0
        for outcome in settled
        if outcome.end_to_end is not None
    ]
    terminal_counts: dict[str, int] = {"completed": 0, "failed": 0}
    for outcome in settled:
        terminal_counts[str(outcome.status)] = terminal_counts.get(str(outcome.status), 0) + 1
    error_codes: dict[str, int] = {}
    for outcome in settled:
        if outcome.error_code:
            error_codes[outcome.error_code] = error_codes.get(outcome.error_code, 0) + 1

    problems: list[str] = []
    if upload_errors:
        problems.append(
            f"{len(upload_errors)}/{args.docs} upload request(s) did not return 202 "
            f"(by status: {_format_counter(http_errors_by_status)})"
        )
    if accepted != len(uploads):
        problems.append(f"{len(uploads) - accepted} of {len(uploads)} uploaded file(s) were not accepted")
    if rejected_histogram:
        problems.append(
            f"{len(rejected_values)} file(s) rejected by the API "
            f"(codes: {_format_counter(rejected_histogram)})"
        )
    if unsettled:
        if len(timed_out) == len(unsettled):
            problems.append(
                f"{len(unsettled)}/{len(accepted_refs)} document(s) did not reach a terminal "
                f"state within {args.poll_timeout:g}s"
            )
        else:
            problems.append(
                f"{len(unsettled)}/{len(accepted_refs)} document(s) did not reach a terminal "
                f"state ({len(timed_out)} timed out after {args.poll_timeout:g}s; "
                "see unsettled_details)"
            )
    if terminal_counts.get("failed", 0):
        problems.append(
            f"{terminal_counts['failed']} document(s) finished in the failed state "
            f"(codes: {_format_counter(error_codes)})"
        )

    report = {
        "label": args.label,
        "base_url": base_url,
        "concurrency": args.concurrency,
        "docs": args.docs,
        "parser": args.parser,
        "file": str(file_path),
        "file_bytes": len(file_bytes),
        "started_at": started_at,
        "request_timeout_seconds": args.request_timeout,
        "poll_timeout_seconds": args.poll_timeout,
        "upload": {
            "requests": len(uploads),
            "accepted": accepted,
            "rejected_files": len(rejected_values),
            "http_errors": len(upload_errors),
            "responses_5xx": len(five_xx),
            "transport_errors": len(transport_errors),
            "peak_in_flight": in_flight.peak,
            "latency_ms": _latency_block(upload_latency_ms),
            "phase_seconds": round(upload_phase_seconds, 3),
        },
        "settle": {
            "expected": len(accepted_refs),
            "settled": len(settled),
            "unsettled": len(unsettled),
            "timed_out": len(timed_out),
            "poll_timeout_seconds": args.poll_timeout,
            "poll_interval_cap_seconds": MAX_POLL_DELAY,
            "end_to_end_ms": _latency_block(end_to_end_ms),
            "phase_seconds": round(settle_phase_seconds, 3),
        },
        "throughput": {
            "accepted_per_second_upload_phase": _rate(accepted, upload_phase_seconds),
            "settled_per_second_whole_run": _rate(len(settled), total_run_seconds),
            "total_run_seconds": round(total_run_seconds, 3),
        },
        "terminal_statuses": terminal_counts,
        "error_codes": error_codes,
        "rejected_codes": rejected_histogram,
        "http_errors_by_status": http_errors_by_status,
        "upload_error_details": [
            {"index": outcome.index, "detail": outcome.detail or outcome.transport_error or ""}
            for outcome in upload_errors[:MAX_DETAILS]
        ],
        "unsettled_details": [
            {"id": outcome.document_id, "detail": outcome.detail} for outcome in unsettled[:MAX_DETAILS]
        ],
        "problems": problems,
        "success": not problems,
        "exit_code": 0 if not problems else 1,
    }

    # Writing the file is part of the contract when asked for: a failure here fails the run.
    if args.json_out:
        out_path = Path(args.json_out)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as exc:
            report["problems"].append(f"could not write --json-out {out_path}: {exc}")
            report["success"] = False
            report["exit_code"] = 1

    _print_report(report)
    # One machine-readable line, last on stdout, easy to grep with `grep '^JSON '`.
    print("JSON " + json.dumps(report, sort_keys=True, separators=(",", ":")))
    return int(report["exit_code"])


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)  # --help exits here, before httpx is needed
    httpx = _import_httpx()
    try:
        return asyncio.run(_run_async(args, httpx))
    except KeyboardInterrupt:
        print("FAIL: interrupted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

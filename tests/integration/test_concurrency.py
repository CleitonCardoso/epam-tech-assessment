"""Concurrency acceptance tests against a live stack.

The worker consumes the Redis stream with a bounded number of slots (WORKER_CONCURRENCY). These
tests assert the externally visible consequences: a burst of simultaneous uploads is fully
accepted, the API stays responsive while the burst is in flight, and every accepted document
settles in a finite time.

Marked `integration`; they need the running Compose stack:

    docker compose up -d
    docker compose --profile test run --rm \
        -e API_BASE_URL=http://api:8000 \
        -v "$(pwd)/samples:/app/samples:ro" \
        test python -m pytest tests/integration -m integration -q

No live-AI cost: parser=pypdf only. The summary is the configured summarizer (the labelled
fake stand-in in AI_MODE=fake), never a paid provider call.

Everything here is outcome-based: ids come from the upload responses (never assumed to be in
order), deadlines are finite, and the only sleeps are short polling intervals.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest

from tests.conftest import API_BASE_URL, sample

RUN_CONCURRENCY_TESTS = os.getenv("RUN_CONCURRENCY_TESTS") == "1"

# Opt-in on purpose. These tests upload BURST_SIZE documents at once and then wait for all of them,
# which against a live provider means dozens of real model calls: they are slow, they cost quota and
# they can legitimately fail on the provider's rate limit rather than on a defect. Run them against
# a disposable fake-mode worker (see docs/PERFORMANCE.md) with RUN_CONCURRENCY_TESTS=1.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not RUN_CONCURRENCY_TESTS,
        reason="concurrency load tests are opt-in: set RUN_CONCURRENCY_TESTS=1 and use a fake-mode worker",
    ),
]

# 20 simultaneous uploads as the brief asks; overridable for a weaker machine.
BURST_SIZE = int(os.getenv("TEST_BURST_SIZE", "20"))
# Other concurrent single-file uploads fired alongside the mixed batch.
MIXED_LOAD_SIZE = int(os.getenv("TEST_MIXED_LOAD_SIZE", "8"))
# Deadline for every accepted document of one test to reach a terminal state. Deliberately
# generous: with a single worker slot this is BURST_SIZE x (parse + summary) of real work.
BURST_DEADLINE_SECONDS = float(os.getenv("TEST_BURST_TIMEOUT", "180"))
TEST_REQUEST_TIMEOUT = float(os.getenv("TEST_REQUEST_TIMEOUT", "60"))

HEALTH_MAX_SECONDS = 1.0
HEALTH_PROBE_INTERVAL = 0.005
TERMINAL = frozenset({"completed", "failed"})


def _pdf(name: str) -> tuple[str, bytes]:
    """Read a sample PDF once; skip the test if the fixture is missing."""
    path = sample(name)
    data = path.read_bytes()
    assert data, f"{path} is empty (run: python scripts/make_samples.py)"
    return path.name, data


def _client() -> httpx.AsyncClient:
    """One client shared by uploads, polls and health probes inside a single scenario."""
    connections = max(10, BURST_SIZE * 2)
    return httpx.AsyncClient(
        base_url=API_BASE_URL,
        timeout=TEST_REQUEST_TIMEOUT,
        limits=httpx.Limits(
            max_connections=connections,
            max_keepalive_connections=connections,
        ),
    )


async def _upload(
    client: httpx.AsyncClient,
    filename: str,
    data: bytes,
    extra: tuple[str, bytes] | None = None,
) -> httpx.Response:
    """POST one file (plus an optional second file) with parser=pypdf."""
    handles: list[tuple[str, tuple[str, bytes, str]]] = [
        ("files", (filename, data, "application/pdf"))
    ]
    if extra is not None:
        handles.append(("files", (extra[0], extra[1], "application/pdf")))
    return await client.post("/api/v1/documents", files=handles, data={"parser": "pypdf"})


async def _upload_many(
    client: httpx.AsyncClient, filename: str, data: bytes, count: int
) -> list[httpx.Response]:
    """Fire `count` uploads simultaneously; a raised request fails the test with context."""
    results = await asyncio.gather(
        *(_upload(client, filename, data) for _ in range(count)),
        return_exceptions=True,
    )
    errors = [result for result in results if isinstance(result, BaseException)]
    assert not errors, (
        f"{len(errors)}/{count} simultaneous uploads raised instead of returning a response: "
        f"{errors[:3]}"
    )
    return [result for result in results if isinstance(result, httpx.Response)]


def _accepted_documents(
    responses: list[httpx.Response], expected: int
) -> list[dict]:
    """Assert every response is 202 and flatten the accepted documents out of the payloads."""
    bad = [
        (response.status_code, response.text[:300])
        for response in responses
        if response.status_code != 202
    ]
    assert not bad, f"expected every upload to return HTTP 202, got: {bad}"
    payloads = [response.json() for response in responses]
    documents = [document for payload in payloads for document in payload["documents"]]
    assert len(documents) == expected, (
        f"expected {expected} accepted document(s) from {len(payloads)} response(s), "
        f"got {len(documents)}"
    )
    return documents


async def _wait_terminal(
    client: httpx.AsyncClient, document_id: str, deadline: float
) -> dict | None:
    """Poll one document until completed|failed; return None if the deadline passes first."""
    delay = 0.05
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        response = await client.get(f"/api/v1/documents/{document_id}")
        assert response.status_code == 200, (
            f"GET /api/v1/documents/{document_id} returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
        body = response.json()
        if body.get("status") in TERMINAL:
            return body
        await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * 1.5, 1.0)


async def _settle_all(
    client: httpx.AsyncClient, document_ids: list[str], deadline: float
) -> list[dict]:
    """Wait for every id concurrently; return the terminal bodies or fail with the missing ids."""
    finals = await asyncio.gather(
        *(_wait_terminal(client, document_id, deadline) for document_id in document_ids)
    )
    missing = [
        document_id
        for document_id, body in zip(document_ids, finals)
        if body is None
    ]
    assert not missing, (
        f"{len(missing)}/{len(document_ids)} document(s) did not reach a terminal state "
        f"within {BURST_DEADLINE_SECONDS:g}s: {missing[:5]}"
    )
    return [body for body in finals if body is not None]


def test_all_concurrent_uploads_are_accepted_and_complete() -> None:
    """A burst of BURST_SIZE uploads: all accepted, all ids unique, all complete, pages>=2."""

    async def scenario() -> None:
        filename, data = _pdf("sample-text.pdf")
        deadline = time.monotonic() + BURST_DEADLINE_SECONDS
        async with _client() as client:
            responses = await _upload_many(client, filename, data, BURST_SIZE)
            documents = _accepted_documents(responses, BURST_SIZE)
            document_ids = [document["id"] for document in documents]
            unique_ids = set(document_ids)
            assert len(unique_ids) == BURST_SIZE, (
                f"every accepted file must get its own id: {len(unique_ids)} unique ids "
                f"for {BURST_SIZE} documents"
            )
            finals = await _settle_all(client, document_ids, deadline)

        not_completed = [
            (body["id"], body.get("status"), (body.get("error") or {}).get("code"))
            for body in finals
            if body.get("status") != "completed"
        ]
        assert not not_completed, (
            f"expected every document to complete, got: {not_completed[:5]}"
        )
        short = [
            (body["id"], body.get("page_count"))
            for body in finals
            if (body.get("page_count") or 0) < 2
        ]
        assert not short, (
            f"expected every document to report page_count >= 2, got: {short[:5]}"
        )

    asyncio.run(scenario())


def test_api_stays_responsive_during_a_burst() -> None:
    """/health/live answers 200 and under 1s for every probe while the upload burst is in flight."""

    async def scenario() -> None:
        filename, data = _pdf("sample-text.pdf")
        deadline = time.monotonic() + BURST_DEADLINE_SECONDS
        async with _client() as client:
            burst = asyncio.create_task(_upload_many(client, filename, data, BURST_SIZE))
            probes: list[tuple[float, int]] = []
            # The task was created but has not run yet, so the first probe is guaranteed to
            # start while the burst is pending; further probes interleave with the uploads.
            while not burst.done():
                assert time.monotonic() < deadline, (
                    f"the upload burst did not finish within {BURST_DEADLINE_SECONDS:g}s"
                )
                started = time.monotonic()
                response = await client.get("/health/live")
                elapsed = time.monotonic() - started
                assert response.status_code == 200, (
                    f"/health/live returned HTTP {response.status_code} during the burst: "
                    f"{response.text[:300]}"
                )
                probes.append((elapsed, response.status_code))
                await asyncio.sleep(HEALTH_PROBE_INTERVAL)
            responses = await burst

        assert probes, "no /health/live probe overlapped the upload burst"
        slow = [round(elapsed, 3) for elapsed, _ in probes if elapsed >= HEALTH_MAX_SECONDS]
        assert not slow, (
            f"/health/live took >= {HEALTH_MAX_SECONDS:g}s under concurrent upload load: {slow}"
        )
        _accepted_documents(responses, BURST_SIZE)

    asyncio.run(scenario())


def test_mixed_batch_under_load() -> None:
    """A valid+corrupt batch returns 202 with a NOT_A_PDF rejection while other uploads succeed."""

    async def scenario() -> None:
        valid_name, valid = _pdf("sample-text.pdf")
        corrupt_name, corrupt = _pdf("sample-corrupt.pdf")
        deadline = time.monotonic() + BURST_DEADLINE_SECONDS
        async with _client() as client:
            uploads = [
                _upload(client, valid_name, valid) for _ in range(MIXED_LOAD_SIZE)
            ]
            # The mixed batch is just one more simultaneous request.
            uploads.append(_upload(client, valid_name, valid, extra=(corrupt_name, corrupt)))
            responses = await asyncio.gather(*uploads)

            documents = _accepted_documents(responses, MIXED_LOAD_SIZE + 1)
            payloads = [response.json() for response in responses]
            # Identify the batch by its per-file outcome, never by request ordering.
            batches = [payload for payload in payloads if payload.get("rejected")]
            assert len(batches) == 1, (
                f"expected exactly one response carrying a rejected[] entry, got {len(batches)}"
            )
            rejected = batches[0]["rejected"]
            codes = [entry.get("code") for entry in rejected]
            assert codes == ["NOT_A_PDF"], (
                f"expected the corrupt file to be rejected with NOT_A_PDF, got: {rejected}"
            )
            assert rejected[0].get("filename") == corrupt_name

            document_ids = [document["id"] for document in documents]
            assert len(set(document_ids)) == MIXED_LOAD_SIZE + 1, (
                "every accepted file must get its own id across concurrent requests"
            )
            finals = await _settle_all(client, document_ids, deadline)

        not_completed = [
            (body["id"], body.get("status"), (body.get("error") or {}).get("code"))
            for body in finals
            if body.get("status") != "completed"
        ]
        assert not not_completed, (
            f"a corrupt file in one batch must not affect other concurrent uploads; "
            f"not completed: {not_completed[:5]}"
        )
        short = [
            (body["id"], body.get("page_count"))
            for body in finals
            if (body.get("page_count") or 0) < 2
        ]
        assert not short, f"expected page_count >= 2 for the valid sample, got: {short[:5]}"

    asyncio.run(scenario())

"""The Redis repository and its lease fencing, against a real Redis.

This is the heart of the system's correctness: "only the current lease holder may write, ACK or
delete". It used to be exercised only by `scripts/resilience.sh` (process-level, shell) and by hand;
this file makes it a regression test.

Runs in the Compose network (the `test` service can reach `redis`):

    docker compose up -d redis
    docker compose --profile test run --rm -e REDIS_URL=redis://redis:6379/0 \
        test python -m pytest tests/integration/test_repository.py -m repository -q

Unique document ids and a per-run stream key keep runs isolated: nothing is flushed and no other
data is touched.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from app.config import Settings
from app.models import (
    DocumentJob,
    DocumentStatus,
    ErrorInfo,
    PageFormat,
    PageResult,
    ParserId,
)
from app.queue import JobQueue
from app.storage.jobs import JobRepository

pytestmark = [pytest.mark.integration, pytest.mark.repository]

PAGES = [PageResult(page_number=1, format=PageFormat.TEXT, content="hello")]


@pytest.fixture()
def settings() -> Settings:
    # A unique stream per test: assertions about stream length and pending entries stay isolated.
    return Settings(
        _env_file=None,
        stream_key=f"test:docs:jobs:{uuid.uuid4().hex[:8]}",
        consumer_group=f"test-workers-{uuid.uuid4().hex[:8]}",
        # A short lease keeps reclaim tests fast; the heartbeat must stay below it (validated).
        lease_seconds=5,
        reclaim_min_idle_ms=50,
        heartbeat_seconds=1,
        job_deadline_seconds=600,
        result_ttl_seconds=60,
    )


@pytest.fixture()
async def redis(settings: Settings) -> AsyncIterator[Redis]:
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.delete(settings.stream_key)
        await client.aclose()


@pytest.fixture()
def repo(redis: Redis, settings: Settings) -> JobRepository:
    return JobRepository(redis, settings)


@pytest.fixture()
def queue(redis: Redis, settings: Settings) -> JobQueue:
    return JobQueue(redis, settings)


def make_job() -> DocumentJob:
    document_id = str(uuid.uuid4())
    return DocumentJob(
        id=document_id,
        filename="a.pdf",
        parser=ParserId.PYPDF,
        source_key=f"uploads/{document_id}/source.pdf",
        deadline_at=time.time() + 600,
    )


async def claim_message(queue: JobQueue, consumer: str = "test-consumer") -> tuple[str, str]:
    """Read the entry the way a worker would, so it lands in the consumer's pending list."""
    await queue.ensure_group()
    messages = await queue.read_new(consumer, block_ms=50)
    assert messages, "the job was not enqueued"
    return messages[0]


class TestEnqueueAndRead:
    async def test_acceptance_writes_the_job_and_the_stream_entry_together(
        self, repo: JobRepository, redis: Redis, settings: Settings
    ) -> None:
        job = make_job()
        await repo.create_and_enqueue(job)

        stored = await repo.get(job.id)
        assert stored is not None
        assert stored.status is DocumentStatus.QUEUED
        assert stored.filename == "a.pdf"
        assert await redis.xlen(settings.stream_key) == 1

    async def test_unknown_document_is_none_not_an_error(self, repo: JobRepository) -> None:
        assert await repo.get(str(uuid.uuid4())) is None

    async def test_pages_are_absent_until_checkpointed(self, repo: JobRepository) -> None:
        job = make_job()
        await repo.create_and_enqueue(job)
        assert await repo.get_pages(job.id) is None


class TestLease:
    async def test_only_one_holder_can_acquire(self, repo: JobRepository) -> None:
        job = make_job()
        await repo.create_and_enqueue(job)

        assert await repo.acquire_lease(job.id, "token-a") is True
        assert await repo.acquire_lease(job.id, "token-b") is False

    async def test_renewal_requires_the_current_token(self, repo: JobRepository) -> None:
        job = make_job()
        await repo.create_and_enqueue(job)
        await repo.acquire_lease(job.id, "token-a")

        assert await repo.renew_lease(job.id, "token-a") is True
        assert await repo.renew_lease(job.id, "token-b") is False

    async def test_release_by_a_non_owner_does_not_steal_the_lease(
        self, repo: JobRepository
    ) -> None:
        job = make_job()
        await repo.create_and_enqueue(job)
        await repo.acquire_lease(job.id, "token-a")

        await repo.release_lease(job.id, "token-b")
        # A stale worker cannot release someone else's lease, so the owner still holds it.
        assert await repo.renew_lease(job.id, "token-a") is True


class TestFencedWrites:
    async def test_a_stale_token_cannot_change_the_stage(self, repo: JobRepository) -> None:
        job = make_job()
        await repo.create_and_enqueue(job)
        await repo.acquire_lease(job.id, "token-a")

        rejected = await repo.set_stage(
            job.id, token="stale", status=DocumentStatus.PARSING, stage="parsing"
        )
        assert rejected is False
        assert (await repo.get(job.id)).status is DocumentStatus.QUEUED

    async def test_a_stale_token_cannot_write_pages(self, repo: JobRepository) -> None:
        job = make_job()
        await repo.create_and_enqueue(job)
        await repo.acquire_lease(job.id, "token-a")

        rejected = await repo.save_pages(job.id, token="stale", pages=PAGES, warnings=[])
        assert rejected is False
        assert await repo.get_pages(job.id) is None

    async def test_the_owner_checkpoints_pages_and_resumes_from_them(
        self, repo: JobRepository
    ) -> None:
        job = make_job()
        await repo.create_and_enqueue(job)
        await repo.acquire_lease(job.id, "token-a")

        assert await repo.save_pages(job.id, token="token-a", pages=PAGES, warnings=["w"]) is True
        pages, warnings = await repo.get_pages(job.id)
        assert [page.content for page in pages] == ["hello"]
        assert warnings == ["w"]
        # The stage flipped to summarising, which is what makes recovery skip extraction.
        assert (await repo.get(job.id)).status is DocumentStatus.SUMMARIZING


class TestFencedCommit:
    async def test_stale_worker_cannot_commit_or_acknowledge(
        self,
        repo: JobRepository,
        queue: JobQueue,
        redis: Redis,
        settings: Settings,
    ) -> None:
        job = make_job()
        await repo.create_and_enqueue(job)
        stream_id, _ = await claim_message(queue)
        await repo.acquire_lease(job.id, "token-a")

        # Simulate the lease expiring while the worker was frozen, then a new owner taking over.
        await redis.delete(repo.lease_key(job.id))
        assert await repo.acquire_lease(job.id, "token-b") is True

        committed = await repo.commit_completed(
            job.id,
            token="token-a",
            summary="stale summary",
            page_count=1,
            warnings=[],
            stream_id=stream_id,
            group=settings.consumer_group,
        )
        assert committed is False, "a worker that lost its lease must not write a result"
        assert (await repo.get(job.id)).status is not DocumentStatus.COMPLETED
        assert await repo.get_summary(job.id) is None
        # And crucially: the message is still pending, so it is not silently lost.
        assert (await queue.pending())["pending"] == 1

    async def test_owner_commits_terminal_state_and_cleans_the_stream(
        self,
        repo: JobRepository,
        queue: JobQueue,
        redis: Redis,
        settings: Settings,
    ) -> None:
        job = make_job()
        await repo.create_and_enqueue(job)
        stream_id, _ = await claim_message(queue)
        await repo.acquire_lease(job.id, "token-a")
        await repo.save_pages(job.id, token="token-a", pages=PAGES, warnings=[])

        committed = await repo.commit_completed(
            job.id,
            token="token-a",
            summary="a summary",
            page_count=1,
            warnings=[],
            stream_id=stream_id,
            group=settings.consumer_group,
        )
        assert committed is True

        stored = await repo.get(job.id)
        assert stored.status is DocumentStatus.COMPLETED
        assert stored.page_count == 1
        assert await repo.get_summary(job.id) == "a summary"
        assert (await queue.pending())["pending"] == 0
        assert await redis.xlen(settings.stream_key) == 0, "the entry must be ACKed and deleted"
        # Results expire; a result that never expires is a memory leak.
        assert await redis.ttl(repo.summary_key(job.id)) > 0
        assert await redis.ttl(repo.job_key(job.id)) > 0

    async def test_failure_keeps_pages_readable_and_records_a_stable_code(
        self, repo: JobRepository, queue: JobQueue, settings: Settings
    ) -> None:
        job = make_job()
        await repo.create_and_enqueue(job)
        stream_id, _ = await claim_message(queue)
        await repo.acquire_lease(job.id, "token-a")
        await repo.save_pages(job.id, token="token-a", pages=PAGES, warnings=[])

        failed = await repo.commit_failed(
            job.id,
            token="token-a",
            error=ErrorInfo(code="SUMMARY_FAILED", message="boom", retryable=False),
            page_count=1,
            warnings=[],
            stream_id=stream_id,
            group=settings.consumer_group,
        )
        assert failed is True
        stored = await repo.get(job.id)
        assert stored.status is DocumentStatus.FAILED
        assert stored.error is not None
        assert stored.error.code == "SUMMARY_FAILED"
        # A failed summary must not destroy the extracted pages.
        assert await repo.get_pages(job.id) is not None


class TestRecoveryMechanics:
    async def test_attempt_counter_increments(self, repo: JobRepository) -> None:
        job = make_job()
        await repo.create_and_enqueue(job)
        assert await repo.increment_attempt(job.id) == 1
        assert await repo.increment_attempt(job.id) == 2

    async def test_a_pending_entry_can_be_reclaimed_by_another_consumer(
        self,
        repo: JobRepository,
        queue: JobQueue,
        redis: Redis,
        settings: Settings,
    ) -> None:
        job = make_job()
        await repo.create_and_enqueue(job)
        await claim_message(queue, consumer="dying-worker")

        # Nothing is reclaimable while the entry is fresh...
        assert await queue.claim_stale("replacement") == []

        # ...but once it has been idle longer than the reclaim threshold, another consumer takes
        # it over. The threshold is 50 ms in this fixture so the test does not sleep for seconds.
        await asyncio.sleep(0.2)
        reclaimed = await queue.claim_stale("replacement")
        assert reclaimed, "a stale pending entry must be reclaimable"
        assert reclaimed[0][1] == job.id

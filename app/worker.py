"""Worker process: `python -m app.worker`.

Consumes the Redis Stream, takes a per-job lease, processes, and acknowledges only after a
terminal state is durable. If the worker dies the entry stays pending, another worker takes it
over after `LEASE_SECONDS`, and the pages checkpoint means the extraction is not repeated.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import signal
import socket
import time
import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings
from app.models import DocumentJob, ErrorInfo
from app.parsers.base import ParserError
from app.processing import JobProcessor, LeaseLostError, classify
from app.queue import JobQueue
from app.registry import ProviderRegistry
from app.storage.jobs import JobRepository
from app.storage.objects import S3ObjectStore

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        settings: Settings,
        *,
        redis: Redis,
        repo: JobRepository,
        store: S3ObjectStore,
        queue: JobQueue,
        registry: ProviderRegistry,
        consumer: str,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._repo = repo
        self._store = store
        self._queue = queue
        self._registry = registry
        self._consumer = consumer
        self._processor = JobProcessor(settings=settings, repo=repo, store=store, registry=registry)
        self._stop = False

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def request_stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        logger.info(
            "worker_started consumer=%s ai_mode=%s stream=%s group=%s concurrency=%d",
            self._consumer,
            self._settings.ai_mode,
            self._settings.stream_key,
            self._settings.consumer_group,
            self._settings.worker_concurrency,
        )
        await self._wait_for_group()

        # Slots, not threads: each job is mostly waiting on S3/provider I/O, so asyncio gives the
        # parallelism. The semaphore is also the backpressure mechanism — when every slot is busy we
        # stop reading the stream instead of piling work into memory.
        slots = asyncio.Semaphore(self._settings.worker_concurrency)
        in_flight: set[asyncio.Task[None]] = set()

        def _finished(task: asyncio.Task[None]) -> None:
            in_flight.discard(task)
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:  # a crash in one job must not take the worker down
                logger.error("job_task_crashed", exc_info=exc)

        while not self._stop:
            try:
                messages = await self._queue.read_new(
                    self._consumer, count=self._settings.worker_batch_size
                )
                if not messages:
                    # Nothing new: look for entries whose consumer died.
                    messages = await self._queue.claim_stale(
                        self._consumer, count=self._settings.worker_batch_size
                    )
            except RedisError as exc:
                logger.warning("redis_unavailable error=%s", type(exc).__name__)
                await asyncio.sleep(2)
                continue

            for stream_id, document_id in messages:
                if self._stop:
                    # Leave the entry pending: a replacement worker resumes it.
                    break
                if slots.locked():
                    logger.debug("worker_saturated in_flight=%d", len(in_flight))
                await slots.acquire()
                task = asyncio.create_task(
                    self._run_slot(stream_id, document_id, slots), name=f"job:{document_id}"
                )
                in_flight.add(task)
                task.add_done_callback(_finished)

        await self._drain(in_flight)
        logger.info("worker_stopped consumer=%s", self._consumer)

    async def _run_slot(self, stream_id: str, document_id: str, slots: asyncio.Semaphore) -> None:
        """One job, holding one concurrency slot for as long as it runs."""
        try:
            await self._handle(stream_id, document_id)
        except RedisError as exc:
            logger.warning("handle_redis_error id=%s error=%s", document_id, type(exc).__name__)
        except Exception:
            logger.exception("handle_unexpected_error id=%s", document_id)
        finally:
            slots.release()

    async def _drain(self, in_flight: set[asyncio.Task[None]]) -> None:
        """On shutdown, give in-flight jobs a bounded chance to finish.

        Anything still running after the grace period is cancelled and left pending: its lease
        expires and another worker reclaims it from the page checkpoint. Nothing is ever
        acknowledged just because shutdown began.
        """
        if not in_flight:
            return
        grace = self._settings.worker_shutdown_grace_seconds
        logger.info("worker_draining in_flight=%d grace_s=%.0f", len(in_flight), grace)
        _done, still_running = await asyncio.wait(in_flight, timeout=grace)
        if still_running:
            for task in still_running:
                task.cancel()
            await asyncio.gather(*still_running, return_exceptions=True)
            logger.warning(
                "worker_shutdown_incomplete abandoned=%d entries remain pending until the lease expires",
                len(still_running),
            )

    async def _wait_for_group(self) -> None:
        delay = 1.0
        while not self._stop:
            try:
                await self._queue.ensure_group()
                return
            except RedisError as exc:
                logger.warning(
                    "waiting_for_redis error=%s retry_in=%.0fs", type(exc).__name__, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)

    # ── one message ────────────────────────────────────────────────────────────
    async def _handle(self, stream_id: str, document_id: str) -> None:
        job = await self._repo.get(document_id)
        if job is None:
            # The hash expired or was removed: nothing to process, and keeping the entry
            # pending forever would feed a claim loop.
            logger.warning(
                "orphan_stream_entry stream_id=%s document_id=%s", stream_id, document_id
            )
            await self._queue.ack(stream_id)
            return

        if job.status.is_terminal:
            await self._queue.ack(stream_id)
            return

        token = uuid.uuid4().hex
        if not await self._repo.acquire_lease(document_id, token):
            logger.info("job_leased_elsewhere id=%s", document_id)
            return

        heartbeat = asyncio.create_task(self._heartbeat(document_id, token))
        try:
            await self._attempt_loop(job, stream_id=stream_id, token=token)
        except LeaseLostError:
            logger.warning("lease_lost id=%s (no state written)", document_id)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self._repo.release_lease(document_id, token)

    async def _attempt_loop(self, job: DocumentJob, *, stream_id: str, token: str) -> None:
        while True:
            attempt = await self._repo.increment_attempt(job.id)
            if attempt > self._settings.max_job_attempts:
                await self._fail(
                    job,
                    stream_id=stream_id,
                    token=token,
                    error=ErrorInfo(
                        code="ATTEMPTS_EXHAUSTED",
                        message=f"Gave up after {self._settings.max_job_attempts} attempts.",
                        retryable=False,
                    ),
                )
                return

            started = time.monotonic()
            try:
                await self._processor.process(job, token=token, stream_id=stream_id)
                return
            except LeaseLostError:
                raise
            except RedisError:
                # The job store itself is unavailable. This is NOT a property of the document
                # and must never become a terminal failure: re-raise so the message stays
                # pending (nothing is acknowledged) and a reclaim resumes from the checkpoint.
                logger.warning("job_deferred_redis_unavailable id=%s attempt=%d", job.id, attempt)
                raise
            except ParserError as exc:
                elapsed = time.monotonic() - started
                retryable = exc.retryable
                logger.warning(
                    "job_attempt_failed id=%s attempt=%d code=%s retryable=%s duration_s=%.2f",
                    job.id,
                    attempt,
                    exc.code,
                    retryable,
                    elapsed,
                )
                if retryable and attempt < self._settings.max_job_attempts:
                    await self._repo.mark_retry(
                        job.id,
                        token=token,
                        error=classify(exc),
                        attempt=attempt,
                    )
                    await asyncio.sleep(self._retry_delay(exc, attempt))
                    continue
                await self._fail(job, stream_id=stream_id, token=token, error=classify(exc))
                return
            except Exception as exc:
                logger.exception("job_unexpected_error id=%s attempt=%d", job.id, attempt)
                await self._fail(job, stream_id=stream_id, token=token, error=classify(exc))
                return

    def _retry_delay(self, exc: ParserError, attempt: int) -> float:
        """Backoff with jitter, preferring the provider's own Retry-After when it supplied one."""
        backoffs = self._settings.retry_backoff_seconds
        base = backoffs[min(attempt - 1, len(backoffs) - 1)]
        retry_after = getattr(exc, "retry_after", None)
        if retry_after:
            # Honour the provider, but cap it: a 429 asking for ten minutes would otherwise hold
            # the lease past the job deadline and turn a retry into a stuck document.
            return min(float(retry_after), self._settings.retry_after_max_seconds)
        # Jitter keeps several workers from retrying in lockstep after a shared outage.
        return base * (1.0 + random.uniform(-0.2, 0.2))

    async def _fail(
        self,
        job: DocumentJob,
        *,
        stream_id: str,
        token: str,
        error: ErrorInfo,
    ) -> None:
        committed = await self._repo.commit_failed(
            job.id,
            token=token,
            error=error,
            page_count=job.page_count,
            warnings=job.warnings,
            stream_id=stream_id,
            group=self._settings.consumer_group,
        )
        if not committed:
            raise LeaseLostError()
        logger.info("job_failed id=%s code=%s", job.id, error.code)
        await self._store.delete_source(job.source_key)

    async def _heartbeat(self, document_id: str, token: str) -> None:
        """Keep the lease alive while the job runs, but only while we still own it."""
        interval = self._settings.heartbeat_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                if not await self._repo.renew_lease(document_id, token):
                    logger.warning("lease_renewal_rejected id=%s", document_id)
                    return
            except RedisError as exc:
                logger.warning(
                    "lease_renewal_error id=%s error=%s", document_id, type(exc).__name__
                )


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_worker(settings: Settings) -> Worker:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    return Worker(
        settings,
        redis=redis,
        repo=JobRepository(redis, settings),
        store=S3ObjectStore(settings),
        queue=JobQueue(redis, settings),
        registry=ProviderRegistry(settings),
        consumer=consumer,
    )


def main() -> None:
    settings = get_settings()
    _configure_logging(settings.log_level)
    worker = build_worker(settings)

    def _request_stop(*_: object) -> None:
        worker.request_stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError, NotImplementedError):
            signal.signal(sig, _request_stop)

    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

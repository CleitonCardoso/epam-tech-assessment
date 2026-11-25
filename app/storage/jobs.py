"""Redis repository: the single source of truth for job state.

Two invariants this module exists to protect:

1. **Acceptance is atomic.** A job and its stream entry are created in one MULTI, so a job can
   never be visible without being queued.
2. **Only the lease holder may write.** Every mutation that matters is fenced by a compare of the
   execution token, so a worker that lost its lease (paused, GC'd, network-partitioned) cannot
   overwrite a result or acknowledge someone else's message.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from redis.asyncio import Redis
from redis.exceptions import WatchError

from app.models import (
    DocumentJob,
    DocumentStatus,
    ErrorInfo,
    PageResult,
    utc_now_iso,
)

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

JOB_KEY = "docs:job:{id}"
PAGES_KEY = "docs:pages:{id}"
SUMMARY_KEY = "docs:summary:{id}"
LEASE_KEY = "docs:lease:{id}"

# Renew/delete only if the caller still owns the token.
_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class JobRepository:
    def __init__(self, redis: Redis, settings: "Settings") -> None:
        self._redis = redis
        self._settings = settings

    # ── keys ───────────────────────────────────────────────────────────────────
    @staticmethod
    def job_key(document_id: str) -> str:
        return JOB_KEY.format(id=document_id)

    @staticmethod
    def pages_key(document_id: str) -> str:
        return PAGES_KEY.format(id=document_id)

    @staticmethod
    def summary_key(document_id: str) -> str:
        return SUMMARY_KEY.format(id=document_id)

    @staticmethod
    def lease_key(document_id: str) -> str:
        return LEASE_KEY.format(id=document_id)

    @staticmethod
    def _to_hash(job: DocumentJob) -> dict[str, str]:
        return {
            "id": job.id,
            "filename": job.filename,
            "parser": job.parser.value,
            "status": job.status.value,
            "stage": job.stage or "",
            "attempt": str(job.attempt),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "source_key": job.source_key,
            "page_count": "" if job.page_count is None else str(job.page_count),
            "warnings": json.dumps(job.warnings),
            "error": job.error.model_dump_json() if job.error else "",
            "deadline_at": f"{job.deadline_at:.3f}",
        }

    @staticmethod
    def _from_hash(raw: dict[str, str]) -> DocumentJob:
        page_count = raw.get("page_count") or ""
        error_raw = raw.get("error") or ""
        return DocumentJob(
            id=raw["id"],
            filename=raw["filename"],
            parser=raw["parser"],
            status=DocumentStatus(raw["status"]),
            stage=(raw.get("stage") or None),
            attempt=int(raw.get("attempt") or 0),
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
            source_key=raw["source_key"],
            page_count=int(page_count) if page_count else None,
            warnings=json.loads(raw.get("warnings") or "[]"),
            error=ErrorInfo.model_validate_json(error_raw) if error_raw else None,
            deadline_at=float(raw.get("deadline_at") or 0.0),
        )

    # ── acceptance ─────────────────────────────────────────────────────────────
    async def create_and_enqueue(self, job: DocumentJob) -> None:
        """Job hash + stream entry, atomically. Raises RedisError upward: the caller must not
        report acceptance unless this returns normally."""
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(self.job_key(job.id), mapping=self._to_hash(job))
            pipe.xadd(self._settings.stream_key, {"document_id": job.id})
            await pipe.execute()

    # ── reads ──────────────────────────────────────────────────────────────────
    async def get(self, document_id: str) -> DocumentJob | None:
        raw = await self._redis.hgetall(self.job_key(document_id))
        if not raw:
            return None
        return self._from_hash(raw)

    async def exists(self, document_id: str) -> bool:
        return bool(await self._redis.exists(self.job_key(document_id)))

    async def get_pages(self, document_id: str) -> tuple[list[PageResult], list[str]] | None:
        raw = await self._redis.get(self.pages_key(document_id))
        if not raw:
            return None
        payload = json.loads(raw)
        pages = [PageResult.model_validate(item) for item in payload.get("pages", [])]
        return pages, list(payload.get("warnings", []))

    async def get_summary(self, document_id: str) -> str | None:
        return await self._redis.get(self.summary_key(document_id))

    # ── fenced writes ──────────────────────────────────────────────────────────
    async def _begin_fenced(self, pipe: Any, document_id: str, token: str) -> bool:
        lease_key = self.lease_key(document_id)
        await pipe.watch(lease_key)
        current = await pipe.get(lease_key)
        if current != token:
            await pipe.unwatch()
            return False
        pipe.multi()
        return True

    async def set_stage(
        self,
        document_id: str,
        *,
        token: str,
        status: DocumentStatus,
        stage: str | None,
        attempt: int | None = None,
    ) -> bool:
        pipe = self._redis.pipeline(transaction=True)
        try:
            if not await self._begin_fenced(pipe, document_id, token):
                return False
            mapping = {
                "status": status.value,
                "stage": stage or "",
                "updated_at": utc_now_iso(),
            }
            if attempt is not None:
                mapping["attempt"] = str(attempt)
            pipe.hset(self.job_key(document_id), mapping=mapping)
            await pipe.execute()
            return True
        except WatchError:
            return False

    async def save_pages(
        self,
        document_id: str,
        *,
        token: str,
        pages: list[PageResult],
        warnings: list[str],
    ) -> bool:
        """Persist pages *before* summarising: their presence is the recovery checkpoint."""
        payload = json.dumps(
            {
                "pages": [p.model_dump(mode="json") for p in pages],
                "warnings": warnings,
            }
        )
        pipe = self._redis.pipeline(transaction=True)
        try:
            if not await self._begin_fenced(pipe, document_id, token):
                return False
            pipe.set(self.pages_key(document_id), payload)
            pipe.hset(
                self.job_key(document_id),
                mapping={
                    "status": DocumentStatus.SUMMARIZING.value,
                    "stage": "summarizing",
                    "page_count": str(len(pages)),
                    "warnings": json.dumps(warnings),
                    "updated_at": utc_now_iso(),
                },
            )
            await pipe.execute()
            return True
        except WatchError:
            return False

    async def commit_completed(
        self,
        document_id: str,
        *,
        token: str,
        summary: str,
        page_count: int,
        warnings: list[str],
        stream_id: str,
        group: str,
    ) -> bool:
        pipe = self._redis.pipeline(transaction=True)
        try:
            if not await self._begin_fenced(pipe, document_id, token):
                return False
            ttl = self._settings.result_ttl_seconds
            now = utc_now_iso()
            pipe.hset(
                self.job_key(document_id),
                mapping={
                    "status": DocumentStatus.COMPLETED.value,
                    "stage": "",
                    "page_count": str(page_count),
                    "warnings": json.dumps(warnings),
                    "error": "",
                    "updated_at": now,
                },
            )
            pipe.set(self.summary_key(document_id), summary, ex=ttl)
            pipe.expire(self.pages_key(document_id), ttl)
            pipe.expire(self.job_key(document_id), ttl)
            pipe.xack(self._settings.stream_key, group, stream_id)
            pipe.xdel(self._settings.stream_key, stream_id)
            await pipe.execute()
            return True
        except WatchError:
            return False

    async def commit_failed(
        self,
        document_id: str,
        *,
        token: str,
        error: ErrorInfo,
        page_count: int | None,
        warnings: list[str],
        stream_id: str,
        group: str,
    ) -> bool:
        pipe = self._redis.pipeline(transaction=True)
        try:
            if not await self._begin_fenced(pipe, document_id, token):
                return False
            ttl = self._settings.result_ttl_seconds
            mapping = {
                "status": DocumentStatus.FAILED.value,
                "stage": "",
                "warnings": json.dumps(warnings),
                "error": error.model_dump_json(),
                "updated_at": utc_now_iso(),
            }
            if page_count is not None:
                mapping["page_count"] = str(page_count)
            pipe.hset(self.job_key(document_id), mapping=mapping)
            # Pages are kept (a failed summary still has useful pages) but must expire too.
            pipe.expire(self.pages_key(document_id), ttl)
            pipe.expire(self.job_key(document_id), ttl)
            pipe.xack(self._settings.stream_key, group, stream_id)
            pipe.xdel(self._settings.stream_key, stream_id)
            await pipe.execute()
            return True
        except WatchError:
            return False

    async def mark_retry(self, document_id: str, *, token: str, error: ErrorInfo, attempt: int) -> bool:
        pipe = self._redis.pipeline(transaction=True)
        try:
            if not await self._begin_fenced(pipe, document_id, token):
                return False
            pipe.hset(
                self.job_key(document_id),
                mapping={
                    "status": DocumentStatus.RETRYING.value,
                    "stage": "",
                    "attempt": str(attempt),
                    "error": error.model_dump_json(),
                    "updated_at": utc_now_iso(),
                },
            )
            await pipe.execute()
            return True
        except WatchError:
            return False

    # ── lease ──────────────────────────────────────────────────────────────────
    async def acquire_lease(self, document_id: str, token: str) -> bool:
        return bool(
            await self._redis.set(
                self.lease_key(document_id),
                token,
                nx=True,
                px=self._settings.lease_seconds * 1000,
            )
        )

    async def renew_lease(self, document_id: str, token: str) -> bool:
        renewed = await self._redis.eval(
            _RENEW, 1, self.lease_key(document_id), token, self._settings.lease_seconds * 1000
        )
        return bool(renewed)

    async def release_lease(self, document_id: str, token: str) -> None:
        try:
            await self._redis.eval(_RELEASE, 1, self.lease_key(document_id), token)
        except Exception:  # noqa: BLE001 — release is best effort, never fail a job over it
            logger.warning("lease_release_failed document_id=%s", document_id)

    async def increment_attempt(self, document_id: str) -> int:
        return int(await self._redis.hincrby(self.job_key(document_id), "attempt", 1))

"""Redis Streams consumer-group operations.

The stream carries only `document_id`. Everything else lives in the job hash, so a stream entry
is a pointer, never a payload.

Delivery is at-least-once: a crash after a provider call but before the checkpoint repeats that
call. That is stated in the README as a property, not hidden.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from redis.asyncio import Redis
from redis.exceptions import ResponseError

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

_FIELD = "document_id"


class JobQueue:
    def __init__(self, redis: Redis, settings: "Settings") -> None:
        self._redis = redis
        self._settings = settings
        self._stream = settings.stream_key
        self._group = settings.consumer_group

    async def ensure_group(self) -> None:
        """Create the group at 0 so jobs already in the stream are consumed too, not just new
        ones. `BUSYGROUP` is the only error that means 'already set up'."""
        try:
            await self._redis.xgroup_create(
                self._stream, self._group, id="0", mkstream=True
            )
            logger.info("consumer_group_created stream=%s group=%s", self._stream, self._group)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read_new(
        self, consumer: str, block_ms: int | None = None, count: int = 1
    ) -> list[tuple[str, str]]:
        block = self._settings.worker_read_block_ms if block_ms is None else block_ms
        response = await self._redis.xreadgroup(
            self._group,
            consumer,
            {self._stream: ">"},
            count=count,
            block=block,
        )
        return self._flatten(response)

    async def claim_stale(self, consumer: str, count: int = 10) -> list[tuple[str, str]]:
        """Take over entries whose consumer died. The claim threshold equals the lease length,
        so a live worker is never stolen from: it renews its lease every HEARTBEAT_SECONDS."""
        min_idle = self._settings.effective_reclaim_idle_ms
        result = await self._redis.xautoclaim(
            self._stream,
            self._group,
            consumer,
            min_idle_time=min_idle,
            start_id="0-0",
            count=count,
        )
        # redis-py >=5 returns (cursor, entries, deleted); older versions omit the third field.
        entries = result[1] if len(result) > 1 else []
        return [
            (entry[0], self._field(entry[1]))
            for entry in entries
            if self._field(entry[1])
        ]

    @staticmethod
    def _field(fields: dict) -> str:
        value = fields.get(_FIELD) or fields.get(_FIELD.encode())  # type: ignore[union-attr]
        if isinstance(value, bytes):
            value = value.decode()
        return value or ""

    def _flatten(self, response) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for _stream, entries in response or []:
            for entry_id, fields in entries:
                doc_id = self._field(fields)
                if doc_id:
                    out.append((entry_id, doc_id))
        return out

    async def ack(self, stream_id: str) -> None:
        """ACK and delete in one round trip. Safe only because this design has exactly one
        consumer group; with a second group, XDEL would hide the entry from it."""
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xack(self._stream, self._group, stream_id)
            pipe.xdel(self._stream, stream_id)
            await pipe.execute()

    async def pending(self) -> dict[str, int]:
        """Diagnostics for the README: per-consumer pending counts."""
        summary = await self._redis.xpending(self._stream, self._group)
        if isinstance(summary, dict):
            return {
                "pending": int(summary.get("pending", 0)),
                "consumers": int(summary.get("consumers", 0)),
            }
        return {"pending": 0, "consumers": 0}

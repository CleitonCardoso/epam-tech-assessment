"""One document, end to end: parse -> checkpoint -> summarize -> commit.

Every state write goes through the repository's lease fence, so this module never has to
reason about whether it still owns the job — it just asks and gets a boolean.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from app.models import DocumentJob, DocumentStatus, ErrorInfo
from app.parsers.base import (
    PageResult,
    ParserError,
    PermanentError,
    TransientError,
    validate_pages,
)
from app.registry import ParserUnavailable, ProviderRegistry
from app.storage.jobs import JobRepository
from app.storage.objects import ObjectStoreError, S3ObjectStore

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)


class LeaseLost(Exception):
    """This execution no longer owns the job; stop immediately and write nothing."""


def _merge_warnings(*groups: list[str]) -> list[str]:
    seen: list[str] = []
    for group in groups:
        for item in group:
            if item and item not in seen:
                seen.append(item)
    return seen


class JobProcessor:
    def __init__(
        self,
        *,
        settings: "Settings",
        repo: JobRepository,
        store: S3ObjectStore,
        registry: ProviderRegistry,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._store = store
        self._registry = registry

    async def process(self, job: DocumentJob, *, token: str, stream_id: str) -> None:
        started = time.monotonic()

        if job.deadline_at and time.time() > job.deadline_at:
            raise PermanentError(
                "The job deadline was exceeded before processing started.",
                code="DEADLINE_EXCEEDED",
            )

        pages: list[PageResult]
        warnings: list[str] = list(job.warnings)

        checkpoint = await self._repo.get_pages(job.id)
        if checkpoint is not None:
            pages, stored_warnings = checkpoint
            warnings = _merge_warnings(stored_warnings, job.warnings)
            logger.info(
                "job_resumed id=%s pages=%d (skipping extraction)", job.id, len(pages)
            )
        else:
            pages, warnings = await self._extract(job, token=token, warnings=warnings)

        await self._summarize(job, pages, warnings, token=token, stream_id=stream_id)
        logger.info(
            "job_completed id=%s pages=%d duration_s=%.2f",
            job.id,
            len(pages),
            time.monotonic() - started,
        )

    # ── stages ─────────────────────────────────────────────────────────────────
    async def _extract(
        self, job: DocumentJob, *, token: str, warnings: list[str]
    ) -> tuple[list[PageResult], list[str]]:
        if not await self._repo.set_stage(
            job.id, token=token, status=DocumentStatus.PARSING, stage="parsing"
        ):
            raise LeaseLost()

        try:
            parser = self._registry.get_parser(job.parser)
        except ParserUnavailable as exc:
            raise PermanentError(str(exc), code="PARSER_UNAVAILABLE") from exc

        try:
            pdf = await self._store.get_source(job.source_key)
        except ObjectStoreError as exc:
            if exc.missing:
                raise PermanentError(
                    "The uploaded source PDF is no longer available.", code="SOURCE_MISSING"
                ) from exc
            raise TransientError(
                "The object store could not be read.", code="SOURCE_UNAVAILABLE"
            ) from exc

        try:
            result = await asyncio.wait_for(
                parser.parse(pdf, filename=job.filename, limits=self._registry.limits()),
                timeout=self._settings.provider_timeout_seconds + 30,
            )
        except asyncio.TimeoutError as exc:
            raise TransientError(
                "The parser did not finish in time.", code="PARSER_TIMEOUT"
            ) from exc

        pages = validate_pages(result.pages)
        merged = _merge_warnings(warnings, result.warnings)

        if not await self._repo.save_pages(job.id, token=token, pages=pages, warnings=merged):
            raise LeaseLost()
        logger.info("pages_checkpointed id=%s pages=%d", job.id, len(pages))
        return pages, merged

    async def _summarize(
        self,
        job: DocumentJob,
        pages: list[PageResult],
        warnings: list[str],
        *,
        token: str,
        stream_id: str,
    ) -> None:
        if not await self._repo.set_stage(
            job.id, token=token, status=DocumentStatus.SUMMARIZING, stage="summarizing"
        ):
            raise LeaseLost()

        try:
            summarizer = self._registry.get_summarizer()
        except ParserUnavailable as exc:
            raise PermanentError(str(exc), code="SUMMARIZER_UNAVAILABLE") from exc

        try:
            summary = await asyncio.wait_for(
                summarizer.summarize(
                    pages, timeout_seconds=self._settings.provider_timeout_seconds
                ),
                timeout=self._settings.provider_timeout_seconds + 15,
            )
        except asyncio.TimeoutError as exc:
            raise TransientError(
                "Summarization did not finish in time.", code="SUMMARY_TIMEOUT"
            ) from exc

        summary = (summary or "").strip()
        if not summary:
            raise PermanentError(
                "The summarizer returned an empty document summary.", code="SUMMARY_EMPTY"
            )

        committed = await self._repo.commit_completed(
            job.id,
            token=token,
            summary=summary,
            page_count=len(pages),
            warnings=warnings,
            stream_id=stream_id,
            group=self._settings.consumer_group,
        )
        if not committed:
            raise LeaseLost()

        # Only after the terminal state is durable. A leftover object is harmless; a job
        # pointing at a deleted object is not.
        await self._store.delete_source(job.source_key)


def classify(exc: BaseException) -> ErrorInfo:
    """Map any exception to the client-facing error record."""
    if isinstance(exc, ParserError):
        return ErrorInfo(code=exc.code, message=exc.message, retryable=exc.retryable)
    logger.exception("unexpected_processing_error")
    return ErrorInfo(
        code="INTERNAL_ERROR",
        message="Processing failed unexpectedly. The server logs have the details.",
        retryable=False,
    )

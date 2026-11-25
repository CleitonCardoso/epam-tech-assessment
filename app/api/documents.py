"""Upload and status endpoints.

The API never parses a PDF and never calls a model: it validates bounded inputs, stores the
source object, records the job atomically with its stream entry, and returns. Everything
expensive happens in the worker process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.models import (
    DocumentAccepted,
    DocumentDetail,
    DocumentJob,
    DocumentStatus,
    ParserId,
    ParsersResponse,
    RejectedFile,
    UploadResponse,
    utc_now_iso,
)
from app.registry import ParserUnavailable, ProviderRegistry
from app.storage.jobs import JobRepository
from app.storage.objects import ObjectStoreError, S3ObjectStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["documents"])

PDF_SIGNATURE = b"%PDF-"
READ_CHUNK = 1024 * 1024
MAX_FILENAME = 200


@dataclass(slots=True)
class AppContext:
    settings: Settings
    redis: Redis
    repo: JobRepository
    store: S3ObjectStore
    registry: ProviderRegistry


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


def _safe_filename(name: str | None) -> str:
    base = os.path.basename(name or "") or "document.pdf"
    cleaned = "".join(ch for ch in base if ch.isprintable() and ch not in '\\/:*?"<>|')
    return cleaned[:MAX_FILENAME] or "document.pdf"


async def _read_bounded(upload: UploadFile, limit: int) -> tuple[bytes, bool]:
    """Read at most `limit` bytes; the second value says whether the file exceeded it."""
    buffer = bytearray()
    exceeded = False
    while True:
        chunk = await upload.read(READ_CHUNK)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > limit:
            exceeded = True
            break
    return bytes(buffer), exceeded


@router.get("/parsers", response_model=ParsersResponse)
async def list_parsers(ctx: Ctx) -> ParsersResponse:
    return ParsersResponse(ai_mode=ctx.registry.ai_mode, parsers=ctx.registry.describe())


@router.post(
    "/documents",
    status_code=202,
    response_model=UploadResponse,
    responses={
        413: {"description": "A batch or file limit was exceeded"},
        422: {"description": "The request was rejected before any job was created"},
        503: {"description": "A required dependency is unavailable"},
    },
)
async def create_documents(
    ctx: Ctx,
    parser: Annotated[str, Form(description="pypdf | gemini | gpt-5-mini")],
    files: Annotated[list[UploadFile], File(description="One or more PDF files")],
):
    settings = ctx.settings

    try:
        parser_id = ParserId(parser)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown parser {parser!r}. Use one of: {', '.join(p.value for p in ParserId)}.",
        ) from None

    try:
        ctx.registry.get_parser(parser_id)
    except ParserUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Parser {parser_id.value!r} is not available right now. {exc}",
        ) from exc

    if not files:
        raise HTTPException(status_code=422, detail="No files were uploaded.")

    if len(files) > settings.max_batch_files:
        raise HTTPException(
            status_code=413,
            detail=f"Too many files: {len(files)} > MAX_BATCH_FILES={settings.max_batch_files}.",
        )

    rejected: list[RejectedFile] = []
    accepted: list[DocumentAccepted] = []
    accepted_objects: list[str] = []
    total_bytes = 0

    for upload in files:
        filename = _safe_filename(upload.filename)
        data, exceeded = await _read_bounded(upload, settings.max_upload_bytes)
        await upload.close()

        if exceeded:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"{filename} exceeds MAX_UPLOAD_BYTES="
                    f"{settings.max_upload_bytes} bytes."
                ),
            )
        total_bytes += len(data)
        if total_bytes > settings.max_batch_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Batch exceeds MAX_BATCH_BYTES={settings.max_batch_bytes} bytes.",
            )

        if not data:
            rejected.append(
                RejectedFile(filename=filename, code="EMPTY_FILE", message="The file is empty.")
            )
            continue
        if not data.startswith(PDF_SIGNATURE):
            rejected.append(
                RejectedFile(
                    filename=filename,
                    code="NOT_A_PDF",
                    message="The file does not start with the %PDF- signature.",
                )
            )
            continue

        document_id = str(uuid.uuid4())
        source_key = ctx.store.source_key(document_id)

        try:
            await ctx.store.put_source(document_id, data)
        except ObjectStoreError:
            logger.warning("source_upload_failed document_id=%s", document_id)
            rejected.append(
                RejectedFile(
                    filename=filename,
                    code="STORAGE_UNAVAILABLE",
                    message="The object store is unavailable; the file was not accepted.",
                )
            )
            continue

        job = DocumentJob(
            id=document_id,
            filename=filename,
            parser=parser_id,
            status=DocumentStatus.QUEUED,
            source_key=source_key,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            deadline_at=time.time() + settings.job_deadline_seconds,
        )

        try:
            await ctx.repo.create_and_enqueue(job)
        except RedisError:
            # Definite rejection: nothing was committed, so the just-uploaded object is an
            # orphan. Remove it rather than leave an unreferenced PDF behind.
            await ctx.store.delete_source(source_key)
            logger.warning("enqueue_failed document_id=%s", document_id)
            rejected.append(
                RejectedFile(
                    filename=filename,
                    code="QUEUE_UNAVAILABLE",
                    message="The job queue is unavailable; the file was not accepted.",
                )
            )
            continue

        accepted_objects.append(source_key)
        accepted.append(
            DocumentAccepted(
                id=document_id,
                filename=filename,
                parser=parser_id,
                status=DocumentStatus.QUEUED,
                status_url=f"/api/v1/documents/{document_id}",
            )
        )
        logger.info(
            "document_accepted document_id=%s parser=%s bytes=%d",
            document_id,
            parser_id.value,
            len(data),
        )

    payload = UploadResponse(documents=accepted, rejected=rejected)

    if accepted:
        return payload

    # Nothing was accepted. The status must say why, and the per-file outcomes must survive.
    if any(r.code in {"STORAGE_UNAVAILABLE", "QUEUE_UNAVAILABLE"} for r in rejected):
        return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))
    return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))


@router.get(
    "/documents/{document_id}",
    response_model=DocumentDetail,
    responses={404: {"description": "Unknown or expired document"}, 503: {"description": "Redis unavailable"}},
)
async def get_document(document_id: str, ctx: Ctx) -> DocumentDetail:
    try:
        job = await ctx.repo.get(document_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail="Document not found. Unknown id, or the result has expired.",
            )
        checkpoint = await ctx.repo.get_pages(document_id)
        summary = await ctx.repo.get_summary(document_id)
    except RedisError as exc:
        raise HTTPException(
            status_code=503, detail="The job store is unavailable; try again shortly."
        ) from exc

    pages, warnings = checkpoint if checkpoint is not None else ([], job.warnings)
    return DocumentDetail(
        # `warnings` is passed explicitly below, so it is excluded from the job dump.
        **job.model_dump(exclude={"warnings"}),
        pages=pages,
        summary=summary,
        warnings=warnings or job.warnings,
    )

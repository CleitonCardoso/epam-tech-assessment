"""Wire and internal models shared by the API and the worker.

FROZEN CONTRACT — see docs/CONTRACT.md. Both processes import from here; nothing in this
module may import from `app.api`, `app.storage`, `app.queue` or `app.processing`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    """Single timestamp format everywhere: 2026-11-25T12:00:00Z."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ParserId(StrEnum):
    PYPDF = "pypdf"
    GEMINI = "gemini"
    GPT5_MINI = "gpt-5-mini"


class PageFormat(StrEnum):
    TEXT = "text"
    MARKDOWN = "markdown"


class DocumentStatus(StrEnum):
    QUEUED = "queued"
    PARSING = "parsing"
    SUMMARIZING = "summarizing"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (DocumentStatus.COMPLETED, DocumentStatus.FAILED)


class PageResult(BaseModel):
    """One extracted page. `page_number` is 1-based and contiguous by the time it is stored."""

    page_number: int = Field(ge=1)
    format: PageFormat
    content: str


class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool = False


class DocumentJob(BaseModel):
    """The Redis hash `docs:job:<id>`. Internal record; never returned raw to clients."""

    id: str
    filename: str
    parser: ParserId
    status: DocumentStatus = DocumentStatus.QUEUED
    stage: Literal["parsing", "summarizing"] | None = None
    attempt: int = 0
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    source_key: str
    page_count: int | None = None
    warnings: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None
    # Absolute epoch seconds; the worker refuses to start work past it.
    deadline_at: float = 0.0


# ── API responses ──────────────────────────────────────────────────────────────


class DocumentAccepted(BaseModel):
    id: str
    filename: str
    parser: ParserId
    status: DocumentStatus
    status_url: str


class RejectedFile(BaseModel):
    filename: str
    code: str
    message: str


class UploadResponse(BaseModel):
    """202 when at least one file was accepted; 422/413/503 when every file failed."""

    documents: list[DocumentAccepted] = Field(default_factory=list)
    rejected: list[RejectedFile] = Field(default_factory=list)


class DocumentDetail(DocumentJob):
    """GET /api/v1/documents/{id}. Fields that are not ready yet are null, never faked."""

    pages: list[PageResult] = Field(default_factory=list)
    summary: str | None = None


class ParserInfo(BaseModel):
    id: ParserId
    label: str
    output_format: PageFormat
    enabled: bool
    requires_api_key: bool
    note: str | None = None


class ParsersResponse(BaseModel):
    ai_mode: Literal["live", "fake"]
    parsers: list[ParserInfo]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    checks: dict[str, str] = Field(default_factory=dict)
    detail: str | None = None


class DocumentSummary(BaseModel):
    """One row of `GET /api/v1/documents` — enough to rebuild a UI after a refresh."""

    id: str
    filename: str
    parser: ParserId
    status: DocumentStatus
    page_count: int | None = None
    created_at: str
    status_url: str


class DocumentList(BaseModel):
    documents: list[DocumentSummary]
    next_before: float | None = None

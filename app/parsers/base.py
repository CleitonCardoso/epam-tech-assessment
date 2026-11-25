"""Parser / summarizer contracts.

FROZEN CONTRACT — see docs/CONTRACT.md. The worker only ever talks to these protocols, so a
provider implementation can be swapped for a fake without touching orchestration code.

Every failure that can happen inside a parser or a summarizer must be raised as a
ParserError subclass: the worker turns `retryable` into a bounded retry and anything else
into a terminal `failed` state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.models import PageResult, ParserId


@dataclass(slots=True)
class ParseLimits:
    """Bounds handed to a parser; the parser enforces them, the worker only passes them on."""

    max_pages: int
    max_chars: int
    max_ai_pages: int
    timeout_seconds: float


@dataclass(slots=True)
class ParseResult:
    pages: list[PageResult]
    warnings: list[str] = field(default_factory=list)


class ParserError(Exception):
    """Stable, client-safe failure. `code` is part of the API surface."""

    code: str = "PARSER_ERROR"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        # Seconds the provider asked us to wait (Retry-After), when it said so. The worker
        # prefers this over its own backoff and clamps it to RETRY_AFTER_MAX_SECONDS.
        self.retry_after = retry_after


class PermanentError(ParserError):
    """Retrying cannot help: corrupt input, unsupported document, bad configuration."""

    retryable = False


class TransientError(ParserError):
    """Worth a bounded retry: rate limit, timeout, selected 5xx."""

    retryable = True


class OcrRequiredError(PermanentError):
    """PyPDF found no extractable text on any page; the caller should choose Gemini."""

    code = "OCR_REQUIRED"


@runtime_checkable
class Parser(Protocol):
    id: ParserId

    async def parse(self, pdf: bytes, *, filename: str, limits: ParseLimits) -> ParseResult: ...


@runtime_checkable
class Summarizer(Protocol):
    async def summarize(self, pages: list[PageResult], *, timeout_seconds: float) -> str: ...


def validate_pages(pages: list[PageResult], *, expected_min: int = 1) -> list[PageResult]:
    """Enforce the invariants the API promises: ordered, unique, 1-based, contiguous."""
    if len(pages) < expected_min:
        raise PermanentError("The document produced no pages.", code="NO_PAGES")
    numbers = [p.page_number for p in pages]
    if numbers != list(range(1, len(pages) + 1)):
        raise PermanentError(
            "Extracted pages are not contiguous and ordered.", code="PAGE_SEQUENCE_INVALID"
        )
    return pages

"""Deterministic, network-free stand-ins used when ``AI_MODE=fake``.

They implement the frozen parser/summarizer interfaces from ``app.parsers.base`` so the
orchestration code cannot tell them apart from real providers. Output depends only on the
input (never on randomness or wall-clock time), and ``delay_seconds`` is honoured so
concurrency/lease behaviour can be exercised without paid API calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import io

from pypdf import PdfReader

from app.models import PageFormat, PageResult, ParserId
from app.parsers.base import (
    OcrRequiredError,
    ParseLimits,
    ParserError,
    ParseResult,
    PermanentError,
)

# Attached to every document produced in fake mode, so the synthetic nature of the output
# travels with the result instead of living only in the UI banner and the logs.
FAKE_WARNING = (
    "ai_mode=fake: page content and summary are deterministic stand-ins; no model was called."
)


def _count_pages(pdf: bytes, *, max_pages: int, encrypted_code: str = "ENCRYPTED_PDF") -> int:
    """Blocking page count via pypdf; raises ParserError subclasses only."""
    try:
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted:
            raise PermanentError("Password-protected PDFs are not supported.", code=encrypted_code)
        count = len(reader.pages)
    except ParserError:
        raise
    except Exception as exc:
        raise PermanentError("The uploaded file is not a valid PDF.", code="INVALID_PDF") from exc
    if count > max_pages:
        raise PermanentError(
            f"The document has {count} pages; the limit is {max_pages}.",
            code="TOO_MANY_PAGES",
        )
    if count == 0:
        # A page-based parser needs at least one page; all-empty documents are OCR_REQUIRED
        # in the real parser, but here there is not even a page to stand in for.
        raise OcrRequiredError("No extractable text; choose the Gemini parser.")
    return count


class FakeParser:
    """Deterministic markdown pages, one per source page, without any AI call."""

    def __init__(self, parser_id: ParserId, *, delay_seconds: float = 0.0) -> None:
        self.id = parser_id
        self._delay_seconds = delay_seconds

    async def parse(self, pdf: bytes, *, filename: str, limits: ParseLimits) -> ParseResult:
        if self._delay_seconds > 0:
            await asyncio.sleep(self._delay_seconds)
        # Mirror the limits of the parser this fake stands in for: AI parsers are capped by
        # max_ai_pages, the text parser by max_pages.
        max_pages = limits.max_ai_pages if self.id != ParserId.PYPDF else limits.max_pages
        page_count = await asyncio.to_thread(_count_pages, pdf, max_pages=max_pages)
        pages = [
            PageResult(
                page_number=number,
                format=PageFormat.MARKDOWN,
                content=f"## Page {number}\n\n(fake markdown for page {number} of {filename})",
            )
            for number in range(1, page_count + 1)
        ]
        return ParseResult(pages=pages, warnings=[FAKE_WARNING])


class FakeSummarizer:
    """Deterministic summary derived only from the page texts."""

    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self._delay_seconds = delay_seconds

    async def summarize(self, pages: list[PageResult], *, timeout_seconds: float) -> str:
        if self._delay_seconds > 0:
            await asyncio.sleep(self._delay_seconds)
        text = "\n".join(page.content for page in pages)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return (
            "## Summary (fake mode)\n\n"
            f"- pages: {len(pages)}\n"
            f"- characters: {len(text)}\n"
            f"- content sha256: {digest[:16]}\n\n"
            "_Deterministic stand-in: AI_MODE=fake, so no model was called. Set AI_MODE=live and "
            "GEMINI_API_KEY for a real Gemini summary._"
        )

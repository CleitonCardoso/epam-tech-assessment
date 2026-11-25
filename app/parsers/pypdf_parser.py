"""PyPDF text parser: the offline, no-credentials baseline.

Runs entirely off the event loop (``asyncio.to_thread``) and maps every failure to a
``ParserError`` subclass so the worker can decide between retry and terminal failure.
"""

from __future__ import annotations

import asyncio
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


def _parse_sync(pdf: bytes, *, max_pages: int, max_chars: int) -> ParseResult:
    """Blocking part. Raises ParserError subclasses only."""
    try:
        reader = PdfReader(io.BytesIO(pdf))
        # `is_encrypted` is true even when an empty password would unlock the file; we never
        # prompt for a password, so any encryption is treated as unsupported input.
        if reader.is_encrypted:
            raise PermanentError("Password-protected PDFs are not supported.", code="ENCRYPTED_PDF")
        total_pages = len(reader.pages)
    except ParserError:
        raise
    except Exception as exc:  # corrupt xref / truncated file / not a PDF at all
        raise PermanentError("The uploaded file is not a valid PDF.", code="INVALID_PDF") from exc

    if total_pages > max_pages:
        raise PermanentError(
            f"The document has {total_pages} pages; the limit is {max_pages}.",
            code="TOO_MANY_PAGES",
        )

    pages: list[PageResult] = []
    characters = 0
    for index in range(total_pages):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception as exc:
            raise PermanentError(
                f"Page {index + 1} could not be read from the PDF.", code="INVALID_PDF"
            ) from exc
        characters += len(text)
        if characters > max_chars:
            raise PermanentError(
                f"The document exceeds the extraction limit of {max_chars} characters.",
                code="TOO_MUCH_TEXT",
            )
        # Empty pages are kept: page numbers must stay contiguous and the shape must match
        # the source document.
        pages.append(PageResult(page_number=index + 1, format=PageFormat.TEXT, content=text))

    if not any(page.content.strip() for page in pages):
        raise OcrRequiredError("No extractable text; choose the Gemini parser.")

    return ParseResult(pages=pages)


class PyPdfParser:
    """Extract plain text, page by page, with PyPDF."""

    id = ParserId.PYPDF

    async def parse(self, pdf: bytes, *, filename: str, limits: ParseLimits) -> ParseResult:
        # `filename` is part of the frozen interface; the text parser does not need it.
        return await asyncio.to_thread(
            _parse_sync, pdf, max_pages=limits.max_pages, max_chars=limits.max_chars
        )

"""Split a PDF into single-page PDFs.

Shared by every AI-backed parser, because deterministic page attribution is a property of the
prompt strategy, not of the provider: one request per page means the model cannot decide where the
page boundaries are. Blocking work — callers run it through `asyncio.to_thread`.
"""

from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter

from app.parsers.base import OcrRequiredError, PermanentError


def split_pdf_pages(pdf: bytes, *, max_pages: int) -> list[bytes]:
    """Return one single-page PDF per page, in document order."""
    try:
        reader = PdfReader(io.BytesIO(pdf))
    except Exception as exc:
        raise PermanentError("The uploaded file is not a valid PDF.", code="INVALID_PDF") from exc

    if getattr(reader, "is_encrypted", False):
        raise PermanentError("Password-protected PDFs are not supported.", code="ENCRYPTED_PDF")

    try:
        total = len(reader.pages)
    except Exception as exc:
        raise PermanentError("The PDF page tree could not be read.", code="INVALID_PDF") from exc

    if total == 0:
        raise OcrRequiredError("The PDF contains no pages to transcribe.")
    if total > max_pages:
        raise PermanentError(
            f"The document has {total} pages; the AI-parser limit is {max_pages}. "
            "Use the PyPDF parser for large documents.",
            code="TOO_MANY_PAGES_FOR_AI",
        )

    chunks: list[bytes] = []
    for index in range(total):
        try:
            writer = PdfWriter()
            writer.add_page(reader.pages[index])
            buffer = io.BytesIO()
            writer.write(buffer)
            chunks.append(buffer.getvalue())
        except Exception as exc:
            raise PermanentError(
                f"Page {index + 1} could not be isolated for extraction.",
                code="PAGE_SPLIT_FAILED",
            ) from exc
    return chunks

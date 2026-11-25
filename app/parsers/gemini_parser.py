"""Gemini markdown extraction: one model call per page, one page per call.

Why split the PDF instead of sending the whole document in one request: page attribution must be
deterministic. A whole-document call returns one blob and the model decides where pages begin, which
silently mis-attributes content whenever the document has no visible page breaks.

The cost is honest and documented: N calls for an N-page document, which is why this parser is capped
by MAX_AI_PAGES and paced by ProviderPacer.
"""

from __future__ import annotations

import asyncio
import logging

from app.models import PageFormat, PageResult, ParserId
from app.parsers.base import ParseLimits, Parser, ParseResult, PermanentError, TransientError
from app.parsers.pdf_pages import split_pdf_pages
from app.providers.gemini import GeminiClient

logger = logging.getLogger(__name__)

PAGE_PROMPT = """Transcribe this PDF page into faithful GitHub-flavored Markdown.

Rules:
- Preserve the reading order, headings, lists and tables.
- Transcribe only what is on the page. Do not summarize, translate, explain or invent.
- Keep the original language.
- If the page contains no readable text (for example a photo or a blank page), return
  exactly: (no extractable text)
- Return only the transcription, with no preamble and no surrounding code fence.
"""

EMPTY_PAGE_MARKER = "(no extractable text)"


class GeminiParser(Parser):
    id = ParserId.GEMINI

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_concurrency: int = 4,
        max_requests_per_minute: int = 0,
    ) -> None:
        self._client = GeminiClient(
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
            max_requests_per_minute=max_requests_per_minute,
        )
        self._timeout = timeout_seconds

    async def parse(self, pdf: bytes, *, filename: str, limits: ParseLimits) -> ParseResult:
        chunks = await asyncio.to_thread(split_pdf_pages, pdf, max_pages=limits.max_ai_pages)

        pages: list[PageResult] = []
        warnings: list[str] = []

        # Sequential on purpose: free-tier rate limits punish parallel page calls, and the
        # ordering guarantee is then trivial to reason about. Pacing bounds the request rate.
        for number, chunk in enumerate(chunks, start=1):
            content = await self._transcribe_page(number, chunk, limits)
            if not content:
                warnings.append(f"Page {number} produced no extractable text; it was kept empty.")
            pages.append(
                PageResult(page_number=number, format=PageFormat.MARKDOWN, content=content)
            )

        if all(not page.content.strip() for page in pages):
            raise PermanentError(
                "The document produced no extractable text with the Gemini parser.",
                code="OCR_REQUIRED",
            )

        return ParseResult(pages=pages, warnings=warnings)

    async def _transcribe_page(self, number: int, chunk: bytes, limits: ParseLimits) -> str:
        from google.genai import types

        part = types.Part.from_bytes(data=chunk, mime_type="application/pdf")
        try:
            text = await self._client.generate([part, PAGE_PROMPT], timeout_seconds=self._timeout)
        except TransientError:
            raise
        except PermanentError as exc:
            raise PermanentError(
                f"Page {number} could not be transcribed: {exc.message}", code=exc.code
            ) from exc

        text = text.strip()
        if text.strip() == EMPTY_PAGE_MARKER:
            return ""
        if len(text) > limits.max_chars:
            raise PermanentError(
                f"Page {number} exceeded the {limits.max_chars}-character limit.",
                code="TOO_MUCH_TEXT",
            )
        return text

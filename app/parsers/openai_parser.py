"""GPT-5 Mini parser: one model call per page, faithful markdown transcription.

Same contract as the Gemini parser (`Parser` protocol), same page-splitting strategy, different
provider. That is the point of the adapter design: `registry.py` decides which one runs, and the
worker never learns which provider it used.

Live verification needs `OPENAI_API_KEY`; without it the registry reports this parser as disabled
with a reason rather than advertising a capability it cannot exercise.
"""

from __future__ import annotations

import asyncio
import logging

from app.models import PageFormat, PageResult, ParserId
from app.parsers.base import ParseLimits, Parser, ParseResult, PermanentError, TransientError
from app.parsers.pdf_pages import split_pdf_pages
from app.providers.openai_adapter import OpenAIClient

logger = logging.getLogger(__name__)

PAGE_PROMPT = """Transcribe this PDF page into faithful GitHub-flavored Markdown.

Rules:
- Preserve reading order, headings, lists and tables.
- Transcribe only what is on the page. Do not summarize, translate, explain or invent content.
- Keep the original language.
- If the page contains no readable text (a photo or a blank page), return exactly:
  (no extractable text)
- Return only the transcription, with no preamble and no surrounding code fence.
"""

EMPTY_PAGE_MARKER = "(no extractable text)"


class OpenAIParser(Parser):
    id = ParserId.GPT5_MINI

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_concurrency: int = 4,
        max_requests_per_minute: int = 0,
    ) -> None:
        self._client = OpenAIClient(
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

        # Sequential on purpose: free/low tiers rate-limit per minute, and ordering is then
        # trivially correct. The pacer bounds the request rate independently.
        for number, chunk in enumerate(chunks, start=1):
            content = await self._transcribe_page(number, chunk, limits)
            if not content:
                warnings.append(f"Page {number} produced no extractable text; it was kept empty.")
            pages.append(
                PageResult(page_number=number, format=PageFormat.MARKDOWN, content=content)
            )

        if all(not page.content.strip() for page in pages):
            raise PermanentError(
                "The document produced no extractable text with the GPT-5 Mini parser.",
                code="OCR_REQUIRED",
            )

        return ParseResult(pages=pages, warnings=warnings)

    async def _transcribe_page(self, number: int, chunk: bytes, limits: ParseLimits) -> str:
        try:
            text = await self._client.transcribe_page(
                chunk, prompt=PAGE_PROMPT, timeout_seconds=self._timeout
            )
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

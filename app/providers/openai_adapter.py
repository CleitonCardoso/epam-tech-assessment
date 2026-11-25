"""OpenAI adapter (GPT-5 Mini): the only module that knows the OpenAI SDK exists.

The brief lists "GPT-5 Mini (OCR)" as a selectable parser alongside PyPDF and Gemini. This adapter
sends **one page per request** to the Responses API as an inline PDF file input, which is what makes
page attribution deterministic — the same strategy as the Gemini adapter, and the reason the splitter
lives in `parsers/pdf_pages.py` rather than here.

Request shape (Responses API, `openai>=2`):

    input=[{"role": "user", "content": [
        {"type": "input_file", "filename": "page.pdf", "file_data": "data:application/pdf;base64,..."},
        {"type": "input_text", "text": "<prompt>"},
    ]}]

That shape is asserted by `tests/unit/test_openai_parser.py` against a stub, so a regression in the
payload is caught without a key. A **live** call has not been made here: no OpenAI credentials exist
in this environment, and the registry keeps the parser disabled until `OPENAI_API_KEY` is set.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from app.parsers.base import ParserError, PermanentError, TransientError
from app.providers.pacing import ProviderPacer

logger = logging.getLogger(__name__)


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def _retry_after_seconds(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None  # HTTP-date form: ignored rather than guessed


def map_openai_error(exc: BaseException, *, default_code: str = "PROVIDER_ERROR") -> ParserError:
    """Translate any OpenAI SDK failure into the worker's retry/permanent vocabulary."""
    if isinstance(exc, ParserError):
        return exc

    status = _status_code(exc)
    retry_after = _retry_after_seconds(exc)
    text = f"{type(exc).__name__}: {exc}".lower()

    if status is not None:
        if status == 429 or status >= 500:
            return TransientError(
                f"The AI provider is temporarily unavailable (HTTP {status}).",
                code=default_code,
                retry_after=retry_after,
            )
        if status in (401, 403):
            return PermanentError(
                "The AI provider rejected the credentials (check OPENAI_API_KEY).",
                code="PROVIDER_CONFIG",
            )
        return PermanentError(
            f"The AI provider rejected the request (HTTP {status}). "
            "Check OPENAI_MODEL and the account's access to it.",
            code="PROVIDER_CONFIG",
        )

    if any(
        hint in text
        for hint in ("timeout", "timed out", "connection", "unavailable", "429", "rate limit")
    ):
        return TransientError(
            "The AI provider call timed out or lost its connection.",
            code=default_code,
            retry_after=retry_after,
        )
    return TransientError("The AI provider call failed.", code=default_code)


class OpenAIClient:
    """One page in, markdown out. Paced and timeout-bounded like the Gemini client."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_concurrency: int = 4,
        max_requests_per_minute: int = 0,
    ) -> None:
        self._model = model
        self._timeout = timeout_seconds
        # Imported here so a missing/broken SDK disables the provider through the registry
        # instead of crashing the process at import time.
        from openai import AsyncOpenAI

        # The SDK retries 5xx internally by default; one attempt keeps OUR attempt budget and
        # wall-clock timeout authoritative. Same decision as the Gemini adapter.
        self._client = AsyncOpenAI(api_key=api_key, max_retries=0, timeout=timeout_seconds)
        self._pacer = ProviderPacer(
            max_concurrency=max_concurrency,
            max_requests_per_minute=max_requests_per_minute,
        )

    @property
    def model(self) -> str:
        return self._model

    async def transcribe_page(
        self, page_pdf: bytes, *, prompt: str, timeout_seconds: float | None = None
    ) -> str:
        timeout = timeout_seconds or self._timeout
        encoded = base64.b64encode(page_pdf).decode("ascii")
        file_data = f"data:application/pdf;base64,{encoded}"

        async def _call() -> Any:
            return await self._client.responses.create(
                model=self._model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_file",
                                "filename": "page.pdf",
                                "file_data": file_data,
                            },
                            {"type": "input_text", "text": prompt},
                        ],
                    }
                ],
            )

        await self._pacer.acquire()
        try:
            try:
                response = await asyncio.wait_for(_call(), timeout=timeout)
            except TimeoutError as exc:
                raise TransientError(
                    f"The AI provider did not answer within {timeout:.0f}s.",
                    code="PROVIDER_TIMEOUT",
                ) from exc
            except ParserError:
                raise
            except Exception as exc:
                raise map_openai_error(exc) from exc
        finally:
            self._pacer.release()

        text = getattr(response, "output_text", None)
        if not text or not str(text).strip():
            raise PermanentError(
                "The AI provider returned an empty transcription.", code="PROVIDER_EMPTY_RESPONSE"
            )
        return str(text).strip()

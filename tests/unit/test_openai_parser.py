"""GPT-5 Mini adapter and parser tests, without an OpenAI key.

The brief ranks "Document processing via GPT-5 Mini (OCR)" above the frontend, so this parser is
implemented for real. What these tests pin down:

1. the exact request payload sent to the Responses API (a regression here would only show up in
   production, where nobody is watching);
2. page attribution — one request per page, ordered, markdown-labelled;
3. the error classification the worker depends on (retryable vs permanent, Retry-After).

The one thing they cannot cover is a live call: no OpenAI credentials exist in this environment, so
the adapter is exercised against a stub and the registry keeps it disabled until `OPENAI_API_KEY` is
set. That limitation is stated in the README rather than hidden.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.models import PageFormat, ParserId
from app.parsers.base import ParseLimits, PermanentError, TransientError
from app.parsers.openai_parser import EMPTY_PAGE_MARKER, OpenAIParser
from app.providers.openai_adapter import (
    OpenAIClient,
    _retry_after_seconds,
    map_openai_error,
)

LIMITS = ParseLimits(max_pages=10, max_chars=50_000, max_ai_pages=10, timeout_seconds=10)


def _pdf_with_pages(count: int) -> bytes:
    """A minimal valid PDF with `count` blank pages."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class TestRequestPayload:
    def test_sends_one_inline_pdf_page_per_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[dict] = []

        class FakeResponses:
            async def create(self, **kwargs):
                captured.append(kwargs)
                return SimpleNamespace(output_text="## Page\n\ncontent")

        class FakeClient:
            def __init__(self, **_: object) -> None:
                self.responses = FakeResponses()

        import openai

        monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)
        client = OpenAIClient(api_key="sk-test", model="gpt-5-mini", timeout_seconds=10)

        text = asyncio.run(
            client.transcribe_page(b"%PDF-1.4 fake page bytes", prompt="transcribe this")
        )

        assert text == "## Page\n\ncontent"
        assert len(captured) == 1
        payload = captured[0]
        assert payload["model"] == "gpt-5-mini"
        message = payload["input"][0]
        assert message["role"] == "user"
        parts = message["content"]
        assert [part["type"] for part in parts] == ["input_file", "input_text"]
        assert parts[0]["file_data"].startswith("data:application/pdf;base64,")
        assert parts[0]["filename"].endswith(".pdf")
        assert parts[1]["text"] == "transcribe this"

    def test_empty_provider_answer_is_a_permanent_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeResponses:
            async def create(self, **_: object):
                return SimpleNamespace(output_text="   ")

        class FakeClient:
            def __init__(self, **_: object) -> None:
                self.responses = FakeResponses()

        import openai

        monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)
        client = OpenAIClient(api_key="sk-test", model="gpt-5-mini", timeout_seconds=10)

        with pytest.raises(PermanentError) as excinfo:
            asyncio.run(client.transcribe_page(b"%PDF-1.4", prompt="x"))
        assert excinfo.value.code == "PROVIDER_EMPTY_RESPONSE"
        assert excinfo.value.retryable is False


class TestErrorMapping:
    def _api_error(self, status: int, *, headers: dict[str, str] | None = None) -> Exception:
        exc = Exception(f"HTTP {status}")
        exc.status_code = status  # type: ignore[attr-defined]
        exc.response = SimpleNamespace(headers=headers or {})  # type: ignore[attr-defined]
        return exc

    def test_rate_limit_is_transient_and_carries_retry_after(self) -> None:
        mapped = map_openai_error(self._api_error(429, headers={"retry-after": "7"}))
        assert isinstance(mapped, TransientError)
        assert mapped.retry_after == 7.0

    def test_server_error_is_transient(self) -> None:
        assert isinstance(map_openai_error(self._api_error(503)), TransientError)

    def test_bad_key_is_permanent(self) -> None:
        mapped = map_openai_error(self._api_error(401))
        assert isinstance(mapped, PermanentError)
        assert mapped.code == "PROVIDER_CONFIG"

    def test_unknown_model_is_permanent(self) -> None:
        assert isinstance(map_openai_error(self._api_error(404)), PermanentError)

    def test_http_date_retry_after_is_ignored(self) -> None:
        exc = self._api_error(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
        assert _retry_after_seconds(exc) is None


class TestParserContract:
    def _parser(self) -> OpenAIParser:
        return OpenAIParser(api_key="sk-test", model="gpt-5-mini", timeout_seconds=10)

    def test_one_call_per_page_in_markdown_in_order(self) -> None:
        parser = self._parser()
        calls: list[int] = []

        async def fake_transcribe(page: bytes, *, prompt: str, timeout_seconds=None) -> str:
            calls.append(len(calls) + 1)
            return f"## Page {len(calls)}\n\ntext"

        parser._client.transcribe_page = fake_transcribe  # type: ignore[method-assign]
        result = asyncio.run(parser.parse(_pdf_with_pages(3), filename="x.pdf", limits=LIMITS))

        assert calls == [1, 2, 3], "the parser must call the provider once per page, in order"
        assert [page.page_number for page in result.pages] == [1, 2, 3]
        assert all(page.format is PageFormat.MARKDOWN for page in result.pages)
        assert parser.id is ParserId.GPT5_MINI

    def test_blank_page_marker_becomes_an_empty_page_with_a_warning(self) -> None:
        parser = self._parser()
        seen = {"n": 0}

        async def fake_transcribe(page: bytes, *, prompt: str, timeout_seconds=None) -> str:
            seen["n"] += 1
            # First page is a scan with no text, second page has content: a mixed document must
            # keep its page numbering and report the blank page instead of failing the document.
            return EMPTY_PAGE_MARKER if seen["n"] == 1 else "## Page 2\n\ntext"

        parser._client.transcribe_page = fake_transcribe  # type: ignore[method-assign]
        result = asyncio.run(parser.parse(_pdf_with_pages(2), filename="x.pdf", limits=LIMITS))

        assert [page.page_number for page in result.pages] == [1, 2]
        assert result.pages[0].content == ""
        assert result.pages[1].content.startswith("## Page 2")
        assert result.warnings, "a page with no text should be reported, not silently dropped"

    def test_a_document_with_no_text_at_all_is_ocr_required(self) -> None:
        parser = self._parser()

        async def fake_transcribe(page: bytes, *, prompt: str, timeout_seconds=None) -> str:
            return ""

        parser._client.transcribe_page = fake_transcribe  # type: ignore[method-assign]
        with pytest.raises(PermanentError) as excinfo:
            asyncio.run(parser.parse(_pdf_with_pages(2), filename="x.pdf", limits=LIMITS))
        assert excinfo.value.code == "OCR_REQUIRED"

    def test_transient_failure_propagates_so_the_worker_retries(self) -> None:
        parser = self._parser()

        async def fake_transcribe(page: bytes, *, prompt: str, timeout_seconds=None) -> str:
            raise TransientError("rate limited", code="PROVIDER_ERROR", retry_after=2)

        parser._client.transcribe_page = fake_transcribe  # type: ignore[method-assign]
        with pytest.raises(TransientError) as excinfo:
            asyncio.run(parser.parse(_pdf_with_pages(1), filename="x.pdf", limits=LIMITS))
        assert excinfo.value.retryable is True
        assert excinfo.value.retry_after == 2

    def test_permanent_page_failure_names_the_page(self) -> None:
        parser = self._parser()

        async def fake_transcribe(page: bytes, *, prompt: str, timeout_seconds=None) -> str:
            raise PermanentError("blocked", code="PROVIDER_REJECTED")

        parser._client.transcribe_page = fake_transcribe  # type: ignore[method-assign]
        with pytest.raises(PermanentError) as excinfo:
            asyncio.run(parser.parse(_pdf_with_pages(1), filename="x.pdf", limits=LIMITS))
        assert "Page 1" in excinfo.value.message
        assert excinfo.value.code == "PROVIDER_REJECTED"

    def test_the_ai_page_cap_is_enforced_before_any_call(self) -> None:
        parser = self._parser()
        calls = []

        async def fake_transcribe(page: bytes, *, prompt: str, timeout_seconds=None) -> str:
            calls.append(1)
            return "text"

        parser._client.transcribe_page = fake_transcribe  # type: ignore[method-assign]
        tiny_limit = ParseLimits(max_pages=50, max_chars=50_000, max_ai_pages=1, timeout_seconds=10)
        with pytest.raises(PermanentError) as excinfo:
            asyncio.run(parser.parse(_pdf_with_pages(3), filename="x.pdf", limits=tiny_limit))
        assert excinfo.value.code == "TOO_MANY_PAGES_FOR_AI"
        assert calls == [], "the page cap must be checked before spending any provider call"

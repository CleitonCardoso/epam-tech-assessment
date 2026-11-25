"""Builds parser/summarizer instances from settings.

The registry is the only place that decides which provider is *enabled*. The API reports that
decision verbatim, so an unimplemented or unconfigured provider can never be advertised as
working — it shows up as `enabled: false` with a reason, or as a 503 at upload time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.models import PageFormat, ParserId, ParserInfo
from app.parsers.base import ParseLimits, Parser, Summarizer

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

_LABELS = {
    ParserId.PYPDF: ("PyPDF", PageFormat.TEXT, "Local extract_text(); returns plain text, not markdown."),
    ParserId.GEMINI: ("Google Gemini", PageFormat.MARKDOWN, "Per-page markdown transcription."),
    ParserId.GPT5_MINI: ("GPT-5 Mini", PageFormat.MARKDOWN, "Optional provider; enabled only when configured."),
}


class ParserUnavailable(RuntimeError):
    """Requested parser cannot run in the current configuration."""


@dataclass(slots=True)
class _Entry:
    parser_id: ParserId
    available: bool
    handler: Parser | None
    note: str | None
    requires_api_key: bool


class ProviderRegistry:
    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        self._entries: dict[ParserId, _Entry] = {}
        self._summarizer: Summarizer | None = None
        self._fake = settings.ai_mode == "fake"
        self._build()

    # ── construction ───────────────────────────────────────────────────────────
    def _build(self) -> None:
        self._build_pypdf()
        self._build_gemini()
        self._build_openai()
        self._build_summarizer()

    def _build_pypdf(self) -> None:
        try:
            from app.parsers.pypdf_parser import PyPdfParser

            self._entries[ParserId.PYPDF] = _Entry(
                ParserId.PYPDF, True, PyPdfParser(), None, False
            )
        except Exception as exc:  # noqa: BLE001 - an import failure must not stop the app
            logger.error("parser_unavailable parser=pypdf error=%s", exc)
            self._entries[ParserId.PYPDF] = _Entry(
                ParserId.PYPDF, False, None, f"adapter unavailable: {type(exc).__name__}", False
            )

    def _fake_parser(self, parser_id: ParserId) -> Parser | None:
        try:
            from app.providers.fake import FakeParser

            return FakeParser(parser_id, delay_seconds=self._settings.fake_delay_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.error("parser_unavailable parser=%s error=%s", parser_id.value, exc)
            return None

    def _build_gemini(self) -> None:
        if self._fake:
            handler = self._fake_parser(ParserId.GEMINI)
            self._entries[ParserId.GEMINI] = _Entry(
                ParserId.GEMINI, handler is not None, handler,
                None if handler else "fake provider unavailable", True,
            )
            return
        if not self._settings.gemini_api_key:
            self._entries[ParserId.GEMINI] = _Entry(
                ParserId.GEMINI, False, None, "GEMINI_API_KEY is not configured", True
            )
            return
        try:
            from app.parsers.gemini_parser import GeminiParser

            handler = GeminiParser(
                api_key=self._settings.gemini_api_key,
                model=self._settings.gemini_model,
                timeout_seconds=self._settings.provider_timeout_seconds,
                max_concurrency=self._settings.provider_max_concurrency,
                max_requests_per_minute=self._settings.provider_max_requests_per_minute,
            )
            self._entries[ParserId.GEMINI] = _Entry(ParserId.GEMINI, True, handler, None, True)
        except Exception as exc:  # noqa: BLE001
            logger.error("parser_unavailable parser=gemini error=%s", exc)
            self._entries[ParserId.GEMINI] = _Entry(
                ParserId.GEMINI, False, None, f"adapter unavailable: {type(exc).__name__}", True
            )

    def _build_openai(self) -> None:
        # The unimplemented check comes FIRST, before the fake-mode branch. A parser with no
        # implementation must never be advertised as usable, not even as a labelled stand-in:
        # "enabled" would imply the capability exists. Fake mode substitutes only providers that
        # have a real implementation to stand in for.
        try:
            from app.parsers import openai_parser
        except Exception as exc:  # noqa: BLE001
            logger.error("parser_unavailable parser=gpt-5-mini error=%s", exc)
            self._entries[ParserId.GPT5_MINI] = _Entry(
                ParserId.GPT5_MINI, False, None, f"adapter unavailable: {type(exc).__name__}", True
            )
            return

        if not getattr(openai_parser, "IMPLEMENTED", False):
            # Never advertise a provider whose adapter is a declaration only. The reason is
            # part of the API surface, so a reviewer can see why it is off.
            self._entries[ParserId.GPT5_MINI] = _Entry(
                ParserId.GPT5_MINI,
                False,
                None,
                "not implemented in this delivery; use pypdf or gemini",
                True,
            )
            return

        if self._fake:
            handler = self._fake_parser(ParserId.GPT5_MINI)
            self._entries[ParserId.GPT5_MINI] = _Entry(
                ParserId.GPT5_MINI, handler is not None, handler,
                None if handler else "fake provider unavailable", True,
            )
            return

        if not self._settings.openai_api_key:
            self._entries[ParserId.GPT5_MINI] = _Entry(
                ParserId.GPT5_MINI, False, None, "OPENAI_API_KEY is not configured", True
            )
            return
        try:
            handler = openai_parser.OpenAIParser(
                api_key=self._settings.openai_api_key,
                model=self._settings.openai_model,
                timeout_seconds=self._settings.provider_timeout_seconds,
            )
            self._entries[ParserId.GPT5_MINI] = _Entry(
                ParserId.GPT5_MINI, True, handler, None, True
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("parser_unavailable parser=gpt-5-mini error=%s", exc)
            self._entries[ParserId.GPT5_MINI] = _Entry(
                ParserId.GPT5_MINI, False, None, f"adapter unavailable: {type(exc).__name__}", True
            )

    def _build_summarizer(self) -> None:
        if self._fake:
            try:
                from app.providers.fake import FakeSummarizer

                self._summarizer = FakeSummarizer(delay_seconds=self._settings.fake_delay_seconds)
            except Exception as exc:  # noqa: BLE001
                logger.error("summarizer_unavailable error=%s", exc)
            return
        if not self._settings.gemini_api_key:
            return
        try:
            from app.providers.gemini import GeminiSummarizer

            self._summarizer = GeminiSummarizer(
                api_key=self._settings.gemini_api_key,
                model=self._settings.gemini_model,
                timeout_seconds=self._settings.provider_timeout_seconds,
                max_concurrency=self._settings.provider_max_concurrency,
                max_requests_per_minute=self._settings.provider_max_requests_per_minute,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("summarizer_unavailable error=%s", exc)

    # ── queries ────────────────────────────────────────────────────────────────
    @property
    def ai_mode(self) -> str:
        return self._settings.ai_mode

    def limits(self) -> ParseLimits:
        return ParseLimits(
            max_pages=self._settings.max_pages,
            max_chars=self._settings.max_extracted_chars,
            max_ai_pages=self._settings.max_ai_pages,
            timeout_seconds=self._settings.provider_timeout_seconds,
        )

    def get_parser(self, parser_id: ParserId) -> Parser:
        blocking = self._blocking_reason()
        if blocking is not None:
            raise ParserUnavailable(blocking)
        entry = self._entries.get(parser_id)
        if entry is None or not entry.available or entry.handler is None:
            reason = entry.note if entry else "unknown parser"
            raise ParserUnavailable(f"Parser {parser_id.value!r} is not available: {reason}")
        return entry.handler

    def _blocking_reason(self) -> str | None:
        """Why no document can be processed at all, or None when work can be accepted.

        Every document must end with a summary, and the assessment mandates Gemini for it. If
        summarization cannot run, *every* parser would produce a job that is guaranteed to fail
        after the expensive extraction has already happened. Refusing the upload up front is the
        difference between an honest 503 and a job that lies about being accepted.
        """
        if self._fake:
            return None
        ready, detail = self.summarizer_ready()
        if ready:
            return None
        return (
            f"summarization is unavailable ({detail}) and every document requires a summary; "
            "set GEMINI_API_KEY, or set AI_MODE=fake to run without credentials"
        )

    def get_summarizer(self) -> Summarizer:
        if self._summarizer is None:
            if self._fake:
                raise ParserUnavailable("Fake summarizer could not be constructed")
            raise ParserUnavailable(
                "Summarization requires GEMINI_API_KEY (the assessment mandates Gemini for summaries)"
            )
        return self._summarizer

    def summarizer_ready(self) -> tuple[bool, str]:
        if self._summarizer is not None:
            return True, "fake summarizer" if self._fake else f"gemini:{self._settings.gemini_model}"
        if self._fake:
            return False, "fake summarizer unavailable"
        return False, "gemini_api_key not configured"

    def describe(self) -> list[ParserInfo]:
        out: list[ParserInfo] = []
        blocking = self._blocking_reason()
        for parser_id in (ParserId.PYPDF, ParserId.GEMINI, ParserId.GPT5_MINI):
            entry = self._entries.get(parser_id)
            label, output_format, base_note = _LABELS[parser_id]
            if entry is None:
                out.append(
                    ParserInfo(
                        id=parser_id, label=label, output_format=output_format,
                        enabled=False, requires_api_key=False, note="not registered",
                    )
                )
                continue
            note = entry.note or base_note
            # Only the AI parsers are substituted in fake mode. PyPDF is always the real
            # local extractor, and saying otherwise would be a lie in the API surface.
            if entry.available and self._fake and entry.requires_api_key:
                note = f"fake mode: deterministic stand-in, no model call. {base_note}"
            if blocking is not None:
                # Nothing can finish a document, so advertise nothing as usable.
                note = blocking
            out.append(
                ParserInfo(
                    id=parser_id,
                    label=label,
                    output_format=output_format,
                    enabled=entry.available and blocking is None,
                    requires_api_key=entry.requires_api_key,
                    note=note,
                )
            )
        return out

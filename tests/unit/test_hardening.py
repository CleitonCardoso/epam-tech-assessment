"""Regression tests for the hardening found by the load and fault-injection runs.

Two failures motivated these:

1. A transient Redis error during commit was classified as INTERNAL_ERROR and permanently failed a
   document. A dependency blip must never be terminal.
2. Under a 20-document burst the provider answered 429/503, our fixed 2/5/10s backoff spent the
   whole retry budget, and the `Retry-After` header was ignored.

These are pure unit tests: no Redis, no network, no Docker.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.parsers.base import ParserError, PermanentError, TransientError
from app.providers.gemini import _retry_after_seconds, map_provider_error
from app.worker import Worker


class _Headers(dict):
    pass


class _Response:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = _Headers(headers)


class _ProviderError(Exception):
    def __init__(self, message: str, *, code: int | None = None, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.code = code
        self.response = _Response(headers or {})


def _worker(**overrides) -> Worker:
    """A Worker shell: only the retry-delay policy is exercised, so no collaborators needed."""
    worker = object.__new__(Worker)
    worker._settings = Settings(_env_file=None, **overrides)
    return worker


class TestRetryAfterParsing:
    def test_reads_the_header_as_seconds(self) -> None:
        exc = _ProviderError("429", code=429, headers={"retry-after": "7"})
        assert _retry_after_seconds(exc) == 7.0

    def test_ignores_http_date_form_instead_of_guessing(self) -> None:
        exc = _ProviderError("429", code=429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
        assert _retry_after_seconds(exc) is None

    def test_absent_header_is_none(self) -> None:
        assert _retry_after_seconds(_ProviderError("boom")) is None


class TestProviderErrorMapping:
    def test_429_is_transient_and_carries_retry_after(self) -> None:
        mapped = map_provider_error(
            _ProviderError("rate limited", code=429, headers={"retry-after": "3"}),
            default_code="PROVIDER_ERROR",
        )
        assert isinstance(mapped, TransientError)
        assert mapped.retryable is True
        assert mapped.retry_after == 3.0

    def test_503_is_transient(self) -> None:
        mapped = map_provider_error(_ProviderError("unavailable", code=503), default_code="PROVIDER_ERROR")
        assert isinstance(mapped, TransientError)

    def test_401_is_permanent(self) -> None:
        mapped = map_provider_error(_ProviderError("bad key", code=401), default_code="PROVIDER_ERROR")
        assert isinstance(mapped, PermanentError)
        assert mapped.retryable is False

    def test_a_parser_error_passes_through_unchanged(self) -> None:
        original = PermanentError("nope", code="ENCRYPTED_PDF")
        assert map_provider_error(original, default_code="PROVIDER_ERROR") is original


class TestRetryDelayPolicy:
    def test_provider_retry_after_is_honoured(self) -> None:
        worker = _worker(retry_after_max_seconds=30)
        exc = TransientError("slow down", retry_after=5)
        assert worker._retry_delay(exc, 1) == pytest.approx(5.0)

    def test_retry_after_is_clamped(self) -> None:
        worker = _worker(retry_after_max_seconds=10)
        exc = TransientError("come back tomorrow", retry_after=600)
        assert worker._retry_delay(exc, 1) == pytest.approx(10.0)

    def test_backoff_grows_and_stays_jittered(self) -> None:
        worker = _worker(retry_backoff_seconds=(2.0, 5.0, 10.0))
        for attempt, expected in ((1, 2.0), (2, 5.0), (4, 10.0)):
            delay = worker._retry_delay(TransientError("x"), attempt)
            # +/-20% jitter around the configured backoff, so workers do not retry in lockstep.
            assert expected * 0.8 <= delay <= expected * 1.2


class TestSettingsForPacing:
    def test_defaults_are_sane(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.provider_max_concurrency >= 1
        assert settings.provider_max_requests_per_minute == 0  # disabled unless configured
        assert settings.retry_after_max_seconds > 0

    def test_zero_concurrency_is_rejected(self) -> None:
        with pytest.raises(Exception):
            Settings(_env_file=None, provider_max_concurrency=0)


class TestParserErrorContract:
    def test_retry_after_defaults_to_none(self) -> None:
        assert ParserError("x").retry_after is None

    def test_retry_after_is_preserved(self) -> None:
        assert TransientError("x", retry_after=12.5).retry_after == 12.5

"""Contract tests: the models and settings every other module depends on.

These are deliberately dependency-free (no Redis, no S3, no network) so they can run anywhere,
including in the `--profile test` container before the stack is up.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import (
    DocumentDetail,
    DocumentJob,
    DocumentStatus,
    ErrorInfo,
    ParserId,
    PageFormat,
    PageResult,
    utc_now_iso,
)


def make_settings(**overrides) -> Settings:
    # _env_file=None keeps a developer's .env out of the assertions; explicit kwargs win over
    # the environment so the tests are deterministic either way.
    return Settings(_env_file=None, **overrides)


class TestModels:
    def test_parser_ids_match_the_api(self) -> None:
        assert [p.value for p in ParserId] == ["pypdf", "gemini", "gpt-5-mini"]

    def test_terminal_statuses(self) -> None:
        assert DocumentStatus.COMPLETED.is_terminal
        assert DocumentStatus.FAILED.is_terminal
        assert not DocumentStatus.QUEUED.is_terminal
        assert not DocumentStatus.PARSING.is_terminal
        assert not DocumentStatus.SUMMARIZING.is_terminal
        assert not DocumentStatus.RETRYING.is_terminal

    def test_timestamp_is_utc_z(self) -> None:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", utc_now_iso())

    def test_page_number_is_one_based(self) -> None:
        with pytest.raises(ValidationError):
            PageResult(page_number=0, format=PageFormat.TEXT, content="x")

    def test_page_formats_are_distinct(self) -> None:
        text = PageResult(page_number=1, format=PageFormat.TEXT, content="plain")
        markdown = PageResult(page_number=2, format=PageFormat.MARKDOWN, content="# md")
        assert text.format.value == "text"
        assert markdown.format.value == "markdown"

    def test_document_detail_composes_job_and_result(self) -> None:
        job = DocumentJob(
            id="11111111-1111-1111-1111-111111111111",
            filename="a.pdf",
            parser=ParserId.PYPDF,
            source_key="uploads/x/source.pdf",
        )
        detail = DocumentDetail(
            **job.model_dump(),
            pages=[PageResult(page_number=1, format=PageFormat.TEXT, content="hi")],
            summary="summary",
        )
        assert detail.status is DocumentStatus.QUEUED
        assert detail.pages[0].page_number == 1
        assert detail.error is None

    def test_error_info_round_trips(self) -> None:
        info = ErrorInfo(code="OCR_REQUIRED", message="Use Gemini.", retryable=False)
        assert ErrorInfo.model_validate_json(info.model_dump_json()) == info


class TestSettingsValidation:
    def test_defaults_are_compose_happy(self) -> None:
        settings = make_settings()
        assert settings.redis_url.startswith("redis://redis:6379")
        assert settings.s3_endpoint_url == "http://localstack:4566"
        assert settings.stream_key == "docs:jobs"
        assert settings.consumer_group == "document-workers"
        assert settings.heartbeat_seconds < settings.lease_seconds

    def test_heartbeat_must_be_lower_than_lease(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(heartbeat_seconds=90, lease_seconds=90)

    def test_lease_must_fit_the_deadline(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(lease_seconds=300, job_deadline_seconds=600)

    def test_ai_pages_cannot_exceed_pages(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(max_ai_pages=100, max_pages=50)

    def test_batch_must_admit_one_max_file(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(max_batch_bytes=1024, max_upload_bytes=2048)

    def test_reclaim_idle_defaults_to_the_lease(self) -> None:
        settings = make_settings(lease_seconds=30, job_deadline_seconds=600)
        assert settings.effective_reclaim_idle_ms == 30_000

    def test_ai_availability_by_mode(self) -> None:
        assert make_settings(ai_mode="fake", gemini_api_key="").ai_available
        assert make_settings(ai_mode="live", gemini_api_key="").ai_available is False
        assert make_settings(ai_mode="live", gemini_api_key="k").ai_available is True

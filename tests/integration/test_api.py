"""Acceptance tests against a live stack.

Each test asserts an outcome, not that a mock was called. Run with:

    docker compose up -d
    docker compose --profile test run --rm \
        -e API_BASE_URL=http://api:8000 test \
        python -m pytest tests/integration -m integration -q

or from the host, with API_BASE_URL=http://localhost:8000.
"""

from __future__ import annotations

import time

import pytest

from tests.conftest import (
    JOB_TIMEOUT,
    sample,
    submit,
    unique_name,
    upload_and_wait,
    wait_for_terminal,
)

pytestmark = pytest.mark.integration


class TestReadiness:
    def test_live_is_cheap_and_ready_reports_dependencies(self, client) -> None:
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json() == {"status": "ok"}

        ready = client.get("/health/ready")
        assert ready.status_code == 200, ready.text
        checks = ready.json()["checks"]
        assert checks["redis"] == "ok"
        assert "ok" in checks["s3"]
        assert checks["ai_mode"] in {"live", "fake"}

    def test_parsers_endpoint_only_advertises_what_works(self, client) -> None:
        payload = client.get("/api/v1/parsers").json()
        assert payload["ai_mode"] in {"live", "fake"}
        by_id = {entry["id"]: entry for entry in payload["parsers"]}
        assert "pypdf" in by_id and by_id["pypdf"]["enabled"] is True
        for entry in payload["parsers"]:
            if not entry["enabled"]:
                # A disabled parser must say why; an unexplained gap is a lie by omission.
                assert entry["note"], f"{entry['id']} is disabled without a reason"


class TestHappyPath:
    def test_upload_completes_with_ordered_pages_and_one_summary(self, client) -> None:
        document = upload_and_wait(client, [sample("sample-text.pdf")], "pypdf")

        assert document["status"] == "completed"
        assert document["error"] is None
        assert document["page_count"] >= 2
        assert len(document["pages"]) == document["page_count"]
        assert [page["page_number"] for page in document["pages"]] == list(
            range(1, document["page_count"] + 1)
        )
        for page in document["pages"]:
            # PyPDF never produces markdown, and the API must not mislabel it.
            assert page["format"] == "text"
            assert page["content"].strip()
        assert document["summary"] and document["summary"].strip()
        assert document["filename"] == "sample-text.pdf"

    def test_gemini_parser_returns_markdown_pages(self, client, ai_mode) -> None:
        document = upload_and_wait(client, [sample("sample-text.pdf")], "gemini")

        if document["status"] != "completed":
            code = (document.get("error") or {}).get("code")
            if code in {"PROVIDER_ERROR", "PROVIDER_TIMEOUT"}:
                # The provider throttled or dropped the call. That is a property of the world, not
                # of this codebase, so report it as skipped rather than pretending the app failed.
                # The opt-in live test (test_live_ai.py) is the strict gate for the provider path;
                # see docs/PERFORMANCE.md for the pacing knobs that make these retries succeed.
                pytest.skip(f"provider unavailable during this run ({code})")
            pytest.fail(f"gemini parser failed with {code}: {document.get('error')}")

        assert document["page_count"] >= 1
        assert all(page["format"] == "markdown" for page in document["pages"])
        if ai_mode == "fake":
            # The stand-in must be labelled on the document itself, not only in the UI.
            assert any("ai_mode=fake" in warning for warning in document["warnings"])


class TestBatchIndependence:
    def test_one_corrupt_file_does_not_block_the_valid_one(self, client) -> None:
        response = submit(client, [sample("sample-text.pdf"), sample("sample-corrupt.pdf")])

        assert response.status_code == 202, response.text
        payload = response.json()
        assert len(payload["documents"]) == 1
        assert len(payload["rejected"]) == 1
        assert payload["rejected"][0]["filename"] == "sample-corrupt.pdf"
        assert payload["rejected"][0]["code"] == "NOT_A_PDF"

        finished = wait_for_terminal(client, payload["documents"][0]["id"])
        assert finished["status"] == "completed"


class TestValidation:
    def test_unknown_parser_is_rejected(self, client) -> None:
        response = submit(client, [sample("sample-text.pdf")], "not-a-parser")
        assert response.status_code == 422
        assert "not-a-parser" in response.text

    def test_corrupt_file_only_is_rejected_with_a_reason(self, client) -> None:
        response = submit(client, [sample("sample-corrupt.pdf")])
        assert response.status_code == 422
        rejected = response.json()["rejected"]
        assert rejected[0]["code"] == "NOT_A_PDF"

    def test_no_files_is_rejected(self, client) -> None:
        response = client.post("/api/v1/documents", data={"parser": "pypdf"})
        assert response.status_code == 422

    def test_oversized_file_is_refused(self, client) -> None:
        # Just over the documented 20 MB per-file limit, with a valid PDF signature so the
        # rejection is about size and nothing else.
        oversized = b"%PDF-1.4\n" + b"0" * (21 * 1024 * 1024)
        response = client.post(
            "/api/v1/documents",
            files=[("files", ("big.pdf", oversized, "application/pdf"))],
            data={"parser": "pypdf"},
        )
        assert response.status_code == 413, response.text

    def test_unknown_document_id_is_404(self, client) -> None:
        response = client.get("/api/v1/documents/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404


class TestFailureBehaviour:
    def test_blank_pdf_fails_with_ocr_required_and_keeps_a_stable_code(self, client) -> None:
        document = upload_and_wait(client, [sample("sample-blank.pdf")], "pypdf")

        assert document["status"] == "failed"
        assert document["error"]["code"] == "OCR_REQUIRED"
        assert document["error"]["retryable"] is False
        # A failed terminal state must not leak a stack trace or provider payload.
        assert "Traceback" not in document["error"]["message"]


class TestConcurrency:
    def test_api_stays_responsive_while_a_job_is_queued(self, client) -> None:
        """The assessment's first requirement: concurrent requests while work is processing.

        This does not need a slow provider — it asserts that upload + status + health all
        answer quickly right after a job is accepted, and that the job still finishes.
        """
        started = time.monotonic()
        response = submit(client, [sample("sample-text.pdf")], "pypdf")
        assert response.status_code == 202
        document_id = response.json()["documents"][0]["id"]

        for path in ("/health/live", f"/api/v1/documents/{document_id}"):
            call_started = time.monotonic()
            probe = client.get(path)
            elapsed = time.monotonic() - call_started
            assert probe.status_code == 200
            assert elapsed < 2.0, f"{path} took {elapsed:.2f}s while a job was queued"

        finished = wait_for_terminal(client, document_id, timeout=JOB_TIMEOUT)
        assert finished["status"] == "completed"
        assert time.monotonic() - started < JOB_TIMEOUT


class TestIdempotentAcceptance:
    def test_two_uploads_of_the_same_file_get_independent_ids(self, client) -> None:
        first = submit(client, [sample("sample-text.pdf")], "pypdf")
        second = submit(client, [sample("sample-text.pdf")], "pypdf")
        assert first.status_code == second.status_code == 202

        ids = {
            first.json()["documents"][0]["id"],
            second.json()["documents"][0]["id"],
        }
        assert len(ids) == 2, "each accepted file must receive its own id"

        for document_id in ids:
            assert wait_for_terminal(client, document_id)["status"] == "completed"

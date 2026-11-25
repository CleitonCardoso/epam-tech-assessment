"""Opt-in live-AI verification — skipped unless real credentials are present.

The assessment mandates Gemini for markdown extraction and for the document summary. Nothing in
the default test run may call a paid provider, so this file is skipped unless both of these are
true:

    AI_MODE=live  and a real GEMINI_API_KEY     (in .env)
    RUN_LIVE_AI_TESTS=1                         (exported for the test run)

Run it after setting the key:

    docker compose up -d --force-recreate api worker
    docker compose --profile test run --rm \
        -e API_BASE_URL=http://api:8000 -e RUN_LIVE_AI_TESTS=1 \
        -v "$(pwd)/samples:/app/samples:ro" \
        test python -m pytest tests/integration/test_live_ai.py -q

Skipping is reported as skipped, never as passed. That is the point: a claim of "Gemini works"
must be backed by a call that actually happened.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import sample, upload_and_wait

LIVE_ENABLED = os.getenv("RUN_LIVE_AI_TESTS") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not LIVE_ENABLED,
        reason="live AI disabled; set RUN_LIVE_AI_TESTS=1 with a real GEMINI_API_KEY",
    ),
]


def test_live_gemini_extracts_markdown_and_summarises(client) -> None:
    ai_mode = client.get("/api/v1/parsers").json()["ai_mode"]
    assert ai_mode == "live", (
        "RUN_LIVE_AI_TESTS=1 needs the stack in AI_MODE=live with a real key; "
        f"the API reports ai_mode={ai_mode!r}"
    )

    document = upload_and_wait(client, [sample("sample-text.pdf")], "gemini", timeout=180)

    assert document["status"] == "completed", document.get("error")
    assert document["page_count"] >= 2

    # Markdown came from the model, not from the deterministic stand-in.
    assert all(page["format"] == "markdown" for page in document["pages"])
    assert not any("ai_mode=fake" in warning for warning in document["warnings"])

    summary = document["summary"] or ""
    assert summary.strip(), "a completed document must carry a summary"
    assert "fake mode" not in summary.lower(), "the summary came from the stand-in, not Gemini"

    # The transcription must reflect the real page content, not a placeholder.
    first_page = document["pages"][0]["content"].lower()
    assert "page 1" in first_page
    assert "fake markdown" not in first_page

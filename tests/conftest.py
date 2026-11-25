"""Helpers shared by the test suites.

Deliberately contains **no** session-wide autouse fixture: `pytest tests/unit` must run offline,
with no Redis, no S3 and no running API. The fixtures that need a live stack live in
`tests/integration/conftest.py`, so they only apply to the tests that ask for them.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import httpx
import pytest

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
SAMPLES = Path(__file__).resolve().parent.parent / "samples"
READY_TIMEOUT = float(os.getenv("TEST_READY_TIMEOUT", "60"))
JOB_TIMEOUT = float(os.getenv("TEST_JOB_TIMEOUT", "60"))
TERMINAL = {"completed", "failed"}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: needs a running Compose stack")


def sample(name: str) -> Path:
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"sample {name} missing; run python scripts/make_samples.py")
    return path


def submit(
    client: httpx.Client,
    files: list[Path],
    parser: str = "pypdf",
    *,
    extra_forms: dict[str, str] | None = None,
) -> httpx.Response:
    handles = [("files", (path.name, path.read_bytes(), "application/pdf")) for path in files]
    data = {"parser": parser}
    if extra_forms:
        data.update(extra_forms)
    return client.post("/api/v1/documents", files=handles, data=data)


def wait_for_terminal(client: httpx.Client, document_id: str, timeout: float = JOB_TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/documents/{document_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in TERMINAL:
            return body
        time.sleep(0.3)
    pytest.fail(f"document {document_id} did not finish within {timeout}s (last: {body})")


def upload_and_wait(
    client: httpx.Client, files: list[Path], parser: str = "pypdf", *, timeout: float = JOB_TIMEOUT
) -> dict:
    response = submit(client, files, parser)
    assert response.status_code == 202, response.text
    document = response.json()["documents"][0]
    return wait_for_terminal(client, document["id"], timeout=timeout)


def unique_name(prefix: str = "t") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"

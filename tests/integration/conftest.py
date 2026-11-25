"""Fixtures that genuinely need a running stack — scoped to the integration suite.

Keeping them here (and not in `tests/conftest.py`) is what allows `pytest tests/unit` to run
offline: a session-wide autouse fixture in the parent conftest would make every unit test wait
for an API that is not supposed to exist.
"""

from __future__ import annotations

import time

import httpx
import pytest

from tests.conftest import API_BASE_URL, READY_TIMEOUT


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    with httpx.Client(base_url=API_BASE_URL, timeout=30.0) as http:
        yield http


@pytest.fixture(scope="session", autouse=True)
def stack_ready(client: httpx.Client) -> None:
    """Fail fast with a useful message instead of a wall of connection errors."""
    deadline = time.monotonic() + READY_TIMEOUT
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            response = client.get("/health/ready")
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    pytest.exit(
        f"The stack at {API_BASE_URL} never became ready ({last_error}). "
        "Start it with: docker compose up -d",
        returncode=1,
    )


@pytest.fixture(scope="session")
def ai_mode(client: httpx.Client) -> str:
    return client.get("/api/v1/parsers").json()["ai_mode"]

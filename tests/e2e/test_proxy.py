"""End-to-end checks through nginx, the way a reviewer actually reaches the app.

Everything else in this repository talks to `api:8000` directly. This file goes through the
frontend container instead, which is where the interesting failures live: a proxy that forwards
wrongly, an `/api` path that falls through to the SPA, an asset that 404s after a rebuild.

Run (inside the Compose network, the frontend service must be up):

    docker compose --profile test run --rm -e FRONTEND_URL=http://frontend:80 \
        -v "$(pwd)/samples:/app/samples:ro" test \
        python -m pytest tests/e2e -m e2e -q
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

import httpx
import pytest

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://frontend:80").rstrip("/")
SAMPLES = Path(__file__).resolve().parents[2] / "samples"
SETTLE_TIMEOUT = 90.0
ASSET_RE = re.compile(r'(?:src|href)="(/assets/[^"]+)"')

# `integration` keeps these out of the offline unit run; `e2e` selects them explicitly.
pytestmark = [pytest.mark.integration, pytest.mark.e2e]


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    with httpx.Client(base_url=FRONTEND_URL, timeout=30.0, follow_redirects=True) as http:
        try:
            http.get("/")
        except httpx.HTTPError as exc:
            pytest.exit(f"frontend not reachable at {FRONTEND_URL}: {exc}", returncode=1)
        yield http


class TestShell:
    def test_spa_shell_is_served(self, client: httpx.Client) -> None:
        response = client.get("/")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/html")
        body = response.text
        assert 'id="root"' in body, "the SPA mount point is missing"
        # index.html must never be cached, or clients keep a bundle whose assets were deleted.
        assert "no-store" in response.headers.get("cache-control", ""), response.headers

    def test_every_referenced_asset_loads(self, client: httpx.Client) -> None:
        shell = client.get("/").text
        assets = sorted(set(ASSET_RE.findall(shell)))
        assert assets, f"the shell references no hashed assets: {shell[:200]}"

        for asset in assets:
            response = client.get(asset)
            assert response.status_code == 200, f"{asset} -> {response.status_code}"
            assert len(response.content) > 100, f"{asset} is suspiciously small"

    def test_unknown_route_falls_back_to_the_spa(self, client: httpx.Client) -> None:
        response = client.get("/uma-rota-que-nao-existe")
        assert response.status_code == 200
        assert 'id="root"' in response.text


class TestApiThroughProxy:
    def test_health_is_proxied(self, client: httpx.Client) -> None:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_parsers_are_proxied_as_json(self, client: httpx.Client) -> None:
        response = client.get("/api/v1/parsers")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        payload = response.json()
        assert payload["ai_mode"] in {"live", "fake"}
        assert any(entry["enabled"] for entry in payload["parsers"])

    def test_unknown_document_is_a_json_404_not_the_spa(self, client: httpx.Client) -> None:
        """A misconfigured `location /api` would return index.html with 200 here."""
        response = client.get(f"/api/v1/documents/{uuid.uuid4()}")
        assert response.status_code == 404, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert 'id="root"' not in response.text

    def test_upload_and_result_through_the_proxy(self, client: httpx.Client) -> None:
        pdf = SAMPLES / "sample-text.pdf"
        assert pdf.exists(), f"missing sample {pdf}"

        upload = client.post(
            "/api/v1/documents",
            files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
            data={"parser": "pypdf"},
        )
        assert upload.status_code == 202, upload.text
        document_id = upload.json()["documents"][0]["id"]

        deadline = time.monotonic() + SETTLE_TIMEOUT
        body: dict = {}
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/documents/{document_id}")
            assert response.status_code == 200, response.text
            body = response.json()
            if body["status"] in {"completed", "failed"}:
                break
            time.sleep(0.5)

        assert body.get("status") == "completed", body
        assert body["page_count"] >= 2
        assert [page["page_number"] for page in body["pages"]] == list(
            range(1, body["page_count"] + 1)
        )
        assert body["summary"]


class TestProxyLimits:
    def test_body_limit_is_generous_enough_for_the_documented_file_size(
        self, client: httpx.Client
    ) -> None:
        """nginx caps bodies at 25 MB; the API accepts 20 MB. A 21 MB body must be refused by one
        of them rather than buffered and then accepted."""
        oversized = b"%PDF-1.4\n" + b"0" * (21 * 1024 * 1024)
        response = client.post(
            "/api/v1/documents",
            files=[("files", ("big.pdf", oversized, "application/pdf"))],
            data={"parser": "pypdf"},
        )
        assert response.status_code in {413}, (
            f"expected 413 from nginx or the API, got {response.status_code}"
        )

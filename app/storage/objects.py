"""S3 object storage adapter (LocalStack locally, real S3 in production).

Only the four operations the application needs are exposed, so replacing LocalStack with a
shared volume or another object store is a change in this file alone.

boto3 is synchronous: every call is pushed to a thread so the API event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

SOURCE_KEY_TEMPLATE = "uploads/{document_id}/source.pdf"
_CHUNK = 64 * 1024


class ObjectStoreError(Exception):
    """Any object-storage failure. Callers decide whether it is retryable."""

    def __init__(self, message: str, *, missing: bool = False):
        super().__init__(message)
        self.missing = missing


class S3ObjectStore:
    def __init__(self, settings: "Settings") -> None:
        self._bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id or None,
            aws_secret_access_key=settings.aws_secret_access_key or None,
            config=Config(
                # Path-style is required by LocalStack: virtual-hosted URLs (bucket.localhost)
                # do not resolve, and the call hangs instead of failing fast.
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
                signature_version="s3v4",
            ),
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    @staticmethod
    def source_key(document_id: str) -> str:
        # Generated key only — never a client-supplied filename (path traversal).
        return SOURCE_KEY_TEMPLATE.format(document_id=document_id)

    async def put_source(self, document_id: str, data: bytes) -> str:
        key = self.source_key(document_id)
        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType="application/pdf",
                ContentLength=len(data),
            )
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreError(f"Could not store the source document: {exc}") from exc
        return key

    async def get_source(self, key: str) -> bytes:
        try:
            response = await asyncio.to_thread(self._client.get_object, Bucket=self._bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            missing = code in {"NoSuchKey", "404", "NotFound"}
            raise ObjectStoreError(f"Could not read {key}: {exc}", missing=missing) from exc
        except BotoCoreError as exc:
            raise ObjectStoreError(f"Could not read {key}: {exc}") from exc
        body = response["Body"]
        try:
            return await asyncio.to_thread(body.read, _CHUNK * 4096)
        finally:
            body.close()

    async def delete_source(self, key: str) -> None:
        """Best effort by design: a leftover object is harmless, a crash here is not."""
        try:
            await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("source_delete_failed key=%s error=%s", key, type(exc).__name__)

    async def healthy(self) -> tuple[bool, str]:
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self._bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "") or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", "")
            return False, f"bucket {self._bucket!r} unreachable (HTTP {code})"
        except BotoCoreError as exc:
            return False, f"bucket {self._bucket!r} unreachable ({type(exc).__name__})"
        return True, f"bucket {self._bucket!r} ok"

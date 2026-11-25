"""Validated settings for both processes.

FROZEN CONTRACT — see docs/CONTRACT.md. Every knob is an environment variable of the same
name in upper case (REDIS_URL, AI_MODE, ...). Defaults target the Compose network, so
`docker compose up` needs no .env; host-side runs override the two URLs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # ── wiring ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://redis:6379/0"
    s3_endpoint_url: str = "http://localstack:4566"
    s3_bucket: str = "documents"
    aws_region: str = "us-east-1"
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"

    # ── AI ─────────────────────────────────────────────────────────────────────
    # "fake" is an explicit, visibly-labelled deterministic stand-in for tests and demos
    # without credentials. "live" never falls back to fake; it refuses to serve AI parsers
    # when the credentials/model are missing.
    ai_mode: Literal["live", "fake"] = "live"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"

    # ── limits ─────────────────────────────────────────────────────────────────
    max_upload_bytes: int = 20 * 1024 * 1024
    max_batch_files: int = 5
    max_batch_bytes: int = 50 * 1024 * 1024
    # Simultaneous uploads the API will buffer. Past this, requests wait briefly and get a 503
    # with Retry-After instead of being buffered anyway (memory is the constraint).
    max_concurrent_uploads: int = 16
    upload_slot_timeout_seconds: float = 10.0
    max_pages: int = 50
    # Per-page Gemini calls cost latency; cap how many pages an AI parser sends.
    max_ai_pages: int = 20
    max_extracted_chars: int = 200_000
    provider_timeout_seconds: float = 60.0
    # Cap simultaneous provider calls per client instance. Pacing lives in ProviderPacer, and each
    # client gets its own (Gemini parser, Gemini summarizer, OpenAI parser), so the effective ceiling
    # per process is this value times the number of active clients. Each in-flight job makes one call
    # at a time, so it is normally bounded by worker_concurrency; the cap exists so a future per-page
    # concurrency bump cannot silently multiply the request rate.
    provider_max_concurrency: int = 4
    # Client-side rate limit for provider calls, in requests per minute. 0 disables it. Set it to
    # your provider's published limit (free tiers are single-digit RPM) instead of discovering it
    # through 429s that exhaust the retry budget.
    provider_max_requests_per_minute: int = 0
    # A Retry-After longer than this is clamped. If you set provider_max_requests_per_minute, keep
    # this meaningfully larger than 60/rpm.
    retry_after_max_seconds: float = 30.0

    # ── job lifecycle ──────────────────────────────────────────────────────────
    max_job_attempts: int = 3
    job_deadline_seconds: int = 600
    lease_seconds: int = 90
    heartbeat_seconds: int = 15
    result_ttl_seconds: int = 86_400
    retry_backoff_seconds: tuple[float, ...] = (2.0, 5.0, 10.0)

    # ── queue ──────────────────────────────────────────────────────────────────
    stream_key: str = "docs:jobs"
    consumer_group: str = "document-workers"
    worker_read_block_ms: int = 2_000
    reclaim_min_idle_ms: int = 0  # 0 → derived from lease_seconds at runtime
    # Jobs processed in parallel by ONE worker process. Each job is independent and holds its own
    # lease, so this is the main throughput knob: with 1, N documents take N x job-duration.
    # Bounded by memory, not by Redis: each in-flight job holds its PDF and extracted pages.
    worker_concurrency: int = 4
    # How many stream entries to fetch per read; >= concurrency so capacity is never idle.
    worker_batch_size: int = 8
    # On SIGTERM, stop reading and give in-flight jobs this long to finish before exiting.
    # Anything unfinished stays pending and is reclaimed after the lease expires.
    worker_shutdown_grace_seconds: float = 30.0

    # ── diagnostics ────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    # Deterministic latency for FakeParser/FakeSummarizer, used by the concurrency test.
    fake_delay_seconds: float = 0.0

    @model_validator(mode="after")
    def _check_relationships(self) -> Settings:
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("HEARTBEAT_SECONDS must be lower than LEASE_SECONDS")
        if self.lease_seconds * 3 > self.job_deadline_seconds:
            raise ValueError("LEASE_SECONDS must be at most a third of JOB_DEADLINE_SECONDS")
        if self.max_ai_pages > self.max_pages:
            raise ValueError("MAX_AI_PAGES cannot exceed MAX_PAGES")
        if self.max_batch_bytes < self.max_upload_bytes:
            raise ValueError("MAX_BATCH_BYTES cannot be lower than MAX_UPLOAD_BYTES")
        if not self.retry_backoff_seconds:
            raise ValueError("RETRY_BACKOFF_SECONDS cannot be empty")
        if self.worker_concurrency < 1:
            raise ValueError("WORKER_CONCURRENCY must be at least 1")
        if self.worker_batch_size < 1:
            raise ValueError("WORKER_BATCH_SIZE must be at least 1")
        if self.worker_shutdown_grace_seconds <= 0:
            raise ValueError("WORKER_SHUTDOWN_GRACE_SECONDS must be positive")
        if self.provider_max_concurrency < 1:
            raise ValueError("PROVIDER_MAX_CONCURRENCY must be at least 1")
        if self.provider_max_requests_per_minute < 0:
            raise ValueError("PROVIDER_MAX_REQUESTS_PER_MINUTE must be 0 (disabled) or positive")
        if self.max_concurrent_uploads < 1:
            raise ValueError("MAX_CONCURRENT_UPLOADS must be at least 1")
        if self.upload_slot_timeout_seconds <= 0:
            raise ValueError("UPLOAD_SLOT_TIMEOUT_SECONDS must be positive")
        if self.retry_after_max_seconds <= 0:
            raise ValueError("RETRY_AFTER_MAX_SECONDS must be positive")
        return self

    @property
    def effective_reclaim_idle_ms(self) -> int:
        return self.reclaim_min_idle_ms or self.lease_seconds * 1000

    @property
    def ai_available(self) -> bool:
        """Whether summarization is possible; fake mode always is.

        Summarization is Gemini-only by contract, so this deliberately checks the Gemini key even
        though `gpt-5-mini` is now implemented: an OpenAI key alone cannot summarize a document.
        Extraction availability is the registry's job, not this flag's.
        """
        if self.ai_mode == "fake":
            return True
        return bool(self.gemini_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()

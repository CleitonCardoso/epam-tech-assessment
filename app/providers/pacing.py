"""Client-side pacing shared by the provider adapters.

Two independent limits, because they fail differently:

- a **semaphore** bounds how many requests are in flight at once (connection pressure, memory);
- a **token bucket** bounds requests per minute (the provider's published rate limit).

Without them N parallel jobs means N calls per second, the provider answers 429, and our retry budget
is spent on errors instead of work. Measured effect on the Gemini path: an unpaced 20-document burst
failed several documents; paced at 20 RPM it completed 20/20 at 0.76 docs/s (= RPM / calls per doc).
"""

from __future__ import annotations

import asyncio
import time


class ProviderPacer:
    """Per-client concurrency + rate limiting."""

    def __init__(self, *, max_concurrency: int, max_requests_per_minute: int = 0) -> None:
        self._slots = asyncio.Semaphore(max(1, max_concurrency))
        self._max_rpm = max(0, max_requests_per_minute)
        self._tokens = float(self._max_rpm)
        self._tokens_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Take a concurrency slot and one rate-limit token (waiting if necessary)."""
        await self._slots.acquire()
        try:
            await self._take_token()
        except BaseException:
            self._slots.release()
            raise

    def release(self) -> None:
        self._slots.release()

    async def _take_token(self) -> None:
        if not self._max_rpm:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    float(self._max_rpm),
                    self._tokens + (now - self._tokens_at) * (self._max_rpm / 60.0),
                )
                self._tokens_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) * (60.0 / self._max_rpm))

"""Optional aggregate response-body pacing for the site's HTTPX clients.

Hooks wrap streaming bodies before HTTPX buffers them. Native HTTPX transports,
proxy/environment handling, redirects, retries and content decoding stay intact.
This paces application reads, not packets: OS/network buffers can still burst.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

CHUNK_SIZE = 16 * 1024
_ACTIVE: ContextVar[DownloadRateLimiter | None] = ContextVar("download_rate", default=None)


def validate_limit(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("download limit must be a finite number >= 0 MB/s (0 = unlimited)")
    return value


class DownloadRateLimiter:
    def __init__(self, mb_per_second: float):
        self.bytes_per_second = validate_limit(mb_per_second) * 1048576
        if self.bytes_per_second == 0:
            raise ValueError("a rate limiter needs a positive limit")
        self._deadline = 0.0
        self._lock = threading.Lock()

    def delay(self, size: int) -> float:
        # One schedule shared by async file/page requests and sitemap threads.
        with self._lock:
            now = time.monotonic()
            self._deadline = max(now, self._deadline) + size / self.bytes_per_second
            return max(0.0, self._deadline - now)

    def on_response(self, response: httpx.Response) -> None:
        if not response.is_stream_consumed:
            response.stream = _SyncBody(response.stream, self)

    async def on_async_response(self, response: httpx.Response) -> None:
        if not response.is_stream_consumed:
            response.stream = _AsyncBody(response.stream, self)


class _SyncBody(httpx.SyncByteStream):
    def __init__(self, stream, limiter: DownloadRateLimiter):
        self.stream = stream
        self.limiter = limiter

    def __iter__(self):
        for chunk in self.stream:
            for offset in range(0, len(chunk), CHUNK_SIZE):
                part = chunk[offset:offset + CHUNK_SIZE]
                time.sleep(self.limiter.delay(len(part)))
                yield part

    def close(self):
        self.stream.close()


class _AsyncBody(httpx.AsyncByteStream):
    def __init__(self, stream, limiter: DownloadRateLimiter):
        self.stream = stream
        self.limiter = limiter

    async def __aiter__(self):
        async for chunk in self.stream:
            for offset in range(0, len(chunk), CHUNK_SIZE):
                part = chunk[offset:offset + CHUNK_SIZE]
                await asyncio.sleep(self.limiter.delay(len(part)))
                yield part

    async def aclose(self):
        await self.stream.aclose()


@contextmanager
def limit_downloads(mb_per_second: float):
    rate = validate_limit(mb_per_second)
    token = _ACTIVE.set(DownloadRateLimiter(rate) if rate else None)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def client_options(*, asynchronous: bool = False) -> dict:
    limiter = _ACTIVE.get()
    if limiter is None:
        return {}
    hook = limiter.on_async_response if asynchronous else limiter.on_response
    return {"event_hooks": {"response": [hook]}}

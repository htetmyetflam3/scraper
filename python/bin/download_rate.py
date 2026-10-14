"""Response-body rate display and optional pacing for the site's HTTPX clients.

Hooks wrap streaming bodies before HTTPX buffers them. Native HTTPX transports,
proxy/environment handling, redirects, retries and content decoding stay intact.
This paces application reads, not packets: OS/network buffers can still burst.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
import urllib.parse
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

CHUNK_SIZE = 16 * 1024
REPORT_SECONDS = 5.0
REPORT_BYTES = 5 * 1048576
SMALL_BODY = 256 * 1024
_ACTIVE: ContextVar[DownloadRateLimiter | DownloadMonitor | None] = ContextVar("download_rate", default=None)


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


class _Progress:
    """Body-only per-response average; excludes TLS/header and save time."""

    def __init__(self, response: httpx.Response, log):
        self.log = log
        self.started = self.last_report = time.monotonic()
        self.received = self.reported = 0
        length = response.headers.get("content-length", "")
        self.total = int(length) if length.isdigit() else None
        path = urllib.parse.unquote(response.url.path)
        # Do not print signed query strings or allow filenames to inject lines.
        self.name = " ".join((path.rsplit("/", 1)[-1] or response.url.host).split())
        mime = response.headers.get("content-type", "").lower()
        self.document = (
            path.lower().endswith((".pdf", ".docx"))
            or "application/pdf" in mime
            or "wordprocessingml" in mime
            or "attachment" in response.headers.get("content-disposition", "").lower()
        )

    def update(self, size: int):
        self.received += size
        now = time.monotonic()
        if (
            self.received - self.reported >= REPORT_BYTES
            or now - self.last_report >= REPORT_SECONDS
        ):
            self.report(now)

    def report(self, now: float, *, complete: bool = False):
        elapsed = max(now - self.started, 0.001)
        if self.total:
            amount = (
                f"{self.received / self.total:.0%} "
                f"({self.received / 1048576:.1f}/{self.total / 1048576:.1f} MB)"
            )
        else:
            amount = f"{self.received / 1048576:.1f} MB received"
        status = " body complete;" if complete else ""
        self.log(
            f"[download-progress] {self.name} {amount};{status} "
            f"{self.received / 1048576 / elapsed:.2f} MB/s avg "
            f"@ {time.strftime('%H:%M:%S')}"
        )
        self.reported = self.received
        self.last_report = now

    def finish(self):
        # Avoid a speed line for every tiny crawl page; always report documents.
        if self.document or self.received >= SMALL_BODY or self.reported:
            self.report(time.monotonic(), complete=True)


class DownloadMonitor:
    def __init__(self, limiter: DownloadRateLimiter | None, log):
        self.limiter = limiter
        self.log = log

    def on_response(self, response: httpx.Response) -> None:
        if not response.is_stream_consumed:
            response.stream = _SyncBody(
                response.stream, self.limiter, _Progress(response, self.log)
            )

    async def on_async_response(self, response: httpx.Response) -> None:
        if not response.is_stream_consumed:
            response.stream = _AsyncBody(
                response.stream, self.limiter, _Progress(response, self.log)
            )


class _SyncBody(httpx.SyncByteStream):
    def __init__(self, stream, limiter: DownloadRateLimiter | None, progress=None):
        self.stream = stream
        self.limiter = limiter
        self.progress = progress

    def __iter__(self):
        for chunk in self.stream:
            step = CHUNK_SIZE if self.limiter else max(len(chunk), 1)
            for offset in range(0, len(chunk), step):
                part = chunk[offset:offset + step]
                if self.limiter:
                    time.sleep(self.limiter.delay(len(part)))
                if self.progress:
                    self.progress.update(len(part))
                yield part
        if self.progress:
            self.progress.finish()

    def close(self):
        self.stream.close()


class _AsyncBody(httpx.AsyncByteStream):
    def __init__(self, stream, limiter: DownloadRateLimiter | None, progress=None):
        self.stream = stream
        self.limiter = limiter
        self.progress = progress

    async def __aiter__(self):
        async for chunk in self.stream:
            step = CHUNK_SIZE if self.limiter else max(len(chunk), 1)
            for offset in range(0, len(chunk), step):
                part = chunk[offset:offset + step]
                if self.limiter:
                    await asyncio.sleep(self.limiter.delay(len(part)))
                if self.progress:
                    self.progress.update(len(part))
                yield part
        if self.progress:
            self.progress.finish()

    async def aclose(self):
        await self.stream.aclose()


@contextmanager
def limit_downloads(mb_per_second: float, *, log=None):
    rate = validate_limit(mb_per_second)
    limiter = DownloadRateLimiter(rate) if rate else None
    token = _ACTIVE.set(DownloadMonitor(limiter, log) if log is not None else limiter)
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

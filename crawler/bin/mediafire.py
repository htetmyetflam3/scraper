"""MediaFire/same-site PDF and DOCX landing-page recovery; no saving/history."""

from __future__ import annotations

import asyncio
import html
import io
import re
import urllib.parse
import zipfile

import httpx
from bs4 import BeautifulSoup
from download_links import guard_download_url
from pdf_support import is_pdf, looks_like_html
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)


def _is_mediafire_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == "mediafire.com" or host.endswith(".mediafire.com")


def _same_download_site(page_url: str, candidate_url: str) -> bool:
    """Only follow extracted links on the same origin or MediaFire sibling hosts."""
    source = urllib.parse.urlsplit(page_url)
    candidate = urllib.parse.urlsplit(candidate_url)
    source_host = (source.hostname or "").lower()
    candidate_host = (candidate.hostname or "").lower()
    if source.scheme not in ("http", "https") or candidate.scheme not in (
        "http",
        "https",
    ):
        return False
    if source.scheme == "https" and candidate.scheme != "https":
        return False

    def effective_port(parts):
        return parts.port or (443 if parts.scheme == "https" else 80)

    if source_host == candidate_host:
        return effective_port(source) == effective_port(candidate)
    return (
        _is_mediafire_host(source_host)
        and _is_mediafire_host(candidate_host)
        and effective_port(source) == effective_port(candidate)
    )


QUOTED_FILE_LINK = re.compile(
    r"""['\"]([^'\"\s<>]+?\.(?:pdf|docx)(?:\?[^'\"\s<>]*)?)['\"]""", re.I
)
RAW_FILE_LINK = re.compile(
    r"""https?://[^\s'\"<>]+?\.(?:pdf|docx)(?:\?[^\s'\"<>]*)?""", re.I
)
DOWNLOAD_VALUE = re.compile(
    r"""(?:download(?:[_-]?(?:url|link))?|direct[_-]?link)["']?\s*[:=]\s*["']([^"']+)["']""",
    re.I,
)


def extract_download_links(
    page_html: str,
    page_url: str,
    limit: int = 8,
    *,
    extensions: tuple[str, ...] = ("pdf",),
) -> list[str]:
    """Extract same-site/MediaFire PDF targets, optionally including DOCX.

    MediaFire share URLs often return HTML instead of file bytes; the actual
    target is commonly in a Download button. A marked download control may
    expose an extensionless endpoint; the response validator decides whether it
    is truly a PDF or DOCX. Other domains are never followed here.
    """
    normalized_extensions = tuple(
        "." + extension.lower().lstrip(".") for extension in extensions
    )
    text = html.unescape(page_html or "").replace("\\/", "/")
    candidates: dict[str, int] = {}

    def add(raw: str, *, download_control: bool = False) -> None:
        raw = html.unescape((raw or "").strip().strip("\"'"))
        if not raw or raw.lower().startswith(("javascript:", "data:", "mailto:")):
            return
        target = guard_download_url(urllib.parse.urljoin(page_url, raw))
        if target is None:
            return
        parts = urllib.parse.urlsplit(target)
        if not _same_download_site(page_url, target):
            return
        path = urllib.parse.unquote(parts.path).lower()
        is_file_path = path.endswith(normalized_extensions)
        if not is_file_path and not download_control:
            return
        target = urllib.parse.urldefrag(target)[0]
        if target == urllib.parse.urldefrag(page_url)[0]:
            return
        # An explicitly marked Download control takes precedence over incidental files.
        priority = 0 if download_control else 1
        if target not in candidates or priority < candidates[target]:
            candidates[target] = priority

    soup = BeautifulSoup(text, "html.parser")
    for meta in soup.find_all(
        "meta", attrs={"http-equiv": re.compile(r"^refresh$", re.I)}
    ):
        content = meta.get("content", "")
        match = re.search(r"""url\s*=\s*["']?([^"';]+)""", content, re.I)
        if match:
            add(match.group(1), download_control=True)

    url_attributes = {
        "href",
        "action",
        "formaction",
        "data-href",
        "data-url",
        "data-download-url",
        "data-link",
    }
    for element in soup.find_all(True):
        attrs = element.attrs
        marker_values = [
            attrs.get(key, "") for key in ("id", "class", "title", "aria-label")
        ]
        marker_values.append(element.get_text(" ", strip=True)[:200])
        marker = " ".join(
            " ".join(value) if isinstance(value, list) else str(value)
            for value in marker_values
        ).lower()
        is_download_control = bool(element.has_attr("download") or "download" in marker)
        for name, value in attrs.items():
            if isinstance(value, list):
                value = " ".join(value)
            if not isinstance(value, str):
                continue
            if name.lower() in url_attributes:
                add(
                    value,
                    download_control=is_download_control or "download" in name.lower(),
                )
            elif name.lower() == "onclick":
                for match in QUOTED_FILE_LINK.finditer(value):
                    add(match.group(1), download_control=is_download_control)
                for match in re.finditer(r"""https?://[^\s"'<>]+""", value, re.I):
                    add(match.group(0), download_control=is_download_control)

    # Some provider pages put the target in JSON/JavaScript instead of an anchor.
    for pattern in (QUOTED_FILE_LINK, RAW_FILE_LINK):
        for match in pattern.finditer(text):
            add(match.group(1) if pattern is QUOTED_FILE_LINK else match.group(0))
    for match in DOWNLOAD_VALUE.finditer(text):
        add(match.group(1), download_control=True)

    return [
        url for url, _priority in sorted(candidates.items(), key=lambda item: item[1])
    ][:limit]


def is_docx(data: bytes) -> bool:
    """Accept only a ZIP containing the defining parts of a Word document."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
        return "[Content_Types].xml" in names and "word/document.xml" in names
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return False


def is_html_response(response) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    return (
        "html" in content_type
        or "xhtml" in content_type
        or looks_like_html(response.content)
    )


async def fetch_with_retries(
    client: httpx.AsyncClient,
    url: str,
    retries: int,
    timeout: float,
    request_headers: dict[str, str] | None = None,
) -> httpx.Response:
    """GET with exponential backoff + jitter (tenacity), honouring 429/503 waits."""
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(retries),
        wait=wait_exponential_jitter(initial=1, max=30, jitter=1),
        retry=retry_if_exception_type((httpx.HTTPError, httpx.StreamError)),
        reraise=True,
    ):
        with attempt:
            response = await client.get(
                url,
                headers=request_headers,
                timeout=timeout,
                follow_redirects=True,
            )
            if response.status_code in (429, 503):
                wait = float(response.headers.get("Retry-After") or 0) or 5.0
                await asyncio.sleep(min(wait, 60))
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return response
    raise RuntimeError("unreachable")


async def _recover_file_response(
    client: httpx.AsyncClient,
    page_response: httpx.Response,
    retries: int,
    timeout: float,
    *,
    allow_docx: bool,
    seen: set[str] | None = None,
    depth: int = 0,
) -> httpx.Response | None:
    if depth >= 2:
        return None
    seen = seen if seen is not None else set()
    source_url = str(page_response.url)
    page_html = getattr(page_response, "text", None)
    if not isinstance(page_html, str):
        page_html = page_response.content.decode("utf-8-sig", "replace")
    extensions = ("pdf", "docx") if allow_docx else ("pdf",)
    for candidate in extract_download_links(
        page_html, source_url, extensions=extensions
    ):
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            response = await fetch_with_retries(
                client,
                candidate,
                retries,
                timeout,
                request_headers={
                    "Referer": source_url,
                    "Accept": (
                        "application/pdf,"
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
                        "*/*;q=0.8"
                    ),
                },
            )
        except Exception:  # a broken/expired button must not stop trying candidates
            continue
        if is_pdf(response.content) or (allow_docx and is_docx(response.content)):
            return response
        if is_html_response(response):
            nested = await _recover_file_response(
                client,
                response,
                retries,
                timeout,
                allow_docx=allow_docx,
                seen=seen,
                depth=depth + 1,
            )
            if nested is not None:
                return nested
    return None


async def recover_pdf_response(
    client: httpx.AsyncClient,
    page_response: httpx.Response,
    retries: int,
    timeout: float,
    *,
    seen: set[str] | None = None,
    depth: int = 0,
) -> httpx.Response | None:
    """Follow PDF links embedded in MediaFire or same-site HTML responses."""
    return await _recover_file_response(
        client,
        page_response,
        retries,
        timeout,
        allow_docx=False,
        seen=seen,
        depth=depth,
    )


async def recover_file_response(
    client: httpx.AsyncClient,
    page_response: httpx.Response,
    retries: int,
    timeout: float,
    *,
    seen: set[str] | None = None,
    depth: int = 0,
) -> httpx.Response | None:
    """Follow PDF or DOCX targets embedded in MediaFire landing-page HTML."""
    return await _recover_file_response(
        client,
        page_response,
        retries,
        timeout,
        allow_docx=True,
        seen=seen,
        depth=depth,
    )

"""Crawl one site at a time, resolve file hosts, and save PDF/DOCX files."""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import html
import json
import os
import re
import struct
import sys
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from domain_guard import excluded_site_reason
from download_links import FILE_EXTENSION, extract_file_links, guard_download_url
from mediafire import (
    fetch_with_retries,
    is_docx,
    is_html_response,
    recover_file_response,
)
from pdf_support import is_pdf, linearize, looks_like_html
from site_size_guard import FALLBACK_SITEMAP_PATHS, large_site_reason

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_FILE = REPO_ROOT / "entry.txt"
OUTPUT_DIR = REPO_ROOT / "pdfs"
MAX_SITE_PAGES = 3_000
REQUEST_TIMEOUT = 30.0
SITEMAP_TIMEOUT = 12.0
MAX_PROVIDER_STEPS = 3
MAX_PROVIDER_TARGETS = 8
DOCUMENT_ACCEPT = (
    "application/pdf,"
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
    "*/*;q=0.8"
)

PROVIDER_DOMAINS = {
    "drive": (
        "drive.google.com",
        "docs.google.com",
        "drive.usercontent.google.com",
        "drive.googleusercontent.com",
    ),
    "mega": ("mega.nz", "mega.co.nz"),
    "mediafire": ("mediafire.com",),
    "dropbox": ("dropbox.com", "dropboxusercontent.com"),
    "terabox": (
        "terabox.com",
        "teraboxapp.com",
        "1024terabox.com",
        "1024tera.com",
        "terabox.app",
    ),
}

DOWNLOAD_MARKER = re.compile(
    r"\b(download(?:\s+now|\s+file)?|get\s+file|pdf|docx)\b", re.I
)
NON_DOWNLOAD_MARKER = re.compile(
    r"\b(share|telegram|reply|comment|print|whatsapp|facebook|twitter|pinterest|bookmark|follow)\b",
    re.I,
)
PROVIDER_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
PROVIDER_DOWNLOAD_FIELD = re.compile(
    r"(?:dlink|download[_-]?(?:url|link)|direct[_-]?link)\s*[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']",
    re.I,
)


@dataclass
class DownloadCandidate:
    url: str
    label: str = ""
    provider: str | None = None
    payload: bytes | None = None
    response_url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class DownloadSummary:
    sites: int = 0
    pages: int = 0
    candidates: int = 0
    saved: list[Path] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _hostname(url: str) -> str | None:
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port
    except ValueError:
        return None
    if not host:
        return None
    try:
        return host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return host.rstrip(".").lower()


def _site_key(url: str) -> str | None:
    host = _hostname(url)
    if host is None:
        return None
    return host[4:] if host.startswith("www.") else host


def _domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _provider_kind(url: str) -> str | None:
    host = _hostname(url)
    if host is None:
        return None
    for provider, domains in PROVIDER_DOMAINS.items():
        if any(_domain_matches(host, domain) for domain in domains):
            return provider
    return None


def _is_scribd(url: str) -> bool:
    host = _hostname(url)
    return host == "scribd.com" or bool(host and host.endswith(".scribd.com"))


def _is_provider_share_url(url: str, provider: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = _hostname(url) or ""
    path = urllib.parse.unquote(parts.path).lower()
    query = urllib.parse.parse_qs(parts.query)
    if _is_document_path(url):
        return True
    if provider == "drive":
        return bool(
            re.search(r"/(?:file|document|spreadsheets|presentation)/d/", path)
            or path in {"/open", "/uc", "/download"}
            or query.get("id")
        )
    if provider == "mega":
        return path.startswith(("/file/", "/folder/")) or parts.fragment.startswith("!")
    if provider == "mediafire":
        return path.startswith(("/file/", "/view/", "/download/")) or host.split(
            ".", 1
        )[0].startswith("download")
    if provider == "dropbox":
        return (
            host == "dropboxusercontent.com"
            or host.endswith(".dropboxusercontent.com")
            or path.startswith(("/s/", "/scl/fi/", "/scl/fo/"))
        )
    if provider == "terabox":
        return path.startswith(("/s/", "/share/", "/sharing/", "/download/")) or bool(
            query.get("surl")
        )
    return False


def _normalized_page_url(url: str, site_key: str) -> str | None:
    try:
        parts = urllib.parse.urlsplit(url)
        if (
            parts.scheme.lower() not in ("http", "https")
            or not parts.netloc
            or parts.username is not None
            or parts.password is not None
        ):
            return None
        parts.port
    except ValueError:
        return None
    cleaned = guard_download_url(url)
    if cleaned is None or excluded_site_reason(cleaned) is not None:
        return None
    if _site_key(cleaned) != site_key:
        return None
    return cleaned


def _provider_url(url: str, base_url: str | None = None) -> tuple[str, str] | None:
    resolved = urllib.parse.urljoin(base_url or "", html.unescape(url.strip()))
    try:
        parts = urllib.parse.urlsplit(resolved)
        if (
            parts.scheme.lower() not in ("http", "https")
            or not parts.netloc
            or parts.username is not None
            or parts.password is not None
        ):
            return None
        parts.port
    except ValueError:
        return None
    provider = _provider_kind(resolved)
    if provider is None or not _is_provider_share_url(resolved, provider):
        return None
    # MEGA's public file key is in the fragment and must not be discarded.
    if provider != "mega":
        resolved = urllib.parse.urldefrag(resolved)[0]
    return resolved, provider


def _candidate_key(candidate: DownloadCandidate) -> str:
    if candidate.provider == "mega":
        return candidate.url
    if candidate.provider:
        return urllib.parse.urldefrag(candidate.url)[0]
    return guard_download_url(candidate.url) or candidate.url


def _same_download_site(url: str, site_key: str) -> bool:
    return _site_key(url) == site_key


def _is_document_path(url: str) -> bool:
    try:
        return bool(FILE_EXTENSION.search(urllib.parse.urlsplit(url).path))
    except ValueError:
        return False


def _decode_document_type(data: bytes) -> str | None:
    if is_pdf(data):
        return "pdf"
    if is_docx(data):
        return "docx"
    return None


def _meaningful_label(label: str) -> str:
    return " ".join((label or "").split())[:180]


def _add_candidate(
    candidates: dict[str, DownloadCandidate],
    url: str,
    label: str = "",
    *,
    provider: str | None = None,
    payload: bytes | None = None,
    response_url: str | None = None,
    headers: dict[str, str] | None = None,
) -> bool:
    candidate = DownloadCandidate(
        url=url,
        label=_meaningful_label(label),
        provider=provider or _provider_kind(url),
        payload=payload,
        response_url=response_url,
        headers=headers or {},
    )
    key = _candidate_key(candidate)
    existing = candidates.get(key)
    if existing is None:
        candidates[key] = candidate
        return True
    if payload is not None and existing.payload is None:
        candidates[key] = candidate
    elif (
        not existing.label or existing.label.lower() in {"download", "pdf", "docx"}
    ) and candidate.label:
        existing.label = candidate.label
    return False


def _extract_provider_links(
    page_html: str, page_url: str
) -> list[tuple[str, str, str]]:
    """Collect only known file-host URLs, never ordinary external navigation."""
    soup = BeautifulSoup(page_html, "html.parser")
    found: dict[str, tuple[str, str]] = {}

    def add(raw: str, label: str = "") -> None:
        provider_url = _provider_url(raw, page_url)
        if provider_url is None:
            return
        url, provider = provider_url
        found.setdefault(url, (provider, _meaningful_label(label)))

    for anchor in soup.find_all("a", href=True):
        add(
            anchor.get("href", ""),
            anchor.get_text(" ", strip=True) or anchor.get("title", ""),
        )

    for element in soup.find_all(True):
        marker_parts = [
            element.get_text(" ", strip=True),
            element.get("title", ""),
            element.get("aria-label", ""),
            element.get("id", ""),
            element.get("class", []),
        ]
        marker = " ".join(
            " ".join(part) if isinstance(part, list) else str(part)
            for part in marker_parts
        )
        for name, value in element.attrs.items():
            if isinstance(value, list):
                value = " ".join(str(item) for item in value)
            if not isinstance(value, str):
                continue
            if name.lower() in {
                "href",
                "action",
                "formaction",
                "data-url",
                "data-href",
                "data-link",
                "data-download-url",
            }:
                add(value, marker)
            elif name.lower() == "onclick":
                for match in PROVIDER_URL.finditer(value.replace("\\/", "/")):
                    add(match.group(0).rstrip(").,;}"), marker)

    for match in PROVIDER_URL.finditer(page_html.replace("\\/", "/")):
        add(match.group(0).rstrip(").,;}"))
    return [(url, provider, label) for url, (provider, label) in found.items()]


def _extract_extensionless_controls(
    page_html: str, page_url: str, site_key: str
) -> list[tuple[str, str]]:
    """Find same-site, explicitly download-labelled controls without file suffixes."""
    soup = BeautifulSoup(page_html, "html.parser")
    found: dict[str, str] = {}

    for element in soup.find_all(True):
        attrs = element.attrs
        values = [
            element.get_text(" ", strip=True),
            attrs.get("title", ""),
            attrs.get("aria-label", ""),
            attrs.get("id", ""),
            attrs.get("class", []),
            attrs.get("value", ""),
        ]
        label = " ".join(
            " ".join(str(part) for part in value)
            if isinstance(value, list)
            else str(value)
            for value in values
        )
        if not DOWNLOAD_MARKER.search(label) or NON_DOWNLOAD_MARKER.search(label):
            continue

        raw_targets: list[str] = []
        for name in (
            "href",
            "action",
            "formaction",
            "data-url",
            "data-href",
            "data-link",
            "data-download-url",
        ):
            value = attrs.get(name)
            if isinstance(value, str):
                raw_targets.append(value)
        onclick = attrs.get("onclick", "")
        if isinstance(onclick, str):
            raw_targets.extend(
                match.group(1) for match in re.finditer(r"['\"]([^'\"]+)['\"]", onclick)
            )

        for raw in raw_targets:
            target = _normalized_page_url(urllib.parse.urljoin(page_url, raw), site_key)
            if target and not _is_document_path(target):
                found.setdefault(target, _meaningful_label(label))

    return list(found.items())


def _add_page_downloads(
    candidates: dict[str, DownloadCandidate],
    page_html: str,
    page_url: str,
    site_key: str,
) -> None:
    for url, label in extract_file_links(page_html, page_url):
        if NON_DOWNLOAD_MARKER.search(label):
            continue
        provider = _provider_kind(url)
        if provider is not None:
            _add_candidate(candidates, url, label, provider=provider)
            continue
        if excluded_site_reason(url) is not None:
            continue
        # Off-site PDF/DOCX files are assets, not pages to crawl.
        if _same_download_site(url, site_key) or _is_document_path(url):
            cleaned = guard_download_url(url)
            if cleaned:
                _add_candidate(candidates, cleaned, label)

    for url, provider, label in _extract_provider_links(page_html, page_url):
        _add_candidate(candidates, url, label, provider=provider)

    for url, label in _extract_extensionless_controls(page_html, page_url, site_key):
        _add_candidate(candidates, url, label)


def _sitemap_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _filter_sitemap(
    text: str, final_url: str, site_key: str
) -> tuple[str, list[str], list[str]]:
    """Keep same-site page URLs for counting; collect provider/file URLs separately."""
    try:
        root = ET.fromstring(text.lstrip("\ufeff"))
    except (ET.ParseError, ValueError, TypeError):
        return text, [], []
    kind = _sitemap_local_name(root.tag)
    if kind not in {"urlset", "sitemapindex"}:
        return text, [], []

    page_urls: list[str] = []
    download_urls: list[str] = []
    child_name = "url" if kind == "urlset" else "sitemap"
    for item in list(root):
        if _sitemap_local_name(item.tag) != child_name:
            continue
        loc = next(
            (node for node in item if _sitemap_local_name(node.tag) == "loc"), None
        )
        if loc is None or not (loc.text or "").strip():
            root.remove(item)
            continue
        target = urllib.parse.urljoin(final_url, loc.text.strip())
        provider = _provider_kind(target)
        is_file = _is_document_path(target)
        same_site = _same_download_site(target, site_key)
        if kind == "sitemapindex":
            if not same_site:
                root.remove(item)
            continue
        if provider is not None or (is_file and not same_site):
            if provider is not None or excluded_site_reason(target) is None:
                download_urls.append(target)
            root.remove(item)
        elif same_site:
            page_urls.append(target)
        else:
            root.remove(item)
    sanitized = ET.tostring(root, encoding="unicode")
    return sanitized, page_urls, download_urls


def _sitemap_response_text(response: httpx.Response) -> str:
    data = response.content
    if data.startswith(b"\x1f\x8b"):
        try:
            return gzip.decompress(data).decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as error:
            raise ValueError(f"invalid compressed sitemap: {error}") from error
    return response.text


def _robots_sitemaps(client: httpx.Client, seed_url: str, site_key: str) -> list[str]:
    parts = urllib.parse.urlsplit(seed_url)
    origin = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    current = _normalized_page_url(
        urllib.parse.urljoin(origin, "/robots.txt"), site_key
    )
    if current is None:
        return []
    visited: set[str] = set()
    for _ in range(6):
        if current in visited:
            return []
        visited.add(current)
        try:
            response = client.get(current, follow_redirects=False)
        except Exception:
            return []
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                return []
            target = urllib.parse.urljoin(str(response.url), location)
            current = _normalized_page_url(target, site_key)
            if current is None:
                return []
            continue
        if response.status_code != 200 or not _same_download_site(
            str(response.url), site_key
        ):
            return []
        declared: list[str] = []
        for line in response.text.splitlines():
            directive, separator, value = line.partition(":")
            if not separator or directive.strip().lower() != "sitemap":
                continue
            sitemap = value.split("#", 1)[0].strip()
            if not sitemap:
                continue
            candidate = urllib.parse.urljoin(str(response.url), sitemap)
            normalized = _normalized_page_url(candidate, site_key)
            if normalized:
                declared.append(normalized)
        return list(dict.fromkeys(declared))
    return []


def _sitemap_scan(seed_url: str) -> tuple[str | None, list[str], list[str]]:
    """Use the retained sitemap guard and return same-site pages/provider files."""
    site_key = _site_key(seed_url)
    if site_key is None:
        return None, [], []
    records: list[tuple[str, str]] = []
    collected_downloads: list[str] = []
    with httpx.Client(
        timeout=SITEMAP_TIMEOUT,
        follow_redirects=False,
        headers={"Accept": "application/xml,text/xml,*/*;q=0.5"},
    ) as client:

        def fetch_sitemap(url: str) -> tuple[str, str]:
            current = _normalized_page_url(url, site_key)
            if current is None:
                raise ValueError("off-site or invalid sitemap URL")
            for _ in range(6):
                response = client.get(current, follow_redirects=False)
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("sitemap redirect has no Location")
                    target = urllib.parse.urljoin(str(response.url), location)
                    current = _normalized_page_url(target, site_key)
                    if current is None:
                        return "", target
                    continue
                response.raise_for_status()
                final_url = str(response.url)
                sanitized, _pages, downloads = _filter_sitemap(
                    _sitemap_response_text(response), final_url, site_key
                )
                if _same_download_site(final_url, site_key):
                    records.append((sanitized, final_url))
                    collected_downloads.extend(downloads)
                return sanitized, final_url
            raise ValueError("too many sitemap redirects")

        declared_sitemaps = _robots_sitemaps(client, seed_url, site_key)
        origin_parts = urllib.parse.urlsplit(seed_url)
        origin = urllib.parse.urlunsplit(
            (origin_parts.scheme, origin_parts.netloc, "", "", "")
        )
        sitemap_candidates = list(
            dict.fromkeys(
                [
                    *declared_sitemaps,
                    *(origin + path for path in FALLBACK_SITEMAP_PATHS),
                ]
            )
        )
        reason = large_site_reason(
            seed_url,
            fetch_sitemap,
            declared_sitemaps=sitemap_candidates,
            max_pages=MAX_SITE_PAGES,
            allow_url=lambda url: _site_key(url) == site_key,
        )

    page_urls: list[str] = []
    for text, final_url in records:
        _sanitized, pages, _downloads = _filter_sitemap(text, final_url, site_key)
        page_urls.extend(pages)
    return (
        reason,
        list(dict.fromkeys(page_urls)),
        list(dict.fromkeys(collected_downloads)),
    )


def _sitemap_urls_from_page(page_html: str, page_url: str, site_key: str) -> list[str]:
    soup = BeautifulSoup(page_html, "html.parser")
    found = []
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        if not isinstance(href, str):
            continue
        label_values = [
            link.get_text(" ", strip=True),
            link.get("title", ""),
            link.get("aria-label", ""),
            link.get("id", ""),
            link.get("class", []),
            *(image.get("alt", "") for image in link.find_all("img")),
        ]
        label = " ".join(
            " ".join(str(part) for part in value)
            if isinstance(value, list)
            else str(value)
            for value in label_values
        )
        if NON_DOWNLOAD_MARKER.search(label):
            continue
        candidate = _normalized_page_url(urllib.parse.urljoin(page_url, href), site_key)
        if candidate:
            found.append(candidate)
    return list(dict.fromkeys(found))


async def _crawl_site(
    client: httpx.AsyncClient,
    seed_urls: list[str],
    candidates: dict[str, DownloadCandidate],
    process_candidates: Callable[[], Awaitable[None]],
) -> int:
    """Crawl one site depth-first, downloading each page's files before continuing."""
    site_key = _site_key(seed_urls[0])
    if site_key is None:
        return 0
    try:
        sitemap_reason, sitemap_pages, sitemap_downloads = await asyncio.to_thread(
            _sitemap_scan, seed_urls[0]
        )
    except Exception as error:
        sitemap_reason, sitemap_pages, sitemap_downloads = None, [], []
        print(f"[sitemap] {seed_urls[0]}: {error}")
    if sitemap_reason:
        print(f"[skip-large] {seed_urls[0]}: {sitemap_reason}")
        return 0

    for url in sitemap_downloads:
        provider = _provider_kind(url)
        if provider is not None:
            _add_candidate(candidates, url, provider=provider)
        elif excluded_site_reason(url) is None and _is_document_path(url):
            cleaned = guard_download_url(url)
            if cleaned:
                _add_candidate(candidates, cleaned)

    stack: list[str] = []
    scheduled: set[str] = set()

    def schedule(raw_url: str) -> None:
        provider_url = _provider_url(raw_url)
        if provider_url:
            url, provider = provider_url
            _add_candidate(candidates, url, provider=provider)
            return
        normalized = _normalized_page_url(raw_url, site_key)
        if normalized is None:
            return
        if _is_document_path(normalized):
            if excluded_site_reason(normalized) is None:
                cleaned = guard_download_url(normalized)
                if cleaned:
                    _add_candidate(candidates, cleaned)
            return
        if normalized in scheduled or len(scheduled) >= MAX_SITE_PAGES:
            return
        scheduled.add(normalized)
        stack.append(normalized)

    for sitemap_page in reversed(sitemap_pages):
        schedule(sitemap_page)
    for seed in reversed(seed_urls):
        schedule(seed)
    await process_candidates()

    async def visit_page(page_url: str) -> None:
        print(f"[page] {page_url}", flush=True)
        response = await client.get(
            page_url,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT,
        )

        if response.is_redirect:
            location = response.headers.get("location")
            if location:
                target = urllib.parse.urljoin(str(response.url), location)
                provider_url = _provider_url(target)
                if provider_url:
                    url, provider = provider_url
                    _add_candidate(candidates, url, provider=provider)
                elif _is_document_path(target) and excluded_site_reason(target) is None:
                    cleaned = guard_download_url(target)
                    if cleaned:
                        _add_candidate(candidates, cleaned)
                else:
                    schedule(target)
            return

        if response.status_code != 200:
            return
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        content_type_lower = content_type.strip().lower()
        html_body = content_type_lower in {
            "text/html",
            "application/xhtml+xml",
        } or looks_like_html(response.content)
        if not html_body:
            possible_file = (
                _is_document_path(page_url)
                or "pdf" in content_type_lower
                or "wordprocessingml" in content_type_lower
                or "octet-stream" in content_type_lower
                or not content_type_lower
            )
            if possible_file:
                document_type = _decode_document_type(response.content)
                if document_type:
                    _add_candidate(
                        candidates,
                        page_url,
                        Path(urllib.parse.urlsplit(page_url).path).name,
                        payload=response.content,
                        response_url=str(response.url),
                        headers=dict(response.headers),
                    )
            return

        page_html = response.text
        _add_page_downloads(candidates, page_html, str(response.url), site_key)
        next_pages = _sitemap_urls_from_page(page_html, str(response.url), site_key)
        for next_url in reversed(next_pages):
            schedule(next_url)

    while stack:
        page_url = stack.pop()
        try:
            await visit_page(page_url)
        except (httpx.HTTPError, ValueError, TypeError) as error:
            print(f"[page-error] {page_url}: {error}")
        except Exception as error:
            print(f"[page-error] {page_url}: {type(error).__name__}: {error}")
        await process_candidates()

    return len(scheduled)


def _drive_ids(url: str) -> list[str]:
    parts = urllib.parse.urlsplit(url)
    match = re.search(r"/file/d/([^/]+)", parts.path)
    file_id = (
        match.group(1)
        if match
        else urllib.parse.parse_qs(parts.query).get("id", [None])[0]
    )
    if not file_id:
        return []
    return [
        urllib.parse.urlunsplit(
            (
                "https",
                "drive.usercontent.google.com",
                "/download",
                "id=" + urllib.parse.quote(file_id) + "&export=download&confirm=t",
                "",
            )
        ),
        urllib.parse.urlunsplit(
            (
                "https",
                "drive.google.com",
                "/uc",
                "export=download&id=" + urllib.parse.quote(file_id),
                "",
            )
        ),
    ]


def _provider_attempt_urls(candidate: DownloadCandidate) -> list[str]:
    if candidate.provider == "drive":
        return list(dict.fromkeys([*_drive_ids(candidate.url), candidate.url]))
    if candidate.provider == "dropbox":
        parts = urllib.parse.urlsplit(candidate.url)
        host = _hostname(candidate.url) or ""
        if host == "dropboxusercontent.com" or host.endswith(".dropboxusercontent.com"):
            return [candidate.url]
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        alternatives = []
        for key in ("dl", "raw"):
            replaced = [(name, value) for name, value in query if name.lower() != key]
            replaced.append((key, "1"))
            alternatives.append(
                urllib.parse.urlunsplit(
                    parts._replace(
                        query=urllib.parse.urlencode(replaced, doseq=True), fragment=""
                    )
                )
            )
        return list(dict.fromkeys([*alternatives, candidate.url]))
    return [candidate.url]


def _provider_page_targets(
    page_html: str, page_url: str, provider: str
) -> list[tuple[str, str, str, dict[str, str]]]:
    """Extract explicit provider download controls; never walk provider navigation."""
    soup = BeautifulSoup(page_html, "html.parser")
    found: dict[str, tuple[str, str, dict[str, str]]] = {}

    def add(raw: str, label: str = "", *, allow_external: bool = False) -> None:
        absolute = urllib.parse.urljoin(page_url, html.unescape(raw.strip()))
        parts = urllib.parse.urlsplit(absolute)
        if (
            parts.scheme.lower() not in ("http", "https")
            or not parts.netloc
            or parts.username is not None
            or parts.password is not None
        ):
            return
        try:
            parts.port
        except ValueError:
            return
        target_provider = _provider_kind(absolute)
        if target_provider != provider and not (
            allow_external and parts.scheme == "https"
        ):
            return
        found.setdefault(absolute, ("GET", _meaningful_label(label), {}))

    for element in soup.find_all(True):
        marker_parts = [
            element.get_text(" ", strip=True),
            element.get("title", ""),
            element.get("aria-label", ""),
            element.get("id", ""),
            element.get("class", []),
        ]
        marker = " ".join(
            " ".join(str(part) for part in value)
            if isinstance(value, list)
            else str(value)
            for value in marker_parts
        )
        if NON_DOWNLOAD_MARKER.search(marker) or not DOWNLOAD_MARKER.search(marker):
            continue
        for name in (
            "href",
            "formaction",
            "action",
            "data-url",
            "data-href",
            "data-link",
            "data-download-url",
        ):
            value = element.get(name)
            if isinstance(value, str):
                add(value, marker)

    for match in PROVIDER_DOWNLOAD_FIELD.finditer(page_html.replace("\\/", "/")):
        add(match.group(1), allow_external=True)

    return [
        (url, method, label, data)
        for url, (method, label, data) in list(found.items())[:MAX_PROVIDER_TARGETS]
    ]


def _provider_controls_from_drive(
    page_html: str, page_url: str
) -> list[tuple[str, str, str, dict[str, str]]]:
    soup = BeautifulSoup(page_html, "html.parser")
    results: list[tuple[str, str, str, dict[str, str]]] = []
    for form in soup.find_all("form"):
        action = urllib.parse.urljoin(page_url, form.get("action", ""))
        if _provider_kind(action) != "drive":
            continue
        data = []
        for input_element in form.find_all("input"):
            name = input_element.get("name")
            if name:
                data.append((name, input_element.get("value", "")))
        method = form.get("method", "get").upper()
        if method == "POST":
            results.append((action, "POST", "Drive confirmation", dict(data)))
        else:
            query = urllib.parse.parse_qsl(
                urllib.parse.urlsplit(action).query, keep_blank_values=True
            )
            query.extend(data)
            results.append(
                (
                    urllib.parse.urlunsplit(
                        urllib.parse.urlsplit(action)._replace(
                            query=urllib.parse.urlencode(query, doseq=True)
                        )
                    ),
                    "GET",
                    "Drive confirmation",
                    {},
                )
            )

    for anchor in soup.find_all("a", href=True):
        label = anchor.get_text(" ", strip=True) or anchor.get("title", "") or ""
        if NON_DOWNLOAD_MARKER.search(label) or not DOWNLOAD_MARKER.search(label):
            continue
        url = urllib.parse.urljoin(page_url, anchor["href"])
        if _provider_kind(url) == "drive":
            results.append((url, "GET", _meaningful_label(label), {}))
    return results[:MAX_PROVIDER_TARGETS]


def _mega_file_key(url: str) -> tuple[str, bytes] | None:
    parts = urllib.parse.urlsplit(url)
    match = re.search(r"/file/([A-Za-z0-9_-]+)", parts.path)
    fragment = urllib.parse.unquote(parts.fragment)
    if match:
        handle, key_text = match.group(1), fragment
    elif fragment.startswith("!"):
        fields = fragment[1:].split("!")
        if len(fields) < 2:
            return None
        handle, key_text = fields[0], fields[1]
    else:
        return None
    try:
        file_key = base64.urlsafe_b64decode(key_text + "=" * (-len(key_text) % 4))
    except (ValueError, base64.binascii.Error):
        return None
    if len(file_key) != 32:
        return None
    return handle, file_key


def _mega_filename(attributes: str, aes_key: bytes) -> str:
    try:
        encrypted = base64.urlsafe_b64decode(attributes + "=" * (-len(attributes) % 4))
        decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(b"\0" * 16)).decryptor()
        plain = decryptor.update(encrypted) + decryptor.finalize()
        padding = plain[-1]
        if 0 < padding <= 16 and plain.endswith(bytes([padding]) * padding):
            plain = plain[:-padding]
        if not plain.startswith(b"MEGA"):
            return ""
        metadata = json.loads(plain[4:].decode("utf-8"))
        return str(metadata.get("n", ""))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return ""


async def _download_mega(
    client: httpx.AsyncClient, candidate: DownloadCandidate
) -> tuple[bytes, str, dict[str, str], str] | None:
    parsed = _mega_file_key(candidate.url)
    if parsed is None:
        return None
    handle, file_key = parsed
    words = struct.unpack(">8I", file_key)
    aes_key = struct.pack(
        ">4I", *(words[index] ^ words[index + 4] for index in range(4))
    )
    iv = struct.pack(">4I", words[4], words[5], 0, 0)
    api_url = "https://g.api.mega.co.nz/cs"
    try:
        response = await client.post(
            api_url,
            params={"id": str(int(time.time() * 1000))},
            json=[{"a": "g", "g": 1, "p": handle}],
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()
        if (
            not isinstance(result, list)
            or not result
            or not isinstance(result[0], dict)
        ):
            return None
        info = result[0]
        download_url = info.get("g")
        if not isinstance(download_url, str):
            return None
        encrypted_response = await client.get(
            download_url, follow_redirects=True, timeout=REQUEST_TIMEOUT
        )
        encrypted_response.raise_for_status()
        decryptor = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).decryptor()
        data = decryptor.update(encrypted_response.content) + decryptor.finalize()
        document_type = _decode_document_type(data)
        if document_type is None:
            return None
        filename = _mega_filename(str(info.get("at", "")), aes_key)
        if filename:
            candidate.label = filename
        return (
            data,
            document_type,
            dict(encrypted_response.headers),
            str(encrypted_response.url),
        )
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return None


def _filename_hint(
    candidate: DownloadCandidate,
    response_url: str,
    headers: dict[str, str],
    extension: str,
) -> str:
    if candidate.provider == "mega" and candidate.label:
        hinted = candidate.label
    else:
        disposition = headers.get("content-disposition", "")
        if disposition:
            message = Message()
            message["content-disposition"] = disposition
            hinted = message.get_filename() or ""
        else:
            hinted = ""
        if not hinted:
            hinted = urllib.parse.unquote(
                Path(urllib.parse.urlsplit(response_url).path).name
            )
    if not hinted or hinted.lower() in {"file", "download", "view", "uc", ""}:
        hinted = candidate.label or ""
    hinted = hinted.replace("\\", "/").rsplit("/", 1)[-1]
    hinted = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", hinted).strip(" .")
    if not hinted:
        host = _hostname(candidate.url) or "book"
        digest = hashlib.sha256(candidate.url.encode("utf-8")).hexdigest()[:10]
        hinted = f"{host}-{digest}"
    suffix = "." + extension
    if hinted.lower().endswith((".pdf", ".docx")):
        hinted = hinted[: -len(Path(hinted).suffix)]
    return hinted[:180].rstrip(" .") + suffix


def _reserve_unique_path(directory: Path, filename: str) -> Path:
    base = Path(filename)
    index = 1
    while True:
        candidate = (
            directory / base.name
            if index == 1
            else directory / f"{base.stem} ({index}){base.suffix}"
        )
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            index += 1
            continue
        os.close(descriptor)
        return candidate


def _save_payload(
    data: bytes,
    extension: str,
    candidate: DownloadCandidate,
    response_url: str,
    headers: dict[str, str],
    output_dir: Path,
) -> tuple[Path | None, str]:
    extension = extension.lower()
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = _filename_hint(candidate, response_url, headers, extension)
    fd, temp_name = tempfile.mkstemp(prefix=".download-", suffix=".tmp", dir=output_dir)
    os.close(fd)
    temp_path = Path(temp_name)
    destination: Path | None = None
    try:
        temp_path.write_bytes(data)
        if extension == "pdf":
            outcome = linearize(temp_path)
            if outcome.state in {"broken", "failed"}:
                return None, f"PDF validation/linearization failed: {outcome.detail}"
            detail = (
                "unlocked and linearized"
                if outcome.state == "linearized"
                else "already linearized"
            )
            if outcome.state == "locked":
                detail = "saved encrypted: requires a user password"
        else:
            detail = "saved DOCX"
        destination = _reserve_unique_path(output_dir, filename)
        os.replace(temp_path, destination)
        return destination, detail
    except OSError as error:
        if destination is not None:
            destination.unlink(missing_ok=True)
        return None, str(error)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


async def _http_response(
    client: httpx.AsyncClient,
    url: str,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
) -> httpx.Response | None:
    try:
        if method == "POST":
            response = await client.post(
                url, data=data or {}, follow_redirects=True, timeout=REQUEST_TIMEOUT
            )
        else:
            response = await client.get(
                url, follow_redirects=True, timeout=REQUEST_TIMEOUT
            )
        response.raise_for_status()
        return response
    except (httpx.HTTPError, ValueError):
        return None


def _payload_from_response(
    response: httpx.Response, candidate: DownloadCandidate
) -> tuple[bytes, str, dict[str, str], str] | None:
    document_type = _decode_document_type(response.content)
    if document_type is None:
        return None
    return response.content, document_type, dict(response.headers), str(response.url)


async def _resolve_candidate(
    client: httpx.AsyncClient, candidate: DownloadCandidate
) -> tuple[bytes, str, dict[str, str], str] | None:
    if candidate.payload is not None:
        document_type = _decode_document_type(candidate.payload)
        if document_type:
            return (
                candidate.payload,
                document_type,
                candidate.headers,
                candidate.response_url or candidate.url,
            )

    if candidate.provider == "mega":
        return await _download_mega(client, candidate)

    seen: set[str] = set()
    attempts = _provider_attempt_urls(candidate)
    for attempt_url in attempts:
        pending = [(attempt_url, "GET", {}, 0)]
        while pending:
            url, method, data, depth = pending.pop(0)
            if url in seen or len(seen) >= MAX_PROVIDER_STEPS:
                continue
            seen.add(url)
            if candidate.provider == "mediafire":
                try:
                    response = await fetch_with_retries(
                        client,
                        url,
                        retries=2,
                        timeout=REQUEST_TIMEOUT,
                        request_headers={"Accept": DOCUMENT_ACCEPT},
                    )
                except Exception:
                    continue
            else:
                response = await _http_response(client, url, method=method, data=data)
                if response is None:
                    continue

            payload = _payload_from_response(response, candidate)
            if payload is not None:
                return payload

            if not is_html_response(response) or depth >= 2:
                continue
            if candidate.provider in {None, "mediafire"}:
                recovered = await recover_file_response(
                    client, response, retries=2, timeout=REQUEST_TIMEOUT
                )
                if recovered is not None:
                    payload = _payload_from_response(recovered, candidate)
                    if payload is not None:
                        return payload
                if candidate.provider is None:
                    for nested_url, nested_provider, label in _extract_provider_links(
                        response.text, str(response.url)
                    ):
                        nested = DownloadCandidate(
                            nested_url, label=label, provider=nested_provider
                        )
                        payload = await _resolve_candidate(client, nested)
                        if payload is not None:
                            return payload
                    continue

            targets: list[tuple[str, str, str, dict[str, str]]] = []
            if candidate.provider == "drive":
                targets.extend(
                    _provider_controls_from_drive(response.text, str(response.url))
                )
            targets.extend(
                _provider_page_targets(
                    response.text, str(response.url), candidate.provider or ""
                )
            )
            for target_url, target_method, label, target_data in targets:
                # TeraBox's explicit dlink/downloadUrl fields can point at a CDN host;
                # these are fetched only as file candidates and never crawled as pages.
                if target_url not in seen and len(seen) < MAX_PROVIDER_STEPS:
                    pending.append((target_url, target_method, target_data, depth + 1))
    return None


def _candidate_from_entry(
    url: str, candidates: dict[str, DownloadCandidate], sites: dict[str, list[str]]
) -> None:
    url = url.strip()
    if not url or url.startswith("#"):
        return
    if _is_scribd(url):
        print(f"[skip-scribd] {url}")
        return
    provider_kind = _provider_kind(url)
    provider_url = _provider_url(url)
    if provider_url:
        clean_url, provider = provider_url
        _add_candidate(candidates, clean_url, provider=provider)
        return
    if provider_kind is not None:
        print(f"[skip-provider-url] {url}")
        return
    if excluded_site_reason(url) is not None:
        print(f"[skip-domain] {url}")
        return
    if _is_document_path(url):
        clean_url = guard_download_url(url)
        if clean_url:
            _add_candidate(candidates, clean_url)
        return
    key = _site_key(url)
    if key is None:
        print(f"[skip-invalid] {url}")
        return
    clean_url = _normalized_page_url(url, key)
    if clean_url is None:
        print(f"[skip-invalid] {url}")
        return
    sites.setdefault(key, []).append(clean_url)


async def _download_entries_async(
    entry_source: str | Path, output_dir: Path
) -> DownloadSummary:
    summary = DownloadSummary()
    source = os.fspath(entry_source).strip()
    parts = urllib.parse.urlsplit(source)
    if parts.scheme.lower() in {"http", "https"} and parts.netloc:
        entries = [source]
    else:
        entry_path = Path(source)
        try:
            entries = entry_path.read_text(encoding="utf-8-sig").splitlines()
        except OSError as error:
            raise FileNotFoundError(
                f"Cannot read entry file {entry_path}: {error}"
            ) from error
    if not entries:
        print(f"[empty] {source} has no URLs")
        return summary

    candidates: dict[str, DownloadCandidate] = {}
    sites: dict[str, list[str]] = {}
    for line in entries:
        _candidate_from_entry(line, candidates, sites)
    summary.sites = len(sites)
    if not candidates and not sites:
        print(f"[empty] {source} has no usable URLs")
        return summary

    processed_candidates: set[str] = set()
    async with httpx.AsyncClient(
        follow_redirects=False,
        headers={"Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8"},
    ) as client:

        async def download_one(candidate: DownloadCandidate) -> None:
            try:
                payload = await _resolve_candidate(client, candidate)
            except Exception as error:
                payload = None
                reason = str(error) or type(error).__name__
            else:
                reason = (
                    "provider did not return a valid PDF/DOCX "
                    "(possibly expired, private, or confirmation-blocked)"
                )
            if payload is None:
                summary.unresolved.append(candidate.url)
                print(f"[unresolved] {candidate.url}: {reason}")
                return
            data, extension, headers, final_url = payload
            path, detail = await asyncio.to_thread(
                _save_payload,
                data,
                extension,
                candidate,
                final_url,
                headers,
                output_dir,
            )
            if path is None:
                summary.unresolved.append(candidate.url)
                print(f"[rejected] {candidate.url}: {detail}")
                return
            summary.saved.append(path)
            print(f"[saved] {path.name} ({detail})")

        async def process_candidates() -> None:
            for key, candidate in list(candidates.items()):
                if key in processed_candidates:
                    continue
                processed_candidates.add(key)
                summary.candidates += 1
                print(f"[download] {candidate.url}", flush=True)
                await download_one(candidate)

        await process_candidates()
        for seed_urls in sites.values():
            count = await _crawl_site(client, seed_urls, candidates, process_candidates)
            summary.pages += count
            print(f"[site] {seed_urls[0]}: checked {count:,} page URL(s)")
            await process_candidates()
    return summary


def download_entries(
    entry_source: str | Path = ENTRY_FILE,
    output_dir: str | Path = OUTPUT_DIR,
) -> DownloadSummary:
    """Process one site or entry-file sites sequentially, saving each file in turn."""
    return asyncio.run(_download_entries_async(entry_source, Path(output_dir)))


if __name__ == "__main__":
    try:
        source = sys.argv[1] if len(sys.argv) > 1 else ENTRY_FILE
        if len(sys.argv) > 2:
            raise ValueError("Usage: site_downloader.py [site-url-or-entry-file]")
        report = download_entries(source)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(
        f"Finished: {report.sites} site(s), {report.pages:,} page URL(s), "
        f"{report.candidates} candidate(s), {len(report.saved)} saved, "
        f"{len(report.unresolved)} unresolved."
    )

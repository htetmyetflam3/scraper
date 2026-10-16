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
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from disk_floor import DiskTight, check_disk, size_text
from domain_guard import excluded_site_reason
from download_rate import client_options as _rate_client_options, limit_downloads
from download_links import FILE_EXTENSION, extract_file_links, guard_download_url
from mediafire import (
    fetch_with_retries,
    is_docx,
    is_html_response,
    recover_file_response,
)
from pdf_support import is_pdf, looks_like_html
from save_local import (
    filename_hint as _filename_hint,
    reserve_unique_path as _reserve_unique_path,
    save_payload as _save_payload,
)
from site_login import ensure_logged_in_async
from site_size_guard import FALLBACK_SITEMAP_PATHS, large_site_reason

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIR = REPO_ROOT.parent / "scan"
ENTRY_FILE = SCAN_DIR / "entry.txt"
BLOCKED_FILE = SCAN_DIR / "blocked.txt"
DOWNLOADED_FILE = SCAN_DIR / "downloaded.txt"
TIMELINE_FILE = SCAN_DIR / "timeline.log"
FAILED_FILE = SCAN_DIR / "failed.txt"
OUTPUT_DIR = REPO_ROOT / "pdfs"
MAX_SITE_PAGES = 3_000
REQUEST_TIMEOUT = 30.0
SITEMAP_TIMEOUT = 12.0
MAX_PROVIDER_STEPS = 3
MAX_PROVIDER_TARGETS = 8
# After the crawl (every URL found and every download tried): each URL in
# scan/failed.txt and scan/blocked.txt gets RETRY_TURNS turns, one try per
# turn, no waiting; one that works is removed from both files.
RETRY_TURNS = 3
RETRY_STATUSES = ("unresolved", "rejected", "blocked")
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


class StopRun(BaseException):
    """Raised at a safe point when the night guard says stop (6-hour mark).

    A BaseException like KeyboardInterrupt, so no ``except Exception`` in
    the crawl swallows it; pages are journaled only after their files, so a
    rerun resumes exactly like after Ctrl+C."""


@dataclass
class DownloadCandidate:
    url: str
    label: str = ""
    provider: str | None = None
    payload: bytes | None = None
    response_url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    method: str = "GET"
    data: dict[str, str] = field(default_factory=dict)


@dataclass
class DownloadSummary:
    sites: int = 0
    pages: int = 0
    candidates: int = 0
    saved: list[Path] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    no_download: list[str] = field(default_factory=list)
    downloaded: list[str] = field(default_factory=list)
    stopped: str = ""
    retried: int = 0  # URLs from scan/failed.txt + scan/blocked.txt that got the retry turns
    recovered: list[str] = field(default_factory=list)
    retry_left: int = 0


def retry_line(summary: DownloadSummary) -> str:
    """The retry turns' result, for the finished email and the night report."""
    return (
        f"🔁 Retry ({RETRY_TURNS} turns over scan/failed.txt + scan/blocked.txt): "
        f"{len(summary.recovered)} of {summary.retried} URL(s) recovered, "
        f"{summary.retry_left} still failing."
    )


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


# Site-internal search endpoints. Every search page links more searches with
# fresh queries, forming a near-infinite space the visited-set can never
# close, so these URLs are never scheduled as crawl pages. Files and
# file-host links are unaffected.
SEARCH_PATH_SEGMENTS = frozenset({"search"})
SEARCH_QUERY_KEYS = frozenset(
    {
        "s",  # WordPress
        "q",  # Blogger /search?q=, site search boxes
        "query",
        "search",
        "search_query",
        "searchword",  # Joomla
        "keyword",
        "keywords",
    }
)


def _is_site_search_url(url: str) -> bool:
    """True for site-internal search-result pages (``?s=``, ``/search?q=``)."""
    try:
        parts = urllib.parse.urlsplit(url)
        first = urllib.parse.unquote(parts.path).strip("/").split("/", 1)[0].lower()
    except ValueError:
        return False
    if first in SEARCH_PATH_SEGMENTS:
        return True
    try:
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        # Absurd field counts are loop-junk by definition: skip them too.
        return True
    return any(key.lower() in SEARCH_QUERY_KEYS for key, _ in query)


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
    if _is_site_search_url(cleaned):
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


# Session-rotating CSRF token fields. They must ride along on the POST, but a
# fresh token every run would make dedupe/blocked identities useless, so the
# stable form identity below excludes them (the book id etc. still pins it).
VOLATILE_FORM_FIELDS = frozenset(
    {
        "_token",  # Laravel
        "csrf_token",  # Symfony / Flask-WTF
        "csrfmiddlewaretoken",  # Django
        "authenticity_token",  # Rails
        "__requestverificationtoken",  # ASP.NET (compared lowercased)
    }
)


def _stable_form_key(method: str, url: str, data: dict[str, str]) -> str:
    """Dedupe/block identity for form submissions, e.g.
    ``POST https://site/download-book uid=42655``. Forms posting to one
    shared action URL (one per book) must NOT collapse to a single key."""
    kept = sorted(
        (name, value)
        for name, value in data.items()
        if name.lower() not in VOLATILE_FORM_FIELDS
    )
    identity = f"{method.upper()} {url}"
    if kept:
        identity += " " + urllib.parse.urlencode(kept, doseq=True)
    return identity


def _blocked_identity(candidate: DownloadCandidate) -> str:
    """What goes into scan/blocked.txt: stable form identity for POSTs, else
    the bare URL (unchanged behaviour for plain candidates)."""
    if candidate.method == "POST" or candidate.data:
        return _stable_form_key(candidate.method, candidate.url, candidate.data)
    return candidate.url.strip()


def _candidate_key(candidate: DownloadCandidate) -> str:
    if candidate.method == "POST" or candidate.data:
        return _stable_form_key(candidate.method, candidate.url, candidate.data)
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
    method: str = "GET",
    data: dict[str, str] | None = None,
) -> bool:
    candidate = DownloadCandidate(
        url=url,
        label=_meaningful_label(label),
        provider=provider or _provider_kind(url),
        payload=payload,
        response_url=response_url,
        headers=headers or {},
        method=method,
        data=dict(data or {}),
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


#: ``<input>`` types that never carry a submittable download-form value.
_FORM_SKIP_TYPES = frozenset({"submit", "button", "image", "reset", "file"})


def _extract_download_forms(
    page_html: str, page_url: str, site_key: str
) -> list[tuple[str, str, str, dict[str, str]]]:
    """Find same-site download forms as ``(action URL, method, label, data)``.

    Replicates what the page's own ``$.post(action, $(form).serialize())``
    sends: every named, enabled, successful control (hidden inputs, text,
    selects, textareas, checked boxes) except buttons and file inputs.
    CSRF tokens stay in the data (the POST needs them); only the dedupe
    identity excludes them. GET forms fold their fields into the action URL;
    anything that is not GET is submitted as POST.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    found: dict[str, tuple[str, str, str, dict[str, str]]] = {}
    for form in soup.find_all("form"):
        values: list[str | list[str]] = [
            form.get_text(" ", strip=True),
            form.get("title", ""),
            form.get("aria-label", ""),
            form.get("id", ""),
            form.get("class", []),
        ]
        label = " ".join(
            " ".join(str(part) for part in value)
            if isinstance(value, list)
            else str(value)
            for value in values
        )
        if not DOWNLOAD_MARKER.search(label) or NON_DOWNLOAD_MARKER.search(label):
            continue

        base_action = form.get("action") or page_url
        base_method = (form.get("method") or "GET").strip().upper() or "GET"
        variants = [(base_action, base_method)]
        for submitter in form.find_all(["button", "input"]):
            subtype = (submitter.get("type") or "").lower()
            if submitter.name == "input" and subtype not in {"submit", "image"}:
                continue
            if submitter.name == "button" and subtype not in {"", "submit"}:
                continue
            override = submitter.get("formaction")
            if isinstance(override, str) and override.strip():
                override_method = submitter.get("formmethod")
                if isinstance(override_method, str) and override_method.strip():
                    variants.append((override, override_method.strip().upper()))
                else:
                    variants.append((override, base_method))

        data: dict[str, str] = {}
        for field in form.find_all(["input", "select", "textarea"]):
            name = field.get("name")
            if not isinstance(name, str) or not name or field.has_attr("disabled"):
                continue
            if field.name == "input":
                field_type = (field.get("type") or "text").lower()
                if field_type in _FORM_SKIP_TYPES:
                    continue
                if field_type in {"checkbox", "radio"} and not field.has_attr("checked"):
                    continue
                value = field.get("value", "")
                data[name] = value if isinstance(value, str) else str(value)
            elif field.name == "textarea":
                data[name] = field.get_text()
            else:  # select: selected options, else the first for single-select
                options = field.find_all("option")
                selected = [opt for opt in options if opt.has_attr("selected")]
                if not selected and not field.has_attr("multiple") and options:
                    selected = options[:1]
                for option in selected:  # multi-select collapses to last (dict)
                    if option.has_attr("value"):
                        picked = option["value"]
                        data[name] = picked if isinstance(picked, str) else str(picked)
                    else:
                        data[name] = option.get_text(strip=True)

        label = _meaningful_label(label)
        for raw_action, raw_method in variants:
            absolute = urllib.parse.urljoin(page_url, raw_action)
            if raw_method == "GET" and data:
                parts = urllib.parse.urlsplit(absolute)
                query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                query.extend(sorted(data.items()))
                absolute = urllib.parse.urlunsplit(
                    parts._replace(
                        query=urllib.parse.urlencode(query, doseq=True), fragment=""
                    )
                )
            target = _normalized_page_url(absolute, site_key)
            if target is None:
                continue
            if _is_document_path(target):
                # A file target is fetched directly, never submitted to.
                key = _stable_form_key("GET", target, {})
                found.setdefault(key, (target, "GET", label, {}))
                continue
            method = "GET" if raw_method == "GET" else "POST"
            fields = {} if method == "GET" else dict(data)
            key = _stable_form_key(method, target, fields)
            found.setdefault(key, (target, method, label, fields))

    return list(found.values())


def _add_page_downloads(
    candidates: dict[str, DownloadCandidate],
    page_html: str,
    page_url: str,
    site_key: str,
    known_pages: set[str] | None = None,
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

    post_actions: set[str] = set()
    for url, method, label, data in _extract_download_forms(
        page_html, page_url, site_key
    ):
        _add_candidate(candidates, url, label, method=method, data=data)
        if method == "POST":
            post_actions.add(url)

    for url, label in _extract_extensionless_controls(page_html, page_url, site_key):
        # Targets already scheduled as pages are covered by the page visit
        # itself (same file/payload handling); candidating them again only
        # re-fetches the page and mislabels it as a failed download.
        if known_pages is not None and url in known_pages:
            continue
        # A form action is submitted to, never GET: the form candidate owns
        # it, so a duplicate GET would only earn a doomed 405/419 verdict.
        if url in post_actions:
            continue
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


def _sitemap_scan(
    seed_url: str, max_pages: int | None = MAX_SITE_PAGES
) -> tuple[str | None, list[str], list[str]]:
    """Use the retained sitemap guard and return same-site pages/provider files.

    max_pages=None disables the large-site skip (single-site runs).
    """
    site_key = _site_key(seed_url)
    if site_key is None:
        return None, [], []
    records: list[tuple[str, str]] = []
    collected_downloads: list[str] = []
    with httpx.Client(
        **_rate_client_options(),
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
            max_pages=max_pages if max_pages is not None else 0,
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


def _journal_path(site_key: str) -> Path:
    """Per-site crawl journal: finished pages + their children, as JSONL."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", site_key).strip("_") or "site"
    return SCAN_DIR / f"crawl-{safe}.jsonl"


def _load_journal(site_key: str) -> tuple[set[str], dict[str, list[str]]]:
    """(finished page URLs, page -> children). Corrupt lines are skipped so a
    kill mid-write never breaks the next run."""
    done: set[str] = set()
    children: dict[str, list[str]] = {}
    try:
        lines = _journal_path(site_key).read_text(encoding="utf-8").splitlines()
    except OSError:
        return done, children
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        url = record.get("u")
        links = record.get("c")
        if not isinstance(url, str) or not isinstance(links, list):
            continue
        clean = [link for link in links if isinstance(link, str)]
        done.add(url)
        children[url] = clean
    return done, children


class _CrawlJournal:
    """Append-only per-site journal; every line is flushed so Ctrl+C and kills
    lose at most the page in flight (which is simply retried next run)."""

    def __init__(self, site_key: str) -> None:
        self.handle = None
        try:
            path = _journal_path(site_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = path.open("a", encoding="utf-8")
        except OSError as error:
            print(f"[journal-warn] {site_key}: {error}; continuing without resume")

    def append(self, url: str, children: list[str]) -> None:
        if self.handle is None:
            return
        try:
            self.handle.write(json.dumps({"u": url, "c": children}) + "\n")
            self.handle.flush()
        except OSError as error:
            print(f"[journal-warn] cannot record {url}: {error}")
            self.handle = None

    def close(self) -> None:
        if self.handle is not None:
            try:
                self.handle.close()
            except OSError:
                pass
            self.handle = None


async def _crawl_site(
    client: httpx.AsyncClient,
    seed_urls: list[str],
    candidates: dict[str, DownloadCandidate],
    process_candidates: Callable[[], Awaitable[None]],
    max_pages: int | None = MAX_SITE_PAGES,
    guard=None,
) -> int:
    """Crawl one site breadth-first, expanding every sibling branch to its leaves.

    Starts at the site root(s) plus sitemap entry points, visits each level
    before descending, and fetches up to max_pages fresh URLs per run.
    max_pages=None removes the limit and the sitemap-size skip (single-site
    runs). Finished pages are journaled, so reruns replay past them without
    network and continue where the previous run stopped. Each page's files
    are downloaded before continuing.

    With a night guard (main.py --process) the crawl can also be stopped
    between pages (StopRun); a page that fails is skipped the same way.
    """
    site_key = _site_key(seed_urls[0])
    if site_key is None:
        return 0
    if max_pages is None:
        print(f"[nocap] {site_key}: single-site run, no page limit")
    try:
        sitemap_reason, sitemap_pages, sitemap_downloads = await asyncio.to_thread(
            _sitemap_scan, seed_urls[0], max_pages=max_pages
        )
    except Exception as error:
        sitemap_reason, sitemap_pages, sitemap_downloads = None, [], []
        print(f"[sitemap] {seed_urls[0]}: {error}")
    if sitemap_reason:
        print(f"[skip-large] {seed_urls[0]}: {sitemap_reason}")
        return 0
    done, journal_children = _load_journal(site_key)
    journal = _CrawlJournal(site_key)
    if done:
        _timeline("resume", f"{site_key} pages={len(done):,}")
        print(
            f"[resume] {site_key}: {len(done):,} page(s) finished on earlier "
            f"run(s); replaying them without network @ {_stamp()}"
        )

    for url in sitemap_downloads:
        provider = _provider_kind(url)
        if provider is not None:
            _add_candidate(candidates, url, provider=provider)
        elif excluded_site_reason(url) is None and _is_document_path(url):
            cleaned = guard_download_url(url)
            if cleaned:
                _add_candidate(candidates, cleaned)

    queue: deque[str] = deque()
    scheduled: set[str] = set()
    depths: dict[str, int] = {}
    discovered: list[str] = []
    cap_logged = False
    fresh_scheduled = 0
    fresh_fetched = 0
    replayed = 0
    live_announced = False

    def schedule(raw_url: str, depth: int = 0) -> None:
        nonlocal cap_logged, fresh_scheduled
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
        if normalized in scheduled:
            return
        # Recorded before the cap check so cap-cut branches stay in the
        # journal and the next run can continue them instead of losing them.
        discovered.append(normalized)
        journaled = normalized in done
        if (
            not journaled
            and max_pages is not None
            and fresh_scheduled >= max_pages
        ):
            if not cap_logged:
                cap_logged = True
                print(
                    f"[cap] {site_key}: {max_pages:,}-URL limit reached "
                    "this run; rerun to continue",
                    flush=True,
                )
            return
        scheduled.add(normalized)
        if not journaled:
            fresh_scheduled += 1
        depths[normalized] = depth
        queue.append(normalized)

    for seed in seed_urls:
        schedule(seed, 0)
    for sitemap_page in sitemap_pages:
        schedule(sitemap_page, 0)
    await process_candidates()

    async def visit_page(page_url: str) -> list[str] | None:
        """Fetch one page; returns discovered children, or None when the page
        must be retried (non-200), so transient failures are never
        journaled done."""
        depth = depths.get(page_url, 0)
        print(f"[page] depth={depth} {page_url}", flush=True)
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
                    schedule(target, depth)
            return list(discovered)

        if response.status_code != 200:
            return None
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
            return []

        page_html = response.text
        _add_page_downloads(
            candidates, page_html, str(response.url), site_key, scheduled
        )
        next_pages = _sitemap_urls_from_page(page_html, str(response.url), site_key)
        for next_url in next_pages:
            schedule(next_url, depth + 1)
        return list(discovered)

    try:
        while queue:
            page_url = queue.popleft()
            if page_url in done:
                for child in journal_children.get(page_url, []):
                    schedule(child, depths.get(page_url, 0) + 1)
                replayed += 1
                continue
            if guard is not None:
                guard.checkpoint()
            discovered.clear()
            children: list[str] | None = None
            had_candidates = len(candidates)
            try:
                children = await visit_page(page_url)
            except (httpx.HTTPError, ValueError, TypeError) as error:
                print(f"[page-error] {page_url}: {error}")
            except Exception as error:
                print(f"[page-error] {page_url}: {type(error).__name__}: {error}")
            await process_candidates()
            if children is not None:
                fresh_fetched += 1
                journal.append(page_url, children)
                if (
                    not children
                    and depths.get(page_url, 0) == 0
                    and len(candidates) == had_candidates
                ):
                    print(
                        f"[seed-empty] {page_url}: entry page gave no links or "
                        "downloads; check the seed URL"
                    )
                if replayed and not live_announced:
                    live_announced = True
                    _timeline("live", f"{site_key} from={page_url}")
                    print(
                        f"[resume] {site_key}: replayed {replayed:,} journaled "
                        f"page(s); continuing live from {page_url} @ {_stamp()}"
                    )
    finally:
        journal.close()
    return fresh_fetched

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


# Local saving (filename hints, collision-free names, PDF validation
# and atomic writes) lives in save_local.py, imported above under the
# original private names so every call site here stays unchanged.


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


def _posted_response_url(response: httpx.Response, base_url: str) -> str | None:
    """Follow-a-link responses: some endpoints (form download handlers) answer
    with a bare URL instead of the file, and the page JS navigates to it."""
    if len(response.content) > 8192:  # a URL never exceeds this
        return None
    text = response.content.decode("utf-8-sig", "replace").strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        if not isinstance(parsed, str):
            return None
        text = parsed.strip()
    if (
        not text
        or len(text) > 2048
        or any(
            char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in text
        )
        or any(char in "<>\"\\^`{|}" for char in text)
    ):
        return None
    absolute = urllib.parse.urljoin(base_url, text)
    try:
        parts = urllib.parse.urlsplit(absolute)
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
    return guard_download_url(absolute) or absolute


#: Returned by :func:`_resolve_candidate` when the fetched page offers no
#: download at all: no file link, file-host link, or download control. This
#: is *not* a failed download, just a plain page that should not be retried.
_NO_DOWNLOAD = object()


def _has_download_affordance(
    page_html: str, page_url: str, candidate_url: str
) -> bool:
    """True when the page shows any file link, file-host link, form, control."""
    if extract_file_links(page_html, page_url):
        return True
    if _extract_provider_links(page_html, page_url):
        return True
    site_key = _site_key(candidate_url)
    if site_key is None:
        return False
    if _extract_download_forms(page_html, page_url, site_key):
        return True
    return bool(_extract_extensionless_controls(page_html, page_url, site_key))


async def _resolve_candidate(
    client: httpx.AsyncClient, candidate: DownloadCandidate
) -> tuple[bytes, str, dict[str, str], str] | None:
    """Resolve to ``(bytes, ext, headers, url)``; ``_NO_DOWNLOAD`` for plain
    pages without any download; ``None`` when the download genuinely failed
    (expired, private, confirmation-blocked, network error)."""
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
    for attempt_index, attempt_url in enumerate(attempts):
        if attempt_index == 0 and candidate.method == "POST":
            pending = [(attempt_url, candidate.method, dict(candidate.data), 0)]
        else:
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

            if depth < 2:
                posted_url = _posted_response_url(response, url)
                # Same pattern as provider targets below: queue without
                # pre-adding to seen (the loop-top seen.add would skip it).
                if (
                    posted_url is not None
                    and posted_url not in seen
                    and len(seen) < MAX_PROVIDER_STEPS
                ):
                    pending.append((posted_url, "GET", {}, depth + 1))
                    continue

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
                    if not _is_document_path(
                        candidate.url
                    ) and not _has_download_affordance(
                        response.text, str(response.url), candidate.url
                    ):
                        return _NO_DOWNLOAD
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


def _load_blocked_urls(path: Path | None = None) -> set[str]:
    """Candidate URLs recorded as download-less; skipped without fetching."""
    try:
        lines = (path or BLOCKED_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    }


def _remember_blocked(url: str, path: Path | None = None) -> None:
    """Append a download-less candidate URL for future runs to skip."""
    target = path or BLOCKED_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(url.strip() + "\n")
    except OSError as error:
        print(f"[blocked-warn] cannot record {url}: {error}")


def _is_http_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)


def _retry_urls(failed_path: Path | None = None, blocked_path: Path | None = None) -> list[str]:
    """URLs for the retry turns: scan/failed.txt rows (unresolved, rejected,
    blocked), then scan/blocked.txt lines, in file order, each once. Blocked
    POST forms are stored as form identities, not URLs, so they cannot be
    rebuilt for a retry and stay as they are."""
    urls: list[str] = []
    seen: set[str] = set()
    try:
        failed_lines = (failed_path or FAILED_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        failed_lines = []
    try:
        blocked_lines = (blocked_path or BLOCKED_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        blocked_lines = []
    candidates = [
        parts[2].strip()
        for parts in (line.split("\t") for line in failed_lines)
        if len(parts) >= 3 and parts[1] in RETRY_STATUSES
    ] + [line.strip() for line in blocked_lines]
    for url in candidates:
        if url and url not in seen and _is_http_url(url):
            seen.add(url)
            urls.append(url)
    return urls


def _rewrite_without(target: Path, keep) -> None:
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    kept = [line for line in lines if keep(line)]
    if len(kept) == len(lines):
        return
    temp = target.with_name(target.name + ".tmp")
    try:
        temp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
        temp.replace(target)
    except OSError as error:
        print(f"[retry-warn] cannot update {target.name}: {error}")


def _forget_records(url: str, failed_path: Path | None = None, blocked_path: Path | None = None) -> None:
    """A retried URL worked: remove its unresolved/rejected/blocked rows from
    scan/failed.txt and its line from scan/blocked.txt."""
    url = url.strip()

    def keep_failed(line: str) -> bool:
        parts = line.split("\t")
        return not (len(parts) >= 3 and parts[1] in RETRY_STATUSES and parts[2].strip() == url)

    _rewrite_without(failed_path or FAILED_FILE, keep_failed)
    _rewrite_without(blocked_path or BLOCKED_FILE, lambda line: line.strip() != url)


def _stamp() -> str:
    """Small console stamp, HH:MM:SS, for tracking parallel work at a glance."""
    return time.strftime("%H:%M:%S")


def _file_stamp() -> str:
    """Date+time stamp for the log files, MM-DD HH:MM:SS."""
    return time.strftime("%m-%d %H:%M:%S")


def _timeline(event: str, detail: str = "", path: Path | None = None) -> None:
    """Append one timestamped event line to scan/timeline.log.

    The durable answer to "what ran while zip/upload ran in parallel":
    start/resume/live/saved/pause/summary rows, oldest first, appended
    only. A timeline failure never stops a run.
    """
    target = path or TIMELINE_FILE
    text = " ".join(str(detail).split())
    line = f"{_file_stamp()} {event}" + (f" {text}" if text else "")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as error:
        print(f"[timeline-warn] cannot record {event}: {error}")


def _failed_keys(path: Path | None = None) -> set[tuple[str, str]]:
    """(status, url) pairs already present in scan/failed.txt, for dedupe."""
    try:
        lines = (path or FAILED_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    keys: set[tuple[str, str]] = set()
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 3:
            keys.add((parts[1].strip(), parts[2].strip()))
    return keys


def _remember_failed(
    status: str,
    url: str,
    detail: str = "",
    known: set[tuple[str, str]] | None = None,
    path: Path | None = None,
) -> bool:
    """Append one time/status/url/detail record to scan/failed.txt.

    Blocked pages (no download button) and failed downloads (unresolved,
    rejected) share this one append-only ledger, so what did not make it is
    finally on record instead of only scrolling past in the console.
    Returns True when a new row was written. The ledger is for looking at;
    skipping still comes from scan/blocked.txt only.
    """
    key = (status, url.strip())
    if known is not None and key in known:
        return False
    text = " ".join(str(detail).split())
    line = f"{_file_stamp()}\t{status}\t{url.strip()}\t{text}"
    target = path or FAILED_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as error:
        print(f"[failed-warn] cannot record {url}: {error}")
        return False
    if known is not None:
        known.add(key)
    return True


def note_pause() -> str:
    """Record an interrupt in the timeline; returns the HH:MM:SS stamp."""
    _timeline("pause")
    return _stamp()


def _memory_identity(candidate: DownloadCandidate) -> str:
    """Stable cross-run identity for the downloaded-memory: the canonical
    candidate key with whitespace collapsed (form data can hold newlines)."""
    return " ".join(_candidate_key(candidate).split())


def _scan_output_hashes(output_dir: Path) -> tuple[set[str], int, int]:
    """sha256 of every real file in the output dir -> (hashes, files, bytes).

    Dotfiles (`.download-*.tmp` leftovers) and subdirectories are ignored.
    """
    hashes: set[str] = set()
    file_count = 0
    byte_count = 0
    try:
        entries = list(output_dir.iterdir())
    except OSError:
        return hashes, file_count, byte_count
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_file():
                continue
            digest = hashlib.sha256()
            with entry.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            hashes.add(digest.hexdigest())
            file_count += 1
            byte_count += entry.stat().st_size
        except OSError:
            continue
    return hashes, file_count, byte_count


def _load_downloaded_memory(
    output_dir: Path, path: Path | None = None
) -> tuple[set[str], set[str], int, int]:
    """(known hashes, remembered URL identities, files on disk, disk bytes).

    History is permanent: once a download is recorded it is never fetched
    again, even after the file is moved off disk to free space (to force a
    re-download, delete its line from the memory file). The disk scan only
    adds pre-existing/hand-placed files to the known hashes.

    Bootstrap: disk files whose hash is not recorded yet are appended as
    hash-only lines (sorted, flushed at once) before loading, so archiving
    the disk right after a run never orphans finished files. URL identities
    for bootstrapped files self-heal on their first skip.
    """
    disk_hashes, file_count, byte_count = _scan_output_hashes(output_dir)
    known = set(disk_hashes)
    trusted: set[str] = set()
    target = path or DOWNLOADED_FILE
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    remembered: set[str] = set()
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        digest, _, identity = text.partition(" ")
        identity = " ".join(identity.split())
        if len(digest) != 64:
            continue
        known.add(digest)
        remembered.add(digest)
        if identity:
            trusted.add(identity)
    missing = sorted(disk_hashes - remembered)
    if missing:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                for digest in missing:
                    handle.write(digest + "\n")
            print(f"[history] listed {len(missing)} file(s) from disk")
        except OSError as error:
            print(f"[downloaded-warn] cannot list disk files: {error}")
    return known, trusted, file_count, byte_count


def _remember_downloaded(key: str, digest: str, path: Path | None = None) -> None:
    """Append a hash + identity line so future runs skip this download."""
    target = path or DOWNLOADED_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"{digest} {key}\n")
    except OSError as error:
        print(f"[downloaded-warn] cannot record {key}: {error}")


async def _download_entries_async(
    entry_source: str | Path,
    output_dir: Path,
    download_limit: float = 0.0,
    guard=None,
) -> DownloadSummary:
    with limit_downloads(download_limit, log=lambda message: print(message, flush=True)):
        if download_limit:
            print(
                f"[download-limit] {download_limit:g} MB/s aggregate response-body cap "
                f"(1 MB = 1048576 bytes); uploads unchanged @ {_stamp()}",
                flush=True,
            )
        else:
            print(f"[download-limit] unlimited; uploads unlimited @ {_stamp()}", flush=True)
        return await _run_download_entries_async(entry_source, output_dir, guard)


async def _run_download_entries_async(
    entry_source: str | Path,
    output_dir: Path,
    guard=None,
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
    seen_hashes: set[str] = set()
    blocked_urls = _load_blocked_urls()
    failed_keys = _failed_keys()
    failed_new = 0
    disk_full: DiskTight | None = None
    print(f"[start] {source} @ {_stamp()}")
    _timeline("start", source)
    known_hashes, downloaded_urls, disk_files, disk_bytes = _load_downloaded_memory(
        output_dir
    )
    print(
        f"[known] {disk_files} file(s) ({disk_bytes / 1048576:.1f} MB) on disk, "
        f"{len(downloaded_urls)} remembered URL(s), "
        f"{len(known_hashes)} known file-hash(es)"
    )
    async with httpx.AsyncClient(
        **_rate_client_options(asynchronous=True),
        follow_redirects=False,
        headers={"Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8"},
    ) as client:

        async def fetch_and_save(candidate: DownloadCandidate) -> tuple[str, str, Path | None, str]:
            """One try at candidate -> (outcome, text, path, digest); outcome is
            saved | downloaded | duplicate | no-download | unresolved | rejected.
            What worked is recorded here, the same for the crawl and the retry
            turns (python/pdfs, downloaded.txt, timeline.log)."""
            check_disk(output_dir)  # under the 5 GB kept for the system every command quits
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
            if payload is _NO_DOWNLOAD:
                return "no-download", "page has no download button or file link", None, ""
            if payload is None:
                return "unresolved", reason, None, ""
            data, extension, headers, final_url = payload
            digest = hashlib.sha256(data).hexdigest()
            memory_key = _memory_identity(candidate)
            if digest in known_hashes:
                print(
                    f"[downloaded] {candidate.url}: "
                    "already downloaded on a previous run; skipping"
                )
                _remember_downloaded(memory_key, digest)
                return "downloaded", "already downloaded on a previous run", None, digest
            if digest in seen_hashes:
                print(
                    f"[duplicate] {candidate.url}: "
                    "identical bytes already saved; skipping"
                )
                _remember_downloaded(memory_key, digest)
                return "duplicate", "identical bytes already saved", None, digest
            seen_hashes.add(digest)
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
                return "rejected", detail, None, digest
            summary.saved.append(path)
            if guard is not None:
                try:
                    guard.note_saved(path.stat().st_size)
                except OSError:
                    guard.note_saved(len(data))
            print(f"[saved] {path.name} ({detail}) @ {_stamp()}")
            _timeline("saved", f"{path.name} {detail}")
            _remember_downloaded(memory_key, digest)
            return "saved", detail, path, digest

        async def download_one(candidate: DownloadCandidate) -> None:
            nonlocal failed_new
            outcome, text, _path, _digest = await fetch_and_save(candidate)
            if outcome == "no-download":
                summary.no_download.append(candidate.url)
                print(f"[no-download] {candidate.url}: {text}")
                blocked_key = _blocked_identity(candidate)
                if blocked_key not in blocked_urls:
                    blocked_urls.add(blocked_key)
                    _remember_blocked(blocked_key)
                if _remember_failed("blocked", candidate.url, text, failed_keys):
                    failed_new += 1
                return
            if outcome == "unresolved":
                summary.unresolved.append(candidate.url)
                print(f"[unresolved] {candidate.url}: {text}")
                if _remember_failed("unresolved", candidate.url, text, failed_keys):
                    failed_new += 1
                return
            if outcome == "downloaded":
                summary.downloaded.append(candidate.url)
                return
            if outcome == "duplicate":
                summary.duplicates.append(candidate.url)
                return
            if outcome == "rejected":
                summary.unresolved.append(candidate.url)
                print(f"[rejected] {candidate.url}: {text}")
                if _remember_failed("rejected", candidate.url, text, failed_keys):
                    failed_new += 1

        async def process_candidates() -> None:
            for key, candidate in list(candidates.items()):
                if key in processed_candidates:
                    continue
                if guard is not None:
                    guard.checkpoint()
                processed_candidates.add(key)
                if _blocked_identity(candidate) in blocked_urls:
                    summary.skipped.append(candidate.url)
                    print(
                        f"[blocked] {candidate.url}: "
                        "had no download on a previous run; skipping"
                    )
                    continue
                if _memory_identity(candidate) in downloaded_urls:
                    summary.downloaded.append(candidate.url)
                    print(
                        f"[downloaded] {candidate.url}: "
                        "saved on a previous run; skipping"
                    )
                    continue
                summary.candidates += 1
                if candidate.method == "POST":
                    print(
                        f"[download] POST {candidate.url} "
                        f"({len(candidate.data)} fields)",
                        flush=True,
                    )
                else:
                    print(f"[download] {candidate.url}", flush=True)
                await download_one(candidate)

        async def retry_records() -> None:
            """The retry turns: every URL in scan/failed.txt + scan/blocked.txt
            gets one try per turn, RETRY_TURNS turns; one that works leaves
            both files, one that fails waits for the next turn."""
            urls = _retry_urls()
            summary.retried = summary.retry_left = len(urls)
            if not urls:
                return
            print(
                f"[retry] crawling finished: {len(urls)} URL(s) from scan/{FAILED_FILE.name} "
                f"and scan/{BLOCKED_FILE.name} get {RETRY_TURNS} turns, one try each @ {_stamp()}",
                flush=True,
            )
            _timeline("retry", f"urls={len(urls)} turns={RETRY_TURNS}")
            for turn in range(1, RETRY_TURNS + 1):
                if not urls:
                    break
                print(f"[retry] turn {turn}/{RETRY_TURNS}: {len(urls)} URL(s) @ {_stamp()}", flush=True)
                still: list[str] = []
                for url in urls:
                    if guard is not None:
                        guard.checkpoint()
                    outcome, text, path, _digest = await fetch_and_save(
                        DownloadCandidate(url, provider=_provider_kind(url))
                    )
                    if outcome in {"saved", "downloaded", "duplicate"}:
                        _forget_records(url)
                        blocked_urls.discard(url)
                        for status in RETRY_STATUSES:
                            failed_keys.discard((status, url))
                        summary.recovered.append(url)
                        summary.retry_left -= 1
                        what = f"saved {path.name}" if path is not None else text
                        print(
                            f"[retry-ok] {url}: {what} (turn {turn}/{RETRY_TURNS}); removed from "
                            f"scan/{FAILED_FILE.name} and scan/{BLOCKED_FILE.name}",
                            flush=True,
                        )
                    else:
                        still.append(url)
                        print(f"[retry-fail] {url}: {text} (turn {turn}/{RETRY_TURNS})", flush=True)
                urls = still
            print(
                f"[retry] done: {len(summary.recovered)} recovered, {len(urls)} still failing "
                f"(kept in scan/{FAILED_FILE.name} / scan/{BLOCKED_FILE.name}) @ {_stamp()}",
                flush=True,
            )
            _timeline("retry-done", f"recovered={len(summary.recovered)} left={len(urls)}")

        try:
            await process_candidates()
            single_site = len(sites) == 1
            for seed_urls in sites.values():
                if guard is not None:
                    guard.checkpoint()
                    guard.site_started(seed_urls[0])
                await ensure_logged_in_async(client, seed_urls[0])
                count = await _crawl_site(
                    client,
                    seed_urls,
                    candidates,
                    process_candidates,
                    max_pages=None if single_site else MAX_SITE_PAGES,
                    guard=guard,
                )
                summary.pages += count
                print(f"[site] {seed_urls[0]}: checked {count:,} page URL(s)")
                await process_candidates()
            await retry_records()
        except StopRun as stop:
            summary.stopped = str(stop) or "stop requested"
            print(
                f"[night-stop] downloading stopped ({summary.stopped}); progress "
                f"is journaled - rerun resumes @ {_stamp()}",
                flush=True,
            )
            _timeline("night-stop", summary.stopped)
        except DiskTight as tight:
            disk_full = tight
            summary.stopped = "disk under 5 GB"
            print(
                f"[disk-full] {tight}; progress is journaled - free some space, "
                f"then rerun to resume @ {_stamp()}",
                flush=True,
            )
            _timeline("disk-full", f"free={size_text(tight.free)}")
    if failed_new:
        print(
            f"[failed] {failed_new} new failure record(s) in "
            f"scan/{FAILED_FILE.name} @ {_stamp()}"
        )
    _timeline(
        "summary",
        f"saved={len(summary.saved)} downloaded={len(summary.downloaded)} "
        f"no_download={len(summary.no_download)} "
        f"unresolved={len(summary.unresolved)} pages={summary.pages:,}"
        + (f" stopped={summary.stopped}" if summary.stopped else ""),
    )
    if disk_full is not None:
        disk_full.summary = summary
        raise disk_full
    return summary


def download_entries(
    entry_source: str | Path = ENTRY_FILE,
    output_dir: str | Path = OUTPUT_DIR,
    download_limit: float = 0.0,
    guard=None,
) -> DownloadSummary:
    """Process one site or entry-file sites sequentially, saving each file in turn.

    Crawl journals make reruns resume where the last run stopped. Uploading
    is separate (main.py --upload / --process). download_limit caps aggregate HTTP
    response bodies in MB/s (1048576 bytes); 0 is unlimited. guard is the
    night-run policy from night.py (main.py --process): stop points and the
    disk floor; summary.stopped says why it stopped early.
    """
    return asyncio.run(
        _download_entries_async(
            entry_source, Path(output_dir), download_limit, guard
        )
    )

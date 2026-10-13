"""Standalone sitemap-size estimate, saved separately for a future crawler.

No traversal or hard crawl cap is implemented here. The caller supplies a
fetch_sitemap(url) callback returning (decoded XML text, final URL), and may
supply an allow_url callback for its own robots policy. A missing/incomplete
sitemap never proves that a site is small.
"""

from __future__ import annotations

import dataclasses
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable

DEFAULT_SITE_PAGE_LIMIT = 3_000


MAX_SITEMAP_DOCUMENTS = 32


FALLBACK_SITEMAP_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/wp-sitemap.xml",
)


@dataclasses.dataclass(frozen=True)
class SitePageEstimate:
    """A lower bound from sitemap entries; exact only when every listed map was read."""

    pages: int
    complete: bool
    sitemaps_read: int


def _host(url: str) -> str:
    hostname = (urllib.parse.urlsplit(url).hostname or "").rstrip(".").lower()
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return hostname


def _same_site_host(url: str, seed_host: str) -> bool:
    host = _host(url).removeprefix("www.")
    seed_parts = urllib.parse.urlsplit(
        seed_host if "://" in seed_host else "//" + seed_host
    )
    expected = (seed_parts.hostname or "").lower().removeprefix("www.")
    return bool(host) and host == expected


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_sitemap(text: str) -> tuple[str, list[str]] | None:
    """Return (urlset|index, locations), ignoring namespaces and malformed XML."""
    try:
        root = ET.fromstring(text.lstrip("\ufeff"))
    except (ET.ParseError, ValueError, TypeError):
        return None

    kind = _local_name(root.tag)
    if kind not in ("urlset", "sitemapindex"):
        return None
    entry_tag = "url" if kind == "urlset" else "sitemap"
    locations: list[str] = []
    for entry in root:
        if _local_name(entry.tag) != entry_tag:
            continue
        location = next(
            (child for child in entry if _local_name(child.tag) == "loc"), None
        )
        if location is not None and location.text and location.text.strip():
            locations.append(location.text.strip())
    return kind, locations


def _sitemap_candidates(seed_url: str, declared: list[str]) -> list[str]:
    if declared:
        origin = urllib.parse.urlsplit(seed_url)
        base = urllib.parse.urlunsplit((origin.scheme, origin.netloc, "/", "", ""))
        return list(
            dict.fromkeys(urllib.parse.urljoin(base, value) for value in declared)
        )
    parts = urllib.parse.urlsplit(seed_url)
    origin = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    return [origin + path for path in FALLBACK_SITEMAP_PATHS]


def estimate_sitemap_pages(
    seed_url: str,
    fetch_sitemap: Callable[[str], tuple[str, str]],
    declared_sitemaps: list[str] | tuple[str, ...] = (),
    *,
    stop_after: int = DEFAULT_SITE_PAGE_LIMIT,
    max_documents: int = MAX_SITEMAP_DOCUMENTS,
    allow_url=None,
) -> SitePageEstimate | None:
    """Count sitemap URLs up to a threshold without fetching the site's HTML pages.

    Sitemap indexes are followed, but only within the seed host and with a hard
    document cap. If the cap or a fetch failure leaves maps unread, ``complete``
    is false. A count above ``stop_after`` is still a reliable lower bound and
    can safely trigger the large-site guard.
    """
    seed_host = urllib.parse.urlsplit(seed_url).netloc.lower().removeprefix("www.")
    queue = _sitemap_candidates(seed_url, list(declared_sitemaps))
    seen_maps: set[str] = set()
    seen_pages: set[str] = set()
    complete = True
    valid_map_seen = False
    documents_read = 0

    while queue and documents_read < max_documents:
        sitemap_url = queue.pop(0)
        sitemap_url = urllib.parse.urldefrag(sitemap_url)[0]
        if sitemap_url in seen_maps:
            continue
        seen_maps.add(sitemap_url)
        if (
            urllib.parse.urlsplit(sitemap_url).scheme not in ("http", "https")
            or not _same_site_host(sitemap_url, seed_host)
            or (allow_url is not None and not allow_url(sitemap_url))
        ):
            complete = False
            continue

        try:
            text, final_url = fetch_sitemap(sitemap_url)
        except Exception:
            complete = False
            continue
        documents_read += 1
        if not _same_site_host(final_url, seed_host):
            complete = False
            continue

        parsed = _parse_sitemap(text)
        if parsed is None:
            complete = False
            continue
        valid_map_seen = True
        kind, locations = parsed
        if kind == "sitemapindex":
            for location in locations:
                child_url = urllib.parse.urljoin(final_url, location)
                if child_url not in seen_maps:
                    queue.append(child_url)
            continue

        for location in locations:
            page_url = urllib.parse.urldefrag(location)[0]
            if page_url:
                seen_pages.add(page_url)
            if len(seen_pages) > stop_after:
                return SitePageEstimate(len(seen_pages), False, documents_read)

    if queue:
        complete = False
    if not valid_map_seen:
        return None
    return SitePageEstimate(len(seen_pages), complete, documents_read)


def large_site_reason(
    seed_url: str,
    fetch_sitemap: Callable[[str], tuple[str, str]],
    declared_sitemaps: list[str] | tuple[str, ...] = (),
    *,
    max_pages: int = DEFAULT_SITE_PAGE_LIMIT,
    allow_url: Callable[[str], bool] | None = None,
) -> str | None:
    """Report a skip reason only when sitemap entries exceed the threshold."""
    if max_pages <= 0:
        return None
    estimate = estimate_sitemap_pages(
        seed_url,
        fetch_sitemap,
        declared_sitemaps,
        stop_after=max_pages,
        allow_url=allow_url,
    )
    if estimate is None or estimate.pages <= max_pages:
        return None
    count = (
        f"at least {estimate.pages:,}"
        if not estimate.complete
        else f"{estimate.pages:,}"
    )
    return f"sitemap lists {count} URLs, above the {max_pages:,}-page limit"

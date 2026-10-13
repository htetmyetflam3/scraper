"""Find candidate book sites from SerpApi results; never download files."""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from domain_guard import excluded_site_reason
from download_links import FILE_EXTENSION, extract_file_links, guard_download_url
from serpapi import SerpApiError, serpapi_search

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "entry.txt"
DEFAULT_KEYWORDS = (
    "myanmar books download",
    "myanmar ebooks download",
    "Myanmar PDF free download",
    "free မြန်မာ pdf စာအုပ်များ",
)
SEARCH_PAGES_PER_KEYWORD = 2
RESULTS_PER_PAGE = 10
MAX_PAGES_PER_SITE = 10
REQUEST_TIMEOUT = 15.0
SCRIBD_DOMAIN = "scribd.com"
HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def _hostname(url: str) -> str | None:
    try:
        host = urllib.parse.urlsplit(url).hostname
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


def _is_scribd_url(url: str) -> bool:
    host = _hostname(url)
    return host == SCRIBD_DOMAIN or bool(host and host.endswith("." + SCRIBD_DOMAIN))


def _normalized_page_url(url: str, site_key: str | None = None) -> str | None:
    """Accept only safe HTTP(S) URLs on the expected, non-excluded site."""
    try:
        parts = urllib.parse.urlsplit(url)
        if (
            parts.scheme.lower() not in ("http", "https")
            or not parts.netloc
            or parts.username is not None
            or parts.password is not None
        ):
            return None
        parts.port  # reject malformed ports before passing a URL to httpx
    except ValueError:
        return None

    cleaned = guard_download_url(url)
    if cleaned is None or excluded_site_reason(cleaned) is not None:
        return None
    if site_key is not None and _site_key(cleaned) != site_key:
        return None
    return cleaned


def _site_root(url: str) -> str:
    """Return the result's scheme and host, without its search-result path."""
    parts = urllib.parse.urlsplit(url)
    host = _hostname(url)
    if host is None:
        raise ValueError(f"Not an HTTP site URL: {url!r}")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError(f"Not an HTTP site URL: {url!r}") from error
    default_port = 80 if parts.scheme.lower() == "http" else 443
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    return urllib.parse.urlunsplit((parts.scheme.lower(), netloc, "/", "", ""))


def _is_direct_download(url: str) -> bool:
    return bool(
        guard_download_url(url)
        and FILE_EXTENSION.search(urllib.parse.urlsplit(url).path)
    )


def _crawl_site(client: httpx.Client, seed_url: str, visited: set[str]) -> bool:
    """Depth-first follow same-site anchors, stopping on a PDF/DOCX link."""
    seed = _normalized_page_url(seed_url)
    if seed is None or FILE_EXTENSION.search(urllib.parse.urlsplit(seed).path):
        return False
    site_key = _site_key(seed)
    if site_key is None:
        return False

    stack = [seed]
    while stack and len(visited) < MAX_PAGES_PER_SITE:
        page_url = _normalized_page_url(stack.pop(), site_key)
        if (
            page_url is None
            or page_url in visited
            or FILE_EXTENSION.search(urllib.parse.urlsplit(page_url).path)
        ):
            continue

        # The set is shared between all search results for this domain, so the
        # ten-page ceiling cannot be bypassed by returning a second result URL.
        visited.add(page_url)
        print(
            f"[page] {site_key} {len(visited)}/{MAX_PAGES_PER_SITE}: {page_url}",
            flush=True,
        )
        try:
            response = client.get(page_url)
        except (httpx.HTTPError, ValueError) as error:
            print(f"[page-error] {page_url}: {error}", flush=True)
            continue
        if response.status_code != 200:
            # In particular, do not follow search-engine or site redirectors.
            print(f"[page-skip] HTTP {response.status_code}: {page_url}", flush=True)
            continue
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type.strip().lower() not in HTML_CONTENT_TYPES:
            continue

        html = response.text
        file_links = extract_file_links(html, str(response.url))
        if file_links:
            print(
                f"[found] {len(file_links)} PDF/DOCX link(s) on {response.url}",
                flush=True,
            )
            return True

        soup = BeautifulSoup(html, "html.parser")
        base_tag = soup.find("base", href=True)
        base_url = (
            urllib.parse.urljoin(str(response.url), base_tag["href"])
            if base_tag
            else str(response.url)
        )
        next_pages: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "")
            if not isinstance(href, str):
                continue
            target = _normalized_page_url(
                urllib.parse.urljoin(base_url, href), site_key
            )
            if (
                target is not None
                and not FILE_EXTENSION.search(urllib.parse.urlsplit(target).path)
                and target not in visited
                and target not in next_pages
            ):
                next_pages.append(target)

        # Stack order preserves the page's anchor order while still backtracking.
        stack.extend(reversed(next_pages))

    return False


def _read_api_key(explicit_key: str | None) -> str:
    if explicit_key and explicit_key.strip():
        return explicit_key.strip()
    environment_key = os.environ.get("SERPAPI", "").strip()
    if environment_key:
        return environment_key

    env_file = REPO_ROOT / ".env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        entry = line.strip()
        if entry.startswith("export "):
            entry = entry[7:].lstrip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        name, value = entry.split("=", 1)
        if name.strip() != "SERPAPI":
            continue
        value = value.strip()
        if value[:1] in ('"', "'"):
            closing_quote = value.find(value[0], 1)
            if closing_quote > 0:
                value = value[1:closing_quote]
        else:
            value = value.split("#", 1)[0].strip()
        if value:
            return value
    raise SerpApiError(
        "Set SERPAPI in the environment or in the repository-root .env file."
    )


def find_entry(api_key: str | None = None) -> list[str]:
    """Search the old four keywords and write discovered site roots to entry.txt.

    Each keyword uses at most two SerpApi pages. Scribd result URLs are written
    unchanged and are never fetched. Other allowed domains are searched by
    depth-first traversal, with at most ten unique same-site page requests per
    domain. A site's root URL is written only when a PDF/DOCX link or button is found.
    """
    key = _read_api_key(api_key)
    search_results: list[dict] = []
    for keyword in DEFAULT_KEYWORDS:
        for page_number in range(SEARCH_PAGES_PER_KEYWORD):
            print(
                f"[search] {keyword!r}, page {page_number + 1}/"
                f"{SEARCH_PAGES_PER_KEYWORD}",
                flush=True,
            )
            try:
                results, has_next_page = serpapi_search(
                    keyword, page_number * RESULTS_PER_PAGE, key
                )
            except SerpApiError:
                raise
            except httpx.HTTPError as error:
                raise SerpApiError(f"SerpApi request failed: {error}") from error
            search_results.extend(results)
            print(
                f"[search] received {len(results)} result(s); "
                f"{len(search_results)} total so far",
                flush=True,
            )
            if not has_next_page:
                break

    print(f"[crawl] scanning {len(search_results)} search result(s)", flush=True)

    output_lines: list[str] = []
    written_lines: set[str] = set()

    def add_line(value: str) -> None:
        if value not in written_lines:
            output_lines.append(value)
            written_lines.add(value)

    site_visits: dict[str, set[str]] = {}
    successful_sites: set[str] = set()
    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=False,
        headers={"Accept": "text/html,application/xhtml+xml"},
    ) as client:
        for result_number, result in enumerate(search_results, start=1):
            raw_url = result.get("url")
            if not isinstance(raw_url, str) or not raw_url:
                continue
            if _is_scribd_url(raw_url):
                add_line(raw_url)
                print(
                    f"[scribd] kept result {result_number}/{len(search_results)}",
                    flush=True,
                )
                continue

            seed = _normalized_page_url(raw_url)
            if seed is None:
                continue
            site_key = _site_key(seed)
            if site_key is None or site_key in successful_sites:
                continue

            print(
                f"[result] {result_number}/{len(search_results)}: {seed}",
                flush=True,
            )
            visited = site_visits.setdefault(site_key, set())
            if _is_direct_download(seed):
                found_download = True
                print(f"[found] direct file link: {seed}", flush=True)
            else:
                previous_page_count = len(visited)
                found_download = _crawl_site(client, seed, visited)
                if not found_download:
                    checked = len(visited) - previous_page_count
                    print(
                        f"[site] no file link on {site_key} after "
                        f"{checked} new page(s)",
                        flush=True,
                    )
            if found_download:
                successful_sites.add(site_key)
                add_line(_site_root(seed))
                print(f"[site-found] {site_key}: added to entry.txt", flush=True)

    OUTPUT_FILE.write_text(
        "\n".join(output_lines) + ("\n" if output_lines else ""),
        encoding="utf-8",
    )
    return output_lines


if __name__ == "__main__":
    try:
        entries = find_entry()
    except (SerpApiError, OSError) as error:
        raise SystemExit(str(error)) from error
    print(f"Wrote {len(entries)} links to {OUTPUT_FILE}")

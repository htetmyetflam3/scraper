"""Find candidate book sites from SerpApi results; never download files."""

from __future__ import annotations

import os
import re
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
REQUEST_TIMEOUT = 15.0
SCRIBD_DOMAIN = "scribd.com"
HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})

DOWNLOAD_MARKER = re.compile(
    r"\b(download(?:\s+now|\s+file)?|get\s+file|pdf|docx)\b", re.I
)
NON_DOWNLOAD_MARKER = re.compile(
    r"\b(share|telegram|reply|comment|print|whatsapp|facebook|twitter|pinterest|bookmark|follow)\b",
    re.I,
)


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


def _raw_segments(url: str) -> list[str]:
    """Path segments as they appear in the URL (still percent-encoded)."""
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return []
    return [segment for segment in path.split("/") if segment]


def _decoded_first_segment(url: str) -> str:
    segments = _raw_segments(url)
    if not segments:
        return ""
    return urllib.parse.unquote(segments[0])


def _is_prefix_child(current_url: str, candidate_url: str) -> bool:
    """True when the candidate path extends the current path deeper."""
    try:
        current_path = urllib.parse.unquote(
            urllib.parse.urlsplit(current_url).path or "/"
        )
        candidate_path = urllib.parse.unquote(
            urllib.parse.urlsplit(candidate_url).path or "/"
        )
    except ValueError:
        return False
    if not current_path.endswith("/"):
        current_path += "/"
    return candidate_path.startswith(current_path) and len(candidate_path) > len(
        current_path
    )


def _guide_chain(site_root: str, seed: str) -> list[str]:
    """Root plus each ancestor of the search-result path, ending at the seed.

    Example: seed ``example.com/category/books/`` yields
    ``example.com/``, ``example.com/category/``,
    ``example.com/category/books/``.
    """
    segments = _raw_segments(seed)
    if not segments:
        return [site_root]
    base = site_root.rstrip("/")
    chain = [site_root]
    for index in range(1, len(segments)):
        chain.append(base + "/" + "/".join(segments[:index]) + "/")
    chain.append(seed)
    return list(dict.fromkeys(chain))


def _fetch_html_page(
    client: httpx.Client,
    page_url: str,
    site_key: str,
    visited: set[str],
    depth: int,
) -> tuple[str, str] | None:
    """Fetch one same-site HTML page; None means dead end, never retry."""
    normalized = _normalized_page_url(page_url, site_key)
    if normalized is None or normalized in visited:
        return None
    try:
        if FILE_EXTENSION.search(urllib.parse.urlsplit(normalized).path):
            return None
    except ValueError:
        return None
    visited.add(normalized)
    print(f"[page] {site_key} depth={depth}: {normalized}", flush=True)
    try:
        response = client.get(normalized)
    except (httpx.HTTPError, ValueError) as error:
        print(f"[page-error] {normalized}: {error}", flush=True)
        return None
    if response.status_code != 200:
        # In particular, do not follow search-engine or site redirectors.
        print(f"[page-skip] HTTP {response.status_code}: {normalized}", flush=True)
        return None
    content_type = response.headers.get("content-type", "").split(";", 1)[0]
    if content_type.strip().lower() not in HTML_CONTENT_TYPES:
        return None
    return response.text, str(response.url)


def _child_candidates(
    page_html: str,
    page_final_url: str,
    current_url: str,
    site_key: str,
    visited: set[str],
) -> list[str]:
    """Same-site anchors that extend the current path, in page order."""
    soup = BeautifulSoup(page_html, "html.parser")
    base_tag = soup.find("base", href=True)
    base_url = (
        urllib.parse.urljoin(page_final_url, base_tag["href"])
        if base_tag
        else page_final_url
    )
    children: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if not isinstance(href, str):
            continue
        target = _normalized_page_url(urllib.parse.urljoin(base_url, href), site_key)
        if target is None or target in visited or target in children:
            continue
        try:
            if FILE_EXTENSION.search(urllib.parse.urlsplit(target).path):
                continue
        except ValueError:
            continue
        if _is_prefix_child(current_url, target):
            children.append(target)
    return children


def _extract_download_controls(
    page_html: str, page_final_url: str, site_key: str
) -> list[tuple[str, str]]:
    """Same-site download-labelled controls that name no file suffix.

    These are buttons such as "Download" pointing at ``/download?id=...``.
    File-suffixed links are found by ``extract_file_links`` instead.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    base_tag = soup.find("base", href=True)
    base_url = (
        urllib.parse.urljoin(page_final_url, base_tag["href"])
        if base_tag
        else page_final_url
    )
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
                match.group(1)
                for match in re.finditer(r"['\"]([^'\"]+)['\"]", onclick)
            )
        for raw in raw_targets:
            target = _normalized_page_url(urllib.parse.urljoin(base_url, raw), site_key)
            if target is None:
                continue
            try:
                if FILE_EXTENSION.search(urllib.parse.urlsplit(target).path):
                    continue
            except ValueError:
                continue
            found.setdefault(target, " ".join(label.split())[:180])
    return list(found.items())


def _crawl_site(client: httpx.Client, seed_url: str, visited: set[str]) -> bool:
    """Follow one guided branch from the root down to its leaf page.

    The search-result path selects the single allowed top-level branch:
    a result of ``example.com/category/books/`` crawls ``example.com/``,
    then ``example.com/category/``, then ``example.com/category/books/``,
    then keeps expanding to deeper prefix children. Sibling branches such
    as ``example.com/latest/`` or ``example.com/author/`` are never
    visited. There is no page budget; the chain runs as deep as prefix
    children exist. A leaf (no deeper child) without a PDF/DOCX link or
    download button gives up the whole site.
    """
    seed = _normalized_page_url(seed_url)
    if seed is None:
        return False
    try:
        if FILE_EXTENSION.search(urllib.parse.urlsplit(seed).path):
            return False
    except ValueError:
        return False
    site_key = _site_key(seed)
    if site_key is None:
        return False
    root = _site_root(seed)
    locked_first = _decoded_first_segment(seed)

    current_url: str | None = None
    current_html = ""
    current_final = ""
    depth = 0

    def check_downloads(page_html: str, final_url: str) -> bool:
        file_links = extract_file_links(page_html, final_url)
        if file_links:
            print(
                f"[found] {len(file_links)} PDF/DOCX link(s) on {final_url}",
                flush=True,
            )
            return True
        controls = _extract_download_controls(page_html, final_url, site_key)
        if controls:
            print(
                f"[found] {len(controls)} download button(s) on {final_url}",
                flush=True,
            )
            return True
        return False

    for guide_url in _guide_chain(root, seed):
        if _is_direct_download(guide_url):
            print(f"[found] direct file link: {guide_url}", flush=True)
            return True
        fetched = _fetch_html_page(client, guide_url, site_key, visited, depth)
        depth += 1
        if fetched is None:
            continue
        page_html, final_url = fetched
        current_url, current_html, current_final = guide_url, page_html, final_url
        if check_downloads(page_html, final_url):
            return True

    if current_url is None:
        return False

    while True:
        children = _child_candidates(
            current_html, current_final, current_url, site_key, visited
        )
        if locked_first:
            children = [
                child
                for child in children
                if _decoded_first_segment(child) == locked_first
            ]
        elif children:
            locked_first = _decoded_first_segment(children[0])
            children = [
                child
                for child in children
                if _decoded_first_segment(child) == locked_first
            ]
        if not children:
            print(
                f"[leaf] {site_key}: no deeper page under {current_url}; "
                "giving up site",
                flush=True,
            )
            return False
        if len(children) > 1:
            print(
                f"[branch] {site_key}: following {children[0]}; "
                f"ignoring {len(children) - 1} sibling(s) under {current_url}",
                flush=True,
            )
        next_url = children[0]
        if _is_direct_download(next_url):
            print(f"[found] direct file link: {next_url}", flush=True)
            return True
        fetched = _fetch_html_page(client, next_url, site_key, visited, depth)
        depth += 1
        if fetched is None:
            print(
                f"[leaf] {site_key}: {next_url} is a dead end; giving up site",
                flush=True,
            )
            return False
        page_html, final_url = fetched
        current_url, current_html, current_final = next_url, page_html, final_url
        if check_downloads(page_html, final_url):
            return True


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
    unchanged and are never fetched. Every other domain is tried once: the
    first result's path selects one top-level branch, the crawl walks from
    the site root down that guided branch as deep as prefix children exist,
    and a leaf without a PDF/DOCX link or download button gives up the
    whole domain. A site's root URL is written only when such a download
    control is found.
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
    failed_sites: set[str] = set()
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
            if site_key is None:
                continue
            if site_key in successful_sites or site_key in failed_sites:
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
                        f"{checked} page(s); moving to next site",
                        flush=True,
                    )
            if found_download:
                successful_sites.add(site_key)
                add_line(_site_root(seed))
                print(f"[site-found] {site_key}: added to entry.txt", flush=True)
            else:
                failed_sites.add(site_key)

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

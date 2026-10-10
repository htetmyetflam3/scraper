"""Shared crawl code: read pages of one site and collect the PDF/DOCX links on them.

Used by the crawler (dsite.py, search) and the downloader (dsite_download.py, entry list).
Neither script's rules live here: every PDF/DOCX link found on the site is kept.
"""

from __future__ import annotations

import random
import re
import time
import urllib.parse
import urllib.robotparser
from collections import deque

from bs4 import BeautifulSoup

from crawler import BOOK_EXTENSION, Blocked, RateLimited, is_scribd_document_url, upsert


# Page URLs with these extensions are never fetched as HTML.
SKIP_PAGE_EXTENSIONS = {
    ".7z", ".apk", ".avi", ".bmp", ".css", ".csv", ".doc", ".docx", ".epub", ".exe",
    ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".js", ".json", ".m4a", ".mp3", ".mp4",
    ".mpeg", ".mpg", ".odt", ".ogg", ".pdf", ".png", ".ppt", ".pptx", ".rar", ".rss",
    ".svg", ".tar", ".tgz", ".txt", ".wav", ".webm", ".webp", ".woff", ".woff2",
    ".xls", ".xlsx", ".xml", ".zip",
}


def host_key(url: str) -> str:
    """Domain used to group search results into one site: 'www.' is ignored."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    return host.removeprefix("www.")


def same_site(url: str, seed_host: str) -> bool:
    return host_key(url) == seed_host


def without_fragment(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def is_html_candidate(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    extension = path.rsplit("/", 1)[-1]
    if "." in extension:
        return "." + extension.rsplit(".", 1)[-1] not in SKIP_PAGE_EXTENSIONS
    return True


# A quoted file path inside JavaScript, e.g. onclick="location.href='files/book.pdf'".
FILE_IN_CODE = re.compile(r"""['"]([^'"\s<>]+?\.(?:pdf|docx)(?:\?[^'"\s<>]*)?)['"]""", re.I)


def page_links(html: str, page_url: str) -> list[tuple[str, str]]:
    """(absolute URL, visible label) for every http(s) link on the page.

    Besides <a href>, a file (PDF/DOCX) can be named by a button or other element:
    formaction, action, any data-* attribute, or a quoted path in onclick. Those
    are kept only when they point at a file, so a button still counts as a link
    when the page HTML names its file.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_tag = soup.find("base", href=True)
    base = urllib.parse.urljoin(page_url, base_tag["href"]) if base_tag else page_url
    links: dict[str, str] = {}

    def add(href: str, label: str) -> None:
        url = without_fragment(urllib.parse.urljoin(base, href.strip()))
        if url.lower().startswith(("http://", "https://")):
            links.setdefault(url, label)

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        label = anchor.get_text(" ", strip=True) or anchor.get("title", "") or ""
        add(href, " ".join(label.split()))

    for element in soup.find_all(True):
        label = " ".join(element.get_text(" ", strip=True).split())[:200] or element.get("title", "") or ""
        for name, value in element.attrs.items():
            if not isinstance(value, str) or element.name == "a" and name == "href":
                continue
            if name in ("formaction", "action") or name.startswith("data-"):
                if BOOK_EXTENSION.search(urllib.parse.urlsplit(value.strip()).path.lower()):
                    add(value, label)
        code = element.get("onclick", "")
        for match in FILE_IN_CODE.finditer(code if isinstance(code, str) else ""):
            add(match.group(1), label)
    return list(links.items())


class RobotsRules:
    """robots.txt for one host. A missing robots.txt allows everything."""

    def __init__(self, fetcher, host: str, scheme: str = "https") -> None:
        self.parser = urllib.robotparser.RobotFileParser()
        self.parser.allow_all = False
        try:
            text, _ = fetcher.fetch(f"{scheme}://{host}/robots.txt")
            self.parser.parse(text.splitlines())
        except Blocked:
            raise  # the site refuses us outright: the caller skips it
        except Exception:
            self.parser.parse([])  # unreachable or missing robots.txt: allow

    def allowed(self, url: str) -> bool:
        return self.parser.can_fetch("*", url)


def crawl_site(
    seed: str,
    fetcher,
    *,
    entries: dict[str, dict],
    scribd: dict[str, dict],
    max_pages: int,
    max_depth: int,
    delay: float,
    jitter: float,
    sleep=time.sleep,
    log=print,
    save=None,
    stop_at_first_file=False,
) -> dict[str, int]:
    seed = without_fragment(seed)
    seed_host = host_key(seed)
    scheme = urllib.parse.urlsplit(seed).scheme
    stats = {"pages": 0, "pdf": 0, "scribd": 0}

    log(f"  reading robots.txt for {seed_host}")
    try:
        robots = RobotsRules(fetcher, seed_host, scheme)
    except Blocked as error:
        log(f"  robots.txt refused: {error}. Skipping this site.")
        return stats

    queue: deque[tuple[str, int]] = deque([(seed, 0)])
    visited: set[str] = {seed}
    while queue and stats["pages"] < max_pages:
        page_url, depth = queue.popleft()
        if not robots.allowed(page_url):
            log(f"  robots.txt disallows {page_url}")
            continue
        if stats["pages"] > 0:
            sleep(delay + random.uniform(0, jitter))
        log(f"  requesting {page_url}")
        try:
            html, final_url = fetcher.fetch(page_url)
        except (Blocked, RateLimited) as error:
            log(f"  Stopping this site: {error}")
            break
        except Exception as error:  # a broken page must not stop the site
            log(f"  Failed {page_url}: {error}")
            continue
        stats["pages"] += 1
        limit = "no limit" if max_pages == float("inf") else max_pages
        log(f"  [{stats['pages']}/{limit}] depth {depth}: {final_url}")

        found_before = len(entries) + len(scribd)
        for url, label in page_links(html, final_url):
            if is_scribd_document_url(url):
                if url not in scribd:
                    log(f"    Scribd: {url}")
                upsert(scribd, url, label, final_url)
                stats["scribd"] += 1
                continue
            path = urllib.parse.urlsplit(url).path
            if BOOK_EXTENSION.search(path.lower()):
                # Every PDF/DOCX link is kept. The search is already Burmese, so no name filter.
                is_new = url not in entries
                if is_new:
                    stats["pdf"] += 1
                    log(f"    PDF/DOCX found: {url}")
                upsert(entries, url, label, final_url)
                if is_new and save:  # per file: the downloader works on it before the next link
                    save()
                continue
            if same_site(url, seed_host) and depth < max_depth and url not in visited and is_html_candidate(url):
                visited.add(url)
                queue.append((url, depth + 1))
        if stop_at_first_file and stats["pdf"]:
            log(f"  Download link found on {final_url}; stopping this site.")
            break
        if len(entries) + len(scribd) != found_before and save:
            save()
    return stats

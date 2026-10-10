#!/usr/bin/env python3
"""Crawl each site the search engine pointed to and collect its PDF/DOCX links.

    uv run site_crawl.py https://example.org/books/   # crawl this site
    uv run site_crawl.py                              # no URL: search first, then crawl what it finds
    uv run site_crawl.py --search-terms="Myanmar PDF" --search-pages=2

One crawler, two ways to get its starting URLs. Given URLs are crawled as-is.
Without URLs, a small bounded search (--search-terms, --search-pages) runs once,
and every site it returns becomes a starting URL. The search is never repeated
per site.

For each site the crawler starts at the start URL (a given URL, or the page the search returned) and
follows links that stay on the same domain, up to --max-depth levels and
--max-pages pages. Every PDF/DOCX it links to goes into site_entry_list.txt
(the matching filter from crawler.py applies, so --match-mode=loose by default).
Scribd document links are not downloaded here; they go to site_scribd_links.txt
for separate handling. Links to other domains are not followed.

Politeness: robots.txt is honoured, requests are spaced by --delay plus random
--jitter, and the site is abandoned at once if it blocks or rate-limits us.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
import urllib.parse
import urllib.robotparser
from collections import deque
from pathlib import Path

from bs4 import BeautifulSoup

import crawler
from crawler import (
    BOOK_EXTENSION,
    Blocked,
    HttpFetcher,
    RateLimited,
    matches_book_candidate,
    is_scribd_document_url,
    read_book_list,
    read_url_list,
    upsert,
    write_book_list,
)

HERE = Path(__file__).resolve().parent

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


def page_links(html: str, page_url: str) -> list[tuple[str, str]]:
    """(absolute URL, visible label) for every http(s) link on the page."""
    soup = BeautifulSoup(html, "html.parser")
    base_tag = soup.find("base", href=True)
    base = urllib.parse.urljoin(page_url, base_tag["href"]) if base_tag else page_url
    links: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = without_fragment(urllib.parse.urljoin(base, href))
        if not url.lower().startswith(("http://", "https://")):
            continue
        label = anchor.get_text(" ", strip=True) or anchor.get("title", "") or ""
        links.setdefault(url, " ".join(label.split()))
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
    not_kept: list[tuple[str, str]],
    max_pages: int,
    max_depth: int,
    match_mode: str,
    delay: float,
    jitter: float,
    sleep=time.sleep,
    log=print,
) -> dict[str, int]:
    seed = without_fragment(seed)
    seed_host = host_key(seed)
    scheme = urllib.parse.urlsplit(seed).scheme
    stats = {"pages": 0, "pdf": 0, "scribd": 0, "not_kept": 0}

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
        try:
            html, final_url = fetcher.fetch(page_url)
        except (Blocked, RateLimited) as error:
            log(f"  Stopping this site: {error}")
            break
        except Exception as error:  # a broken page must not stop the site
            log(f"  Failed {page_url}: {error}")
            continue
        stats["pages"] += 1
        log(f"  [{stats['pages']}/{max_pages}] depth {depth}: {final_url}")

        for url, label in page_links(html, final_url):
            if is_scribd_document_url(url):
                upsert(scribd, url, label, final_url)
                stats["scribd"] += 1
                continue
            path = urllib.parse.urlsplit(url).path
            if BOOK_EXTENSION.search(path.lower()):
                if matches_book_candidate(url, label, match_mode):
                    if url not in entries:
                        stats["pdf"] += 1
                    upsert(entries, url, label, final_url)
                else:
                    stats["not_kept"] += 1
                    not_kept.append((label, url))
                continue
            if same_site(url, seed_host) and depth < max_depth and url not in visited and is_html_candidate(url):
                visited.add(url)
                queue.append((url, depth + 1))
    return stats


def read_seeds(path: Path) -> list[str]:
    seen: dict[str, str] = {}
    for url in read_url_list(path):
        seen.setdefault(host_key(url), url)
    return list(seen.values())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl the sites found by search and collect PDF/DOCX links.")
    parser.add_argument("urls", nargs="*", help="site URLs to crawl (default: every site in --sites)")
    parser.add_argument("--sites", type=Path, default=HERE / "search_sites.txt", help="sites found by crawler.py")
    parser.add_argument("--entry-list", type=Path, default=HERE / "site_entry_list.txt")
    parser.add_argument("--scribd-list", type=Path, default=HERE / "site_scribd_links.txt")
    parser.add_argument("--max-pages", type=int, default=50, help="pages fetched per site (default 50)")
    parser.add_argument("--max-depth", type=int, default=3, help="link levels below the start page (default 3)")
    parser.add_argument("--match-mode", default="loose", choices=("loose", "filename"))
    parser.add_argument("--delay", type=float, default=5.0, help="seconds between requests (default 5)")
    parser.add_argument("--jitter", type=float, default=2.0, help="extra random seconds (default 2)")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--search-engine", default="mojeek", help="used only when no URL is given")
    parser.add_argument("--search-terms", default="Myanmar book PDF free download",
                        help="comma-separated; each term gives 4 queries. Used only when no URL is given")
    parser.add_argument("--search-pages", type=int, default=2, help="result pages per query (default 2)")
    parser.add_argument("--search-delay", type=float, default=10.0, help="seconds between search requests")
    return parser.parse_args(argv)


def search_for_sites(args: argparse.Namespace) -> int:
    """One bounded search pass with crawler.py; its result sites land in args.sites."""
    print(f"No URL given: searching {args.search_engine} once for {args.search_terms!r} "
          f"({args.search_pages} pages per query).")
    return crawler.main([
        f"--engine={args.search_engine}",
        f"--terms={args.search_terms}",
        f"--pages={args.search_pages}",
        f"--delay={args.search_delay}",
        "--jitter=2",
        "--reset-state",
        f"--sites={args.sites}",
        f"--entry-list={args.entry_list}",
        f"--scribd-list={args.scribd_list}",
    ])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.urls:
        seeds = list(args.urls)
    else:
        if search_for_sites(args) != 0:
            return 1
        seeds = read_seeds(args.sites)
    if not seeds:
        print("No sites found to crawl. Try other --search-terms, or pass a URL.", file=sys.stderr)
        return 1

    entries = read_book_list(args.entry_list)
    scribd = read_book_list(args.scribd_list)
    not_kept: list[tuple[str, str]] = []
    fetcher = HttpFetcher(timeout=args.timeout)
    print(f"Crawling {len(seeds)} site(s); up to {args.max_pages} pages each, depth {args.max_depth}.")

    try:
        for number, seed in enumerate(seeds, 1):
            print(f"\nSite {number}/{len(seeds)}: {host_key(seed)} (from {seed})")
            stats = crawl_site(
                seed,
                fetcher,
                entries=entries,
                scribd=scribd,
                not_kept=not_kept,
                max_pages=args.max_pages,
                max_depth=args.max_depth,
                match_mode=args.match_mode,
                delay=args.delay,
                jitter=args.jitter,
            )
            print(f"  pages: {stats['pages']}; PDF/DOCX found: {stats['pdf']}; Scribd: {stats['scribd']}; "
                  f"not kept: {stats['not_kept']}")
            write_book_list(args.entry_list, entries)
            write_book_list(args.scribd_list, scribd)
    except KeyboardInterrupt:
        print("\nInterrupted. Saved what was found so far.")

    write_book_list(args.entry_list, entries)
    write_book_list(args.scribd_list, scribd)
    print(f"\nPDF/DOCX links: {len(entries)} ({args.entry_list})")
    print(f"Scribd links:   {len(scribd)} ({args.scribd_list})")
    if not_kept:
        print("Sample of links not kept (wrong type or no Burmese match):")
        for label, url in not_kept[:5]:
            print(f"  {label[:70]!r} -> {url[:140]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

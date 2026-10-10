#!/usr/bin/env python3
"""Crawl each site the search engine pointed to and collect its PDF/DOCX links.

    uv run site.py https://example.org/books/   # crawl this site
    uv run site.py                              # no URL: SerpApi searches, then crawl each result

One crawler, two ways to get its starting URLs. Given URLs are crawled as-is.
Without URLs, SerpApi (Google results) runs each search term, and the results
are handled in order: each result site is crawled and its PDFs are downloaded
before the next result. The key is read from SERPAPI in python/.env (or the
environment). Each search is counted against a monthly limit (default 250).

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
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.robotparser
from collections import deque
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from crawler import (
    BOOK_EXTENSION,
    Blocked,
    HttpFetcher,
    RateLimited,
    matches_book_candidate,
    is_scribd_document_url,
    is_site_seed,
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
    parser = argparse.ArgumentParser(description="Crawl websites for PDF/DOCX links, starting from URLs or SerpApi search results.")
    parser.add_argument("urls", nargs="*", help="start URLs (default: search with SerpApi, see --search-terms)")
    parser.add_argument("--entry-list", type=Path, default=HERE / "site_entry_list.txt")
    parser.add_argument("--scribd-list", type=Path, default=HERE / "site_scribd_links.txt")
    parser.add_argument("--max-pages", type=int, default=500,
                        help="safety cap on pages fetched per site (default 500; the site is crawled in full below this)")
    parser.add_argument("--max-depth", type=int, default=10, help="link levels below the start page (default 10)")
    parser.add_argument("--match-mode", default="loose", choices=("loose", "filename"))
    parser.add_argument("--delay", type=float, default=5.0, help="seconds between page requests (default 5)")
    parser.add_argument("--jitter", type=float, default=2.0, help="extra random seconds (default 2)")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--no-download", action="store_true", help="collect links only; do not download after each site")
    parser.add_argument("--search-terms",
                        default="myanmar books download,myanmar ebooks download,Myanmar PDF free download,free မြန်မာ pdf စာအုပ်များ",
                        help="comma-separated search phrases, run in order (no URL given)")
    parser.add_argument("--search-pages", type=int, default=1,
                        help="result pages per phrase; each page is one SerpApi search (default 1)")
    parser.add_argument("--search-gap", type=float, default=60.0,
                        help="minimum seconds between two search requests (default 60)")
    parser.add_argument("--monthly-limit", type=int, default=250, help="SerpApi searches allowed per month (default 250)")
    parser.add_argument("--usage-file", type=Path, default=HERE / "serpapi_usage.json",
                        help="where the searches made this month are counted")
    return parser.parse_args(argv)


SERPAPI_URL = "https://serpapi.com/search.json"
ENV_FILE = HERE / ".env"


class SearchStopped(Exception):
    """The search API refused us, failed or ran out of budget: stop the run, do not retry through it."""


def load_serpapi_key(env_file: Path | None = None) -> str:
    """SERPAPI from the environment, else from a SERPAPI=value line in the .env file."""
    key = os.environ.get("SERPAPI", "").strip()
    if key:
        return key
    env_file = env_file or ENV_FILE
    if env_file.exists():
        for line in env_file.read_text(encoding="utf8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == "SERPAPI":
                return value.strip().strip("\"'")
    return ""


class SearchBudget:
    """Counts the searches made this month, so a run cannot go past the plan's limit."""

    def __init__(self, path: Path, limit: int) -> None:
        self.path = path
        self.limit = limit
        self.month = time.strftime("%Y-%m")
        self.used = 0
        if path.exists():
            data = json.loads(path.read_text(encoding="utf8") or "{}")
            if data.get("month") == self.month:
                self.used = int(data.get("used", 0))

    def remaining(self) -> int:
        return self.limit - self.used

    def record(self) -> None:
        self.used += 1
        self.path.write_text(json.dumps({"month": self.month, "used": self.used}, indent=2), encoding="utf8")


def serpapi_search(query: str, start: int, api_key: str, timeout: float = 60.0) -> tuple[list[dict], bool]:
    """One page of Google results: ([{url, title}], more_pages_available)."""
    params = {"engine": "google", "q": query, "api_key": api_key}
    if start:
        params["start"] = start
    response = httpx.get(SERPAPI_URL, params=params, timeout=timeout)
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code in (401, 403):
        raise SearchStopped(f"SerpApi refused the key (HTTP {response.status_code}). Check SERPAPI in .env.")
    if response.status_code == 429:
        raise SearchStopped("SerpApi rate limit reached (HTTP 429). Stopping; try again later.")
    if response.status_code != 200 or data.get("error"):
        raise SearchStopped(f"SerpApi error: {data.get('error') or 'HTTP ' + str(response.status_code)}")
    results = [
        {"url": item["link"], "title": item.get("title", "")}
        for item in data.get("organic_results", [])
        if item.get("link")
    ]
    more = bool((data.get("serpapi_pagination") or {}).get("next"))
    return results, more


def download_new(entry_list: Path) -> None:
    """Download whatever is in the entry list that is not downloaded yet (download.py keeps history)."""
    import download

    download.main([str(entry_list)])


def run_search(args: argparse.Namespace, fetcher, entries, scribd, not_kept) -> int:
    api_key = load_serpapi_key()
    if not api_key:
        print("No SerpApi key. Put SERPAPI=your-key in python/.env, or set the SERPAPI environment variable.",
              file=sys.stderr)
        return 1
    terms = [term.strip() for term in args.search_terms.split(",") if term.strip()]
    budget = SearchBudget(args.usage_file, args.monthly_limit)
    planned = len(terms) * args.search_pages
    print(f"Searches for this run: {planned}. Used this month: {budget.used}/{budget.limit}.")
    if planned > budget.remaining():
        print(f"This run needs {planned} searches but only {budget.remaining()} are left this month. Not started.",
              file=sys.stderr)
        return 1

    crawled_hosts: set[str] = set()
    last_search = None
    try:
        for term in terms:
            print(f"\nSearch: {term!r}")
            start = 0
            for page in range(args.search_pages):
                if last_search is not None:
                    wait = args.search_gap - (time.monotonic() - last_search)
                    if wait > 0:
                        time.sleep(wait)
                last_search = time.monotonic()
                results, more = serpapi_search(term, start, api_key, timeout=args.timeout)
                budget.record()
                print(f"  page {page + 1}: {len(results)} result(s); searches used {budget.used}/{budget.limit}")
                for result in results:
                    handle_result(result, args, fetcher, entries, scribd, not_kept, crawled_hosts)
                if not results or not more:
                    break
                start += 10
    except SearchStopped as error:
        print(f"\nStopped: {error}")
        return 1
    return 0


def handle_result(result, args, fetcher, entries, scribd, not_kept, crawled_hosts) -> None:
    url, title = result["url"], result["title"]
    if is_scribd_document_url(url):
        upsert(scribd, url, title, "search")
        return
    if BOOK_EXTENSION.search(urllib.parse.urlsplit(url).path.lower()):
        if matches_book_candidate(url, title, args.match_mode):
            upsert(entries, url, title, "search")
        else:
            not_kept.append((title, url))
        return
    if not is_site_seed(url):
        return
    host = host_key(url)
    if host in crawled_hosts:
        return
    crawled_hosts.add(host)
    print(f"  Site: {host} (from {url})")
    stats = crawl_site(
        url, fetcher, entries=entries, scribd=scribd, not_kept=not_kept,
        max_pages=args.max_pages, max_depth=args.max_depth, match_mode=args.match_mode,
        delay=args.delay, jitter=args.jitter,
    )
    print(f"    pages: {stats['pages']}; PDF/DOCX found: {stats['pdf']}; Scribd: {stats['scribd']}")
    write_book_list(args.entry_list, entries)
    write_book_list(args.scribd_list, scribd)
    if stats["pdf"] and not args.no_download:
        download_new(args.entry_list)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    entries = read_book_list(args.entry_list)
    scribd = read_book_list(args.scribd_list)
    not_kept: list[tuple[str, str]] = []
    fetcher = HttpFetcher(timeout=args.timeout)
    try:
        if args.urls:
            for url in args.urls:
                print(f"Site: {host_key(url)} (from {url})")
                stats = crawl_site(
                    url, fetcher, entries=entries, scribd=scribd, not_kept=not_kept,
                    max_pages=args.max_pages, max_depth=args.max_depth, match_mode=args.match_mode,
                    delay=args.delay, jitter=args.jitter,
                )
                print(f"  pages: {stats['pages']}; PDF/DOCX found: {stats['pdf']}; Scribd: {stats['scribd']}")
                write_book_list(args.entry_list, entries)
                write_book_list(args.scribd_list, scribd)
                if stats["pdf"] and not args.no_download:
                    download_new(args.entry_list)
            code = 0
        else:
            code = run_search(args, fetcher, entries, scribd, not_kept)
    except KeyboardInterrupt:
        print("\nInterrupted. Saved what was found so far.")
        code = 130

    write_book_list(args.entry_list, entries)
    write_book_list(args.scribd_list, scribd)
    print(f"\nPDF/DOCX links: {len(entries)} ({args.entry_list})")
    print(f"Scribd links:   {len(scribd)} ({args.scribd_list})")
    if not_kept:
        print("Sample of links not kept (wrong type or no Burmese match):")
        for label, url in not_kept[:5]:
            print(f"  {label[:70]!r} -> {url[:140]}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

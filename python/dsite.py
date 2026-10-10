"""Crawler: search, then find the sites that have PDF/DOCX files. It downloads nothing.

    uv run dsite.py https://example.org/books/   # check this site
    uv run dsite.py                              # no URL: SerpApi searches, then check each result

Without URLs, SerpApi (Google results) runs each search term in turn. Each search
result is checked:

- a PDF/DOCX result goes to site_entry_list.txt as a file URL;
- a Scribd result goes to site_scribd_links.txt, keyed by the document URL, with
  the search engine index link as its Source;
- a site result is read page by page only until the first PDF/DOCX link. That
  site's main link (scheme + host + "/") then goes to
  site_entry_list.txt, and the crawl of that site stops. A site with no hit is
  read up to --max-pages (default 10) and left.

dsite_download.py reads site_entry_list.txt and does the downloading.

The key is read from SERPAPI in python/.env (or the environment). Each search is
counted against a monthly limit (default 250). robots.txt is honoured, requests
are spaced by --delay plus random --jitter, and a site that blocks or rate-limits
us is abandoned at once. Links to other domains are not followed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.robotparser
from pathlib import Path

import httpx

from crawler import (
    BOOK_EXTENSION,
    HttpFetcher,
    is_scribd_document_url,
    is_site_seed,
    read_url_list,
    upsert,
)
from entry_list import read_entries, save_entries
from sitecrawl import crawl_site, host_key
from siteguard import DEFAULT_SITE_PAGE_LIMIT, excluded_site_reason, large_site_reason

HERE = Path(__file__).resolve().parent
SCAN_DIR = HERE.parent / "scan"  # all text output of this crawler goes here

def read_seeds(path: Path) -> list[str]:
    seen: dict[str, str] = {}
    for url in read_url_list(path):
        seen.setdefault(host_key(url), url)
    return list(seen.values())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawler: search, find the sites that link a PDF/DOCX, and register them in the entry list. "
                    "It does not download; dsite_download.py does.")
    parser.add_argument("urls", nargs="*", help="site URLs to check (default: search with SerpApi)")
    parser.add_argument("--entry-list", type=Path, default=SCAN_DIR / "site_entry_list.txt",
                        help="sites (and direct files) for the downloader")
    parser.add_argument("--scribd-list", type=Path, default=SCAN_DIR / "site_scribd_links.txt")
    parser.add_argument("--max-pages", type=int, default=10,
                        help="pages read per site while looking for a download link (default 10)")
    parser.add_argument("--max-site-pages", type=int, default=DEFAULT_SITE_PAGE_LIMIT,
                        help="skip sites whose sitemap lists more than this many URLs; 0 disables this size check")
    parser.add_argument("--max-depth", type=int, default=10, help="link levels below the start page (default 10)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between page requests (default 1)")
    parser.add_argument("--jitter", type=float, default=0.5, help="extra random seconds (default 0.5)")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--search-terms",
                        default="myanmar books download,myanmar ebooks download,Myanmar PDF free download,free မြန်မာ pdf စာအုပ်များ",
                        help="comma-separated search phrases, run in order (no URL given)")
    parser.add_argument("--search-pages", type=int, default=2,
                        help="result pages per keyword; each page is one SerpApi search (default 2)")
    parser.add_argument("--search-gap", type=float, default=60.0,
                        help="minimum seconds between two search requests (default 60)")
    parser.add_argument("--monthly-limit", type=int, default=250, help="SerpApi searches allowed per month (default 250)")
    parser.add_argument("--search-log", type=Path, default=SCAN_DIR / "search_results.txt",
                        help="every search result, written before any site is checked")
    parser.add_argument("--search-cache", type=Path, default=SCAN_DIR / "search_cache.json",
                        help="saved search responses; a rerun reuses them and spends no search")
    parser.add_argument("--fresh-search", action="store_true",
                        help="ignore the saved search responses and search again")
    parser.add_argument("--usage-file", type=Path, default=SCAN_DIR / "serpapi_usage.json",
                        help="where the searches made this month are counted")
    return parser.parse_args(argv)


SERPAPI_URL = "https://serpapi.com/search.json"
ENV_FILE = HERE / ".env"


class SearchStopped(Exception):
    """The search API refused us, failed or ran out of budget: stop the run, do not retry through it."""


def _read_env_text(path: Path) -> str:
    """Read a .env file the way Windows editors and shells write it: UTF-8 (with or without BOM) or UTF-16."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def load_serpapi_key(env_file: Path | None = None) -> str:
    """SERPAPI from the environment, else from a SERPAPI=value line in the .env file.

    Accepts "SERPAPI=key", "export SERPAPI=key", quotes around the value, and a BOM.
    """
    key = os.environ.get("SERPAPI", "").strip()
    if key:
        return key
    env_file = env_file or ENV_FILE
    if env_file.exists():
        for raw_line in _read_env_text(env_file).splitlines():
            line = raw_line.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == "SERPAPI":
                value = value.strip().strip("\"'").strip()
                if value:
                    return value
    return ""


def explain_missing_key(env_file: Path | None = None) -> str:
    env_file = env_file or ENV_FILE
    if not env_file.exists():
        return (f"No SerpApi key. The file {env_file} does not exist. Create it with one line: "
                "SERPAPI=your-key (or set the SERPAPI environment variable).")
    return (f"No SerpApi key. The file {env_file} exists, but it has no line of the form SERPAPI=your-key "
            "with a value after the '='. Check the variable name, and that the file is named exactly .env.")


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


def search_key(term: str, start: int) -> str:
    return f"{term}|{start}"


def load_search_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf8") or "{}")
    except ValueError:
        return {}


def save_search_cache(path: Path, cache: dict) -> None:
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf8")


def log_search_results(path: Path, term: str, start: int, results: list[dict]) -> None:
    """Append every result of one search to the readable list, before anything is crawled."""
    new_file = not path.exists()
    with path.open("a", encoding="utf8") as handle:
        if new_file:
            handle.write("Keyword\tStart\tRank\tTitle\tURL\n")
        for rank, result in enumerate(results, start=1):
            title = " ".join(result["title"].split())
            handle.write(f"{term}\t{start}\t{rank}\t{title}\t{result['url']}\n")


def run_search(args: argparse.Namespace, fetcher, entries, scribd) -> int:
    api_key = load_serpapi_key()
    if not api_key:
        print(explain_missing_key(), file=sys.stderr)
        return 1
    terms = [term.strip() for term in args.search_terms.split(",") if term.strip()]
    cache = {} if args.fresh_search else load_search_cache(args.search_cache)
    budget = SearchBudget(args.usage_file, args.monthly_limit)
    print(f"Keywords: {len(terms)}. Pages per keyword: {args.search_pages}. "
          f"Searches used this month: {budget.used}/{budget.limit}.")

    probed: set[str] = set()
    last_search = None
    try:
        for term in terms:
            print(f"\nSearch: {term!r}")
            start = 0
            page_no = 0
            while True:
                page_no += 1
                key = search_key(term, start)
                if key in cache:
                    results, more = cache[key]["results"], cache[key]["more"]
                    print(f"  page {page_no}: {len(results)} result(s) from saved results (no search used)")
                else:
                    if budget.remaining() <= 0:
                        print(f"\nMonthly SerpApi budget used up ({budget.used}/{budget.limit}). Stopping. "
                              "Saved results are kept; a later run reuses them.")
                        return 1
                    if last_search is not None:
                        wait = args.search_gap - (time.monotonic() - last_search)
                        if wait > 0:
                            time.sleep(wait)
                    last_search = time.monotonic()
                    results, more = serpapi_search(term, start, api_key, timeout=args.timeout)
                    budget.record()
                    # Save the whole result list first, so nothing is lost if the crawl stops or fails.
                    cache[key] = {"results": results, "more": more}
                    save_search_cache(args.search_cache, cache)
                    log_search_results(args.search_log, term, start, results)
                    print(f"  page {page_no}: {len(results)} result(s); searches used {budget.used}/{budget.limit}")
                for result in results:
                    handle_result(result, args, fetcher, entries, scribd, probed, term)
                if not results or not more:
                    break
                if page_no >= args.search_pages:
                    break
                start += 10
    except SearchStopped as error:
        print(f"\nStopped: {error}")
        return 1
    return 0


def search_index_url(term: str) -> str:
    """The search engine's own results page for a keyword. Rows from that search are registered under it."""
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(term)


def site_root(url: str) -> str:
    """The site's main link: scheme and host only."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


def save_lists(args, entries, scribd) -> None:
    save_entries(args.entry_list, entries)  # merges with the file: hand-added rows are kept
    save_entries(args.scribd_list, scribd)


def discover_site(url: str, fetcher, args, scribd, index: str) -> bool:
    """Read the site only until one page links a PDF/DOCX. True if it has one.

    Known unhelpful site classes are rejected before any request. For other
    sites, an available sitemap is counted before HTML pages are crawled. The
    crawler stops at the first file; this guard is intentionally not used by
    dsite_download.py.
    """
    blocked_reason = excluded_site_reason(url)
    if blocked_reason:
        print(f"  Site guard: skipping {host_key(url)}: {blocked_reason}.")
        return False

    found: dict[str, dict] = {}
    scratch_scribd: dict[str, dict] = {}
    page_cap = args.max_pages
    if args.max_site_pages > 0:
        page_cap = min(page_cap, args.max_site_pages)
    stats = crawl_site(
        url, fetcher, entries=found, scribd=scratch_scribd,
        max_pages=page_cap, max_depth=args.max_depth,
        delay=args.delay, jitter=args.jitter, stop_at_first_file=True,
        preflight=lambda seed, site_fetcher, robots: large_site_reason(
            seed, site_fetcher, robots, max_pages=args.max_site_pages,
        ),
    )
    for link, record in scratch_scribd.items():
        upsert(scribd, link, record["name"], index)
    return stats["pdf"] > 0


def handle_result(result, args, fetcher, entries, scribd, probed, term) -> None:
    """One search result. Scribd -> Scribd list (under the search index link). A file -> entry list.
    A site -> checked once; if it has a download, its main link goes to the entry list."""
    url, title = result["url"], result["title"]
    index = search_index_url(term)
    if is_scribd_document_url(url):
        upsert(scribd, url, title, index)
        return
    if BOOK_EXTENSION.search(urllib.parse.urlsplit(url).path.lower()):
        upsert(entries, url, title, index)  # the search is Burmese: no name filter
        return
    blocked_reason = excluded_site_reason(url)
    if blocked_reason:
        print(f"  Site guard: skipping {host_key(url)}: {blocked_reason}.")
        return
    if not is_site_seed(url):
        return
    host = host_key(url)
    if host in probed:
        return
    probed.add(host)
    print(f"  Checking site: {host} (from {url})")
    if discover_site(url, fetcher, args, scribd, index):
        root = site_root(url)
        upsert(entries, root, title, index)
        print(f"    has a download link: registered {root}")
    else:
        print(f"    no PDF/DOCX link in {args.max_pages} page(s) or fewer; not registered")
    save_lists(args, entries, scribd)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for output in (args.entry_list, args.scribd_list, args.usage_file):
        output.parent.mkdir(parents=True, exist_ok=True)
    entries = read_entries(args.entry_list)
    scribd = read_entries(args.scribd_list)
    save_lists(args, entries, scribd)  # create both lists now, so they exist before the first hit
    fetcher = HttpFetcher(timeout=args.timeout)
    try:
        if args.urls:
            for url in args.urls:
                print(f"Site: {host_key(url)} (from {url})")
                if discover_site(url, fetcher, args, scribd, index=url):
                    root = site_root(url)
                    upsert(entries, root, root, url)
                    print(f"  has a download link: registered {root}")
                else:
                    print(f"  no PDF/DOCX link in {args.max_pages} page(s) or fewer; not registered")
                save_lists(args, entries, scribd)
            code = 0
        else:
            code = run_search(args, fetcher, entries, scribd)
    except KeyboardInterrupt:
        print("\nInterrupted. Saved what was found so far.")
        code = 130

    save_lists(args, entries, scribd)
    print(f"\nEntry list:     {len(entries)} ({args.entry_list})")
    print(f"Scribd links:   {len(scribd)} ({args.scribd_list})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

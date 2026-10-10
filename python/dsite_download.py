#!/usr/bin/env python3
"""Downloader: take each file the crawler found, make it readable, and save it.

    uv run dsite_download.py                     # reads scan/site_entry_list.txt

For every PDF/DOCX, one file at a time:

    download  ->  decrypt (unlock)  ->  linearize  ->  saved to disk

A row whose URL is a file is handled directly. A row whose URL is a site (its main
link) is crawled, and each file is handled as soon as the crawler finds it, before
the crawl goes on to the next link. Files already in the download history are not
fetched again.

The crawler (dsite.py) does not download. This script does not search.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import urllib.parse
from pathlib import Path

import download  # python/download.py: fetches one file and records it in the history
import linearize  # python/linearize.py: unlock + linearize + save, in place
from crawler import BOOK_EXTENSION, HttpFetcher, read_book_list, upsert
from dsite import SCAN_DIR, crawl_site, host_key

HERE = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = HERE / "downloaded_files"
SAVED = re.compile(r"saved \((.+), \d+ KB\)$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the PDF/DOCX files listed by dsite.py, unlock and linearize each one, and save it.")
    parser.add_argument("--entry-list", type=Path, default=SCAN_DIR / "site_entry_list.txt")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--max-pages", type=int, default=500, help="pages read per site (default 500)")
    parser.add_argument("--max-depth", type=int, default=10)
    parser.add_argument("--match-mode", default="loose", choices=("loose", "filename"))
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between page requests (default 1)")
    parser.add_argument("--jitter", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--password", action="append", default=[], metavar="PW",
                        help="password to try on an encrypted PDF (repeatable)")
    parser.add_argument("--password-file", type=Path, default=None, metavar="FILE",
                        help="file of passwords to try, one per line")
    return parser.parse_args(argv)


def is_file_url(url: str) -> bool:
    return bool(BOOK_EXTENSION.search(urllib.parse.urlsplit(url).path.lower()))


class Downloads:
    """Holds what one run needs to handle a file: where it goes, its history, its passwords."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.out_dir = args.out_dir
        self.history = download.History(self.out_dir / ".download-history.json")
        self.passwords = tuple(args.password)
        if args.password_file:
            try:
                self.passwords += tuple(line.strip() for line in
                                        args.password_file.read_text(encoding="utf8").splitlines() if line.strip())
            except OSError as error:
                raise SystemExit(f"Could not read {args.password_file}: {error}") from error
        self.fetch_args = download.parse_args([])  # download.py's defaults, with ours applied
        self.fetch_args.timeout = args.timeout
        self.fetch_args.retries = args.retries
        self.fetch_args.delay = 0
        self.fetch_args.concurrency = 1
        self.fetch_args.verify_pdf = False
        self.fetch_args.keep_rejected = False

    def handle(self, url: str, label: str) -> str:
        """download -> unlock -> linearize -> saved. Returns one status line."""
        outcomes = asyncio.run(download.download_all([{"name": label, "url": url}], self.out_dir,
                                                     self.history, self.fetch_args))
        status = outcomes.get(url, "failed: no result")
        if not status.startswith("saved ("):
            return status  # cached, failed or rejected: nothing new on disk
        match = SAVED.search(status)
        if not match:
            return status
        name = match.group(1)
        path = self.out_dir / name
        if path.suffix.lower() != ".pdf":
            return f"{name}: saved (DOCX, not linearized)"
        outcome = linearize.linearize(path, passwords=self.passwords)
        if outcome.ok:
            unlocked = ", unlocked" if outcome.detail == "unlocked" else ""
            return f"{name}: saved, {outcome.state}{unlocked}"
        # The file is kept as downloaded. The linearizer leaves it untouched on failure.
        return f"{name}: saved as downloaded; {outcome.state}: {outcome.detail}"


def drain(queue: dict[str, dict], downloads: Downloads) -> None:
    """Handle every file waiting in the queue, one at a time, then empty it."""
    while queue:
        url = next(iter(queue))
        row = queue.pop(url)
        try:
            status = downloads.handle(url, row["name"])
        except Exception as error:  # a failed file must not stop the run
            status = f"error (the run continues): {error}"
        print(f"    {status}  <- {url}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = read_book_list(args.entry_list)
    if not rows:
        print(f"The entry list is empty or missing: {args.entry_list}. Run dsite.py first.", file=sys.stderr)
        return 1

    downloads = Downloads(args)
    fetcher = HttpFetcher(timeout=args.timeout)
    queue: dict[str, dict] = {}
    scribd_seen: dict[str, dict] = {}  # the downloader does not register Scribd links
    not_kept: list[tuple[str, str]] = []
    try:
        for row in rows.values():
            url = row["url"]
            if is_file_url(url):
                upsert(queue, url, row["name"], row["source"])
                drain(queue, downloads)
                continue
            print(f"\nSite: {host_key(url)} ({url})")
            stats = crawl_site(
                url, fetcher, entries=queue, scribd=scribd_seen, not_kept=not_kept,
                max_pages=args.max_pages, max_depth=args.max_depth, match_mode=args.match_mode,
                delay=args.delay, jitter=args.jitter,
                save=lambda: drain(queue, downloads),
            )
            drain(queue, downloads)
            print(f"  pages: {stats['pages']}; PDF/DOCX found: {stats['pdf']}")
    except KeyboardInterrupt:
        print("\nInterrupted. Files saved so far are kept; a rerun skips them.")
        return 130
    print(f"\nDone. Output: {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Downloader: takes the entry list that dsite.py wrote and downloads the files.

    uv run dsite_download.py                     # reads scan/site_entry_list.txt

A row whose URL is a PDF or DOCX is downloaded directly. A row whose URL is a site
(its main link) is crawled, and every PDF/DOCX it links to is downloaded as soon as
it is found. Files already in the download history are skipped by download.py, so
no file is downloaded twice.

The crawler (dsite.py) does not download. This script does not search.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import download  # python/download.py: downloads a list of file URLs, keeps the history
from crawler import BOOK_EXTENSION, HttpFetcher, read_book_list, upsert, write_book_list
from dsite import SCAN_DIR, crawl_site, host_key

import urllib.parse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download the files listed by dsite.py; crawl listed sites for files.")
    parser.add_argument("--entry-list", type=Path, default=SCAN_DIR / "site_entry_list.txt")
    parser.add_argument("--queue", type=Path, default=SCAN_DIR / "download_queue.txt",
                        help="file links waiting for download.py (rewritten before each download)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="where files are saved (default: python/downloaded_files)")
    parser.add_argument("--max-pages", type=int, default=500, help="pages read per site (default 500)")
    parser.add_argument("--max-depth", type=int, default=10)
    parser.add_argument("--match-mode", default="loose", choices=("loose", "filename"))
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--jitter", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser.parse_args(argv)


def is_file_url(url: str) -> bool:
    return bool(BOOK_EXTENSION.search(urllib.parse.urlsplit(url).path.lower()))


def download_queue(args: argparse.Namespace, queue: dict[str, dict]) -> None:
    """Hand the waiting file links to download.py, then forget them here (download.py keeps the history)."""
    if not queue:
        return
    write_book_list(args.queue, queue)
    argv = [str(args.queue)]
    if args.out_dir:
        argv.append(f"--out-dir={args.out_dir}")
    try:
        download.main(argv)
    except Exception as error:  # a failed download must not stop the run
        print(f"  Download error (the run continues): {error}")
    queue.clear()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.queue.parent.mkdir(parents=True, exist_ok=True)
    rows = read_book_list(args.entry_list)
    if not rows:
        print(f"The entry list is empty or missing: {args.entry_list}. Run dsite.py first.", file=sys.stderr)
        return 1

    fetcher = HttpFetcher(timeout=args.timeout)
    queue: dict[str, dict] = {}
    scribd_seen: dict[str, dict] = {}  # the downloader does not register Scribd links
    not_kept: list[tuple[str, str]] = []
    try:
        for row in rows.values():
            url = row["url"]
            if is_file_url(url):
                upsert(queue, url, row["name"], row["source"])
                download_queue(args, queue)
                continue
            print(f"\nSite: {host_key(url)} ({url})")
            stats = crawl_site(
                url, fetcher, entries=queue, scribd=scribd_seen, not_kept=not_kept,
                max_pages=args.max_pages, max_depth=args.max_depth, match_mode=args.match_mode,
                delay=args.delay, jitter=args.jitter,
                save=lambda: download_queue(args, queue),
            )
            download_queue(args, queue)
            print(f"  pages: {stats['pages']}; PDF/DOCX found: {stats['pdf']}")
    except KeyboardInterrupt:
        print("\nInterrupted. Files downloaded so far are kept; a rerun skips them.")
        return 130
    print(f"\nDone. Output: {args.out_dir or Path(__file__).resolve().parent / 'downloaded_files'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

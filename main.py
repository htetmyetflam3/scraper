"""Single executor for the scrape pipeline: search, entry-crawl, download."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent / "python" / "bin"
sys.path.insert(0, str(BIN_DIR))

from Dcrawler import download_entries  # noqa: E402
from fetch_results import fetch_results  # noqa: E402
from Scrawler import find_entry  # noqa: E402
from serpapi import SerpApiError  # noqa: E402

USE_ENTRY_FILE: object = object()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the scrape pipeline stages independently or all at once."
    )
    parser.add_argument(
        "--search",
        nargs="*",
        default=None,
        metavar="KEYWORD",
        help=(
            "Call SerpApi and append to search_results.json: bare flag "
            "uses the default keywords, or pass keyword(s) to fetch only "
            "those (e.g. --search \"pdfများ\"). Already-saved keywords "
            "are skipped without API calls."
        ),
    )
    parser.add_argument(
        "--entry",
        action="store_true",
        help="Crawl saved results for download links and write entry.txt.",
    )
    parser.add_argument(
        "--download",
        nargs="?",
        const=USE_ENTRY_FILE,
        default=None,
        metavar="SITE_URL",
        help=(
            "Download files using site links from entry.txt, "
            'or from SITE_URL when given (e.g. --download "https://example.com").'
        ),
    )
    parser.add_argument(
        "--main",
        action="store_true",
        help="Run search, then entry crawl, then download, in order.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.main:
            fetch_results()
            find_entry()
            download_entries()
            return 0
        if args.search is None and not args.entry and args.download is None:
            parser.print_usage(sys.stderr)
            return 2
        if args.search is not None:
            if args.search:
                fetch_results(keywords=args.search)
            else:
                fetch_results()
        if args.entry:
            find_entry()
        if args.download is not None:
            if args.download is USE_ENTRY_FILE:
                download_entries()
            else:
                download_entries(args.download)
    except (SerpApiError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

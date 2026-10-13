"""Single executor for the scrape pipeline: search, entry-crawl, download."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent / "python" / "bin"
sys.path.insert(0, str(BIN_DIR))

from Dcrawler import download_entries  # noqa: E402
from fetch_results import fetch_results  # noqa: E402
from phatmal import (  # noqa: E402
    login_and_verify,
    probe_content,
    run_phatmal,
    slug_from_input,
)
from Scrawler import find_entry  # noqa: E402
from scribd import download_scribd, read_links_file  # noqa: E402
from serpapi import SerpApiError  # noqa: E402

USE_ENTRY_FILE: object = object()
USE_SCRIBD_FILE: object = object()
USE_PHATMAL_LIBRARY: object = object()


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
        help="Crawl saved results for download links and write scan/entry.txt.",
    )
    parser.add_argument(
        "--download",
        nargs="?",
        const=USE_ENTRY_FILE,
        default=None,
        metavar="SITE_URL",
        help=(
            "Download files using site links from scan/entry.txt, "
            'or from SITE_URL when given (e.g. --download "https://example.com").'
        ),
    )
    parser.add_argument(
        "--scribd",
        nargs="?",
        const=USE_SCRIBD_FILE,
        default=None,
        metavar="SCRIBD_URL",
        help=(
            "Download scribd documents from the scribd links file, "
            "or the given document URL when passed."
        ),
    )
    parser.add_argument(
        "--main",
        action="store_true",
        help="Run search, then entry crawl, then download, in order.",
    )
    parser.add_argument(
        "--phatmal-check",
        action="store_true",
        help=(
            "Log in to the phatmal API with the PHATMAL_* .env credentials "
            "and verify the session works."
        ),
    )
    parser.add_argument(
        "--phatmal-probe",
        default=None,
        metavar="SLUG_OR_URL",
        help=(
            "Log in to the phatmal API and probe the likely book endpoints "
            "for a slug or /ebook/<slug> URL, printing each answer's shape."
        ),
    )
    parser.add_argument(
        "--phatmal",
        nargs="?",
        const=USE_PHATMAL_LIBRARY,
        default=None,
        metavar="SLUG_OR_URL",
        help=(
            "Download phatmal ebooks with the PHATMAL_* .env credentials: "
            "bare flag crawls the whole library, or pass a slug or "
            "/ebook/<slug> URL for one book."
        ),
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
        if (
            args.search is None
            and not args.entry
            and args.download is None
            and args.scribd is None
            and not args.phatmal_check
            and args.phatmal_probe is None
            and args.phatmal is None
        ):
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
        if args.scribd is not None:
            if args.scribd is USE_SCRIBD_FILE:
                links = read_links_file()
            else:
                links = [args.scribd]
            summary = download_scribd(links)
            if links and not summary.downloaded:
                return 1
        if args.phatmal_check:
            if not asyncio.run(login_and_verify()):
                return 1
        if args.phatmal_probe is not None:
            slug = slug_from_input(args.phatmal_probe)
            if not slug:
                print(
                    "error: --phatmal-probe needs a slug or /ebook/<slug> URL",
                    file=sys.stderr,
                )
                return 2
            if not asyncio.run(probe_content(slug)):
                return 1
        if args.phatmal is not None:
            target = None if args.phatmal is USE_PHATMAL_LIBRARY else args.phatmal
            if target is not None and not slug_from_input(target):
                print(
                    "error: --phatmal needs a slug or /ebook/<slug> URL",
                    file=sys.stderr,
                )
                return 2
            if not asyncio.run(run_phatmal(target)):
                return 1
    except (SerpApiError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Single entry point for the scrape pipeline: one command per run."""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
import time
import urllib.parse
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent / "python" / "bin"
sys.path.insert(0, str(BIN_DIR))

from Dcrawler import ENTRY_FILE, OUTPUT_DIR, download_entries, note_pause  # noqa: E402
from download_rate import validate_limit  # noqa: E402
from fetch_results import DEFAULT_KEYWORDS, fetch_results  # noqa: E402
from mailer import RunMail  # noqa: E402
from phatmal import run_phatmal  # noqa: E402
from save_remote import watch  # noqa: E402
from Scrawler import find_entry  # noqa: E402
from scribd import download_scribd, read_links_file  # noqa: E402
from serpapi import SerpApiError  # noqa: E402

USE_DEFAULT_KEYWORDS: object = object()
USE_ENTRY_FILE: object = object()
USE_SCRIBD_FILE: object = object()
USE_PHATMAL_LIBRARY: object = object()
DEFAULT_INTERVAL = 60.0

EXAMPLES = """\
examples:
  uv run main.py --findentry
  uv run main.py --findentry "pdfများ"
  uv run main.py --main -l 2
  uv run main.py --download
  uv run main.py --download https://example.com -l 2
  uv run main.py --download my-entries.txt
  uv run main.py --upload -i 60
  uv run main.py --scribd
  uv run main.py --scribd https://www.scribd.com/document/123/title
  uv run main.py --scribd my-scribd-links.txt
  uv run main.py --phatmal
  uv run main.py --phatmal https://phatmal.com/ebook/<slug>

exit codes: 0 ok, 1 failed, 2 usage error, 130 paused (Ctrl+C; rerun resumes)

email: every command except --findentry mails a started notice, then a
finished or failed one naming the part: kaungmyatthu@devnovex.shop ->
htetmyet986@gmail.com via smtp.hostinger.com:465 (MAIL_PASSWORD in .env).
"""


class RunFailed(Exception):
    """A command ran but produced nothing; reported like any other failure."""


def _limit(text: str) -> float:
    try:
        return validate_limit(float(text))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _interval(text: str) -> float:
    try:
        value = float(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None
    if not value >= 1 or value == float("inf"):
        raise argparse.ArgumentTypeError("interval must be a number >= 1 second")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Single entry point for the scrape pipeline.\n"
            "Pick one command; with no command this help is shown and nothing runs."
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_argument_group("commands (one per run)")
    command = commands.add_mutually_exclusive_group()
    command.add_argument(
        "--findentry",
        nargs="?",
        const=USE_DEFAULT_KEYWORDS,
        default=None,
        metavar="KEYWORD",
        help=(
            "Search KEYWORD with SerpApi, or the built-in keywords when empty. "
            "Results always go to scan/search_results.json; keywords already "
            "saved there skip the API. Then crawl only those keywords' results "
            "and append found sites to scan/entry.txt."
        ),
    )
    command.add_argument(
        "--main",
        action="store_true",
        help=(
            "Run search (built-in keywords) -> entry crawl (all saved results) "
            "-> download from scan/entry.txt, in order."
        ),
    )
    command.add_argument(
        "--download",
        nargs="?",
        const=USE_ENTRY_FILE,
        default=None,
        metavar="URL_OR_FILE",
        help=(
            "Download files from the sites in scan/entry.txt, or from "
            "URL_OR_FILE: one site URL or another entries file. Ctrl+C "
            "pauses; rerun resumes."
        ),
    )
    command.add_argument(
        "--upload",
        action="store_true",
        help=(
            "Push every file in python/pdfs to the SSH host (SSH_* in .env), "
            "verify its sha256, then delete the local copy; checks again "
            "every -i seconds until Ctrl+C."
        ),
    )
    command.add_argument(
        "--scribd",
        nargs="?",
        const=USE_SCRIBD_FILE,
        default=None,
        metavar="URL_OR_FILE",
        help=(
            "Download Scribd documents from scan/scribd.txt, or from "
            "URL_OR_FILE: one Scribd URL or a links file. Any non-Scribd URL "
            "stops the run before downloading."
        ),
    )
    command.add_argument(
        "--phatmal",
        nargs="?",
        const=USE_PHATMAL_LIBRARY,
        default=None,
        metavar="URL",
        help=(
            "Download the whole phatmal library (PHATMAL_* in .env), or one "
            "book from a https://phatmal.com/ebook/<slug> URL. Any other URL "
            "is an error."
        ),
    )
    numbers = parser.add_argument_group("numbers")
    numbers.add_argument(
        "-l",
        dest="limit",
        type=_limit,
        default=None,
        metavar="MB_PER_SEC",
        help=(
            "With --download/--main: download speed cap in MB/s "
            "(1 MB = 1048576 bytes). Default 0 = unlimited."
        ),
    )
    numbers.add_argument(
        "-i",
        dest="interval",
        type=_interval,
        default=None,
        metavar="SECONDS",
        help=f"With --upload: seconds between folder checks (default {DEFAULT_INTERVAL:g}).",
    )
    return parser


def _is_url(text: str) -> bool:
    parts = urllib.parse.urlsplit(text)
    return parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)


def _keywords(value: object) -> list[str]:
    if value is USE_DEFAULT_KEYWORDS or not str(value).strip():
        return list(DEFAULT_KEYWORDS)
    return [str(value).strip()]


def _scribd_links(value: object) -> list[str]:
    if value is USE_SCRIBD_FILE:
        return read_links_file()
    text = str(value).strip()
    return [text] if _is_url(text) else read_links_file(text)


def _download_details(summary) -> list[str]:
    return [
        f"📚 Saved {len(summary.saved)} new file(s) · {len(summary.downloaded)} already "
        f"downloaded · {len(summary.unresolved)} could not be downloaded."
    ]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and not (args.main or args.download is not None):
        parser.error("-l works with --download or --main")
    if args.interval is not None and not args.upload:
        parser.error("-i works with --upload")
    if (
        args.findentry is None
        and not args.main
        and args.download is None
        and not args.upload
        and args.scribd is None
        and args.phatmal is None
    ):
        parser.print_help()
        return 0
    limit = args.limit or 0.0
    job = "main.py " + shlex.join(sys.argv[1:] if argv is None else argv)
    mail = None if args.findentry is not None else RunMail(job)
    part = "download"
    details: list[str] = []
    try:
        if mail is not None:
            mail.send_started()
        if args.findentry is not None:
            keywords = _keywords(args.findentry)
            fetch_results(keywords=keywords)
            find_entry(keywords=keywords)
        elif args.main:
            part = "search"
            fetch_results()
            part = "entry crawl"
            find_entry()
            part = "download"
            details = _download_details(download_entries(download_limit=limit))
        elif args.download is not None:
            source = ENTRY_FILE if args.download is USE_ENTRY_FILE else args.download
            details = _download_details(download_entries(source, download_limit=limit))
        elif args.upload:
            part = "upload"
            watch(OUTPUT_DIR, args.interval or DEFAULT_INTERVAL)
        elif args.scribd is not None:
            part = "scribd download"
            links = _scribd_links(args.scribd)
            summary = download_scribd(links)
            if links and not summary.downloaded:
                raise RunFailed(f"nothing downloaded; all {len(links)} link(s) failed")
            details = [
                f"📄 Downloaded {len(summary.downloaded)} Scribd document(s), "
                f"{len(summary.failed)} failed."
            ]
        else:
            part = "phatmal download"
            target = None if args.phatmal is USE_PHATMAL_LIBRARY else args.phatmal
            if not asyncio.run(run_phatmal(target)):
                raise RunFailed("nothing was saved; see the log above")
    except (SerpApiError, OSError, ValueError, RunFailed) as error:
        print(f"error: {error}", file=sys.stderr)
        if mail is not None:
            mail.send_failed(part, error)
        return 1
    except KeyboardInterrupt:
        if args.upload:
            print(
                "\n[paused] watcher stopped; unarchived files stay local. "
                f"@ {time.strftime('%H:%M:%S')}"
            )
        else:
            print(
                "\n[paused] interrupted; progress is journaled - rerun to "
                f"resume. @ {note_pause()}"
            )
        return 130
    except Exception as error:
        if mail is not None:
            mail.send_failed(part, f"{type(error).__name__}: {error}")
        raise
    if mail is not None:
        mail.send_finished(details)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

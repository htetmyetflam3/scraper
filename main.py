"""Single entry point for the scrape pipeline: one command per run."""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
from systemd_launcher import launch_under_systemd  # noqa: E402

_systemd_status = launch_under_systemd(sys.argv[1:])
if _systemd_status is not None:
    raise SystemExit(_systemd_status)

BIN_DIR = ROOT / "crawler" / "bin"
sys.path.insert(0, str(BIN_DIR))

from Dcrawler import ENTRY_FILE, OUTPUT_DIR, download_entries, note_pause, retry_line  # noqa: E402
from disk_floor import DiskTight  # noqa: E402
from download_rate import validate_limit  # noqa: E402
from fetch_results import DEFAULT_KEYWORDS, fetch_results  # noqa: E402
from mailer import RunMail  # noqa: E402
from night import NightFailed, run_process  # noqa: E402
from phatmal import run_phatmal  # noqa: E402
from save_remote import start_remote, upload_all  # noqa: E402
from Scrawler import find_entry  # noqa: E402
from scribd import download_scribd, read_links_file  # noqa: E402
from serpapi import SerpApiError  # noqa: E402

# --process (night run): hours until it stops at a safe point and mails the
# report - your wake-up time. No flag on purpose: edit this number.
NIGHT_HOURS = 6

USE_DEFAULT_KEYWORDS: object = object()
USE_ENTRY_FILE: object = object()
USE_SCRIBD_FILE: object = object()
USE_PHATMAL_LIBRARY: object = object()
DEFAULT_INTERVAL = 60.0

EXAMPLES = f"""\
examples:
  uv run main.py --findentry
  uv run main.py --findentry "pdfများ"
  uv run main.py --main -l 2
  uv run main.py --download
  uv run main.py --download https://example.com -l 2
  uv run main.py --download my-entries.txt
  uv run main.py --upload
  uv run main.py --process
  uv run main.py --process https://example.com
  uv run main.py --process https://example.com -i 60 -l 5
  uv run main.py --scribd
  uv run main.py --scribd https://www.scribd.com/document/123/title
  uv run main.py --scribd my-scribd-links.txt
  uv run main.py --phatmal
  uv run main.py --phatmal https://phatmal.com/ebook/<slug>

exit codes: 0 ok (also --process stopping at its {NIGHT_HOURS:g}-hour mark), 1 failed,
2 usage error, 130 paused (Ctrl+C; rerun resumes)

email: every command except --findentry mails a started notice, then a
finished (--upload: when pdfs is empty) or failed one naming the part: kaungmyatthu@devnovex.shop ->
htetmyet986@gmail.com via smtp.hostinger.com:465 (MAIL_PASSWORD in .env); a
copy of each goes to that mailbox's Sent folder via imap.hostinger.com:993.
--process mails started, then one report at the {NIGHT_HOURS:g}-hour mark (or when all is
done) listing every problem with its part and time; nothing in between unless
it quits with an error (the disk under 5 GB free): that failed email goes
right away, with the problems so far.

upload (--upload, --process): a file that fails is retried after 15 s, 30 s,
45 s, 1 min, 1 min 30 s, 2 min, 2 min 30 s and 3 min (UPLOAD_WAITS in
crawler/bin/save_remote.py); then uploading stops instead of looping: with an
error when under 5 GB is free, else the files stay in pdfs.

disk: 5 GB is kept free for the system. Every download command (--main,
--download, --process, --scribd, --phatmal) checks it before each file and
quits with an error under it; free space (e.g. uv run main.py --upload), then
rerun to resume.
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
            "Push every file in pdfs to the SSH host (SSH_* in .env), "
            "verify its sha256, then delete the local copy; when the folder "
            "is empty it mails \"finished\" and exits. A file that fails is retried "
            "after 15 s, 30 s, 45 s, 1 min, 1 min 30 s, 2 min, 2 min 30 s "
            "and 3 min; then uploading stops: with an error when under 5 GB "
            "is free, else normally (the files stay in pdfs)."
        ),
    )
    command.add_argument(
        "--process",
        nargs="?",
        const=USE_ENTRY_FILE,
        default=None,
        metavar="URL",
        help=(
            "Night run in one terminal: download from scan/entry.txt, or from "
            "URL (one site URL: no scan/entry.txt needed), while uploading to "
            "the SSH host (checked every -i seconds). Pages and "
            "files that fail or can't be reached are skipped and recorded like "
            f"--download; stops at the {NIGHT_HOURS:g}-hour mark "
            "(NIGHT_HOURS at the top of main.py) and mails "
            "a report. Quits with an error (mailed right away) when the disk "
            "goes under 5 GB free, like every download command; rerun resumes."
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
            "With --download/--main/--process: download speed cap in MB/s "
            "(1 MB = 1048576 bytes). Default 0 = unlimited."
        ),
    )
    numbers.add_argument(
        "-i",
        dest="interval",
        type=_interval,
        default=None,
        metavar="SECONDS",
        help=(
            f"With --process: seconds between upload folder checks "
            f"(default {DEFAULT_INTERVAL:g})."
        ),
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
    lines = [
        f"📚 Saved {len(summary.saved)} new file(s) · {len(summary.downloaded)} already "
        f"downloaded · {len(summary.unresolved)} could not be downloaded."
    ]
    if summary.retried:
        lines.append(retry_line(summary))
    # Run end: flush the collected convertable text through the segmentor
    # into a committed Parquet batch, then release the staged PDFs. A
    # failed flush keeps everything journaled/staged for the next run.
    try:
        stats = text_flush(OUTPUT_DIR)
    except Exception as error:  # noqa: BLE001 - reporting must still go out
        lines.append(f"🧾 Convert: run-end flush crashed: {error}")
        return lines
    print(f"[convert] {stats.line()}", flush=True)
    lines.append(stats.line())
    return lines


RUN_LOG_DIR = Path(__file__).resolve().parent / "scan"


class _Tee:
    """Duplicate a stream into the run log; a dead terminal keeps the run."""

    def __init__(self, *streams: object) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            try:
                stream.write(text)  # type: ignore[attr-defined]
            except OSError:
                pass
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            try:
                stream.flush()  # type: ignore[attr-defined]
            except OSError:
                pass


def start_run_log(job: str) -> None:
    """Append every console line of this run to scan/run.log (best-effort).

    The mail timeline answers "did the notice send"; this answers "what did
    the run print" — the two questions the owner asked when a night run
    produced neither an end mail nor a log."""
    try:
        RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        handle = (RUN_LOG_DIR / "run.log").open("a", encoding="utf-8")
    except OSError:
        return
    handle.write(f"\n=== {time.strftime('%m-%d %H:%M:%S')} {job}\n")
    handle.flush()
    sys.stdout = _Tee(sys.stdout, handle)  # type: ignore[assignment]
    sys.stderr = _Tee(sys.stderr, handle)  # type: ignore[assignment]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and not (
        args.main or args.download is not None or args.process is not None
    ):
        parser.error("-l works with --download, --main or --process")
    if args.interval is not None and args.process is None:
        parser.error("-i works with --process")
    if (
        args.process is not None
        and args.process is not USE_ENTRY_FILE
        and not _is_url(args.process)
    ):
        parser.error(
            "--process takes one site URL (http/https); leave it out to use scan/entry.txt"
        )
    if (
        args.findentry is None
        and not args.main
        and args.download is None
        and not args.upload
        and args.process is None
        and args.scribd is None
        and args.phatmal is None
    ):
        parser.print_help()
        return 0
    limit = args.limit or 0.0
    job = "main.py " + shlex.join(sys.argv[1:] if argv is None else argv)
    start_run_log(job)
    mail = None if args.findentry is not None else RunMail(job)
    part = "download"
    details: list[str] = []
    report = None
    try:
        if mail is not None:
            mail.send_started(
                [
                    f"🕕 Night run: downloads + uploads until the {NIGHT_HOURS:g}-hour "
                    "mark, then one report (no mail in between unless it fails)."
                ]
                if args.process is not None
                else []
            )
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
            details = upload_all(OUTPUT_DIR)
        elif args.process is not None:
            source = ENTRY_FILE if args.process is USE_ENTRY_FILE else args.process
            if args.process is USE_ENTRY_FILE and not ENTRY_FILE.is_file():
                raise FileNotFoundError(
                    f"No entry file at {ENTRY_FILE}; run main.py --findentry first."
                )
            part = "upload"
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            config = start_remote(OUTPUT_DIR)  # checked now, while you are awake
            part = "download"
            report = run_process(
                config,
                args.interval or DEFAULT_INTERVAL,
                limit,
                hours=NIGHT_HOURS,
                entry_file=source,
            )
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
    except NightFailed as error:
        print(f"error: {error}", file=sys.stderr)
        if mail is not None:
            mail.send_failed(
                error.part,
                error,
                details=error.report.details(),
                problems=error.report.problem_rows(),
            )
        return 1
    except DiskTight as error:
        print(f"error: {error}", file=sys.stderr)
        if mail is not None:
            mail.send_failed(part, error)
        return 1
    except (SerpApiError, OSError, ValueError, RunFailed) as error:
        print(f"error: {error}", file=sys.stderr)
        if mail is not None:
            mail.send_failed(part, error)
        return 1
    except KeyboardInterrupt:
        if args.upload:
            print(
                "\n[paused] upload stopped; unarchived files stay local. "
                f"@ {time.strftime('%H:%M:%S')}"
            )
        elif args.process is not None:
            print(
                "\n[paused] interrupted; downloads are journaled and unsent files "
                f"stay in pdfs - rerun to resume. @ {note_pause()}"
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
    if mail is not None and report is not None:
        mail.send_report(
            reached_mark=report.reached_mark,
            mark=f"the {NIGHT_HOURS:g}-hour mark",
            details=report.details(),
            problems=report.problem_rows(),
            source=report.source,
        )
    elif mail is not None:
        mail.send_finished(details)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

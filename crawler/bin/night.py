"""main.py --process: the night run, download + upload in one terminal.

The crawler (Dcrawler) downloads the sites in scan/entry.txt while a
background thread uploads every saved file to the SSH host (save_remote:
push, verify sha256, then free the local copy), checking python/pdfs every
-i seconds.

Night rules (the owner is asleep, nobody can press a key):
- Downloading follows the crawler's own rules, the same as --download: a
  page or site that can't be accessed is skipped (not journaled, so the next
  run tries it again), a page without a download button goes to
  scan/blocked.txt, a download that didn't work goes to scan/failed.txt.
  Nothing is waited for or retried during the run.
- 5 GB of disk is kept free for the system (packages). "Tight" means
  exactly that: under 5 GB. Like every download command, the crawler checks
  it before each file and quits hard when tight: downloading and uploading
  stop and the run exits with an error (NightFailed, part download); main.py
  mails the failure right away with every problem so far.
- Upload: a file that still fails after the upload waits (save_remote's
  UPLOAD_WAITS: 15 s, 30 s, 45 s, 1 min, 1 min 30 s, 2 min, 2 min 30 s, 3 min;
  9 tries) ->
  - downloading goes on and the disk has room: quit uploading (no retrying
    all night); the files stay in python/pdfs for `main.py --upload`.
  - downloading goes on and the disk is tight: quit entirely - stop
    downloading and uploading and exit with an error (NightFailed, part
    upload), mailed right away like above.
  - downloading is finished or stopping: quit uploading.
- At the wake-up mark (`hours`: NIGHT_HOURS at the top of main.py, 6)
  downloading stops at a safe point (journals keep the place, a rerun
  resumes), the uploader gets up to 2 minutes to send what is already
  downloaded, then one report is mailed with every problem, its part
  (download/upload) and time. No other mail in the night, except the failure
  above.
"""

from __future__ import annotations

import _thread
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from disk_floor import DISK_FLOOR, DiskTight, disk_tight, size_text
from Dcrawler import ENTRY_FILE, OUTPUT_DIR, DownloadSummary, StopRun, download_entries, retry_line
from mailer import clock, now
from save_remote import (
    UPLOAD_TRIES,
    RemoteConfig,
    RemoteError,
    pending_files,
    push_file,
    timeline_row,
)

HARD_STOP_GRACE = 300.0  # after a stop request (mark or failure), force it if no safe point came
UPLOAD_STOP_GRACE = 120.0  # after the mark, time to send what is already downloaded


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


@dataclass
class Problem:
    when: datetime
    part: str  # "download" or "upload"
    text: str


class NightFailed(Exception):
    """The night run had to quit entirely (exit with an error); carries the
    report so the failure mail can list what happened before."""

    def __init__(self, part: str, text: str, report: "NightReport") -> None:
        super().__init__(text)
        self.part = part
        self.report = report


@dataclass
class NightReport:
    started: datetime
    ended: datetime
    stop_at: datetime
    reached_mark: bool
    forced: bool
    summary: DownloadSummary | None
    saved_files: int
    saved_bytes: int
    uploaded: int
    uploaded_bytes: int
    left_files: int
    left_bytes: int
    current_site: str
    problems: list[Problem] = field(default_factory=list)
    failure: str = ""  # set when the run quit entirely

    def details(self) -> list[str]:
        if self.summary is not None:
            download = (
                f"📚 Download: saved {len(self.summary.saved)} new file(s) "
                f"({size_text(self.saved_bytes)}) · {len(self.summary.downloaded)} "
                f"already downloaded · {len(self.summary.unresolved)} could not "
                "be downloaded."
            )
        else:
            download = (
                f"📚 Download: saved {self.saved_files} new file(s) "
                f"({size_text(self.saved_bytes)}) before the forced stop."
            )
        lines = [download]
        if self.summary is not None and self.summary.retried:
            lines.append(retry_line(self.summary))
        lines += [
            f"☁️ Upload: {self.uploaded} file(s) ({size_text(self.uploaded_bytes)}) "
            f"sent and verified · {self.left_files} file(s) "
            f"({size_text(self.left_bytes)}) still in python/pdfs.",
        ]
        stopped_early = self.reached_mark or bool(self.failure)
        if stopped_early and self.current_site:
            lines.append(f"📍 Stopped while on {self.current_site}.")
        steps = []
        if self.left_files:
            steps.append("▶ Send the files still on disk: uv run main.py --upload")
        if stopped_early:
            steps.append("▶ Continue: uv run main.py --process (downloads resume where they stopped)")
        if steps:
            lines += ["", *steps]
        return lines

    def problem_rows(self) -> list[tuple[datetime, str, str]]:
        return [(item.when, item.part, item.text) for item in self.problems]


class NightGuard:
    """Night policy shared by the crawler (main thread, asyncio) and the
    upload thread. Dcrawler calls checkpoint/site_started/note_saved (the
    5 GB check before each file is Dcrawler's own, disk_floor.check_disk)."""

    def __init__(
        self,
        output_dir: Path,
        config: RemoteConfig,
        hours: float,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.config = config
        self.hours = hours
        self.started = now()
        self.started_at = time.monotonic()
        self.deadline = self.started_at + hours * 3600
        self.stop_at = self.started + timedelta(hours=hours)
        self.stop_event = threading.Event()  # downloading stops (the mark or Ctrl+C)
        self.upload_stop = threading.Event()  # uploading stops (after the grace, or Ctrl+C)
        self.stop_reason = ""
        self.mark_reached = False
        self.failure = ""  # why the run quit entirely (exit with an error)
        self.failure_part = "upload"  # the part named in the failure email
        self.hard_timer: threading.Timer | None = None
        self.download_done = threading.Event()
        self.download_running = False
        self.hard_stopped = False
        self.lock = threading.Lock()
        self.problems: list[Problem] = []
        self.saved_files = 0
        self.saved_bytes = 0
        self.uploaded = 0
        self.uploaded_bytes = 0
        self.current_site = ""

    # ---------------------------------------------------------------- shared
    def seconds_left(self) -> float:
        return max(self.deadline - time.monotonic(), 0.0)

    def problem(self, part: str, text: str, when: datetime | None = None) -> Problem:
        item = Problem(when or now(), part, text)
        with self.lock:
            self.problems.append(item)
        print(f"[night-problem] {part.upper()}: {text} @ {_stamp()}", flush=True)
        return item

    def request_stop(self, reason: str) -> None:
        """Downloading stops at the next safe point; if none comes within
        HARD_STOP_GRACE (a file trickling in), it is forced."""
        if self.stop_event.is_set():
            return
        self.stop_reason = reason
        self.stop_event.set()
        print(f"[night-stop] {reason}: downloading stops at the next safe point @ {_stamp()}", flush=True)
        self.hard_timer = threading.Timer(HARD_STOP_GRACE, self.hard_stop)
        self.hard_timer.daemon = True
        self.hard_timer.start()

    def reach_mark(self) -> None:
        self.mark_reached = True
        self.request_stop(f"{self.hours:g}-hour mark")

    def fail(self, text: str, part: str = "upload") -> None:
        """Quit entirely: stop uploading and downloading; run_process then
        raises NightFailed and main.py mails the failure right away."""
        if self.failure:
            return
        self.failure = text
        self.failure_part = part
        print(f"[night-fail] {text} Exiting with an error @ {_stamp()}", flush=True)
        self.upload_stop.set()
        self.request_stop("quitting with an error")

    def hard_stop(self) -> None:
        """Safety net: the crawl reached no safe point long after the stop."""
        if self.download_running:
            self.hard_stopped = True
            print(f"[night-stop] no safe point {HARD_STOP_GRACE:.0f}s after the stop; forcing it @ {_stamp()}", flush=True)
            _thread.interrupt_main()

    # ------------------------------------------------- download side (async)
    def checkpoint(self) -> None:
        if self.stop_event.is_set():
            raise StopRun(self.stop_reason)

    def site_started(self, seed_url: str) -> None:
        self.current_site = seed_url

    def note_saved(self, size: int) -> None:
        with self.lock:
            self.saved_files += 1
            self.saved_bytes += max(int(size), 0)


    # ------------------------------------------------------ upload side (thread)
    def _wait(self, seconds: float, *, wake_when_done: bool = False) -> None:
        end = time.monotonic() + seconds
        done_before = self.download_done.is_set()
        while not self.upload_stop.is_set():
            left = end - time.monotonic()
            if left <= 0:
                return
            if wake_when_done and not done_before and self.download_done.is_set():
                return
            self.upload_stop.wait(min(left, 1.0))

    def upload_loop(self, interval: float) -> None:
        try:
            self._upload_loop(interval)
        except Exception as error:  # noqa: BLE001 - a crash is a failure, mailed right away
            self.fail(f"the uploader crashed ({type(error).__name__}: {error}); downloading and uploading stopped, files stay in python/pdfs.")

    def _upload_loop(self, interval: float) -> None:
        while not self.upload_stop.is_set():
            for file in pending_files(self.output_dir):
                if self.upload_stop.is_set() or not self._push(file, interval):
                    return  # stopped, quit uploading, or quit entirely
            if self.download_done.is_set() and not pending_files(self.output_dir):
                return
            self._wait(interval, wake_when_done=True)

    def _retry_wait(self, seconds: float) -> bool:
        """push_file's gap between tries; False once uploading must stop."""
        self._wait(seconds)
        return not self.upload_stop.is_set()

    def _push(self, file: Path, interval: float) -> bool:
        """Push one file (push_file makes all the UPLOAD_WAITS tries), then
        the owner's rule; False = stop uploading."""
        if self.upload_stop.is_set():
            return False
        try:
            size = file.stat().st_size
        except OSError:
            return True  # gone already: nothing to send
        try:
            outcome = push_file(
                file,
                self.config,
                timeline=timeline_row,
                remote_name=str(file.relative_to(self.output_dir)),
                wait=self._retry_wait,
            )
        except RemoteError as error:  # a local problem before any try (e.g. the ledger)
            detail = str(error)
        else:
            if outcome.ok:
                with self.lock:
                    self.uploaded += 1
                    self.uploaded_bytes += size
                return True
            if not file.exists():
                return True
            detail = outcome.detail
        if self.upload_stop.is_set():
            return False
        downloading = not (self.download_done.is_set() or self.stop_event.is_set())
        tight, free = disk_tight(self.output_dir)
        waiting = pending_files(self.output_dir)
        if downloading and tight:
            self.fail(
                f"Uploads failed {UPLOAD_TRIES} tries ({detail}) while the disk was tight ({size_text(free)} free, "
                f"under the {size_text(DISK_FLOOR)} kept for the system): downloading and uploading stopped, "
                f"{len(waiting)} file(s) stay in python/pdfs."
            )
            return False
        if downloading:
            why = f"the disk had room ({size_text(free)} free, more than the {size_text(DISK_FLOOR)} kept for the system)"
        elif self.download_done.is_set():
            why = "downloading had finished"
        else:
            why = "downloading was stopping"
        print(
            f"[upload-stop] {file.name}: {UPLOAD_TRIES} tries failed ({detail}); {why} - uploading "
            f"stops, {len(waiting)} file(s) stay in python/pdfs for `uv run main.py --upload` @ {_stamp()}",
            flush=True,
        )
        self.problem("upload", f"{UPLOAD_TRIES} tries failed ({detail}). {why[0].upper()}{why[1:]}, so uploading stopped.")
        return False


def run_process(
    config: RemoteConfig,
    interval: float,
    download_limit: float = 0.0,
    *,
    hours: float,
    entry_file: str | Path = ENTRY_FILE,
    output_dir: str | Path = OUTPUT_DIR,
) -> NightReport:
    """Download entry_file's sites with uploads running alongside; stop at
    `hours` (main.py's NIGHT_HOURS). config comes from save_remote.start_remote (the SSH
    check is done by the caller while the owner is still awake)."""
    output_dir = Path(output_dir)
    guard = NightGuard(output_dir, config, hours)
    print(
        f"[night] started {clock(guard.started)} (Myanmar time); stops at the "
        f"{hours:g}-hour mark, {clock(guard.stop_at)}; uploads checked every "
        f"{interval:g}s @ {_stamp()}",
        flush=True,
    )
    stop_timer = threading.Timer(guard.seconds_left(), guard.reach_mark)
    stop_timer.daemon = True
    stop_timer.start()
    uploader = threading.Thread(target=guard.upload_loop, args=(interval,), name="uploader", daemon=True)
    uploader.start()
    summary: DownloadSummary | None = None
    try:
        guard.download_running = True
        try:
            summary = download_entries(entry_file, output_dir, download_limit=download_limit, guard=guard)
        except DiskTight as tight:
            summary = tight.summary
            guard.fail(
                f"The disk went under the {size_text(DISK_FLOOR)} kept for the system "
                f"({size_text(tight.free)} free): downloading and uploading stopped, "
                f"{len(pending_files(output_dir))} file(s) stay in python/pdfs.",
                part="download",
            )
        finally:
            guard.download_running = False
            if guard.hard_timer is not None:
                guard.hard_timer.cancel()
        guard.download_done.set()
        if not guard.stop_event.is_set():
            print(f"[night] downloading finished; the uploader sends what is left @ {_stamp()}", flush=True)
        while uploader.is_alive() and not guard.stop_event.is_set():
            uploader.join(1.0)
        if uploader.is_alive() and not guard.failure:
            print(f"[night-stop] sending what is already downloaded (up to {UPLOAD_STOP_GRACE:.0f}s) @ {_stamp()}", flush=True)
            uploader.join(UPLOAD_STOP_GRACE)
        guard.upload_stop.set()
        uploader.join(5.0)
        if uploader.is_alive():
            print(f"[night-stop] an upload is still running; leaving it (the next upload resumes it) @ {_stamp()}", flush=True)
    except KeyboardInterrupt:
        guard.stop_event.set()
        guard.upload_stop.set()
        if not guard.hard_stopped:
            raise  # a real Ctrl+C: main.py prints [paused]
        guard.download_done.set()
    finally:
        stop_timer.cancel()
        if guard.hard_timer is not None:
            guard.hard_timer.cancel()
    left = pending_files(output_dir)
    left_bytes = 0
    for item in left:
        try:
            left_bytes += item.stat().st_size
        except OSError:
            pass
    report = NightReport(
        started=guard.started,
        ended=now(),
        stop_at=guard.stop_at,
        reached_mark=guard.mark_reached and not guard.failure,
        forced=guard.hard_stopped,
        summary=summary,
        saved_files=guard.saved_files,
        saved_bytes=guard.saved_bytes,
        uploaded=guard.uploaded,
        uploaded_bytes=guard.uploaded_bytes,
        left_files=len(left),
        left_bytes=left_bytes,
        current_site=guard.current_site,
        problems=sorted(guard.problems, key=lambda item: item.when),
        failure=guard.failure,
    )
    if report.failure:
        ending = "quit with an error"
    elif report.reached_mark:
        ending = f"stopped at the {hours:g}-hour mark"
    else:
        ending = "finished"
    print(f"[night] {ending}: " + " | ".join(line for line in report.details() if line and not line.startswith("▶")), flush=True)
    for item in report.problems:
        print(f"[night] problem {clock(item.when)} {item.part.upper()}: {item.text}", flush=True)
    if report.failure:
        raise NightFailed(guard.failure_part, report.failure, report)
    return report

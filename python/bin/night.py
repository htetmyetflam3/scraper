"""main.py --process: the night run, download + upload in one terminal.

The crawler (Dcrawler) downloads the sites in scan/entry.txt while a
background thread uploads every saved file to the SSH host (save_remote:
push, verify sha256, then free the local copy), checking python/pdfs every
-i seconds.

Night rules (the owner is asleep, nobody can press a key):
- Nothing ends the run early except finishing everything or an unexpected
  crash. Files that fail are logged and skipped, however many.
- A site that stops answering is waited for (re-checked after 30 s, 1 m,
  2 m, then every 5 m) and the same page is retried once it answers again,
  instead of racing through the queue and "finishing" in the middle of the
  night. A file that fails while the internet is down is retried once it is
  back. Only when other sites are queued and the internet works (the SSH
  host answers) is a site that stays down 20 minutes left for the next run.
- 5 GB of disk is kept free for the system (packages): below it downloading
  pauses until uploads free space. "Tight" means exactly that: under 5 GB.
- Upload: a file that fails 5 tries ->
  - the disk has room: quit uploading (no retrying all night); downloading
    goes on and the files stay in python/pdfs for `main.py --upload`. When
    the disk later gets tight, the uploader tries again (5 tries).
  - the disk is tight while downloading goes on: quit entirely - stop
    downloading and uploading and exit with an error (NightFailed); main.py
    mails the failure right away with every problem so far.
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
import asyncio
import shutil
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from Dcrawler import ENTRY_FILE, OUTPUT_DIR, DownloadSummary, SkipSite, StopRun, download_entries
from mailer import clock, duration, now
from save_remote import (
    RETRY_ATTEMPTS,
    RETRY_DELAY,
    RemoteConfig,
    RemoteError,
    pending_files,
    push_file,
    timeline_row,
)

HARD_STOP_GRACE = 300.0  # after a stop request (mark or failure), force it if no safe point came
UPLOAD_STOP_GRACE = 120.0  # after the mark, time to send what is already downloaded
DISK_FLOOR = 5 * 1024**3  # kept free for the system; under it downloading pauses = "tight"
DISK_CHECK_SECONDS = 30.0
PROBE_TIMEOUT = 15.0
WAIT_STEPS = (30.0, 60.0, 120.0, 300.0)  # gaps between "is it back?" checks
SITE_DOWN_LIMIT = 20 * 60.0
FILE_WAIT_LIMIT = 30 * 60.0
UPLOAD_TRIES = RETRY_ATTEMPTS  # "about 5 failed retries" before the rule decides


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _tcp_answers(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=10):
            return True
    except OSError:
        return False


def _host(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return parts.netloc or url


def _root(url: str) -> str | None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}/"


def size_text(count: float) -> str:
    if count >= 1024**3:
        return f"{count / 1024**3:.1f} GB"
    return f"{count / 1048576:.1f} MB"


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
        lines = [
            download,
            f"☁️ Upload: {self.uploaded} file(s) ({size_text(self.uploaded_bytes)}) "
            f"sent and verified · {self.left_files} file(s) "
            f"({size_text(self.left_bytes)}) still in python/pdfs.",
        ]
        stopped_early = self.reached_mark or bool(self.failure)
        if stopped_early and self.current_site:
            lines.append(f"📍 Stopped while on {self.current_site}.")
        steps = []
        if stopped_early:
            steps.append("▶ Continue: uv run main.py --process (downloads resume where they stopped)")
        if self.left_files:
            steps.append("▶ Send the files still on disk: uv run main.py --upload")
        if steps:
            lines += ["", *steps]
        return lines

    def problem_rows(self) -> list[tuple[datetime, str, str]]:
        return [(item.when, item.part, item.text) for item in self.problems]


class NightGuard:
    """Night policy shared by the crawler (main thread, asyncio) and the
    upload thread. Dcrawler calls checkpoint/site_started/site_answers/
    wait_until_back/pause/file_trouble/wait_for_disk/note_saved."""

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
        self.hard_timer: threading.Timer | None = None
        self.download_done = threading.Event()
        self.download_running = False
        self.hard_stopped = False
        self.lock = threading.Lock()
        self.problems: list[Problem] = []
        self.saved_files = 0
        self.saved_bytes = 0
        self.disk_pauses = 0
        self.disk_paused_for = 0.0
        self.disk_problem: Problem | None = None
        self.uploaded = 0
        self.uploaded_bytes = 0
        self.upload_paused = False
        self.current_site = ""
        self.sites_after = 0

    # ---------------------------------------------------------------- shared
    def seconds_left(self) -> float:
        return max(self.deadline - time.monotonic(), 0.0)

    def disk_check(self) -> tuple[bool, int]:
        """(tight, free bytes): tight = under the DISK_FLOOR kept for the system."""
        free = free_bytes(self.output_dir)
        return free < DISK_FLOOR, free

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

    def fail(self, text: str) -> None:
        """Quit entirely: stop uploading and downloading; run_process then
        raises NightFailed and main.py mails the failure right away."""
        if self.failure:
            return
        self.failure = text
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

    def site_started(self, seed_url: str, sites_after: int) -> None:
        self.current_site = seed_url
        self.sites_after = sites_after

    def note_saved(self, size: int) -> None:
        with self.lock:
            self.saved_files += 1
            self.saved_bytes += max(int(size), 0)

    async def pause(self, seconds: float) -> None:
        """asyncio sleep that ends at the stop (raises StopRun)."""
        end = time.monotonic() + seconds
        while True:
            self.checkpoint()
            left = end - time.monotonic()
            if left <= 0:
                return
            await asyncio.sleep(min(left, 1.0))

    async def site_answers(self, url: str) -> bool:
        """True when the host of url answers its root with any status < 500."""
        root = _root(url)
        if root is None:
            return True
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
                async with client.stream("GET", root) as response:
                    return response.status_code < 500
        except httpx.HTTPError:
            return False

    async def internet_works(self) -> bool:
        """The current site or the SSH host answers -> the internet is up."""
        if self.current_site and await self.site_answers(self.current_site):
            return True
        return await asyncio.to_thread(_tcp_answers, self.config.host, self.config.port)

    async def wait_until_back(self, url: str, error: str) -> None:
        """A page failed and its site does not answer: wait until it does.

        Returns when the site answers (the caller retries the same page).
        Raises StopRun at the stop, SkipSite when other sites are queued, the
        internet works and this site stayed down SITE_DOWN_LIMIT.
        """
        host = _host(url)
        began_clock = now()
        began = time.monotonic()
        print(f"[net-wait] {host} does not answer ({error}); waiting for it, then the same page is retried @ {_stamp()}", flush=True)
        step = 0
        try:
            while True:
                gap = WAIT_STEPS[min(step, len(WAIT_STEPS) - 1)]
                step += 1
                await self.pause(gap)
                down_for = time.monotonic() - began
                if await self.site_answers(url):
                    print(f"[net-back] {host} answers again after {duration(timedelta(seconds=down_for))}; retrying @ {_stamp()}", flush=True)
                    self.problem("download", f"{host} did not answer; waited {duration(timedelta(seconds=down_for))} until it was back.", began_clock)
                    return
                if self.sites_after and down_for >= SITE_DOWN_LIMIT and await self.internet_works():
                    self.problem(
                        "download",
                        f"{host} did not answer for {duration(timedelta(seconds=down_for))} while the "
                        "internet worked; moved on to the next site (a rerun resumes it).",
                        began_clock,
                    )
                    raise SkipSite(f"down for {duration(timedelta(seconds=down_for))}")
                upcoming = WAIT_STEPS[min(step, len(WAIT_STEPS) - 1)]
                print(f"[net-wait] {host} still down after {duration(timedelta(seconds=down_for))}; next check in {upcoming:.0f}s @ {_stamp()}", flush=True)
        except StopRun:
            self.problem("download", f"{host} did not answer; still down at the stop.", began_clock)
            raise

    async def file_trouble(self, url: str, reason: str) -> bool:
        """A file could not be fetched. True = the internet was down and is
        back, retry the file; False = a real failure of that file/host."""
        if await self.site_answers(url) or await self.internet_works():
            return False
        host = _host(url)
        began_clock = now()
        began = time.monotonic()
        print(f"[net-wait] the internet seems down ({host}, the site and the SSH host do not answer); waiting, then retrying {url} @ {_stamp()}", flush=True)
        step = 0
        try:
            while time.monotonic() - began < FILE_WAIT_LIMIT:
                await self.pause(WAIT_STEPS[min(step, len(WAIT_STEPS) - 1)])
                step += 1
                if await self.site_answers(url) or await self.internet_works():
                    waited = duration(timedelta(seconds=time.monotonic() - began))
                    print(f"[net-back] the internet is back after {waited}; retrying {url} @ {_stamp()}", flush=True)
                    self.problem("download", f"the internet was down; waited {waited}, then retried the file.", began_clock)
                    return True
        except StopRun:
            self.problem("download", "the internet was down; still down at the stop.", began_clock)
            raise
        self.problem("download", f"the internet stayed down {duration(timedelta(seconds=FILE_WAIT_LIMIT))}; gave up on {url} ({reason}).", began_clock)
        return False

    async def wait_for_disk(self) -> None:
        """Before each download: pause while the disk is tight (under DISK_FLOOR)."""
        tight, free = self.disk_check()
        if not tight:
            return
        began_clock = now()
        began = time.monotonic()
        print(f"[disk-wait] only {size_text(free)} free ({size_text(DISK_FLOOR)} is kept for the system): downloading paused until uploads free space @ {_stamp()}", flush=True)
        checks = 0
        try:
            while tight:
                await self.pause(DISK_CHECK_SECONDS)
                tight, free = self.disk_check()
                checks += 1
                if checks % 10 == 0 and tight:
                    print(f"[disk-wait] still {size_text(free)} free @ {_stamp()}", flush=True)
        except StopRun:
            self._disk_paused(began_clock, time.monotonic() - began, at_stop=True)
            raise
        waited = time.monotonic() - began
        print(f"[disk-ok] {size_text(free)} free again after {duration(timedelta(seconds=waited))}; downloading continues @ {_stamp()}", flush=True)
        self._disk_paused(began_clock, waited)

    def _disk_paused(self, began_clock: datetime, seconds: float, *, at_stop: bool = False) -> None:
        """One report row for every disk pause: they repeat whenever uploads
        are slower than downloads."""
        self.disk_pauses += 1
        self.disk_paused_for += seconds
        paused = duration(timedelta(seconds=self.disk_paused_for))
        if self.disk_pauses == 1:
            text = f"the disk went under {size_text(DISK_FLOOR)} free (kept for the system); downloading paused {paused} until uploads freed space."
        else:
            text = (
                f"the disk went under {size_text(DISK_FLOOR)} free (kept for the system) {self.disk_pauses} times; "
                f"downloading paused {paused} in total until uploads freed space."
            )
        if at_stop:
            text += " It was still paused at the stop."
        if self.disk_problem is None:
            self.disk_problem = self.problem("download", text, began_clock)
        else:
            self.disk_problem.text = text

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
            if self.upload_paused:
                if self.download_done.is_set():
                    return
                tight, free = self.disk_check()
                if not tight:
                    self._wait(interval, wake_when_done=True)
                    continue
                self.upload_paused = False
                print(f"[upload-resume] the disk got tight ({size_text(free)} free, under {size_text(DISK_FLOOR)}) and downloading is not finished; uploading again @ {_stamp()}", flush=True)
                self.problem("upload", f"uploading started again: the disk got tight ({size_text(free)} free, under {size_text(DISK_FLOOR)}) before downloading finished.")
            for file in pending_files(self.output_dir):
                if self.upload_stop.is_set() or not self._push(file, interval):
                    break
            if self.upload_stop.is_set():
                return
            if self.upload_paused:
                continue
            if self.download_done.is_set() and not pending_files(self.output_dir):
                return
            self._wait(interval, wake_when_done=True)

    def _push(self, file: Path, interval: float) -> bool:
        """Push one file under the owner's rule; False = stop this pass."""
        failures = 0
        while not self.upload_stop.is_set():
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
                )
            except RemoteError as error:
                failures += 1
                detail = str(error)
            else:
                if outcome.ok:
                    with self.lock:
                        self.uploaded += 1
                        self.uploaded_bytes += size
                    return True
                if not file.exists():
                    return True
                failures += RETRY_ATTEMPTS  # push_file already made its tries
                detail = outcome.detail
            if failures < UPLOAD_TRIES:
                print(f"[upload-retry] {file.name}: {detail}; try {failures + 1}/{UPLOAD_TRIES} in {RETRY_DELAY:.0f}s @ {_stamp()}", flush=True)
                self._wait(RETRY_DELAY)
                continue
            downloading = not (self.download_done.is_set() or self.stop_event.is_set())
            tight, free = self.disk_check()
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
            self.upload_paused = True
            return False
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
        raise NightFailed("upload", report.failure, report)
    return report

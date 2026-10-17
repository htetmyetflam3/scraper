"""Email notices for scraper runs: started, finished, failed, night report.

Sent from the Hostinger mailbox kaungmyatthu@devnovex.shop through
smtp.hostinger.com:465 (SSL, certificate verified) to htetmyet986@gmail.com;
then a copy of each sent notice is put in that mailbox's Sent folder through
imap.hostinger.com:993 (SSL, certificate verified), like a mail app does.
The only setting is the mailbox password (same for SMTP and IMAP):
MAIL_PASSWORD in the repo-root .env (the environment wins, same parser as
the other modules).

Mail is best-effort: no password switches it off with one notice and a
failed send or Sent copy only prints a warning, so a night run never stops
because of mail. Times are Myanmar time (UTC+06:30, no daylight saving); 06:00-17:59
counts as day, the rest as night.
"""

from __future__ import annotations

import imaplib
import os
import random
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from site_login import load_dotenv_values

MYANMAR_TIME = timezone(timedelta(hours=6, minutes=30), "MMT")
DAY_START_HOUR = 6
NIGHT_START_HOUR = 18
SEND_TIMEOUT = 20.0
MAX_PROBLEM_ROWS = 15
SMTP_HOST = "smtp.hostinger.com"
SMTP_PORT = 465  # implicit TLS (SSL)
SMTP_USER = "kaungmyatthu@devnovex.shop"  # Hostinger login (SMTP and IMAP), also the From address
MAIL_TO = "htetmyet986@gmail.com"
IMAP_HOST = "imap.hostinger.com"
IMAP_PORT = 993  # implicit TLS (SSL); copies of the sent notices go to the Sent folder
SENT_NAMES = ("sent", "sent items", "sent messages", "sent mail")  # if no folder has the \Sent flag
PASSWORD_SETTING = "MAIL_PASSWORD"

# (frame line, message line, message line); the frame goes above and below.
NIGHT_STARTED = (
    ("˚ ✦  ⋆  🌙  ⋆  ✦ ˚", "The moon is up and the shelves are open.", "Sleep tight — I'll keep fetching till dawn."),
    ("✧･ﾟ: ✧･ﾟ: 🌌 :･ﾟ✧ :･ﾟ✧", "Night shift has begun.", "Books are tiptoeing in while you dream."),
    ("☾  ⋆ ｡ ﾟ ☁︎ ｡ ⋆ ｡ ﾟ ★", "Lights off, downloads on.", "See you in the morning with a fuller shelf."),
)
DAY_STARTED = (
    ("☀️ ～～～～～～～～～～ ☀️", "Bright skies, busy shelves.", "Go enjoy your day — I've got the books."),
    ("🌤  ·  ·  ·  📚  ·  ·  ·  🌤", "Rise and shine, the crawl is on.", "Pages are flowing in one by one."),
    ("✿ ❀ ✿ ❀ ✿ ❀ ✿ ❀ ✿", "A fine day for collecting stories.", "I'll ping you when it's done."),
)
NIGHT_FINISHED = (
    ("⋆ ˚｡⋆ 🌙 ⋆｡˚ ⋆", "All quiet now — the last page is turned.", "The shelf is tucked in for the night."),
    ("✦ ･ﾟ ✧ 🌠 ✧ ﾟ･ ✦", "Mission complete under the stars.", "Sweet dreams, librarian."),
)
MORNING_REPORT = (
    ("☀️ ･ﾟ✧ ～～～～～～ ✧ﾟ･ ☀️", "Good morning, librarian!", "Here is what happened while you slept."),
    ("🌅 · · · 📚 · · · 🌅", "The night shift is over.", "Your shelf grew while you were dreaming."),
)
DAY_FINISHED = (
    ("☀️ ✓ ～～～～～～ ✓ ☀️", "Done and dusted!", "Fresh books are waiting for you."),
    ("🌻 · · · 📚 · · · 🌻", "The crawl is complete.", "Grab a tea and enjoy the haul."),
)


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    user: str
    password: str
    recipient: str
    imap_host: str
    imap_port: int


def load_mail_config() -> tuple[MailConfig | None, str]:
    """(config, "") when MAIL_PASSWORD is set, else (None, reason)."""
    password = os.environ.get(PASSWORD_SETTING)
    if password is None:
        password = load_dotenv_values().get(PASSWORD_SETTING, "")
    if not password.strip():
        return None, f"missing {PASSWORD_SETTING}"
    return MailConfig(SMTP_HOST, SMTP_PORT, SMTP_USER, password, MAIL_TO, IMAP_HOST, IMAP_PORT), ""


def build_message(config: MailConfig, subject: str, body: str) -> EmailMessage:
    """One plain-text UTF-8 message from the mailbox to the recipient."""
    message = EmailMessage()
    message["From"] = config.user
    message["To"] = config.recipient
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    return message


def send(config: MailConfig, message: EmailMessage) -> None:
    """Deliver the message over SMTP SSL; raises on any failure."""
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(config.host, config.port, timeout=SEND_TIMEOUT, context=context) as server:
        server.login(config.user, config.password)
        server.send_message(message)


def save_to_sent(config: MailConfig, message: EmailMessage) -> str:
    """Put a copy of a sent message, marked as read, in the mailbox's Sent
    folder over IMAP SSL; returns the folder name, raises on any failure."""
    context = ssl.create_default_context()
    with imaplib.IMAP4_SSL(config.imap_host, config.imap_port, ssl_context=context, timeout=SEND_TIMEOUT) as imap:
        imap.login(config.user, config.password)
        folder = sent_folder(imap)
        status, data = imap.append(
            _quoted(folder), r"(\Seen)", time.time(), message.as_bytes()
        )
        if status != "OK":
            raise imaplib.IMAP4.error(f"saving to {folder} was refused: {_said(data)}")
    return folder


_LIST_ROW = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?P<delimiter>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)', re.IGNORECASE)


def sent_folder(imap: imaplib.IMAP4) -> str:
    """The folder flagged \\Sent (RFC 6154), else the one named Sent / Sent
    Items / Sent Messages / Sent Mail at any level (Hostinger: INBOX.Sent)."""
    status, rows = imap.list()
    if status != "OK":
        raise imaplib.IMAP4.error(f"listing the folders was refused: {_said(rows)}")
    named: list[tuple[int, str]] = []
    for row in rows:
        entry = _list_entry(row)
        if entry is None:
            continue
        flags, delimiter, name = entry
        if "\\sent" in flags:
            return name
        leaf = name.rsplit(delimiter, 1)[-1] if delimiter else name
        if leaf.lower() in SENT_NAMES:
            named.append((SENT_NAMES.index(leaf.lower()), name))
    if named:
        return min(named)[1]
    raise imaplib.IMAP4.error("the mailbox has no Sent folder")


def _list_entry(row: object) -> tuple[set[str], str, str] | None:
    """(lowercase flags, delimiter, folder name) from one LIST row."""
    literal = None
    if isinstance(row, tuple) and len(row) == 2:  # the name came as a literal
        row, literal = row
    if not isinstance(row, bytes):
        return None
    match = _LIST_ROW.match(row)
    if match is None:
        return None
    flags = {flag.lower() for flag in match["flags"].decode("ascii", "replace").split()}
    delimiter = match["delimiter"].decode("ascii", "replace")
    delimiter = "" if delimiter.upper() == "NIL" else _unquoted(delimiter)
    if isinstance(literal, bytes):
        name = literal.decode("utf-8", "replace")
    else:
        name = _unquoted(match["name"].decode("utf-8", "replace").strip())
    return flags, delimiter, name


def _unquoted(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return re.sub(r'\\(.)', r"\1", text[1:-1])
    return text


def _quoted(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _said(data: object) -> str:
    """Readable text of an IMAP reply or error (bytes items decoded)."""
    items = data if isinstance(data, (list, tuple)) else [data]
    return " ".join(
        item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item) for item in items if item is not None
    )


def now() -> datetime:
    return datetime.now(MYANMAR_TIME)


def is_night(when: datetime) -> bool:
    return not DAY_START_HOUR <= when.astimezone(MYANMAR_TIME).hour < NIGHT_START_HOUR


def clock(when: datetime) -> str:
    return when.astimezone(MYANMAR_TIME).strftime("%I:%M %p").lstrip("0")


def _date(when: datetime) -> str:
    local = when.astimezone(MYANMAR_TIME)
    return f"{local.strftime('%A')} {local.day} {local.strftime('%b %Y')}"


def duration(delta: timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} m"
    if minutes:
        return f"{minutes} m {secs} s"
    return f"{secs} s"


def _framed(art: tuple[str, str, str]) -> list[str]:
    frame, first, second = art
    return [f"   {frame}", f"   {first}", f"   {second}", f"   {frame}"]


def started_message(
    job: str, started: datetime, details: list[str] | tuple[str, ...] = ()
) -> tuple[str, str]:
    night = is_night(started)
    subject = f"{'🌙' if night else '☀️'} Your little scraper started · {clock(started)}"
    body = [
        "Hello! 👋",
        "",
        f"Your little scraper started at {clock(started)}, {_date(started)} (Myanmar time).",
        "",
        *_framed(random.choice(NIGHT_STARTED if night else DAY_STARTED)),
        "",
        *details,
        *([""] if details else []),
        f"Job: {job}",
    ]
    return subject, "\n".join(body) + "\n"


def finished_message(
    job: str, started: datetime, finished: datetime, details: list[str] | tuple[str, ...] = ()
) -> tuple[str, str]:
    subject = f"✅ Your little scraper finished · {clock(finished)}"
    body = [
        "Hello again! 👋",
        "",
        f"Your little scraper finished at {clock(finished)}, {_date(finished)} (Myanmar time).",
        f"It ran for {duration(finished - started)}.",
        "",
        *_framed(random.choice(NIGHT_FINISHED if is_night(finished) else DAY_FINISHED)),
        "",
        *details,
        *([""] if details else []),
        f"Job: {job}",
    ]
    return subject, "\n".join(body) + "\n"


def failed_message(
    job: str,
    started: datetime,
    failed: datetime,
    part: str,
    error: object,
    *,
    details: list[str] | tuple[str, ...] = (),
    problems: list[tuple[datetime, str, str]] | tuple = (),
) -> tuple[str, str]:
    """details/problems: what a --process night run did before it quit."""
    subject = f"⚠️ Your little scraper failed: {part} · {clock(failed)}"
    body = [
        "Heads up! ⚠️",
        "",
        f"The {part.upper()} part failed at {clock(failed)}, {_date(failed)} (Myanmar time),",
        f"after {duration(failed - started)}.",
        "",
        f"Error: {error}",
        "",
    ]
    if problems:
        body += [*_problem_lines(problems), ""]
    if details:
        body += [*details, ""]
    else:
        body += ["Progress is saved: run the same command again to resume.", ""]
    body.append(f"Job: {job}")
    return subject, "\n".join(body) + "\n"


def _problem_lines(problems: list[tuple[datetime, str, str]] | tuple) -> list[str]:
    rows = [f"⚠️ {len(problems)} problem(s) (Myanmar time):"]
    for when, part, text in list(problems)[:MAX_PROBLEM_ROWS]:
        rows.append(f"  • {clock(when)} · {part.upper()} · {text}")
    if len(problems) > MAX_PROBLEM_ROWS:
        rows.append(f"  … and {len(problems) - MAX_PROBLEM_ROWS} more in the console log.")
    return rows


def report_message(
    job: str,
    started: datetime,
    ended: datetime,
    *,
    reached_mark: bool,
    mark: str,
    details: list[str] | tuple[str, ...] = (),
    problems: list[tuple[datetime, str, str]] | tuple = (),
) -> tuple[str, str]:
    """The one mail at the end of main.py --process: at the mark (wake-up
    time) or when everything finished earlier. problems = (when, part, text)."""
    parts = list(dict.fromkeys(part for _when, part, _text in problems))
    local_hour = ended.astimezone(MYANMAR_TIME).hour
    if reached_mark:
        greeting = "Good morning! ☀️" if 4 <= local_hour < 12 else "Hello again! 👋"
        if problems:
            subject = f"⚠️ Your little scraper stopped at {mark} and needs a look: {', '.join(parts)} · {clock(ended)}"
        else:
            subject = f"☀️ Your little scraper stopped at {mark} · {clock(ended)}"
        when_line = (
            f"Your little scraper stopped at {clock(ended)}, {_date(ended)} (Myanmar time): "
            f"it reached {mark}."
        )
        art = MORNING_REPORT if not is_night(ended) else NIGHT_FINISHED
    else:
        greeting = "Hello again! 👋"
        if problems:
            subject = f"⚠️ Your little scraper finished with problems: {', '.join(parts)} · {clock(ended)}"
        else:
            subject = f"✅ Your little scraper finished · {clock(ended)}"
        when_line = (
            f"Your little scraper finished at {clock(ended)}, {_date(ended)} (Myanmar time): "
            "everything in scan/entry.txt is done."
        )
        art = NIGHT_FINISHED if is_night(ended) else DAY_FINISHED
    rows = _problem_lines(problems) if problems else ["✔ No problems."]
    body = [
        greeting,
        "",
        when_line,
        f"It started at {clock(started)} and ran for {duration(ended - started)}.",
        "",
        *_framed(random.choice(art)),
        "",
        *rows,
        "",
        *details,
        *([""] if details else []),
        f"Job: {job}",
    ]
    return subject, "\n".join(body) + "\n"


class RunMail:
    """Started/finished/failed notices for one run; every send is best-effort."""

    def __init__(self, job: str) -> None:
        self.job = job
        self.started = now()
        self.config, reason = load_mail_config()
        if self.config is None:
            print(f"[mail] off: {reason} (set it in .env)", flush=True)
        else:
            print(
                f"[mail] on: {self.config.user} -> {self.config.recipient} via "
                f"{self.config.host}:{self.config.port} (ssl), copy to Sent via "
                f"{self.config.imap_host}:{self.config.imap_port} (ssl)",
                flush=True,
            )

    def _deliver(self, kind: str, compose) -> None:
        if self.config is None:
            return
        try:
            subject, body = compose()
            message = build_message(self.config, subject, body)
            send(self.config, message)
        except Exception as error:  # noqa: BLE001 - mail must never stop a run
            print(f"[mail-warn] {kind} email not sent: {type(error).__name__}: {error}", flush=True)
            return
        print(f"[mail] {kind} email sent @ {clock(now())}", flush=True)
        try:
            folder = save_to_sent(self.config, message)
        except Exception as error:  # noqa: BLE001 - the copy is a convenience
            name = "IMAP error" if isinstance(error, imaplib.IMAP4.error) else type(error).__name__
            print(f"[mail-warn] {kind} email sent, but no copy in Sent: {name}: {_said(error.args) or error}", flush=True)
            return
        print(f"[mail] {kind} email copy saved in {folder}", flush=True)

    def send_started(self, details: list[str] | tuple[str, ...] = ()) -> None:
        self._deliver("started", lambda: started_message(self.job, self.started, details))

    def send_finished(self, details: list[str] | tuple[str, ...] = ()) -> None:
        self._deliver("finished", lambda: finished_message(self.job, self.started, now(), details))

    def send_failed(
        self,
        part: str,
        error: object,
        *,
        details: list[str] | tuple[str, ...] = (),
        problems: list[tuple[datetime, str, str]] | tuple = (),
    ) -> None:
        self._deliver(
            "failed",
            lambda: failed_message(self.job, self.started, now(), part, error, details=details, problems=problems),
        )

    def send_report(
        self,
        *,
        reached_mark: bool,
        mark: str,
        details: list[str] | tuple[str, ...] = (),
        problems: list[tuple[datetime, str, str]] | tuple = (),
    ) -> None:
        self._deliver(
            "report",
            lambda: report_message(
                self.job,
                self.started,
                now(),
                reached_mark=reached_mark,
                mark=mark,
                details=details,
                problems=problems,
            ),
        )

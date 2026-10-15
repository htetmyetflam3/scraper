"""Email notices for scraper runs: started, finished, failed.

Settings come from the environment or the repo-root .env (same parser as
the other modules): SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD and
MAIL_TO (comma-separated for several), plus optional MAIL_FROM (default
SMTP_USER) and SMTP_SECURITY (``ssl`` or ``starttls``; default ``ssl`` on
port 465, else ``starttls``). The password never travels unencrypted.

Mail is best-effort: missing settings switch it off with one notice and a
failed send only prints a warning, so a night run never stops because of
mail. Times are Myanmar time (UTC+06:30, no daylight saving); 06:00-17:59
counts as day, the rest as night.
"""

from __future__ import annotations

import os
import random
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from site_login import load_dotenv_values

MYANMAR_TIME = timezone(timedelta(hours=6, minutes=30), "MMT")
DAY_START_HOUR = 6
NIGHT_START_HOUR = 18
SEND_TIMEOUT = 20.0
REQUIRED_SETTINGS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "MAIL_TO")

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
DAY_FINISHED = (
    ("☀️ ✓ ～～～～～～ ✓ ☀️", "Done and dusted!", "Fresh books are waiting for you."),
    ("🌻 · · · 📚 · · · 🌻", "The crawl is complete.", "Grab a tea and enjoy the haul."),
)


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    security: str
    user: str
    password: str
    sender: str
    recipients: tuple[str, ...]


def load_mail_config() -> tuple[MailConfig | None, str]:
    """(config, "") when every setting is usable, else (None, reason)."""
    dotenv = load_dotenv_values()

    def pick(name: str) -> str:
        value = os.environ.get(name)
        if value is None:
            value = dotenv.get(name, "")
        return value.strip()

    values = {name: pick(name) for name in (*REQUIRED_SETTINGS, "MAIL_FROM", "SMTP_SECURITY")}
    missing = [name for name in REQUIRED_SETTINGS if not values[name]]
    if missing:
        return None, "missing " + ", ".join(missing)
    try:
        port = int(values["SMTP_PORT"])
    except ValueError:
        return None, f"SMTP_PORT is not a number: {values['SMTP_PORT']!r}"
    security = values["SMTP_SECURITY"].lower() or ("ssl" if port == 465 else "starttls")
    if security not in {"ssl", "starttls"}:
        return None, f"SMTP_SECURITY must be ssl or starttls, not {values['SMTP_SECURITY']!r}"
    recipients = tuple(address.strip() for address in values["MAIL_TO"].split(",") if address.strip())
    if not recipients:
        return None, "MAIL_TO has no address"
    return (
        MailConfig(
            host=values["SMTP_HOST"],
            port=port,
            security=security,
            user=values["SMTP_USER"],
            password=values["SMTP_PASSWORD"],
            sender=values["MAIL_FROM"] or values["SMTP_USER"],
            recipients=recipients,
        ),
        "",
    )


def send(config: MailConfig, subject: str, body: str) -> None:
    """Deliver one plain-text UTF-8 message; raises on any failure."""
    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    context = ssl.create_default_context()
    if config.security == "ssl":
        with smtplib.SMTP_SSL(config.host, config.port, timeout=SEND_TIMEOUT, context=context) as server:
            server.login(config.user, config.password)
            server.send_message(message)
        return
    with smtplib.SMTP(config.host, config.port, timeout=SEND_TIMEOUT) as server:
        server.starttls(context=context)
        server.login(config.user, config.password)
        server.send_message(message)


def now() -> datetime:
    return datetime.now(MYANMAR_TIME)


def is_night(when: datetime) -> bool:
    return not DAY_START_HOUR <= when.astimezone(MYANMAR_TIME).hour < NIGHT_START_HOUR


def _clock(when: datetime) -> str:
    return when.astimezone(MYANMAR_TIME).strftime("%I:%M %p").lstrip("0")


def _date(when: datetime) -> str:
    local = when.astimezone(MYANMAR_TIME)
    return f"{local.strftime('%A')} {local.day} {local.strftime('%b %Y')}"


def _duration(delta: timedelta) -> str:
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


def started_message(job: str, started: datetime) -> tuple[str, str]:
    night = is_night(started)
    subject = f"{'🌙' if night else '☀️'} Your little scraper started · {_clock(started)}"
    body = [
        "Hello! 👋",
        "",
        f"Your little scraper started at {_clock(started)}, {_date(started)} (Myanmar time).",
        "",
        *_framed(random.choice(NIGHT_STARTED if night else DAY_STARTED)),
        "",
        f"Job: {job}",
    ]
    return subject, "\n".join(body) + "\n"


def finished_message(
    job: str, started: datetime, finished: datetime, details: list[str] | tuple[str, ...] = ()
) -> tuple[str, str]:
    subject = f"✅ Your little scraper finished · {_clock(finished)}"
    body = [
        "Hello again! 👋",
        "",
        f"Your little scraper finished at {_clock(finished)}, {_date(finished)} (Myanmar time).",
        f"It ran for {_duration(finished - started)}.",
        "",
        *_framed(random.choice(NIGHT_FINISHED if is_night(finished) else DAY_FINISHED)),
        "",
        *details,
        *([""] if details else []),
        f"Job: {job}",
    ]
    return subject, "\n".join(body) + "\n"


def failed_message(
    job: str, started: datetime, failed: datetime, part: str, error: object
) -> tuple[str, str]:
    subject = f"⚠️ Your little scraper failed: {part} · {_clock(failed)}"
    body = [
        "Heads up! ⚠️",
        "",
        f"The {part.upper()} part failed at {_clock(failed)}, {_date(failed)} (Myanmar time),",
        f"after {_duration(failed - started)}.",
        "",
        f"Error: {error}",
        "",
        "Progress is saved: run the same command again to resume.",
        "",
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
            print(f"[mail] off: {reason} (set them in .env)", flush=True)
        else:
            print(
                f"[mail] on -> {', '.join(self.config.recipients)} via "
                f"{self.config.host}:{self.config.port} ({self.config.security})",
                flush=True,
            )

    def _deliver(self, kind: str, compose) -> None:
        if self.config is None:
            return
        try:
            subject, body = compose()
            send(self.config, subject, body)
        except Exception as error:  # noqa: BLE001 - mail must never stop a run
            print(f"[mail-warn] {kind} email not sent: {type(error).__name__}: {error}", flush=True)
            return
        print(f"[mail] {kind} email sent @ {_clock(now())}", flush=True)

    def send_started(self) -> None:
        self._deliver("started", lambda: started_message(self.job, self.started))

    def send_finished(self, details: list[str] | tuple[str, ...] = ()) -> None:
        self._deliver("finished", lambda: finished_message(self.job, self.started, now(), details))

    def send_failed(self, part: str, error: object) -> None:
        self._deliver("failed", lambda: failed_message(self.job, self.started, now(), part, error))

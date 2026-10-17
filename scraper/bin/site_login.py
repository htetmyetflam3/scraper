"""Per-site login using credentials from the environment or repo-root .env.

Both crawlers share one HTTP client per run, so logging in once per host
persists the session cookies for every later request in that run. Login is
best-effort and fail-open: whatever happens, the crawl continues (without
the session when login did not work) and the outcome is logged.

Credentials (host-specific beats the generic default)::

    LOGIN_EMAIL=you@example.com
    LOGIN_PASSWORD=s3cret
    LOGIN_EXAMPLE_COM_EMAIL=other@example.com
    LOGIN_EXAMPLE_COM_PASSWORD=...
    LOGIN_EXAMPLE_COM_URL=https://example.com/login        # optional override
    LOGIN_EXAMPLE_COM_USER_FIELD=email                     # optional override
    LOGIN_EXAMPLE_COM_PASS_FIELD=passwd                    # optional override

Host dots/dashes become underscores and everything is uppercased, e.g.
``mbookshelf.naingdroidapps.com`` -> ``MBOOKSHELF_NAINGDROIDAPPS_COM``.
``LOGIN_<HOST>_USERNAME`` is accepted wherever ``_EMAIL`` is.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Tag

REPO_ROOT = Path(__file__).resolve().parent.parent  # python/
DOTENV_PATH = REPO_ROOT.parent / ".env"  # repo root, next to main.py
LOGIN_TIMEOUT = 20.0

LOGOUT_MARKER = re.compile(r"\b(log\s?out|logout|sign\s?out|signout)\b", re.I)
_USER_HINTS = (
    "email",
    "e-mail",
    "user",
    "login",
    "account",
    "member",
    "phone",
    "mobile",
    "username",
)
_NON_USER_TYPES = frozenset(
    {
        "hidden",
        "password",
        "submit",
        "button",
        "image",
        "reset",
        "file",
        "checkbox",
        "radio",
    }
)
_NON_SUBMIT_TYPES = frozenset({"submit", "button", "image", "reset", "file"})


def load_dotenv_values(path: Path | None = None) -> dict[str, str]:
    """Parse a .env file the same way fetch_results does (quotes, export)."""
    try:
        lines = (path or DOTENV_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        entry = line.strip()
        if entry.startswith("export "):
            entry = entry[7:].lstrip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        name, value = entry.split("=", 1)
        name = name.strip()
        if not name or name in values:
            continue
        value = value.strip()
        if value[:1] in ('"', "'"):
            closing_quote = value.find(value[0], 1)
            if closing_quote > 0:
                value = value[1:closing_quote]
        else:
            value = value.split("#", 1)[0].strip()
        values[name] = value
    return values


def _host_token(host: str) -> str:
    return re.sub(r"[^0-9A-Z]+", "_", host.upper()).strip("_")


@dataclass
class LoginCredentials:
    username: str
    password: str
    login_url: str | None = None
    user_field: str | None = None
    pass_field: str | None = None


def load_credentials(host: str) -> LoginCredentials | None:
    """Credentials for a host: host-specific keys, else the generic default."""
    dotenv = load_dotenv_values()

    def pick(*names: str) -> str:
        for name in names:
            value = os.environ.get(name)
            if value is None:
                value = dotenv.get(name, "")
            if value.strip():
                return value.strip()
        return ""

    token = _host_token(host)
    username = pick(
        f"LOGIN_{token}_EMAIL",
        f"LOGIN_{token}_USERNAME",
        "LOGIN_EMAIL",
        "LOGIN_USERNAME",
    )
    password = pick(f"LOGIN_{token}_PASSWORD", "LOGIN_PASSWORD")
    if not username or not password:
        return None
    return LoginCredentials(
        username=username,
        password=password,
        login_url=pick(f"LOGIN_{token}_URL", "LOGIN_URL") or None,
        user_field=pick(f"LOGIN_{token}_USER_FIELD", "LOGIN_USER_FIELD") or None,
        pass_field=pick(f"LOGIN_{token}_PASS_FIELD", "LOGIN_PASS_FIELD") or None,
    )


@dataclass
class LoginSubmission:
    url: str
    method: str
    data: dict[str, str]


def _named_inputs(form: Tag, name: str) -> list[Tag]:
    return [
        element
        for element in form.find_all("input")
        if element.get("name") == name and not element.has_attr("disabled")
    ]


def _user_score(element: Tag) -> int:
    haystack = (
        f"{element.get('name', '')} {element.get('id', '')} "
        f"{' '.join(element.get('class', []))} "
        f"{element.get('placeholder', '')} {element.get('autocomplete', '')}"
    ).lower()
    score = sum(1 for hint in _USER_HINTS if hint in haystack)
    if (element.get("type") or "text").lower() == "email":
        score += 2
    if (element.get("autocomplete") or "").lower() in {"username", "email"}:
        score += 2
    return score


def _iter_login_forms(
    page_html: str, *, user_field: str | None = None, pass_field: str | None = None
) -> list[tuple[Tag, Tag, Tag]]:
    """Login-capable forms as (form, user input, pass input), best first.

    A form qualifies with a usable username + password pair. Single-password
    forms sort before multi-password ones, so a login form wins over a
    register form on the same page.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    ranked: list[tuple[int, Tag, Tag, Tag]] = []
    for form in soup.find_all("form"):
        if pass_field:
            pass_inputs = _named_inputs(form, pass_field)
        else:
            pass_inputs = [
                element
                for element in form.find_all("input")
                if (element.get("type") or "").lower() == "password"
                and element.get("name")
                and not element.has_attr("disabled")
            ]
        if not pass_inputs:
            continue
        if user_field:
            user_inputs = _named_inputs(form, user_field)
        else:
            user_inputs = [
                element
                for element in form.find_all("input")
                if element.get("name")
                and not element.has_attr("disabled")
                and (element.get("type") or "text").lower() not in _NON_USER_TYPES
            ]
        if not user_inputs:
            continue
        best_user = max(user_inputs, key=_user_score)
        rank = 0 if pass_field else len(pass_inputs)
        ranked.append((rank, form, best_user, pass_inputs[0]))
    ranked.sort(key=lambda item: item[0])
    return [(form, user, secret) for _, form, user, secret in ranked]


def _collect_login_data(
    form: Tag, user_input: Tag, pass_input: Tag, username: str, password: str
) -> dict[str, str]:
    """Serialize the form like a browser: hidden tokens, checked boxes,
    default values, plus the filled-in credentials."""
    data: dict[str, str] = {}
    for field in form.find_all(["input", "select", "textarea"]):
        name = field.get("name")
        if not isinstance(name, str) or not name or field.has_attr("disabled"):
            continue
        if field is user_input:
            data[name] = username
            continue
        if field is pass_input:
            data[name] = password
            continue
        if field.name == "input":
            field_type = (field.get("type") or "text").lower()
            if field_type in _NON_SUBMIT_TYPES:
                continue
            if field_type in {"checkbox", "radio"}:
                if field.has_attr("checked"):
                    value = field.get("value", "on")
                    data[name] = value if isinstance(value, str) else str(value)
                continue
            value = field.get("value", "")
            data[name] = value if isinstance(value, str) else str(value)
        elif field.name == "textarea":
            data[name] = field.get_text()
        else:  # select: selected options, else the first for single-select
            options = field.find_all("option")
            selected = [opt for opt in options if opt.has_attr("selected")]
            if not selected and not field.has_attr("multiple") and options:
                selected = options[:1]
            for option in selected:
                if option.has_attr("value"):
                    picked = option["value"]
                    data[name] = picked if isinstance(picked, str) else str(picked)
                else:
                    data[name] = option.get_text(strip=True)
    return data


def find_login_form(
    page_html: str,
    page_url: str,
    username: str,
    password: str,
    *,
    user_field: str | None = None,
    pass_field: str | None = None,
) -> LoginSubmission | None:
    """Build the submission for the best login form on the page, if any.

    Off-site actions are allowed (SSO often posts to an accounts host); only
    the scheme/userinfo is validated, never the host.
    """
    for form, user_input, pass_input in _iter_login_forms(
        page_html, user_field=user_field, pass_field=pass_field
    ):
        absolute = urllib.parse.urljoin(page_url, form.get("action") or page_url)
        data = _collect_login_data(form, user_input, pass_input, username, password)
        method = (form.get("method") or "GET").strip().upper() or "GET"
        if method == "GET" and data:
            parts = urllib.parse.urlsplit(absolute)
            query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            query.extend(sorted(data.items()))
            absolute = urllib.parse.urlunsplit(
                parts._replace(
                    query=urllib.parse.urlencode(query, doseq=True), fragment=""
                )
            )
            data = {}
        try:
            parts = urllib.parse.urlsplit(absolute)
            if (
                parts.scheme.lower() not in ("http", "https")
                or not parts.netloc
                or parts.username is not None
                or parts.password is not None
            ):
                continue
            parts.port
        except ValueError:
            continue
        return LoginSubmission(
            url=absolute,
            method="GET" if method == "GET" else "POST",
            data={} if method == "GET" else data,
        )
    return None


def page_has_login_form(page_html: str) -> bool:
    """True when the page shows any usable username+password form."""
    return bool(_iter_login_forms(page_html))


def page_shows_logged_in(page_html: str) -> bool:
    """True when visible page text offers a logout (session is active)."""
    soup = BeautifulSoup(page_html, "html.parser")
    for dead in soup(["script", "style", "noscript"]):
        dead.decompose()
    return bool(LOGOUT_MARKER.search(soup.get_text(" ", strip=True)))


async def _fetch_async(
    client: httpx.AsyncClient, url: str
) -> tuple[str, str] | None:
    try:
        response = await client.get(url, follow_redirects=True, timeout=LOGIN_TIMEOUT)
        response.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return None
    return response.text, str(response.url)


def _fetch_sync(client: httpx.Client, url: str) -> tuple[str, str] | None:
    try:
        response = client.get(url, follow_redirects=True, timeout=LOGIN_TIMEOUT)
        response.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return None
    return response.text, str(response.url)


async def _submit_async(
    client: httpx.AsyncClient, submission: LoginSubmission
) -> bool:
    try:
        if submission.method == "POST":
            response = await client.post(
                submission.url,
                data=submission.data,
                follow_redirects=True,
                timeout=LOGIN_TIMEOUT,
            )
        else:
            response = await client.get(
                submission.url, follow_redirects=True, timeout=LOGIN_TIMEOUT
            )
        response.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return False
    return True


def _submit_sync(client: httpx.Client, submission: LoginSubmission) -> bool:
    try:
        if submission.method == "POST":
            response = client.post(
                submission.url,
                data=submission.data,
                follow_redirects=True,
                timeout=LOGIN_TIMEOUT,
            )
        else:
            response = client.get(
                submission.url, follow_redirects=True, timeout=LOGIN_TIMEOUT
            )
        response.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return False
    return True


def _login_host(seed_url: str) -> str:
    try:
        return urllib.parse.urlsplit(seed_url).hostname or ""
    except ValueError:
        return ""


async def ensure_logged_in_async(
    client: httpx.AsyncClient, seed_url: str
) -> bool:
    """Log the shared client into the seed's host when credentials exist.

    Silent when the host has no credentials (the common case). Fail-open:
    any failure just logs and the crawl continues without the session.
    """
    host = _login_host(seed_url)
    if not host:
        return False
    creds = load_credentials(host)
    if creds is None:
        return False
    print(f"[login] {host}: credentials found; logging in…", flush=True)
    pages: list[tuple[str, str]] = []
    if creds.login_url:  # explicit override beats seed discovery
        fetched = await _fetch_async(client, creds.login_url)
        if fetched is not None:
            pages.append(fetched)
        else:
            print(
                f"[login] {host}: login page unreachable; "
                "falling back to the seed page",
                flush=True,
            )
    fetched = await _fetch_async(client, seed_url)
    if fetched is not None:
        pages.append(fetched)
    submission = None
    for page_html, final_url in pages:
        submission = find_login_form(
            page_html,
            final_url,
            creds.username,
            creds.password,
            user_field=creds.user_field,
            pass_field=creds.pass_field,
        )
        if submission is not None:
            break
    if submission is None:
        if not pages:
            print(
                f"[login] {host}: site unreachable; continuing without login",
                flush=True,
            )
            return False
        print(f"[login] {host}: no login form found; continuing", flush=True)
        return True
    if not await _submit_async(client, submission):
        print(
            f"[login] {host}: login request failed "
            "(rejected or unreachable); continuing without login",
            flush=True,
        )
        return False
    verify = await _fetch_async(client, seed_url)
    if verify is None:
        print(
            f"[login] {host}: submitted but the seed is unreachable; "
            "the session may still be active",
            flush=True,
        )
        return True
    if not page_has_login_form(verify[0]) or page_shows_logged_in(verify[0]):
        print(f"[login] {host}: success", flush=True)
        return True
    print(
        f"[login] {host}: still seeing a login form; continuing without login",
        flush=True,
    )
    return False


def ensure_logged_in_sync(client: httpx.Client, seed_url: str) -> bool:
    """Sync twin of :func:`ensure_logged_in_async` for Scrawler."""
    host = _login_host(seed_url)
    if not host:
        return False
    creds = load_credentials(host)
    if creds is None:
        return False
    print(f"[login] {host}: credentials found; logging in…", flush=True)
    pages: list[tuple[str, str]] = []
    if creds.login_url:  # explicit override beats seed discovery
        fetched = _fetch_sync(client, creds.login_url)
        if fetched is not None:
            pages.append(fetched)
        else:
            print(
                f"[login] {host}: login page unreachable; "
                "falling back to the seed page",
                flush=True,
            )
    fetched = _fetch_sync(client, seed_url)
    if fetched is not None:
        pages.append(fetched)
    submission = None
    for page_html, final_url in pages:
        submission = find_login_form(
            page_html,
            final_url,
            creds.username,
            creds.password,
            user_field=creds.user_field,
            pass_field=creds.pass_field,
        )
        if submission is not None:
            break
    if submission is None:
        if not pages:
            print(
                f"[login] {host}: site unreachable; continuing without login",
                flush=True,
            )
            return False
        print(f"[login] {host}: no login form found; continuing", flush=True)
        return True
    if not _submit_sync(client, submission):
        print(
            f"[login] {host}: login request failed "
            "(rejected or unreachable); continuing without login",
            flush=True,
        )
        return False
    verify = _fetch_sync(client, seed_url)
    if verify is None:
        print(
            f"[login] {host}: submitted but the seed is unreachable; "
            "the session may still be active",
            flush=True,
        )
        return True
    if not page_has_login_form(verify[0]) or page_shows_logged_in(verify[0]):
        print(f"[login] {host}: success", flush=True)
        return True
    print(
        f"[login] {host}: still seeing a login form; continuing without login",
        flush=True,
    )
    return False

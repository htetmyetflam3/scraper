"""Phatmal (Expo app) API driver.

The site is a JS-rendered SPA: its static HTML is an empty shell with no
forms or content, so both HTML crawlers are blind here. This module drives
the JSON API directly instead: ``POST /api/auth/login`` with the .env
credentials returns a Bearer JWT, which authorizes every later API call.
"""

from __future__ import annotations

import asyncio
import os

import httpx

from site_login import load_dotenv_values

API_BASE = "https://api.phatmal.com/api"
REFERER = "https://phatmal.com/"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)
REQUEST_TIMEOUT = 20.0

# Where the JWT may live in the login response (tried in order). The exact
# key is still unconfirmed, so every common shape is accepted and a miss
# logs the actual top-level keys for diagnosis.
_TOKEN_PATHS = (
    ("token",),
    ("access_token",),
    ("accessToken",),
    ("jwt",),
    ("data", "token"),
    ("data", "access_token"),
    ("data", "accessToken"),
    ("result", "token"),
    ("auth", "token"),
)


def load_api_config() -> tuple[str, str, str] | None:
    """(email, password, api_key) from env or repo-root .env, else None."""
    dotenv = load_dotenv_values()

    def pick(name: str) -> str:
        value = os.environ.get(name)
        if value is None:
            value = dotenv.get(name, "")
        return value.strip()

    email = pick("PHATMAL_EMAIL")
    password = pick("PHATMAL_PASSWORD")
    api_key = pick("PHATMAL_API_KEY")
    if not email or not password or not api_key:
        missing = ", ".join(
            name
            for name, value in (
                ("PHATMAL_EMAIL", email),
                ("PHATMAL_PASSWORD", password),
                ("PHATMAL_API_KEY", api_key),
            )
            if not value
        )
        print(f"[phatmal] missing {missing}; add to the environment or .env")
        return None
    return email, password, api_key


def _api_headers(api_key: str, token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "User-Agent": USER_AGENT,
        "Referer": REFERER,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def extract_token(payload: object) -> str | None:
    """Find the JWT in a login response of any common shape."""
    if not isinstance(payload, dict):
        return None
    for path in _TOKEN_PATHS:
        node: object = payload
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, str) and node.strip():
            return node.strip()
    return None


def _top_keys(payload: object) -> str:
    if isinstance(payload, dict):
        return ", ".join(sorted(str(key) for key in payload)[:12]) or "(empty)"
    return f"(not an object: {type(payload).__name__})"


async def api_login(
    client: httpx.AsyncClient, email: str, password: str, api_key: str
) -> str | None:
    """POST the credentials; return the Bearer JWT or None. Never logs secrets."""
    try:
        response = await client.post(
            f"{API_BASE}/auth/login",
            json={"email": email, "password": password},
            headers=_api_headers(api_key),
            timeout=REQUEST_TIMEOUT,
        )
    except (httpx.HTTPError, ValueError) as error:
        print(f"[phatmal] login request failed: {error}")
        return None
    if response.status_code != 200:
        print(f"[phatmal] login rejected (HTTP {response.status_code})")
        return None
    try:
        payload = response.json()
    except ValueError:
        print("[phatmal] login answered non-JSON; cannot continue")
        return None
    token = extract_token(payload)
    if token is None:
        print(
            "[phatmal] login ok but no token found; "
            f"top-level keys: {_top_keys(payload)}"
        )
        return None
    print("[phatmal] login ok")
    return token


async def api_me(
    client: httpx.AsyncClient, api_key: str, token: str
) -> dict | None:
    """GET the session endpoint; proves the token works. Returns the user object."""
    try:
        response = await client.get(
            f"{API_BASE}/auth/me",
            headers=_api_headers(api_key, token),
            timeout=REQUEST_TIMEOUT,
        )
    except (httpx.HTTPError, ValueError) as error:
        print(f"[phatmal] session check failed: {error}")
        return None
    if response.status_code != 200:
        print(f"[phatmal] session rejected (HTTP {response.status_code})")
        return None
    try:
        payload = response.json()
    except ValueError:
        print("[phatmal] session answered non-JSON")
        return None
    if not isinstance(payload, dict):
        print("[phatmal] session answered unexpectedly")
        return None
    print("[phatmal] session ok")
    return payload


async def login_and_verify() -> bool:
    """Log in with the configured credentials and prove the session works."""
    config = load_api_config()
    if config is None:
        return False
    email, password, api_key = config
    async with httpx.AsyncClient(follow_redirects=True) as client:
        token = await api_login(client, email, password, api_key)
        if token is None:
            return False
        return await api_me(client, api_key, token) is not None


# --- content probing -----------------------------------------------------------
# The book-list/detail endpoints are still unknown (mobile network capture
# is impractical), so --phatmal-probe tries the likely REST paths with the
# real session and prints each answer's SHAPE (keys, not values). One run
# reveals the contract; then the real crawl gets built on top.

DETAIL_PATH_TEMPLATES = (
    "ebooks/{slug}",
    "ebook/{slug}",
    "books/{slug}",
    "book/{slug}",
    "library/{slug}",
    "novels/{slug}",
)
LIST_PATHS = ("ebooks", "books", "library", "ebooks/list")

_SECRET_KEY_HINTS = (
    "token",
    "password",
    "secret",
    "passwd",
    "pwd",
    "authorization",
    "cookie",
    "session",
)


def slug_from_input(value: str) -> str:
    """Book slug from a bare slug or a full /ebook/<slug> URL."""
    text = value.strip()
    if "://" in text:
        text = text.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    text = text.strip().strip("/")
    if not text or not all(char.isalnum() or char in "-_.~%" for char in text):
        return ""
    return text


def describe_shape(value: object, _depth: int = 0) -> str:
    """Compact structure summary: keys and scalar previews, secrets redacted."""
    if _depth > 3:
        return "…"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if len(value) <= 48:
            return repr(value)
        return f"{value[:44]!r}…[{len(value)}]"
    if isinstance(value, list):
        if not value:
            return "[]"
        head = describe_shape(value[0], _depth + 1)
        return f"[{head} x{len(value)}]" if len(value) > 1 else f"[{head}]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = []
        for key in list(value)[:8]:
            name = str(key)
            if any(hint in name.lower() for hint in _SECRET_KEY_HINTS):
                parts.append(f"{name}: <redacted>")
            else:
                parts.append(f"{name}: {describe_shape(value[key], _depth + 1)}")
        if len(value) > 8:
            parts.append(f"+{len(value) - 8} more")
        return "{" + ", ".join(parts) + "}"
    return type(value).__name__


async def api_get_json(
    client: httpx.AsyncClient, api_key: str, token: str, path: str
) -> tuple[int, object | None]:
    """Authorized GET; returns (status, parsed JSON or None)."""
    try:
        response = await client.get(
            f"{API_BASE}/{path.lstrip('/')}",
            headers=_api_headers(api_key, token),
            timeout=REQUEST_TIMEOUT,
        )
    except (httpx.HTTPError, ValueError) as error:
        print(f"[phatmal] GET {path} failed: {error}")
        return -1, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return 200, response.json()
    except ValueError:
        print(f"[phatmal] GET {path} answered non-JSON")
        return 200, None


async def probe_content(slug: str) -> bool:
    """Try the likely content endpoints with a fresh session; log shapes."""
    config = load_api_config()
    if config is None:
        return False
    email, password, api_key = config
    async with httpx.AsyncClient(follow_redirects=True) as client:
        token = await api_login(client, email, password, api_key)
        if token is None:
            return False
        me = await api_me(client, api_key, token)
        if me is None:
            return False
        print(f"[phatmal] me shape: {describe_shape(me)[:800]}")
        hits: list[str] = []
        probes = [t.format(slug=slug) for t in DETAIL_PATH_TEMPLATES] + list(LIST_PATHS)
        for path in probes:
            status, payload = await api_get_json(client, api_key, token, path)
            if status == 200 and payload is not None:
                hits.append(path)
                print(f"[phatmal] HIT {path}: {describe_shape(payload)[:1500]}")
            else:
                print(f"[phatmal] miss {path} (HTTP {status})")
    if hits:
        print(f"[phatmal] probe found {len(hits)} endpoint(s): {', '.join(hits)}")
        return True
    print("[phatmal] probe found nothing; paste this log for the next step")
    return False


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(login_and_verify()) else 1)

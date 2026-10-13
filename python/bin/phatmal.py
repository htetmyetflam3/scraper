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


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(login_and_verify()) else 1)

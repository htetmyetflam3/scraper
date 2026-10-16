"""Phatmal (Expo app) API driver.

The site is a JS-rendered SPA: its static HTML is an empty shell with no
forms or content, so both HTML crawlers are blind here. This module drives
the JSON API directly instead: ``POST /api/auth/login`` with the .env
credentials returns a Bearer JWT, which authorizes every later API call.
"""

from __future__ import annotations

import io
import os
import re
import urllib.parse
import zipfile
from pathlib import Path

import httpx

from disk_floor import check_disk
from mediafire import is_docx
from pdf_support import is_pdf
from site_login import load_dotenv_values

API_BASE = "https://api.phatmal.com/api"
REFERER = "https://phatmal.com/"
BOOK_HOSTS = frozenset({"phatmal.com", "www.phatmal.com"})
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



def slug_from_url(url: str) -> str:
    """Book slug from a phatmal.com /ebook/<slug> URL; anything else raises ValueError."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        parts, host = None, ""
    segments = [segment for segment in parts.path.split("/") if segment] if parts else []
    slug = segments[1] if len(segments) == 2 and segments[0] == "ebook" else ""
    if (
        parts is None
        or parts.scheme.lower() not in {"http", "https"}
        or host not in BOOK_HOSTS
        or not slug
        or not all(char.isalnum() or char in "-_.~%" for char in slug)
    ):
        raise ValueError(
            f"not a phatmal book URL (https://phatmal.com/ebook/<slug>): {url}"
        )
    return slug


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


# --- library crawl ---------------------------------------------------------------
# Contract (captured 2026-10-13 from the live API via console recon):
#   GET /ebooks?page=N&limit=M -> {success, count, pagination, data: [book]}
#   GET /ebooks/{slug}          -> {success, data: book}
#   book.files: [{url, type, size}]; book.fileUrl + book.fileType ("epub"…).
# Files live on S3 and must be fetched WITHOUT auth headers (S3 rejects an
# unknown Authorization header instead of ignoring it).

REPO_ROOT = Path(__file__).resolve().parent.parent  # python/
SCAN_DIR = REPO_ROOT.parent / "scan"
OUTPUT_DIR = REPO_ROOT / "pdfs"
PHATMAL_FILE = SCAN_DIR / "phatmal.txt"
PAGE_LIMIT = 100
FILE_TIMEOUT = 120.0

_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def is_epub(data: bytes) -> bool:
    """Tolerant EPUB check: a ZIP with mimetype + META-INF/container.xml."""
    if len(data) < 4 or data[:2] != b"PK":
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            if "mimetype" not in names or "META-INF/container.xml" not in names:
                return False
            return archive.read("mimetype").strip() == b"application/epub+zip"
    except Exception:
        return False


def sniff_extension(data: bytes) -> str | None:
    """File extension by content sniffing: epub, pdf, docx, or None."""
    if is_epub(data):
        return "epub"
    if is_pdf(data):
        return "pdf"
    if is_docx(data):
        return "docx"
    return None


def pick_book_file(book: dict) -> tuple[str, str] | None:
    """(file URL, type label): files[] first (epub > pdf > first entry),
    else the legacy fileUrl + fileType pair."""
    files = book.get("files")
    if isinstance(files, list):
        usable = [
            entry
            for entry in files
            if isinstance(entry, dict)
            and isinstance(entry.get("url"), str)
            and entry["url"].strip()
        ]
        for want in ("epub", "pdf"):
            for entry in usable:
                if str(entry.get("type") or "").lower() == want:
                    return entry["url"].strip(), want
        if usable:
            return usable[0]["url"].strip(), str(usable[0].get("type") or "")
    url = book.get("fileUrl")
    if isinstance(url, str) and url.strip():
        return url.strip(), str(book.get("fileType") or "")
    return None


def _book_filename(book: dict, extension: str) -> str:
    title = str(book.get("title") or "").strip()
    authors = book.get("authors") or []
    author = str(authors[0]).strip() if authors else ""
    if title and author:
        base = f"{title} - {author}"
    elif title:
        base = title
    else:
        base = str(book.get("slug") or book.get("_id") or "book")
    base = _FILENAME_BAD.sub("", base).strip().strip(".")
    if not base:
        base = str(book.get("slug") or "book")
    return base[:120] + "." + extension


def _reserve_unique_path(output_dir: Path, filename: str) -> Path:
    candidate = output_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    index = 2
    while (candidate := output_dir / f"{stem} ({index}){suffix}").exists():
        index += 1
    return candidate


def _load_known(path: Path | None = None) -> dict[str, str]:
    """Saved books as slug -> filename (scan/phatmal.txt, append-only)."""
    try:
        lines = (path or PHATMAL_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    known: dict[str, str] = {}
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "|" not in text:
            continue
        slug, filename = text.split("|", 1)
        if slug.strip() and filename.strip():
            known[slug.strip()] = filename.strip()
    return known


def _remember_book(slug: str, filename: str, path: Path | None = None) -> None:
    target = path or PHATMAL_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"{slug}|{filename}\n")
    except OSError as error:
        print(f"[phatmal] cannot record {slug}: {error}")


async def _fetch_book_list(
    client: httpx.AsyncClient, api_key: str, token: str
) -> list[dict]:
    books: list[dict] = []
    page = 1
    while True:
        status, payload = await api_get_json(
            client, api_key, token, f"ebooks?page={page}&limit={PAGE_LIMIT}"
        )
        if status != 200 or not isinstance(payload, dict):
            print(f"[phatmal] list page {page} failed (HTTP {status}); stopping")
            break
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            break
        books.extend(item for item in data if isinstance(item, dict))
        paging = payload.get("pagination")
        if not isinstance(paging, dict) or paging.get("hasMore") is not True:
            break
        page += 1
        pages = paging.get("pages")
        if isinstance(pages, int) and page > pages:
            break
        if page > 500:  # runaway guard; the library is ~236 books
            print("[phatmal] list pages exceeded 500; stopping")
            break
    return books


async def _fetch_book_detail(
    client: httpx.AsyncClient, api_key: str, token: str, slug: str
) -> dict | None:
    status, payload = await api_get_json(client, api_key, token, f"ebooks/{slug}")
    if status == 200 and isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
    return None


async def _download_file(client: httpx.AsyncClient, url: str) -> bytes | None:
    """Plain GET with no auth headers (S3 rejects unknown Authorization)."""
    try:
        response = await client.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=FILE_TIMEOUT
        )
        response.raise_for_status()
        return response.content
    except (httpx.HTTPError, ValueError) as error:
        print(f"[phatmal] file fetch failed: {error}")
        return None


async def download_book(
    client: httpx.AsyncClient,
    api_key: str,
    token: str,
    book: dict,
    output_dir: Path,
    known: dict[str, str],
) -> str:
    """Save one book; returns saved | known | skipped | failed (logs incl)."""
    slug = str(book.get("slug") or "")
    title = str(book.get("title") or slug or "?")
    status = book.get("status")
    if status is not None and status != "published":
        print(f"[phatmal] [skip] {title}: status {status!r}")
        return "skipped"
    if slug and slug in known and (output_dir / known[slug]).exists():
        print(f"[phatmal] [known] {title}")
        return "known"
    picked = pick_book_file(book)
    if picked is None and slug:
        detail = await _fetch_book_detail(client, api_key, token, slug)
        if detail is not None:
            book = detail
            picked = pick_book_file(book)
    if picked is None:
        print(f"[phatmal] [skip] {title}: no file link in API data")
        return "skipped"
    url, _kind = picked
    check_disk(output_dir)  # under the 5 GB kept for the system every command quits
    print(f"[phatmal] [download] {title}", flush=True)
    data = await _download_file(client, url)
    if data is None:
        return "failed"
    extension = sniff_extension(data)
    if extension is None:
        print(
            f"[phatmal] [rejected] {title}: "
            f"not an EPUB/PDF/DOCX ({len(data)} bytes)"
        )
        return "failed"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = _reserve_unique_path(output_dir, _book_filename(book, extension))
    try:
        path.write_bytes(data)
    except OSError as error:
        print(f"[phatmal] [failed] {title}: {error}")
        return "failed"
    print(f"[phatmal] [saved] {path.name} ({len(data) // 1024} KB)")
    if slug:
        known[slug] = path.name
        _remember_book(slug, path.name)
    return "saved"


async def crawl_library(output_dir: Path | None = None) -> dict[str, int]:
    stats = {"total": 0, "saved": 0, "known": 0, "skipped": 0, "failed": 0}
    config = load_api_config()
    if config is None:
        return stats
    email, password, api_key = config
    out = output_dir or OUTPUT_DIR
    async with httpx.AsyncClient(follow_redirects=True) as client:
        token = await api_login(client, email, password, api_key)
        if token is None:
            return stats
        if await api_me(client, api_key, token) is None:
            return stats
        books = await _fetch_book_list(client, api_key, token)
        stats["total"] = len(books)
        known = _load_known()
        for book in books:
            outcome = await download_book(client, api_key, token, book, out, known)
            stats[outcome] += 1
    print(
        f"[phatmal] done: {stats['saved']} saved, {stats['known']} known, "
        f"{stats['skipped']} skipped, {stats['failed']} failed "
        f"of {stats['total']}"
    )
    return stats


async def download_single(url: str, output_dir: Path | None = None) -> bool:
    slug = slug_from_url(url)
    config = load_api_config()
    if config is None:
        return False
    email, password, api_key = config
    out = output_dir or OUTPUT_DIR
    async with httpx.AsyncClient(follow_redirects=True) as client:
        token = await api_login(client, email, password, api_key)
        if token is None:
            return False
        if await api_me(client, api_key, token) is None:
            return False
        book = await _fetch_book_detail(client, api_key, token, slug)
        if book is None:
            print(f"[phatmal] no book found for {slug!r}")
            return False
        outcome = await download_book(client, api_key, token, book, out, _load_known())
        return outcome in {"saved", "known"}


async def run_phatmal(
    target: str | None, output_dir: Path | None = None
) -> bool:
    """None crawls the whole library; else one phatmal.com/ebook/<slug> URL
    (any other URL raises ValueError before logging in). False only when
    nothing was saved and something failed (or the list came back empty)."""
    check_disk(output_dir or OUTPUT_DIR)  # before logging in: no room, no run
    if target is None:
        stats = await crawl_library(output_dir)
        return stats["saved"] > 0 or (
            stats["failed"] == 0 and stats["total"] > 0
        )
    return await download_single(target, output_dir)

#!/usr/bin/env python3
"""Download the book files found by search_crawl.py.

    uv run python/download.py                       # search_entry_list.txt
    uv run python/download.py entry_list.txt
    uv run python/download.py --concurrency=8 --verify-pdf

Reads a crawler TSV ("Book Name<TAB>URL<TAB>Source Page") or a plain URL list,
downloads concurrently with httpx, retries with tenacity, and refuses files
that only pretend to be PDFs: a lot of "PDF" links return an HTML login or
error page with a 200 status, which is worse than a failure because it looks
like a success. For MediaFire share pages, it first tries the embedded Download
link and validates the fetched file instead of saving the landing page.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import io
import json
import re
import shutil
import sys
import urllib.parse
import zipfile
from pathlib import Path

try:
    import httpx
    from tenacity import retry_if_exception_type, stop_after_attempt, wait_exponential_jitter
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("Missing dependencies. Run:  uv sync")

HERE = Path(__file__).resolve().parent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
CONTENT_TYPES = {
    "application/pdf": ".pdf",
    "application/octet-stream": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}
PDF_HEADER_WINDOW = 1024  # readers commonly tolerate a header in the first 1 KiB
HTML_PREFIXES = (b"<!doctype html", b"<html", b"<head", b"<body", b"<meta", b"<title", b"<script", b"<form")


def looks_like_html(data: bytes) -> bool:
    """Identify HTML before accepting a stray ``%PDF-`` string as a signature."""
    prefix = data[:PDF_HEADER_WINDOW]
    if prefix.startswith(bytes((0xEF, 0xBB, 0xBF))):
        prefix = prefix[3:]
    prefix = prefix.lstrip(b" \t\r\n").lower()
    while prefix.startswith(b"<!--"):
        end = prefix.find(b"-->")
        if end < 0:
            return True
        prefix = prefix[end + 3 :].lstrip(b" \t\r\n")
    return prefix.startswith(HTML_PREFIXES)


def pdf_header_offset(data: bytes) -> int | None:
    """Return the location of a PDF header near the beginning of the file.

    Older/non-conforming generators sometimes prepend a short binary prologue
    before ``%PDF-``. Accepting the standard reader window prevents rejecting
    otherwise readable PDFs just because the marker is not byte zero.
    """
    offset = data.find(b"%PDF-", 0, PDF_HEADER_WINDOW)
    return offset if offset >= 0 else None


def is_pdf(data: bytes) -> bool:
    """Validate a PDF signature, then fall back to qpdf for unusual old files."""
    if looks_like_html(data):
        return False
    if pdf_header_offset(data) is not None:
        return True
    try:
        import pikepdf  # noqa: PLC0415

        with pikepdf.open(io.BytesIO(data)):
            return True
    except ImportError:
        return False
    except Exception:  # noqa: BLE001 - unreadable response, not a PDF
        return False


VALIDATORS = {
    ".pdf": is_pdf,
    ".docx": lambda data: data[:4] == b"PK\x03\x04",
}


def safe_filename(name: str) -> str:
    cleaned = UNSAFE_CHARS.sub("_", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" .") or "download"


def filename_from_disposition(header: str | None) -> str | None:
    if not header:
        return None
    encoded = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", header, re.I)
    if encoded:
        from urllib.parse import unquote

        return safe_filename(unquote(encoded[1].strip().strip('"'))) or None
    match = re.search(r'filename\s*=\s*"([^"]+)"', header, re.I) or re.search(r"filename\s*=\s*([^;]+)", header, re.I)
    return safe_filename(match.group(1)) if match else None


def filename_from_url(url: str) -> str:
    from urllib.parse import unquote, urlsplit

    return safe_filename(unquote(urlsplit(url).path.rsplit("/", 1)[-1]))


def choose_filename(url: str, disposition: str | None, content_type: str) -> tuple[str, str]:
    """Return (filename, extension) for a download."""
    name = filename_from_disposition(disposition) or filename_from_url(url) or "download"
    stem, _, extension = name.rpartition(".")
    extension = f".{extension.lower()}" if stem and extension.lower() in ("pdf", "docx") else ""
    if not extension:
        extension = next((ext for key, ext in CONTENT_TYPES.items() if key in (content_type or "").lower()), ".pdf")
    if not stem:
        stem = name or "download"
    return f"{safe_filename(stem)}{extension}", extension


def _is_mediafire_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == "mediafire.com" or host.endswith(".mediafire.com")


def _same_download_site(page_url: str, candidate_url: str) -> bool:
    """Only follow extracted links on the same origin or MediaFire's own hosts."""
    source = urllib.parse.urlsplit(page_url)
    candidate = urllib.parse.urlsplit(candidate_url)
    source_host = (source.hostname or "").lower()
    candidate_host = (candidate.hostname or "").lower()
    if source.scheme not in ("http", "https") or candidate.scheme not in ("http", "https"):
        return False
    if source.scheme == "https" and candidate.scheme != "https":
        return False

    def effective_port(parts):
        return parts.port or (443 if parts.scheme == "https" else 80)

    if source_host == candidate_host:
        return effective_port(source) == effective_port(candidate)
    return (
        _is_mediafire_host(source_host)
        and _is_mediafire_host(candidate_host)
        and effective_port(source) == effective_port(candidate)
    )


QUOTED_PDF_LINK = re.compile(r'''["']([^"']*?\.pdf(?:\?[^"']*)?)["']''', re.I)
RAW_PDF_LINK = re.compile(r'''https?://[^\s"'<>]+?\.pdf(?:\?[^\s"'<>]*)?''', re.I)
DOWNLOAD_VALUE = re.compile(
    r'''(?:download(?:[_-]?(?:url|link))?|direct[_-]?link)["']?\s*[:=]\s*["']([^"']+)["']''',
    re.I,
)


def extract_download_links(page_html: str, page_url: str, limit: int = 8) -> list[str]:
    """Find a PDF/download-button URL embedded in an HTML response.

    MediaFire share URLs often return an HTML page rather than PDF bytes; the
    actual file URL is commonly in its Download button. Links are confined to
    the same origin, except that MediaFire may use a sibling download host.
    """
    text = html.unescape(page_html or "").replace("\\/", "/")
    candidates: dict[str, int] = {}

    def add(raw: str, *, download_control: bool = False) -> None:
        raw = html.unescape((raw or "").strip().strip("\"'"))
        if not raw or raw.lower().startswith(("javascript:", "data:", "mailto:")):
            return
        target = urllib.parse.urljoin(page_url, raw)
        parts = urllib.parse.urlsplit(target)
        if not _same_download_site(page_url, target):
            return
        path = urllib.parse.unquote(parts.path).lower()
        is_pdf_path = path.endswith(".pdf")
        if not is_pdf_path and not download_control:
            return
        target = urllib.parse.urldefrag(target)[0]
        if target == urllib.parse.urldefrag(page_url)[0]:
            return
        # An explicitly marked Download control takes precedence over incidental PDF links.
        priority = 0 if download_control else 1
        if target not in candidates or priority < candidates[target]:
            candidates[target] = priority

    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415

        soup = BeautifulSoup(text, "html.parser")
        for meta in soup.find_all("meta", attrs={"http-equiv": re.compile(r"^refresh$", re.I)}):
            content = meta.get("content", "")
            match = re.search(r'''url\s*=\s*["']?([^"';]+)''', content, re.I)
            if match:
                add(match.group(1), download_control=True)

        url_attributes = {"href", "action", "formaction", "data-href", "data-url", "data-download-url", "data-link"}
        for element in soup.find_all(True):
            attrs = element.attrs
            marker_values = [attrs.get(key, "") for key in ("id", "class", "title", "aria-label")]
            marker_values.append(element.get_text(" ", strip=True)[:200])
            marker = " ".join(
                " ".join(value) if isinstance(value, list) else str(value)
                for value in marker_values
            ).lower()
            is_download_control = bool(
                element.has_attr("download") or "download" in marker
            )
            for name, value in attrs.items():
                if isinstance(value, list):
                    value = " ".join(value)
                if not isinstance(value, str):
                    continue
                if name.lower() in url_attributes:
                    add(value, download_control=is_download_control or "download" in name.lower())
                elif name.lower() == "onclick":
                    for match in QUOTED_PDF_LINK.finditer(value):
                        add(match.group(1), download_control=is_download_control)
                    for match in re.finditer(r'''https?://[^\s"'<>]+''', value, re.I):
                        add(match.group(0), download_control=is_download_control)
    except ImportError:  # pragma: no cover - BeautifulSoup is a project dependency
        pass

    # Some MediaFire pages put the download target in JSON or JavaScript rather
    # than an anchor; inspect quoted links and common download URL assignments.
    for pattern in (QUOTED_PDF_LINK, RAW_PDF_LINK):
        for match in pattern.finditer(text):
            add(match.group(1) if pattern is QUOTED_PDF_LINK else match.group(0))
    for match in DOWNLOAD_VALUE.finditer(text):
        add(match.group(1), download_control=True)

    return [url for url, _priority in sorted(candidates.items(), key=lambda item: item[1])][:limit]


def is_html_response(response) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    return "html" in content_type or "xhtml" in content_type or looks_like_html(response.content)


def read_entries(path: Path) -> list[dict]:
    """Read a crawler TSV or a plain URL list."""
    if not path.exists():
        raise SystemExit(f"Entry list not found: {path}")
    entries: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        columns = line.split("\t")
        url = columns[1].strip() if len(columns) > 1 else columns[0].strip()
        if number == 1 and url.lower() == "url":
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        entries.append({"name": columns[0].strip() if len(columns) > 1 else "", "url": url})
    return entries


class History:
    """url -> filename, persisted as JSON so re-runs skip what is already there."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, str] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf8")

    def existing_path(self, url: str, out_dir: Path) -> Path | None:
        filename = self.data.get(url)
        if not filename:
            return None
        path = out_dir / filename
        return path if path.exists() else None


def unique_path(out_dir: Path, filename: str, url: str, taken: set[str]) -> Path:
    """Avoid two different URLs fighting over one filename."""
    candidate = out_dir / filename
    if filename not in taken and not candidate.exists():
        taken.add(filename)
        return candidate
    stem, _, extension = filename.rpartition(".")
    suffix = hashlib.sha1(url.encode("utf8")).hexdigest()[:10]
    filename = f"{stem}__{suffix}.{extension}" if stem else f"{filename}__{suffix}"
    taken.add(filename)
    return out_dir / filename


async def fetch_with_retries(
    client: httpx.AsyncClient,
    url: str,
    retries: int,
    timeout: float,
    request_headers: dict[str, str] | None = None,
) -> httpx.Response:
    """GET with exponential backoff + jitter (tenacity), honouring 429/503 waits."""
    from tenacity import AsyncRetrying

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(retries),
        wait=wait_exponential_jitter(initial=1, max=30, jitter=1),
        retry=retry_if_exception_type((httpx.HTTPError, httpx.StreamError)),
        reraise=True,
    ):
        with attempt:
            response = await client.get(
                url,
                headers=request_headers,
                timeout=timeout,
                follow_redirects=True,
            )
            if response.status_code in (429, 503):
                wait = float(response.headers.get("Retry-After") or 0) or 5.0
                await asyncio.sleep(min(wait, 60))
                raise httpx.HTTPStatusError(f"HTTP {response.status_code}", request=response.request, response=response)
            response.raise_for_status()
            return response
    raise RuntimeError("unreachable")


async def recover_pdf_response(
    client: httpx.AsyncClient,
    page_response: httpx.Response,
    retries: int,
    timeout: float,
    *,
    seen: set[str] | None = None,
    depth: int = 0,
) -> httpx.Response | None:
    """Follow a PDF link embedded in a MediaFire (or same-site) HTML page."""
    if depth >= 2:
        return None
    seen = seen if seen is not None else set()
    source_url = str(page_response.url)
    page_html = getattr(page_response, "text", None)
    if not isinstance(page_html, str):
        page_html = page_response.content.decode("utf-8-sig", "replace")
    for candidate in extract_download_links(page_html, source_url):
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            response = await fetch_with_retries(
                client,
                candidate,
                retries,
                timeout,
                request_headers={
                    "Referer": source_url,
                    "Accept": "application/pdf,*/*;q=0.8",
                },
            )
        except Exception:  # an expired button must not hide the original validation failure
            continue
        if is_pdf(response.content):
            return response
        if is_html_response(response):
            nested = await recover_pdf_response(
                client,
                response,
                retries,
                timeout,
                seen=seen,
                depth=depth + 1,
            )
            if nested is not None:
                return nested
    return None


async def download_one(
    client: httpx.AsyncClient,
    entry: dict,
    out_dir: Path,
    history: History,
    taken: set[str],
    args: argparse.Namespace,
    semaphore: asyncio.Semaphore,
    lock: asyncio.Lock,
) -> tuple[str, str]:
    url = entry["url"]
    async with semaphore:
        cached = history.existing_path(url, out_dir)
        if cached:
            return url, f"cached ({cached.name})"

        try:
            response = await fetch_with_retries(client, url, args.retries, args.timeout)
        except Exception as error:  # noqa: BLE001 - report any failure and continue
            return url, f"failed: {str(error) or type(error).__name__}"

        filename, extension = choose_filename(
            str(response.url), response.headers.get("Content-Disposition"), response.headers.get("Content-Type", "")
        )
        source_filename = filename
        data = response.content

        # A MediaFire share URL can return a 200 HTML landing page with a real
        # PDF behind its Download button. Follow that same-site/MediaFire link
        # before deciding that the .pdf URL is a fake file.
        if extension == ".pdf" and not is_pdf(data) and is_html_response(response):
            recovered = await recover_pdf_response(client, response, args.retries, args.timeout)
            if recovered is not None:
                response = recovered
                disposition = response.headers.get("Content-Disposition")
                recovered_url = str(response.url)
                recovered_url_name = filename_from_url(recovered_url)
                filename, extension = choose_filename(
                    recovered_url,
                    disposition,
                    response.headers.get("Content-Type", ""),
                )
                if (
                    not disposition
                    and not recovered_url_name.lower().endswith(extension)
                    and source_filename.lower().endswith(extension)
                ):
                    # Some hosts use an opaque `/download` URL; keep the useful
                    # filename from the share URL when the response has no name.
                    filename = source_filename
                data = response.content

        validate = VALIDATORS.get(extension)
        if validate and not validate(data):
            if args.keep_rejected:
                rejected = out_dir / "rejected"
                rejected.mkdir(parents=True, exist_ok=True)
                (rejected / (safe_filename(filename) + ".bin")).write_bytes(data)
            return url, f"rejected: not a real {extension} file ({len(data)} bytes of {response.headers.get('Content-Type', 'unknown type')})"

        if args.verify_pdf and extension == ".pdf":
            try:
                import pikepdf  # noqa: PLC0415

                with pikepdf.open(io.BytesIO(data)):
                    pass
            except ImportError:
                return url, "verify skipped: pikepdf not installed"
            except Exception as error:  # noqa: BLE001
                return url, f"rejected: unreadable PDF ({error})"

        async with lock:
            path = unique_path(out_dir, filename, url, taken)
        path.write_bytes(data)

        if args.delay:
            await asyncio.sleep(args.delay)
        return url, f"saved ({path.name}, {len(data) // 1024} KB)"


async def download_all(entries: list[dict], out_dir: Path, history: History, args: argparse.Namespace) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    taken: set[str] = set(path.name for path in out_dir.glob("*") if path.is_file())
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    lock = asyncio.Lock()
    outcomes: dict[str, str] = {}

    limits = httpx.Limits(max_connections=max(1, args.concurrency), max_keepalive_connections=max(1, args.concurrency))
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*;q=0.8"},
        limits=limits,
        follow_redirects=True,
    ) as client:
        tasks = [
            asyncio.create_task(download_one(client, entry, out_dir, history, taken, args, semaphore, lock))
            for entry in entries
        ]
        try:
            from tqdm import tqdm  # noqa: PLC0415

            with tqdm(total=len(tasks), unit="file", desc="Downloading") as progress:
                for coroutine in asyncio.as_completed(tasks):
                    url, status = await coroutine
                    outcomes[url] = status
                    if not status.startswith("cached") and not status.startswith(("failed", "rejected")):
                        match = re.match(r"saved \((.+),", status)
                        if match:
                            history.data[url] = match.group(1)
                            history.save()
                    progress.set_postfix_str(status.split(" ")[0])
                    progress.update(1)
        except ImportError:
            for coroutine in asyncio.as_completed(tasks):
                url, status = await coroutine
                outcomes[url] = status
                match = re.match(r"saved \((.+),", status)
                if match:
                    history.data[url] = match.group(1)
    history.save()
    return outcomes


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download the book files found by search_crawl.py")
    parser.add_argument("entry_list", nargs="?", default=str(HERE / "search_entry_list.txt"))
    parser.add_argument("--out-dir", type=Path, default=HERE / "downloaded_files")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--delay", type=float, default=0.0, help="seconds to wait after each download")
    parser.add_argument("--limit", type=int, default=0, help="only download the first N entries")
    parser.add_argument("--verify-pdf", action="store_true", help="open each PDF with pikepdf to confirm it reads")
    parser.add_argument("--keep-rejected", action="store_true", help="save files that fail validation under rejected/")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    entries = read_entries(Path(args.entry_list))
    if args.limit:
        entries = entries[: args.limit]
    if not entries:
        print(f"No URLs found in {args.entry_list}", file=sys.stderr)
        return 1

    out_dir = args.out_dir
    history = History(out_dir / ".download-history.json")
    print(f"Entries: {len(entries)}; output: {out_dir}")

    outcomes = asyncio.run(download_all(entries, out_dir, history, args))

    counts: dict[str, int] = {}
    for status in outcomes.values():
        counts[status.split(":")[0].split(" ")[0]] = counts.get(status.split(":")[0].split(" ")[0], 0) + 1
    print("\nSummary:")
    for label, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {label + ':':12s} {count}")
    failures = [url for url, status in outcomes.items() if status.startswith(("failed", "rejected"))]
    if failures:
        print(f"\n{len(failures)} file(s) were not downloaded. Re-run to retry them.")
        for url in failures[:10]:
            print(f"  {url}\n    {outcomes[url]}")
    print(f"Downloaded files: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Download the book files found by search_crawl.py.

    uv run python/download.py                       # search_entry_list.txt
    uv run python/download.py entry_list.txt
    uv run python/download.py --concurrency=8 --verify-pdf

Reads a crawler TSV ("Book Name<TAB>URL<TAB>Source Page") or a plain URL list,
downloads concurrently with httpx, retries with tenacity, and refuses files
that only pretend to be PDFs: a lot of "PDF" links return an HTML login or
error page with a 200 status, which is worse than a failure because it looks
like a success.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import sys
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
VALIDATORS = {
    ".pdf": lambda data: data.lstrip()[:4] == b"%PDF",
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


async def fetch_with_retries(client: httpx.AsyncClient, url: str, retries: int, timeout: float) -> httpx.Response:
    """GET with exponential backoff + jitter (tenacity), honouring 429/503 waits."""
    from tenacity import AsyncRetrying

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(retries),
        wait=wait_exponential_jitter(initial=1, max=30, jitter=1),
        retry=retry_if_exception_type((httpx.HTTPError, httpx.StreamError)),
        reraise=True,
    ):
        with attempt:
            response = await client.get(url, timeout=timeout, follow_redirects=True)
            if response.status_code in (429, 503):
                wait = float(response.headers.get("Retry-After") or 0) or 5.0
                await asyncio.sleep(min(wait, 60))
                raise httpx.HTTPStatusError(f"HTTP {response.status_code}", request=response.request, response=response)
            response.raise_for_status()
            return response
    raise RuntimeError("unreachable")


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

                with pikepdf.open(__import__("io").BytesIO(data)):
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

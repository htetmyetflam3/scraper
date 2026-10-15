"""Download Scribd documents via the vendored Selenium exporter.

Upstream: https://github.com/themrsami/scribd-downloader @ 0396d95,
vendored verbatim as ``python/scribdl/scribd-downloader.py`` and driven
through stdin, one URL per process. This module is stdlib-only on
purpose: importing it never needs selenium or Chrome; those are only
required inside the exporter subprocess. Requires Google Chrome
installed (Selenium headless).
"""

from __future__ import annotations

import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIR = REPO_ROOT.parent / "scan"
SCRIBD_LINKS_FILE = SCAN_DIR / "scribd.txt"
EXPORTER = REPO_ROOT / "scribdl" / "scribd-downloader.py"
PDF_DIR = REPO_ROOT / "pdfs"


@dataclass
class ScribdSummary:
    downloaded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def is_scribd_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return host == "scribd.com" or host.endswith(".scribd.com")


def read_links_file(path: str | Path = SCRIBD_LINKS_FILE) -> list[str]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"No scribd links at {path}; run --entry first."
        ) from error
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]


def download_scribd(
    urls: list[str],
    output_dir: str | Path = PDF_DIR,
) -> ScribdSummary:
    """Export each URL to PDF in ``output_dir``; keeps going past failures."""
    summary = ScribdSummary()
    if not urls:
        print("[scribd] no links to download", flush=True)
        return summary
    if not EXPORTER.is_file():
        raise FileNotFoundError(f"Scribd exporter missing: {EXPORTER}")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    for position, url in enumerate(urls, start=1):
        print(f"[scribd] {position}/{len(urls)}: {url}", flush=True)
        if not is_scribd_url(url):
            print(f"[scribd-skip] not a scribd URL: {url}", flush=True)
            summary.failed.append(url)
            continue
        before = {path.name for path in out.glob("*.pdf")}
        try:
            # No timeout: a large document legitimately takes a long time and
            # the exporter already bounds itself (SCRIBD_* env knobs).
            completed = subprocess.run(
                [sys.executable, str(EXPORTER)],
                input=url + "\n",
                text=True,
                cwd=out,
            )
        except OSError as error:
            print(f"[scribd-error] {url}: {error}", flush=True)
            summary.failed.append(url)
            continue
        if completed.returncode != 0:
            print(
                f"[scribd-failed] {url} (exit {completed.returncode})",
                flush=True,
            )
            summary.failed.append(url)
            continue
        fresh = sorted({path.name for path in out.glob("*.pdf")} - before)
        if fresh:
            print(f"[scribd-saved] {fresh[-1]}", flush=True)
        else:
            print("[scribd-saved] done (no new PDF detected)", flush=True)
        summary.downloaded.append(url)
    print(
        f"[scribd] {len(summary.downloaded)} downloaded, "
        f"{len(summary.failed)} failed",
        flush=True,
    )
    return summary

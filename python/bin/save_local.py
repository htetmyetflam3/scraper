"""Local saving: filename hints, collision-free names, atomic file writes.

Split out of Dcrawler.py without logic changes; Dcrawler imports these back
under their original private names, so the download flow keeps writing
exactly what it always wrote.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import urllib.parse
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING

from pdf_support import linearize

if TYPE_CHECKING:
    from Dcrawler import DownloadCandidate


def _hostname(url: str) -> str | None:
    """Same host normalization as Dcrawler's; kept local to avoid a cycle."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port
    except ValueError:
        return None
    if not host:
        return None
    try:
        return host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return host.rstrip(".").lower()


def filename_hint(
    candidate: DownloadCandidate,
    response_url: str,
    headers: dict[str, str],
    extension: str,
) -> str:
    if candidate.provider == "mega" and candidate.label:
        hinted = candidate.label
    else:
        disposition = headers.get("content-disposition", "")
        if disposition:
            message = Message()
            message["content-disposition"] = disposition
            hinted = message.get_filename() or ""
        else:
            hinted = ""
        if not hinted:
            hinted = urllib.parse.unquote(
                Path(urllib.parse.urlsplit(response_url).path).name
            )
    if not hinted or hinted.lower() in {"file", "download", "view", "uc", ""}:
        hinted = candidate.label or ""
    hinted = hinted.replace("\\", "/").rsplit("/", 1)[-1]
    hinted = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", hinted).strip(" .")
    if not hinted:
        host = _hostname(candidate.url) or "book"
        digest = hashlib.sha256(candidate.url.encode("utf-8")).hexdigest()[:10]
        hinted = f"{host}-{digest}"
    suffix = "." + extension
    if hinted.lower().endswith((".pdf", ".docx")):
        hinted = hinted[: -len(Path(hinted).suffix)]
    return hinted[:180].rstrip(" .") + suffix


def reserve_unique_path(directory: Path, filename: str) -> Path:
    base = Path(filename)
    index = 1
    while True:
        candidate = (
            directory / base.name
            if index == 1
            else directory / f"{base.stem} ({index}){base.suffix}"
        )
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            index += 1
            continue
        os.close(descriptor)
        return candidate


def save_payload(
    data: bytes,
    extension: str,
    candidate: DownloadCandidate,
    response_url: str,
    headers: dict[str, str],
    output_dir: Path,
) -> tuple[Path | None, str]:
    extension = extension.lower()
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = filename_hint(candidate, response_url, headers, extension)
    fd, temp_name = tempfile.mkstemp(prefix=".download-", suffix=".tmp", dir=output_dir)
    os.close(fd)
    temp_path = Path(temp_name)
    destination: Path | None = None
    try:
        temp_path.write_bytes(data)
        if extension == "pdf":
            outcome = linearize(temp_path)
            if outcome.state in {"broken", "failed"}:
                return None, f"PDF validation/linearization failed: {outcome.detail}"
            detail = (
                "unlocked and linearized"
                if outcome.state == "linearized"
                else "already linearized"
            )
            if outcome.state == "locked":
                detail = "saved encrypted: requires a user password"
        else:
            detail = "saved DOCX"
        destination = reserve_unique_path(output_dir, filename)
        os.replace(temp_path, destination)
        return destination, detail
    except OSError as error:
        if destination is not None:
            destination.unlink(missing_ok=True)
        return None, str(error)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

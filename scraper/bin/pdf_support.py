"""PDF byte validation and in-place unlock/linearize; no directory sweep."""

from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pikepdf

PDF_HEADER_WINDOW = 1024  # readers commonly tolerate a header in the first 1 KiB


HTML_PREFIXES = (
    b"<!doctype html",
    b"<html",
    b"<head",
    b"<body",
    b"<meta",
    b"<title",
    b"<script",
    b"<form",
    b"<a",
    b"<div",
    b"<p",
    b"<span",
    b"<button",
    b"<table",
    b"<ul",
    b"<ol",
    b"<nav",
    b"<main",
    b"<article",
    b"<section",
    b"<iframe",
    b"<img",
)


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
        with pikepdf.open(io.BytesIO(data)):
            return True
    except Exception:  # unreadable response, not a PDF
        return False


PDF_MAGIC = b"%PDF"


TMP_SUFFIX = ".linearizing"

OBJECT_STREAM_MARK = b"/ObjStm"  # PDF 1.5 compressed-object streams: text parsers cannot read them
SCAN_CHUNK = 1 << 20


def _has_object_streams(path: Path) -> bool:
    """True when the file's bytes contain /ObjStm (chunked scan, marker overlap kept)."""
    keep = len(OBJECT_STREAM_MARK) - 1
    tail = b""
    with path.open("rb") as handle:
        while chunk := handle.read(SCAN_CHUNK):
            if OBJECT_STREAM_MARK in tail + chunk:
                return True
            tail = chunk[-keep:]
    return False


def _has_pdf_signature(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(PDF_MAGIC)) == PDF_MAGIC
    except OSError:
        return False


@dataclass
class Outcome:
    path: Path
    state: str  # linearized | already | locked | broken | failed
    before: int = 0
    after: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state in ("linearized", "already")


def open_pdf(
    path: Path, passwords: tuple[str, ...] = ()
) -> tuple["pikepdf.Pdf", str | None]:
    """Open `path`, trying each password in turn.

    Most "encrypted" PDFs in the wild are restrictions-only: they carry an owner
    password that stops printing or copying, but no user password, so they open
    with an empty one. Returns (pdf, password that worked).
    """
    last_error: Exception | None = None
    for candidate in (None, "", *passwords):
        try:
            if candidate is None:
                return pikepdf.open(path), None
            return pikepdf.open(path, password=candidate), candidate
        except pikepdf.PasswordError as error:
            last_error = error
    raise last_error or pikepdf.PasswordError(str(path))


def linearize(
    path: Path, dry_run: bool = False, passwords: tuple[str, ...] = ()
) -> Outcome:
    """Rewrite `path` as a linearized PDF without object streams, replacing it only if the result is sound.

    Saving without an `encryption=` argument drops encryption and copy/print
    restrictions after the PDF is opened with one of the supplied passwords.

    With dry_run the file is opened and judged but not written, so a dry run
    reports the same verdict a real run would.
    """
    info = path.stat()
    before = info.st_size
    mode = info.st_mode & 0o777  # mkstemp creates 0600; keep what the file had
    was_encrypted = False
    try:
        try:
            pdf, _password = open_pdf(path, passwords)
        except pikepdf.PasswordError:
            return Outcome(
                path,
                "locked",
                before,
                before,
                "encrypted and none of the supplied passwords fit",
            )
        with pdf:
            pages = len(pdf.pages)
            was_encrypted = bool(pdf.is_encrypted)
            with path.open("rb") as handle:
                header = handle.read(4096)
            if (
                not was_encrypted
                and b"/Linearized" in header
                and not _has_object_streams(path)
            ):
                return Outcome(path, "already", before, before)
            if dry_run:
                return Outcome(
                    path,
                    "linearized",
                    before,
                    before,
                    "would unlock" if was_encrypted else "",
                )
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=path.parent, prefix=path.name, suffix=TMP_SUFFIX
            )
            os.close(tmp_fd)
            tmp = Path(tmp_name)
            try:
                pdf.save(
                    tmp,
                    linearize=True,
                    object_stream_mode=pikepdf.ObjectStreamMode.disable,
                )  # no encryption= argument: the output is unlocked; no /ObjStm
                with pikepdf.open(tmp) as check:
                    if len(check.pages) != pages:
                        raise ValueError(
                            f"page count changed ({pages} -> {len(check.pages)})"
                        )
                    if check.is_encrypted:
                        raise ValueError("output is still encrypted")
                os.replace(tmp, path)
                os.chmod(path, mode)
            finally:
                if tmp.exists():
                    tmp.unlink()
    except Exception as error:  # noqa: BLE001 - a bad file must not stop the run
        return Outcome(
            path, "broken", before, before, str(error) or type(error).__name__
        )

    after = path.stat().st_size
    if not _has_pdf_signature(path):
        return Outcome(path, "failed", before, after, "output is not a valid PDF")
    return Outcome(
        path, "linearized", before, after, "unlocked" if was_encrypted else ""
    )

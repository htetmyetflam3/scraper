"""Offline tests for download.py.

The end-to-end test runs a local HTTP server serving a real PDF, an HTML page
pretending to be a PDF, and a DOCX, which is exactly the case the validator
exists for.
"""

from __future__ import annotations

import asyncio
import http.server
import socketserver
import sys
import threading
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # python/

from download import (  # noqa: E402
    choose_filename,
    download_all,
    filename_from_disposition,
    filename_from_url,
    read_entries,
    safe_filename,
)

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"
HTML_BYTES = b"<!doctype html><html><body>Sign in to continue</body></html>"


def make_docx() -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
    return buffer.getvalue()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/ok"):
            body, ctype = PDF_BYTES, "application/pdf"
        elif self.path.startswith("/fake"):
            body, ctype = HTML_BYTES, "application/pdf"  # lies about its type
        elif self.path.startswith("/docx"):
            body, ctype = make_docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif self.path.startswith("/named"):
            body, ctype = PDF_BYTES, "application/pdf"
            self.send_response(200)
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%E1%80%99%E1%80%BC%E1%80%94%E1%80%BA%E1%80%99%E1%80%AC.pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def test_safe_filename():
    assert safe_filename('a/b:c*d?e"f<g>h|i') == "a_b_c_d_e_f_g_h_i"
    assert safe_filename("  spaced  name.pdf  ") == "spaced name.pdf"
    assert safe_filename("..") == "download"


def test_filename_from_disposition_handles_utf8():
    assert filename_from_disposition("attachment; filename*=UTF-8''%E1%80%99%E1%80%BC%E1%80%94%E1%80%BA%E1%80%99%E1%80%AC.pdf") == "မြန်မာ.pdf"
    assert filename_from_disposition('attachment; filename="report final.pdf"') == "report final.pdf"
    assert filename_from_disposition(None) is None


def test_filename_from_url():
    assert filename_from_url("https://example.org/files/%E1%80%99%E1%80%BC%E1%80%94%E1%80%BA%E1%80%99%E1%80%AC%20book.pdf") == "မြန်မာ book.pdf"
    assert filename_from_url("https://example.org/") == "download"


def test_choose_filename_uses_content_type_when_url_has_no_extension():
    name, extension = choose_filename("https://example.org/get?id=1", None, "application/pdf")
    assert extension == ".pdf"
    assert name.endswith(".pdf")


def test_read_entries_tsv_and_plain(tmp_path):
    tsv = tmp_path / "entries.txt"
    tsv.write_text("Book Name\tURL\tSource Page\nA book\thttps://example.org/a.pdf\thttps://bing.com\nB\thttps://example.org/b.docx\tx\n", encoding="utf8")
    entries = read_entries(tsv)
    assert [entry["url"] for entry in entries] == ["https://example.org/a.pdf", "https://example.org/b.docx"]
    assert entries[0]["name"] == "A book"

    plain = tmp_path / "plain.txt"
    plain.write_text("https://example.org/a.pdf\n\n# comment\nhttps://example.org/b.pdf\n", encoding="utf8")
    assert [entry["url"] for entry in read_entries(plain)] == ["https://example.org/a.pdf", "https://example.org/b.pdf"]


def test_download_end_to_end(tmp_path, server, monkeypatch):
    """A real PDF is saved, an HTML page served as PDF is rejected, DOCX works."""
    monkeypatch.setenv("TQDM_DISABLE", "1")
    entries = [
        {"name": "real", "url": f"{server}/ok/myanmar-book.pdf"},
        {"name": "fake", "url": f"{server}/fake/login-wall.pdf"},
        {"name": "word", "url": f"{server}/docx/notes.docx"},
        {"name": "missing", "url": f"{server}/nope.pdf"},
    ]
    out_dir = tmp_path / "files"

    class Args:
        concurrency = 3
        retries = 2
        timeout = 30.0
        delay = 0
        verify_pdf = False
        keep_rejected = True

    from download import History

    outcomes = asyncio.run(download_all(entries, out_dir, History(out_dir / ".history.json"), Args()))

    assert "saved" in outcomes[f"{server}/ok/myanmar-book.pdf"]
    assert "saved" in outcomes[f"{server}/docx/notes.docx"]
    assert outcomes[f"{server}/fake/login-wall.pdf"].startswith("rejected")
    assert outcomes[f"{server}/nope.pdf"].startswith("failed")

    assert (out_dir / "myanmar-book.pdf").read_bytes() == PDF_BYTES
    assert (out_dir / "notes.docx").read_bytes()[:2] == b"PK"
    assert not (out_dir / "login-wall.pdf").exists()
    assert (out_dir / "rejected").is_dir()


def test_re_run_skips_downloaded_files(tmp_path, server, monkeypatch):
    monkeypatch.setenv("TQDM_DISABLE", "1")
    entries = [{"name": "real", "url": f"{server}/ok/second.pdf"}]
    out_dir = tmp_path / "files"

    class Args:
        concurrency = 2
        retries = 2
        timeout = 30.0
        delay = 0
        verify_pdf = False
        keep_rejected = False

    from download import History

    history = History(out_dir / ".history.json")
    first = asyncio.run(download_all(entries, out_dir, history, Args()))
    assert "saved" in first[f"{server}/ok/second.pdf"]

    history2 = History(out_dir / ".history.json")
    second = asyncio.run(download_all(entries, out_dir, history2, Args()))
    assert second[f"{server}/ok/second.pdf"].startswith("cached")

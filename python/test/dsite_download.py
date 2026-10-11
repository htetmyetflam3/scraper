"""Downloader tests: offline. The network fetch is replaced; unlock and linearize are real."""

import io
from pathlib import Path

import pikepdf
import pytest

import download
from entry_list import save_entries
import dsite_download as downloader

SITE = "https://books.example.org/"


def make_pdf(encryption=None) -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page()
    buffer = io.BytesIO()
    pdf.save(buffer, encryption=encryption)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, url, content, content_type="application/pdf"):
        self.url = url
        self.content = content
        self.headers = {"Content-Type": content_type}


class FakeFetcher:
    """Pages for the crawler. Every page read is recorded in `events`."""

    def __init__(self, pages, events):
        self.pages = pages
        self.events = events

    def fetch(self, url):
        if url.endswith("/robots.txt"):
            raise RuntimeError("no robots.txt")
        self.events.append(("page", url))
        if url not in self.pages:
            raise RuntimeError(f"404 {url}")
        return self.pages[url], url


@pytest.fixture
def files(monkeypatch):
    """Serve file URLs from a dict. Every download is recorded in `events`; a URL can map to bytes or an exception."""
    served: dict[str, object] = {}
    events: list[tuple[str, str]] = []

    async def fake_fetch(client, url, retries, timeout, request_headers=None):
        events.append(("download", url))
        content = served.get(url)
        if content is None:
            raise RuntimeError(f"404 {url}")
        if isinstance(content, Exception):
            raise content
        return FakeResponse(url, content[0], content[1]) if isinstance(content, tuple) else FakeResponse(url, content)

    monkeypatch.setattr(download, "fetch_with_retries", fake_fetch)
    served["events"] = events
    return served


def write_entries(path: Path, rows: dict[str, str]) -> None:
    save_entries(path, {url: {"url": url, "name": url.rsplit("/", 1)[-1] or "site", "source": src}
                        for url, src in rows.items()})


def args_for(tmp_path, *extra):
    return [str(tmp_path / "entry.txt"), f"--out-dir={tmp_path/'out'}",
            "--delay=0", "--jitter=0", *extra]


def test_a_file_is_downloaded_unlocked_linearized_and_saved(tmp_path, monkeypatch, files):
    url = "https://files.example.com/myanmar-novel.pdf"
    files[url] = make_pdf(pikepdf.Encryption(owner="owner", user=""))  # restrictions only: opens with ""
    write_entries(tmp_path / "entry.txt", {url: "src"})
    assert downloader.main(args_for(tmp_path)) == 0
    saved = tmp_path / "out" / "myanmar-novel.pdf"
    assert saved.exists(), "the file is on disk"
    with pikepdf.open(saved) as pdf:
        assert not pdf.is_encrypted, "the saved file is unlocked"
    assert b"/Linearized" in saved.read_bytes()[:4096], "the saved file is linearized"


def test_a_legacy_pdf_header_is_accepted_and_normalized(tmp_path, files):
    url = "https://files.example.com/myanmar-old.pdf"
    files[url] = b"old-format-prefix " + make_pdf()
    write_entries(tmp_path / "entry.txt", {url: "src"})
    assert downloader.main(args_for(tmp_path)) == 0
    saved = tmp_path / "out" / "myanmar-old.pdf"
    assert saved.read_bytes().startswith(b"%PDF-"), "linearize.py rewrites the readable legacy PDF"
    with pikepdf.open(saved) as pdf:
        assert len(pdf.pages) == 1


def test_a_pdf_with_a_user_password_is_kept_and_reported(tmp_path, capsys, files):
    url = "https://files.example.com/myanmar-locked.pdf"
    files[url] = make_pdf(pikepdf.Encryption(owner="owner", user="secret"))
    write_entries(tmp_path / "entry.txt", {url: "src"})
    assert downloader.main(args_for(tmp_path)) == 0
    assert (tmp_path / "out" / "myanmar-locked.pdf").exists(), "kept as downloaded, not deleted"
    assert "locked" in capsys.readouterr().out


def test_a_password_from_the_command_line_unlocks_it(tmp_path, files):
    url = "https://files.example.com/myanmar-locked.pdf"
    files[url] = make_pdf(pikepdf.Encryption(owner="owner", user="secret"))
    write_entries(tmp_path / "entry.txt", {url: "src"})
    assert downloader.main(args_for(tmp_path, "--password=secret")) == 0
    with pikepdf.open(tmp_path / "out" / "myanmar-locked.pdf") as pdf:
        assert not pdf.is_encrypted


def test_a_docx_is_saved_without_linearizing(tmp_path, files):
    url = "https://files.example.com/myanmar-notes.docx"
    files[url] = (b"PK\x03\x04" + b"docx body", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    write_entries(tmp_path / "entry.txt", {url: "src"})
    assert downloader.main(args_for(tmp_path)) == 0
    assert (tmp_path / "out" / "myanmar-notes.docx").read_bytes().startswith(b"PK\x03\x04")


def test_each_file_is_saved_before_the_next_page_is_read(tmp_path, monkeypatch, files):
    events: list = files["events"]
    a_url, b_url = SITE + "myanmar-a.pdf", SITE + "myanmar-b.pdf"
    files[a_url] = make_pdf()
    files[b_url] = make_pdf()
    pages = {
        SITE: '<a href="myanmar-a.pdf">A</a> <a href="p1.html">next</a>',
        SITE + "p1.html": '<a href="myanmar-b.pdf">B</a>',
    }
    write_entries(tmp_path / "entry.txt", {SITE: "src"})
    monkeypatch.setattr(downloader, "HttpFetcher", lambda **_: FakeFetcher(pages, events))
    assert downloader.main(args_for(tmp_path)) == 0

    file_events = [event for event in events if event[0] in ("page", "download")]
    assert file_events == [("page", SITE), ("download", a_url), ("page", SITE + "p1.html"), ("download", b_url)], \
        "download a.pdf before reading p1.html, then download b.pdf"
    assert (tmp_path / "out" / "myanmar-a.pdf").exists() and (tmp_path / "out" / "myanmar-b.pdf").exists()


def test_wordpress_share_buttons_do_not_stall_the_downloader(tmp_path, monkeypatch, files):
    events: list = files["events"]
    pages = {
        SITE: '<a href="policy-terms/?share=telegram">Share on Telegram</a>'
              '<a href="policy-terms/?share=jetpack-whatsapp">WhatsApp</a>'
              '<a href="documents/book.pdf?share=telegram">PDF share button</a>'
              '<a href="after.html">Next</a>',
        SITE + "after.html": "<p>finished</p>",
    }
    write_entries(tmp_path / "entry.txt", {SITE: "src"})
    monkeypatch.setattr(downloader, "HttpFetcher", lambda **_: FakeFetcher(pages, events))
    assert downloader.main(args_for(tmp_path)) == 0
    assert ("page", SITE + "after.html") in events, "the crawl continues past the share buttons"
    assert not any("?share=" in url for kind, url in events if kind == "page")
    assert not any("?share=" in url for kind, url in events if kind == "download")


def test_a_button_that_names_a_file_is_downloaded(tmp_path, monkeypatch, files):
    events: list = files["events"]
    file_url = SITE + "files/myanmar-button.pdf"
    files[file_url] = make_pdf()
    pages = {SITE: "<button onclick=\"location.href='files/myanmar-button.pdf'\">Download</button>"}
    write_entries(tmp_path / "entry.txt", {SITE: "src"})
    monkeypatch.setattr(downloader, "HttpFetcher", lambda **_: FakeFetcher(pages, events))
    assert downloader.main(args_for(tmp_path)) == 0
    assert (tmp_path / "out" / "myanmar-button.pdf").exists()


def test_a_file_already_downloaded_is_not_fetched_again(tmp_path, files):
    events: list = files["events"]
    url = "https://files.example.com/myanmar-novel.pdf"
    files[url] = make_pdf()
    write_entries(tmp_path / "entry.txt", {url: "src"})
    assert downloader.main(args_for(tmp_path)) == 0
    events.clear()
    assert downloader.main(args_for(tmp_path)) == 0
    assert ("download", url) not in events, "the history skips a file already saved"


def test_a_response_that_is_not_a_pdf_is_rejected_and_the_crawl_goes_on(tmp_path, monkeypatch, files):
    events: list = files["events"]
    bad = SITE + "myanmar-bad.pdf"
    files[bad] = (b"<html>login page</html>", "text/html")
    pages = {SITE: '<a href="myanmar-bad.pdf">bad</a> <a href="p1.html">next</a>', SITE + "p1.html": "<p>after</p>"}
    write_entries(tmp_path / "entry.txt", {SITE: "src"})
    monkeypatch.setattr(downloader, "HttpFetcher", lambda **_: FakeFetcher(pages, events))
    assert downloader.main(args_for(tmp_path)) == 0
    assert not (tmp_path / "out" / "myanmar-bad.pdf").exists()
    assert ("page", SITE + "p1.html") in events, "the crawl went on after the rejected file"


def test_a_failed_download_does_not_stop_the_run(tmp_path, monkeypatch, files):
    events: list = files["events"]
    files[SITE + "myanmar-down.pdf"] = RuntimeError("network down")
    pages = {SITE: '<a href="myanmar-down.pdf">x</a> <a href="p1.html">next</a>', SITE + "p1.html": "<p>after</p>"}
    write_entries(tmp_path / "entry.txt", {SITE: "src"})
    monkeypatch.setattr(downloader, "HttpFetcher", lambda **_: FakeFetcher(pages, events))
    assert downloader.main(args_for(tmp_path)) == 0
    assert ("page", SITE + "p1.html") in events


def test_an_empty_entry_list_is_an_error(tmp_path):
    assert downloader.main(args_for(tmp_path)) == 1


def test_scribd_is_not_downloaded(tmp_path, monkeypatch, files):
    events: list = files["events"]
    pages = {SITE: '<a href="https://www.scribd.com/document/1/myanmar-x">Myanmar</a>'}
    write_entries(tmp_path / "entry.txt", {SITE: "src"})
    monkeypatch.setattr(downloader, "HttpFetcher", lambda **_: FakeFetcher(pages, events))
    assert downloader.main(args_for(tmp_path)) == 0
    assert not [event for event in events if event[0] == "download"]


def test_the_downloader_has_no_page_limit_by_default(monkeypatch):
    import math

    seen = {}

    def fake_crawl(url, fetcher, **kwargs):
        seen["max_pages"] = kwargs["max_pages"]
        seen["preflight"] = kwargs.get("preflight")
        return {"pages": 0, "pdf": 0}

    monkeypatch.setattr(downloader, "crawl_site", fake_crawl)
    monkeypatch.setattr(downloader, "HttpFetcher", lambda **_: None)
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    write_entries(tmp / "entry.txt", {SITE: "src"})
    assert downloader.main(args_for(tmp)) == 0
    assert seen["max_pages"] == math.inf, "default 0 means no page limit"
    assert seen["preflight"] is None, "the crawler-only large-site guard is not applied to downloader rows"

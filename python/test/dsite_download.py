"""Downloader tests: offline. download.main is replaced by a recorder; pages come from a fake fetcher."""

from pathlib import Path

import pytest

from crawler import read_book_list, write_book_list
import dsite_download as downloader

SITE = "https://books.example.org/"


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    def fetch(self, url):
        if url.endswith("/robots.txt"):
            raise RuntimeError("no robots.txt")
        self.fetched.append(url)
        if url not in self.pages:
            raise RuntimeError(f"404 {url}")
        return self.pages[url], url


def write_entries(path: Path, rows: dict[str, str]) -> None:
    records = {}
    for url in rows:
        records[url] = {"url": url, "name": url.rsplit("/", 1)[-1] or "site", "source": rows[url]}
    write_book_list(path, records)


@pytest.fixture
def recorder(monkeypatch):
    """Replace download.main: record each call and the file links waiting in the queue file."""
    calls = []

    def fake_main(argv):
        queue_path = Path(argv[0])
        links = sorted(read_book_list(queue_path).keys()) if queue_path.exists() else []
        calls.append(links)
        return 0

    monkeypatch.setattr(downloader.download, "main", fake_main)
    return calls


def args_for(tmp_path, *extra):
    return [f"--entry-list={tmp_path/'entry.txt'}", f"--queue={tmp_path/'queue.txt'}",
            "--delay=0", "--jitter=0", *extra]


def test_a_file_row_is_downloaded_directly(tmp_path, monkeypatch, recorder):
    write_entries(tmp_path / "entry.txt", {"https://files.example.com/myanmar-novel.pdf": "https://www.google.com/x"})
    monkeypatch.setattr(downloader, "HttpFetcher", lambda **_: FakeFetcher({}))
    assert downloader.main(args_for(tmp_path)) == 0
    assert recorder == [["https://files.example.com/myanmar-novel.pdf"]]


def test_a_site_row_is_crawled_and_its_files_downloaded_as_found(tmp_path, monkeypatch, recorder):
    write_entries(tmp_path / "entry.txt", {SITE: "https://www.google.com/x"})
    fetcher = FakeFetcher({
        SITE: '<a href="p1.html">next</a> <a href="p2.html">next</a>',
        SITE + "p1.html": '<a href="myanmar-a.pdf">Myanmar A</a>',
        SITE + "p2.html": '<a href="myanmar-b.pdf">Myanmar B</a>',
    })
    monkeypatch.setattr(downloader, "HttpFetcher", lambda **_: fetcher)
    assert downloader.main(args_for(tmp_path)) == 0
    downloaded = [link for call in recorder for link in call]
    assert SITE + "myanmar-a.pdf" in downloaded and SITE + "myanmar-b.pdf" in downloaded
    assert len(recorder) >= 2, "the download ran while the site was being crawled, once per page with a file"


def test_a_failed_download_does_not_stop_the_run(tmp_path, monkeypatch):
    write_entries(tmp_path / "entry.txt", {"https://files.example.com/myanmar-novel.pdf": "src",
                                           SITE: "src"})

    def broken(argv):
        raise OSError("network down")

    monkeypatch.setattr(downloader.download, "main", broken)
    fetcher = FakeFetcher({SITE: '<a href="p1.html">next</a>', SITE + "p1.html": "<p>after</p>"})
    monkeypatch.setattr(downloader, "HttpFetcher", lambda **_: fetcher)
    assert downloader.main(args_for(tmp_path)) == 0
    assert SITE + "p1.html" in fetcher.fetched, "the crawl went on after the download error"


def test_an_empty_entry_list_is_an_error(tmp_path):
    assert downloader.main(args_for(tmp_path)) == 1


def test_the_downloader_does_not_register_scribd(tmp_path, monkeypatch, recorder):
    write_entries(tmp_path / "entry.txt", {SITE: "src"})
    fetcher = FakeFetcher({SITE: '<a href="https://www.scribd.com/document/1/x">Myanmar</a>'})
    monkeypatch.setattr(downloader, "HttpFetcher", lambda **_: fetcher)
    assert downloader.main(args_for(tmp_path)) == 0
    assert all("scribd" not in link for call in recorder for link in call), "Scribd links are not downloaded"

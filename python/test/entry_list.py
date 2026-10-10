"""Entry list tests: hand-written lists are read, and the crawler never wipes rows on disk."""

import dsite
import dsite_download
from entry_list import read_entries, save_entries


def test_a_hand_written_list_without_a_header_is_read(tmp_path):
    path = tmp_path / "mine.txt"
    path.write_text("https://a.example.com/one.pdf\nhttps://b.example.com/\nnot a link\n", encoding="utf8")
    rows = read_entries(path)
    assert set(rows) == {"https://a.example.com/one.pdf", "https://b.example.com/"}


def test_name_and_source_columns_are_read_when_given(tmp_path):
    path = tmp_path / "mine.txt"
    path.write_text("Book Name\tURL\tSource Page\nMy book\thttps://a.example.com/x.pdf\tmanual\n", encoding="utf8")
    row = read_entries(path)["https://a.example.com/x.pdf"]
    assert row == {"name": "My book", "url": "https://a.example.com/x.pdf", "source": "manual"}


def test_windows_text_is_read_utf8_bom_and_utf16(tmp_path):
    utf8_bom = tmp_path / "bom.txt"
    utf8_bom.write_bytes("\ufeffhttps://a.example.com/one.pdf\r\n".encode("utf-8"))
    assert list(read_entries(utf8_bom)) == ["https://a.example.com/one.pdf"]
    utf16 = tmp_path / "utf16.txt"
    utf16.write_bytes("https://b.example.com/two.pdf\r\n".encode("utf-16"))
    assert list(read_entries(utf16)) == ["https://b.example.com/two.pdf"]


def test_saving_keeps_rows_that_are_only_on_disk(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("https://manual.example.com/added-by-hand.pdf\n", encoding="utf8")
    save_entries(path, {"https://crawl.example.com/": {"name": "crawl", "url": "https://crawl.example.com/",
                                                         "source": "search"}})
    rows = read_entries(path)
    assert "https://manual.example.com/added-by-hand.pdf" in rows, "a hand-added row is not wiped"
    assert "https://crawl.example.com/" in rows


def test_a_crawl_run_keeps_a_hand_written_list(tmp_path, monkeypatch):
    path = tmp_path / "entry.txt"
    path.write_text("https://manual.example.com/added-by-hand.pdf\n", encoding="utf8")
    monkeypatch.setenv("SERPAPI", "")
    monkeypatch.setattr(dsite, "ENV_FILE", tmp_path / "none.env")
    dsite.main([f"--entry-list={path}", f"--scribd-list={tmp_path/'s.txt'}",
                f"--usage-file={tmp_path/'u.json'}", f"--search-log={tmp_path/'sr.txt'}",
                "--search-terms=Myanmar PDF"])
    assert "https://manual.example.com/added-by-hand.pdf" in read_entries(path)


def test_the_downloader_reads_the_list_given_on_the_command_line(tmp_path):
    args = dsite_download.parse_args([str(tmp_path / "mine.txt")])
    assert args.entry_file == tmp_path / "mine.txt"


def test_the_downloader_defaults_to_the_crawler_entry_list():
    assert dsite_download.parse_args([]).entry_file == dsite_download.DEFAULT_ENTRY_LIST
    assert dsite_download.DEFAULT_ENTRY_LIST.name == "site_entry_list.txt"


def test_the_downloader_does_not_import_the_crawler_script():
    from pathlib import Path

    source = Path(dsite_download.__file__).read_text(encoding="utf8")
    assert "import dsite\n" not in source and "from dsite " not in source, "the two scripts are separate"

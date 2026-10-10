"""The entry list: written by the crawler (dsite.py), read by the downloader (dsite_download.py).

Format: tab-separated, with the header "Book Name<TAB>URL<TAB>Source Page". A list you
write by hand may leave the header out, and may have one URL per line. Every http(s)
URL in the file is a row, and any other line is ignored.

Writing never drops a row. The crawler merges its rows into what is already on disk,
so rows you add by hand (during a run as well) are kept.
"""

from __future__ import annotations

import re
from pathlib import Path

from crawler import clean_cell

HEADER = "Book Name\tURL\tSource Page"
URL_IN_TEXT = re.compile(r"https?://\S+", re.I)


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16 with BOM (Windows Notepad "Unicode")
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")  # UTF-8, with or without BOM


def is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))


def parse_line(line: str) -> dict | None:
    if "\t" in line:
        cells = [cell.strip() for cell in line.split("\t")]
        position = next((index for index, cell in enumerate(cells) if is_url(cell)), None)
        if position is None:
            return None  # the header, or a line with no URL
        url = cells[position]
        name = cells[0] if position > 0 else ""
        source = cells[position + 1] if position + 1 < len(cells) else ""
        return {"name": name, "url": url, "source": source}
    match = URL_IN_TEXT.search(line)
    if not match:
        return None
    name = line[:match.start()].strip()
    return {"name": name, "url": match.group(0), "source": ""}


def read_entries(path: Path) -> dict[str, dict]:
    """url -> {name, url, source}. A missing file is an empty list."""
    records: dict[str, dict] = {}
    if not path.exists():
        return records
    for line in read_text(path).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        row = parse_line(line)
        if row is None:
            continue
        row["name"] = clean_cell(row["name"]) or row["url"]
        row["source"] = clean_cell(row["source"])
        records[row["url"]] = row
    return records


def save_entries(path: Path, records: dict[str, dict]) -> None:
    """Merge `records` into the file on disk and write it back. Rows only on disk are kept."""
    merged = read_entries(path)
    for url, record in records.items():
        merged[url] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [HEADER]
    for url in sorted(merged):
        row = merged[url]
        lines.append(f"{clean_cell(row['name']) or url}\t{url}\t{clean_cell(row['source'])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf8")

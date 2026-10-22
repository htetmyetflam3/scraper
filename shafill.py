#!/usr/bin/env python3
"""Round one: fill books.parquet's sha256 column from the PDFs on the shared host.

Rows written before the sha256 column existed carry no hash, so the duplicate
guard in the parquet writer has nothing to match them against. Run this once to
give every existing row the hash of the PDF its text came from.

    uv run shafill.py              # hash the host's convertable/ dir, fill the column
    uv run shafill.py --dry-run    # report only, write nothing
    uv run shafill.py --force      # also overwrite a hash that disagrees with the host
    uv run shafill.py /other/pdfs  # a different pdfs/ dir than the repo's own

What it does: connects to the shared host with the same SSH_* settings
save_remote.py uses, hashes every PDF in ``<SSH_DIR>/convertable/`` in ONE ssh
round trip, matches each hash to a row by the row's ``book`` value, and writes
it into that row's sha256 column. Then it reports what it could not match.

Rules:
- It touches the sha256 column ONLY. author, book, text and segmented come back
  exactly as they were. No row is added, removed or re-segmented.
- Nothing is deleted, here or on the host. The host is only ever read from.
- A row already holding a different hash is left alone and reported as a
  conflict, because that means the file on the host changed or two books were
  matched to one file. --force overwrites it.
- Books whose PDF is no longer on the host fall back to scan/uploaded-host.txt,
  which recorded the hash at upload time. Those are reported separately so you
  can see which hashes came from a live file and which from history.
- A hash it cannot find anywhere is reported, not guessed. Empty sha256 stays
  empty, and that row simply remains outside the guard until you deal with it.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "crawler" / "bin"))

# _run is the same ssh wrapper save_remote.py uses - same key handling, same
# BatchMode/ControlMaster options - so this connects exactly like an upload does.
from save_remote import (  # noqa: E402
    UPLOAD_LEDGER,
    RemoteError,
    _run,
    load_config,
)
from text_collect import (  # noqa: E402
    CONVERT_FINAL_NAME,
    PARQUET_DIR_NAME,
    PARQUET_FILE_NAME,
    set_parquet_sha,
)

SCAN_TIMEOUT = 3600.0  # hashing a whole month of books in one pass can be slow
DISAMBIGUATED = re.compile(r"(.+)-([0-9a-f]{8})$")


def read_rows(pdfs_dir):
    """(books, texts, shas) out of the collection, tolerating a file with no sha."""
    import pyarrow.parquet as pq

    final = pdfs_dir / PARQUET_DIR_NAME / PARQUET_FILE_NAME
    if not final.exists():
        return None, None, None
    table = pq.read_table(final)
    names = set(table.column_names)
    books = table["book"].to_pylist()
    texts = table["text"].to_pylist()
    shas = table["sha256"].to_pylist() if "sha256" in names else [""] * len(books)
    return books, texts, [value or "" for value in shas]


def host_hashes(config):
    """Every PDF's sha256 in the host's convertable/ dir, in ONE ssh round trip.

    ``find -exec sha256sum {} +`` batches the files into as few sha256sum
    processes as the host allows, so this is one connection for the whole
    directory instead of one connection per book.
    """
    remote_dir = f"{config.directory}/{CONVERT_FINAL_NAME}"
    command = (
        f"find {shlex.quote(remote_dir)} -type f -iname '*.pdf' -exec sha256sum {{}} +"
    )
    print(f"[sha] ssh {config.target}:{config.port} -> hashing {remote_dir}/ ...")
    result = _run(config, command, timeout=SCAN_TIMEOUT)
    if result.returncode != 0 and not (result.stdout or "").strip():
        detail = (result.stderr or "").strip().splitlines()
        raise RemoteError(
            f"could not hash {remote_dir}/ on the host"
            + (f": {detail[-1]}" if detail else f" (ssh exit {result.returncode})")
        )
    hashes: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        parts = line.split(None, 1)  # "<64 hex>  <path>"; a path may hold spaces
        if len(parts) != 2 or len(parts[0]) != 64:
            continue
        name = PurePosixPath(parts[1].strip()).name
        hashes[name] = parts[0]
        hashes.setdefault(_stem(name), parts[0])
    print(f"[sha] {len(hashes) // 2} PDF(s) hashed on the host")
    return hashes


def ledger_hashes():
    """name -> sha256 from scan/uploaded-host.txt, for books gone from the host.

    One TAB-separated record per upload: sha256, name, remote path, bytes, stamp.
    """
    hashes: dict[str, str] = {}
    try:
        lines = UPLOAD_LEDGER.read_text(encoding="utf-8").splitlines()
    except OSError:
        return hashes
    for line in lines:
        fields = line.split("\t")
        if len(fields) < 2 or len(fields[0].strip()) != 64:
            continue
        name = fields[1].strip()
        hashes[name] = fields[0].strip()
        hashes.setdefault(_stem(name), fields[0].strip())
    return hashes


def _stem(name: str) -> str:
    return name[:-4] if name.lower().endswith(".pdf") else name


def match(book: str, tables: list[tuple[dict, str]]) -> tuple[str, str]:
    """(sha256, where) for one ``book`` value; where is 'host', 'ledger' or ''.

    ``book`` is the PDF's filename without its extension, so that is tried first.
    The OCR service disambiguates two books that share a filename as
    ``<name>-<first 8 of the sha>``, so a key shaped like that is matched on both
    the name AND the hash prefix - the same rule run.py used to build it.
    """
    keys = [book, f"{book}.pdf", book.lower(), f"{book.lower()}.pdf"]
    shaped = DISAMBIGUATED.fullmatch(book)
    for table, where in tables:
        if shaped:
            base, prefix = shaped.groups()
            for key in (base, f"{base}.pdf"):
                digest = table.get(key, "")
                if digest.startswith(prefix):
                    return digest, where
        for key in keys:
            if table.get(key):
                return table[key], where
    return "", ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fill books.parquet's sha256 column from the shared host."
    )
    parser.add_argument(
        "pdfs", nargs="?", default=str(ROOT / "pdfs"),
        help="the pdfs/ dir holding parquet/books.parquet (default: this repo's own)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be written, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="overwrite a hash that disagrees with the host")
    args = parser.parse_args(argv)

    pdfs_dir = Path(args.pdfs).expanduser()
    if not pdfs_dir.is_absolute():
        pdfs_dir = Path.cwd() / pdfs_dir
    books, texts, shas = read_rows(pdfs_dir)
    if books is None:
        print(f"[sha] no {PARQUET_DIR_NAME}/{PARQUET_FILE_NAME} under {pdfs_dir} - "
              "nothing to fill yet")
        return 1
    print(f"[sha] {len(books)} row(s) in {pdfs_dir / PARQUET_DIR_NAME / PARQUET_FILE_NAME}")

    try:
        config = load_config()
        host = host_hashes(config)
    except RemoteError as error:
        print(f"[sha] {error}")
        print("[sha] falling back to the upload ledger only")
        host = {}
    ledger = ledger_hashes()
    if ledger:
        print(f"[sha] {len(ledger) // 2} name(s) in the upload ledger as a fallback")

    tables = [(host, "host"), (ledger, "ledger")]
    mapping: dict[str, str] = {}
    where = Counter()
    unmatched: list[str] = []
    for book in books:
        digest, source = match(book, tables)
        if digest:
            mapping[book] = digest
            where[source] += 1
        else:
            unmatched.append(book)

    # The duplication round one exists to expose: one text recorded under two books.
    by_text = Counter(text for text in texts if text)
    doubled = sorted(
        {books[i] for i, text in enumerate(texts) if text and by_text[text] > 1}
    )

    print(f"[sha] matched {len(mapping)} row(s): {where['host']} from the host, "
          f"{where['ledger']} from the ledger")
    if unmatched:
        shown = ", ".join(unmatched[:10]) + (" ..." if len(unmatched) > 10 else "")
        print(f"[sha] {len(unmatched)} row(s) with no PDF on the host and no ledger "
              f"entry: {shown}")
    if doubled:
        print(f"[sha] {len(doubled)} row(s) share their text with another book - the "
              f"writer's guard refuses these from now on: "
              f"{', '.join(doubled[:10])}{' ...' if len(doubled) > 10 else ''}")

    if args.dry_run:
        print("[sha] --dry-run: nothing written")
        return 0
    if not mapping:
        print("[sha] nothing matched, so nothing written")
        return 0

    stats = set_parquet_sha(pdfs_dir, mapping, force=args.force)
    if stats is None:
        print("[sha] the parquet could not be read or written")
        return 1
    if unmatched:
        print(f"[sha] {stats['absent']} hash(es) had no row to go into")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[sha] interrupted; the parquet was left as it was")
        sys.exit(130)

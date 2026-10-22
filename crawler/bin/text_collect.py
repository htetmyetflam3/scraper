"""Collected-text journal and the run-end flush to Parquet.

During a download run every convertable PDF (Unicode or Zawgyi-One text)
contributes two things the moment it is saved:

* one JSON line in ``scan/collected.jsonl`` — filename, sha256, encoding,
  pages, the converted + normalised text, and the broken-text scan counts.
  The journal is append-only and fsynced per document, so an interrupted
  run loses nothing and the next run's flush picks the leftovers up;
* the PDF itself, filed directly in ``pdfs/convertable/`` so the background
  uploader pushes convertable books to remote storage one by one immediately
  (just like ``scanned/`` and ``old/``).

When the download run finishes, :func:`flush` sends the whole collected
text as ONE stream through a single segmentor process
(``segment_bridge.mjs`` — the Engine's MonoSyllabism syllable segmentor),
splits the result back per document, and writes one Parquet file for the run
in ``pdfs/parquet/`` (kept on the VPS; never uploaded to shared host storage).
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = BIN_DIR.parent.parent
SCAN_DIR = REPO_ROOT / "scan"
JOURNAL_FILE = SCAN_DIR / "collected.jsonl"
BRIDGE_SCRIPT = BIN_DIR / "segment_bridge.mjs"

CONVERT_STAGE_NAME = ".convert"      # legacy staging dir
CONVERT_FINAL_NAME = "convertable"   # convertable PDFs folder
PARQUET_DIR_NAME = "parquet"
PARQUET_FILE_NAME = "books.parquet"
PARQUET_SHA_COLUMN = "sha256"   # which PDF a row came from; the duplicate guard

DOC_SEPARATOR = "\u001d"  # GROUP SEPARATOR between documents, both directions
_PARQUET_LOCK = threading.Lock()


@contextmanager
def _parquet_file_lock(parquet_dir: Path):
    """Serialise appends across PROCESSES, not just threads.

    The collection is one cumulative books.parquet and two independent processes
    append to it: the scraper (save_remote.push_file) and the continuous-OCR
    service. A threading lock cannot see the other process, so two concurrent
    read-modify-write cycles would each start from the same table and the slower
    one would silently drop the other's row.
    """
    lock_path = parquet_dir / ("." + PARQUET_FILE_NAME + ".lock")
    with lock_path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

# One Parquet file per flushed run keeps batches small and committed: the
# file only appears (rename) after every row in it is complete.
_PARQUET_SCHEMA_COLUMNS = (
    "filename", "sha256", "encoding", "pages",
    "text", "segmented", "scan_counts", "source_url", "collected_at",
)


def append_document(
    filename: str,
    sha256: str,
    encoding: str,
    pages: int,
    text: str,
    scan_counts: dict,
    source_url: str = "",
    author: str = "",
) -> None:
    """Append one convertable document to the journal, durably.

    Raises OSError when the line cannot be written — the caller must NOT
    stage the PDF in that case (a staged file without a journal row would
    never reach convertable/).
    """
    record = {
        "filename": filename,
        "author": author,
        "sha256": sha256,
        "encoding": encoding,
        "pages": pages,
        "text": text,
        "scan_counts": scan_counts,
        "source_url": source_url,
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    with JOURNAL_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_journal() -> list[dict]:
    try:
        lines = JOURNAL_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue  # a torn tail line from a crash; the doc never staged
    return records


def _find_node22() -> str | None:
    """Return a Node 22+ binary path, checking PATH and common version-manager dirs."""
    candidates: list[Path] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            candidates.append(Path(entry) / "node")
    home = Path.home()
    for root in (home, Path("/root")):
        candidates.extend(sorted(root.glob(".nvm/versions/node/v*/bin/node"), reverse=True))
        candidates.extend(sorted(root.glob(".local/share/fnm/node-versions/v*/installation/bin/node"), reverse=True))
        candidates.append(root / ".volta/bin/node")
    candidates.extend((Path("/usr/local/bin/node"), Path("/usr/bin/node")))

    seen: set[str] = set()
    for cand in candidates:
        try:
            resolved = str(cand.resolve())
        except OSError:
            continue
        if resolved in seen or not cand.is_file() or not os.access(cand, os.X_OK):
            continue
        seen.add(resolved)
        try:
            proc = subprocess.run([str(cand), "--version"], capture_output=True, text=True, timeout=5)
            raw = (proc.stdout or proc.stderr or "").strip().lstrip("v")
            if int(raw.split(".", 1)[0]) >= 22:
                return str(cand)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    return shutil.which("node")


def _segment_documents(texts: list[str], log) -> list[str] | None:
    """One segmentor process for the whole run's text; None on failure."""
    node = _find_node22()
    if node is None:
        log("[convert-warn] node not found; segmentation postponed")
        return None
    giant = DOC_SEPARATOR.join(texts)
    fd, out_name = tempfile.mkstemp(prefix=".segment-", suffix=".tmp",
                                    dir=SCAN_DIR)
    os.close(fd)
    out_path = Path(out_name)
    env = dict(os.environ)
    node_dir = str(Path(node).resolve().parent)
    env["PATH"] = f"{node_dir}{os.pathsep}{env.get('PATH', '')}"
    try:
        proc = subprocess.run(
            [node, str(BRIDGE_SCRIPT), str(out_path)],
            input=giant.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
            timeout=3600,
        )
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace")[-400:]
            log(f"[convert-warn] segmentor exited {proc.returncode}: {tail}")
            return None
        segmented = out_path.read_text(encoding="utf-8").split(DOC_SEPARATOR)
        if len(segmented) != len(texts):
            log(
                "[convert-warn] segmentor returned "
                f"{len(segmented)} documents for {len(texts)} sent"
            )
            return None
        return segmented
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"[convert-warn] segmentor failed: {error}")
        return None
    finally:
        out_path.unlink(missing_ok=True)


def parse_convertable_book(path: Path, log=print) -> tuple[str, str, str] | None:
    """Parse a convertable PDF and segment its text for a single Parquet row."""
    from pdf_classify import classify_pdf

    name = path.name
    book = name[:-4] if name.lower().endswith(".pdf") else name
    text = ""
    try:
        verdict = classify_pdf(path.read_bytes())
        text = verdict.text or ""
    except Exception as error:  # noqa: BLE001
        log(f"[convert-warn] parser failed on {name}: {error}")
    if not text:
        for record in _read_journal():
            if record.get("filename") == name and record.get("text"):
                text = record["text"]
                break
    if not text:
        log(f"[convert-warn] no text extracted from {name}")
        return None
    segmented = _segment_documents([text], log)
    if segmented is None or not segmented:
        return None
    return book, text, segmented[0]


def _parquet_columns(pq, sources: list[Path], log):
    """The collection's columns out of `sources`, tolerating older files.

    Files written before the sha256 column existed simply have no value for it, so a
    legacy books.parquet or an old batch-*.parquet still reads; those rows come back
    with an empty sha and stay deniable only once shafill.py has filled them in.
    """
    cols: dict[str, list] = {
        "author": [], "book": [], "text": [], "segmented": [], PARQUET_SHA_COLUMN: [],
    }
    for src in sources:
        try:
            existing = pq.read_table(src)
        except Exception as error:  # noqa: BLE001
            log(f"[convert-warn] could not read {src.name}: {error}")
            return None
        names = set(existing.column_names)
        for key in cols:
            cols[key].extend(
                existing[key].to_pylist() if key in names else [""] * existing.num_rows
            )
    return cols


def write_parquet_rows(output_dir: Path, rows: list[dict], log=print):
    """Append or replace many rows in ONE read-modify-write of books.parquet.

    `rows` are dicts with book, text, segmented, author and sha256. Returns
    (path, added, replaced, denied), or None if the write could not happen.

    Bulk because a run-end flush can carry hundreds of journaled books, and one
    append per book would rewrite the whole growing file each time.

    THE GUARD: a row whose sha256 is already in the table carrying the same text is
    DENIED - not appended, not replaced, nothing rewritten. That is what stops one
    PDF being recorded twice, which two independent writers would otherwise do: the
    run-end flush of scan/collected.jsonl and the per-book append during upload both
    see the same convertable book. It also stops the same file landing twice under two
    book keys, which is possible because the OCR disambiguates equal basenames.
    A matching sha with DIFFERENT text is not denied: the same PDF parsed or OCR'd
    better is an improvement, and it replaces the row under its own book key.
    """
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as error:
        log(f"[convert-warn] pyarrow unavailable: {error}")
        return None

    parquet_dir = Path(output_dir) / PARQUET_DIR_NAME
    final = parquet_dir / PARQUET_FILE_NAME
    parquet_dir.mkdir(parents=True, exist_ok=True)
    with _PARQUET_LOCK, _parquet_file_lock(parquet_dir):
        sources = [final] if final.exists() else sorted(parquet_dir.glob("batch-*.parquet"))
        cols = _parquet_columns(pq, sources, log)
        if cols is None:
            return None
        books, texts = cols["book"], cols["text"]
        by_sha = {
            sha: i for i, sha in enumerate(cols[PARQUET_SHA_COLUMN]) if sha
        }

        added = replaced = denied = 0
        for row in rows:
            book = row.get("book", "")
            text = row.get("text", "")
            sha = row.get("sha256", "") or ""
            hit = by_sha.get(sha) if sha else None
            if hit is not None and texts[hit] == text:
                denied += 1
                if books[hit] == book:
                    log(f"[parquet-skip] {book}: sha256 {sha[:12]} already recorded with "
                        "this exact text, not written again")
                else:
                    log(f"[parquet-skip] {book}: same PDF and text as the existing row "
                        f"{books[hit]!r} (sha256 {sha[:12]}), not recorded twice")
                continue

            values = (
                row.get("author", ""), book, text,
                row.get("segmented", ""), sha,
            )
            if book in books:
                idx = books.index(book)
                for key, value in zip(cols, values):
                    cols[key][idx] = value
                replaced += 1
            else:
                # `books` and `texts` ARE cols["book"] and cols["text"] - the same
                # list objects - so appending through cols already extends them.
                for key, value in zip(cols, values):
                    cols[key].append(value)
                added += 1
            if sha:
                by_sha[sha] = books.index(book)

        if not added and not replaced:
            log(f"[parquet] nothing to write: {denied} row(s) already recorded -> "
                f"{PARQUET_DIR_NAME}/{final.name} ({len(books)} row(s) on VPS)")
            return final, added, replaced, denied

        table = pa.table({key: cols[key] for key in
                          ("author", "book", "text", "segmented", PARQUET_SHA_COLUMN)})
        temp = final.with_name("." + final.name + ".tmp")
        try:
            pq.write_table(table, temp, compression="zstd", compression_level=19)
            os.replace(temp, final)
        except OSError as error:
            log(f"[convert-warn] parquet write failed: {error}")
            temp.unlink(missing_ok=True)
            return None
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
        log(f"[parquet] +{added} new, {replaced} updated, {denied} denied -> "
            f"{PARQUET_DIR_NAME}/{final.name} ({len(books)} row(s) on VPS)")
        return final, added, replaced, denied


def write_parquet_row(
    output_dir: Path,
    book: str,
    text: str,
    segmented: str,
    *,
    author: str = "",
    sha256: str = "",
    log=print,
) -> Path | None:
    """Append one book row to parquet/books.parquet atomically.

    Pass sha256 - the hash of the PDF the text came from - so the duplicate guard in
    write_parquet_rows can see it. Both callers already have it: save_remote computes
    the digest before uploading, and the OCR service stores it in state.sqlite3.
    """
    result = write_parquet_rows(
        output_dir,
        [{"book": book, "text": text, "segmented": segmented,
          "author": author, "sha256": sha256}],
        log,
    )
    return None if result is None else result[0]


def set_parquet_sha(output_dir: Path, hashes: dict[str, str], *, force: bool = False,
                    log=print) -> dict | None:
    """Fill the sha256 column of existing rows from {book: sha256}. One locked pass.

    This is the write half of shafill.py, kept here so the collection has ONE place
    that knows its lock, its dot-temp name and its compression. It touches the
    sha256 column and nothing else - author, book, text and segmented come back as
    they were - and it deliberately does NOT run the duplicate guard: round one has
    to be able to describe the duplicates that are already in the file, not refuse
    to look at them.

    An existing hash that disagrees with `hashes` is kept unless force=True, because
    a disagreement means either the file on the host changed or two different books
    were matched to one file, and silently rewriting that would hide the problem.
    Returns a stats dict, or None if the file could not be read or written.
    """
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as error:
        log(f"[convert-warn] pyarrow unavailable: {error}")
        return None

    parquet_dir = Path(output_dir) / PARQUET_DIR_NAME
    final = parquet_dir / PARQUET_FILE_NAME
    stats = {"filled": 0, "forced": 0, "same": 0, "conflict": 0, "absent": 0,
             "rows": 0, "wrote": False, "path": final}
    if not final.exists():
        log(f"[sha] no {PARQUET_DIR_NAME}/{PARQUET_FILE_NAME} to fill")
        stats["absent"] = len(hashes)
        return stats

    with _PARQUET_LOCK, _parquet_file_lock(parquet_dir):
        cols = _parquet_columns(pq, [final], log)
        if cols is None:
            return None
        books = cols["book"]
        current = cols[PARQUET_SHA_COLUMN]
        stats["rows"] = len(books)
        index = {book: i for i, book in enumerate(books)}
        changed = False
        for book, sha in hashes.items():
            if book not in index:
                stats["absent"] += 1
                continue
            i = index[book]
            if not current[i]:
                current[i] = sha
                stats["filled"] += 1
                changed = True
            elif current[i] == sha:
                stats["same"] += 1
            elif force:
                log(f"[sha-force] {book}: {current[i][:12]} -> {sha[:12]}")
                current[i] = sha
                stats["forced"] += 1
                changed = True
            else:
                log(f"[sha-conflict] {book}: parquet says {current[i][:12]}, host says "
                    f"{sha[:12]} - kept the parquet value (use --force to overwrite)")
                stats["conflict"] += 1

        if not changed:
            log(f"[sha] nothing to write ({stats['same']} already correct, "
                f"{stats['conflict']} conflicting, {stats['absent']} not in the parquet)")
            return stats

        table = pa.table({key: cols[key] for key in
                          ("author", "book", "text", "segmented", PARQUET_SHA_COLUMN)})
        temp = final.with_name("." + final.name + ".tmp")
        try:
            pq.write_table(table, temp, compression="zstd", compression_level=19)
            os.replace(temp, final)
        except OSError as error:
            log(f"[convert-warn] parquet write failed: {error}")
            temp.unlink(missing_ok=True)
            return None
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
        stats["wrote"] = True
        log(f"[sha] wrote {stats['filled']} new hash(es)"
            + (f", forced {stats['forced']}" if stats["forced"] else "")
            + f" into {PARQUET_DIR_NAME}/{final.name} ({stats['rows']} row(s))")
        return stats


def book_name(record: dict) -> str:
    """The collection's `book` value: the PDF's filename without its extension."""
    name = record.get("filename", "")
    return name[:-4] if name.lower().endswith(".pdf") else name




def _release_staged(records: list[dict], output_dir: Path, log) -> int:
    """Move journal-listed PDFs from .convert/ to convertable/."""
    stage = output_dir / CONVERT_STAGE_NAME
    final_dir = output_dir / CONVERT_FINAL_NAME
    moved = 0
    for record in records:
        name = record.get("filename", "")
        source = stage / name
        if not name or not source.exists():
            continue  # text is committed; the PDF never finished staging
        final_dir.mkdir(parents=True, exist_ok=True)
        target = final_dir / name
        index = 1
        while target.exists():
            index += 1
            target = final_dir / f"{source.stem} ({index}){source.suffix}"
        try:
            os.replace(source, target)
            moved += 1
        except OSError as error:
            log(f"[convert-warn] cannot release {name}: {error}")
    return moved


@dataclass
class FlushStats:
    documents: int = 0
    moved: int = 0
    parquet: Path | None = None
    denied: int = 0  # rows the duplicate guard refused (already recorded)
    error: str = ""
    skipped: bool = False  # nothing collected this run

    @property
    def ok(self) -> bool:
        return not self.error

    def line(self) -> str:
        """One human line for the console, the report and the mail."""
        if self.skipped:
            return "🧾 Convert: no convertable text collected."
        if not self.ok:
            return (
                f"🧾 Convert: {self.documents} doc(s) still journaled — "
                f"{self.error} (kept in scan/collected.jsonl for the next run)."
            )
        extra = f", {self.denied} already recorded" if self.denied else ""
        return (
            f"🧾 Convert: {self.documents} doc(s) segmented -> "
            f"{self.parquet.name if self.parquet else '?'}{extra} (kept on VPS)."
        )


def flush(output_dir: str | Path, log=print) -> FlushStats:
    """Run-end flush: journal -> one segmentor pass -> Parquet -> release.

    Safe to call when nothing was collected (returns skipped=True). On any
    failure the journal and staged PDFs are left untouched for the next run.
    """
    output_dir = Path(output_dir)
    stats = FlushStats()
    records = _read_journal()
    stats.documents = len(records)
    if not records:
        stats.skipped = True
        return stats

    log(
        f"[convert] flushing {len(records)} collected document(s) "
        "through the segmentor..."
    )
    segmented = _segment_documents([r.get("text", "") for r in records], log)
    if segmented is None:
        stats.error = "segmentation failed"
        return stats

    # The parquet is the owner's BOOK COLLECTION, not a pipeline database (owner:
    # "don't look from development perspective"): five columns - author | book |
    # text | segmented | sha256. author stays EMPTY on purpose (owner: PDF metadata
    # authorship is untrustworthy - most Burmese authors never embed their name, and
    # online tools overwrite the field and steal credit; the owner will later extract
    # the author from the book CONTENT programmatically). sha256 is the hash of the
    # PDF the text came from, and it is what the duplicate guard keys on: this flush
    # and the per-book append during upload both see the same convertable book, so
    # without it one PDF would be recorded twice. Everything else (encoding, pages,
    # scan counts, source URL, timestamps) stays in the journal rows for crash-safety
    # and debugging but is not carried into the collection file.
    rows = [
        {
            "book": book_name(record),
            "text": record.get("text", ""),
            "segmented": seg,
            "author": record.get("author", ""),
            "sha256": record.get("sha256", "") or "",
        }
        for record, seg in zip(records, segmented)
    ]
    written = write_parquet_rows(output_dir, rows, log)
    if written is None:
        stats.error = "parquet write failed"
        return stats
    parquet, added, replaced, denied = written
    stats.parquet = parquet
    stats.denied = denied

    stats.moved = _release_staged(records, output_dir, log)

    # The batch is committed; retire the journal (kept beside it for audit).
    done = JOURNAL_FILE.with_name(
        f"collected-{time.strftime('%Y%m%d-%H%M%S')}.done.jsonl"
    )
    try:
        os.replace(JOURNAL_FILE, done)
    except OSError as error:
        # Not fatal: rows would repeat in the next batch, and sha256 makes
        # the duplicates visible downstream.
        log(f"[convert-warn] cannot retire the journal: {error}")
    log(
        f"[convert] committed {parquet.name}: {stats.documents} row(s), "
        f"{stats.moved} PDF(s) released"
    )
    return stats


def main(argv: list[str]) -> int:
    """Manual flush: ``python text_collect.py [OUTPUT_DIR]``."""
    from Dcrawler import OUTPUT_DIR  # local import avoids a cycle
    target = Path(argv[0]) if argv else OUTPUT_DIR
    stats = flush(target)
    print(stats.line())
    return 0 if stats.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

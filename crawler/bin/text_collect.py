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

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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

DOC_SEPARATOR = "\u001d"  # GROUP SEPARATOR between documents, both directions
_PARQUET_LOCK = threading.Lock()

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


def _segment_documents(texts: list[str], log) -> list[str] | None:
    """One segmentor process for the whole run's text; None on failure."""
    node = shutil.which("node")
    if node is None:
        log("[convert-warn] node not found; segmentation postponed")
        return None
    giant = DOC_SEPARATOR.join(texts)
    fd, out_name = tempfile.mkstemp(prefix=".segment-", suffix=".tmp",
                                    dir=SCAN_DIR)
    os.close(fd)
    out_path = Path(out_name)
    try:
        proc = subprocess.run(
            [node, str(BRIDGE_SCRIPT), str(out_path)],
            input=giant.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
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


def write_parquet_row(
    output_dir: Path,
    book: str,
    text: str,
    segmented: str,
    *,
    author: str = "",
    log=print,
) -> Path | None:
    """Append one book row to parquet/books.parquet on the VPS atomically."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as error:
        log(f"[convert-warn] pyarrow unavailable: {error}")
        return None

    parquet_dir = Path(output_dir) / PARQUET_DIR_NAME
    final = parquet_dir / PARQUET_FILE_NAME
    with _PARQUET_LOCK:
        authors: list[str] = []
        books: list[str] = []
        texts: list[str] = []
        segmenteds: list[str] = []
        sources = [final] if final.exists() else sorted(parquet_dir.glob("batch-*.parquet"))
        for src in sources:
            try:
                existing = pq.read_table(src)
                authors.extend(existing["author"].to_pylist())
                books.extend(existing["book"].to_pylist())
                texts.extend(existing["text"].to_pylist())
                segmenteds.extend(existing["segmented"].to_pylist())
            except Exception as error:  # noqa: BLE001
                log(f"[convert-warn] could not read {src.name}: {error}")
                return None

        if book in books:
            idx = books.index(book)
            authors[idx] = author
            texts[idx] = text
            segmenteds[idx] = segmented
        else:
            authors.append(author)
            books.append(book)
            texts.append(text)
            segmenteds.append(segmented)

        table = pa.table({
            "author": authors,
            "book": books,
            "text": texts,
            "segmented": segmenteds,
        })
        parquet_dir.mkdir(parents=True, exist_ok=True)
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
        log(f"[parquet] +1 row ({book}) -> {PARQUET_DIR_NAME}/{final.name} ({len(books)} row(s) on VPS)")
        return final


def _write_parquet(records: list[dict], segmented: list[str],
                   parquet_dir: Path, log) -> Path | None:
    """Write one committed Parquet file; dot-temp then atomic rename."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as error:
        log(f"[convert-warn] pyarrow unavailable: {error}")
        return None
    # The parquet is the owner's BOOK COLLECTION, not a pipeline database
    # (owner: "don't look from development perspective"): four columns —
    # author | book | text | segmented. book = the filename without .pdf.
    # author stays EMPTY on purpose (owner: PDF metadata authorship is
    # untrustworthy — most Burmese authors never embed their name, and
    # online tools overwrite the field and steal credit; the owner will
    # later extract the author from the book CONTENT programmatically).
    # Everything else (sha256, encoding, pages, scan counts, source URL,
    # timestamps) stays in the journal rows for crash-safety and debugging
    # but is not carried into the collection file.
    def book_name(r: dict) -> str:
        name = r.get("filename", "")
        return name[:-4] if name.lower().endswith(".pdf") else name
    columns = {
        "author": [r.get("author", "") for r in records],
        "book": [book_name(r) for r in records],
        "text": [r.get("text", "") for r in records],
        "segmented": segmented,
    }
    table = pa.table(columns)
    parquet_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    final = parquet_dir / f"batch-{stamp}.parquet"
    index = 1
    while final.exists():
        index += 1
        final = parquet_dir / f"batch-{stamp}-{index}.parquet"
    temp = final.with_name("." + final.name + ".tmp")
    try:
        # zstd level 19: measured 40% smaller than pyarrow's default level on
        # segmented Burmese text (59.6KB vs 99.5KB per ~480KB column) and ~19%
        # smaller than gzip-9 JSONL of the same rows. Batches are written once
        # per run, so the extra CPU (~seconds) is irrelevant.
        pq.write_table(table, temp, compression="zstd", compression_level=19)
        os.replace(temp, final)  # commit: the uploader may see it only now
    except OSError as error:
        log(f"[convert-warn] parquet write failed: {error}")
        temp.unlink(missing_ok=True)
        return None
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    return final


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
        return (
            f"🧾 Convert: {self.documents} doc(s) segmented -> "
            f"{self.parquet.name if self.parquet else '?'} (kept on VPS)."
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

    parquet = _write_parquet(
        records, segmented, output_dir / PARQUET_DIR_NAME, log
    )
    if parquet is None:
        stats.error = "parquet write failed"
        return stats
    stats.parquet = parquet

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

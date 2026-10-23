#!/usr/bin/env python3
"""Continuous convertable parse: serial read-only SSH pull -> PDF text parser -> Parquet.

A duplicate of deploy/continuous-ocr/run.py with the same flow and the same logic -
the same ledger, the same statuses, the same book_key, the same commit into the ONE
cumulative Parquet, the same restart reconciliation, the same 5 GB disk floor, the
same retained-failure reminder and the same emails. Two things differ, both of them
the point:

  * the queue is ONLY <SSH_DIR>/convertable/ on the shared host, not the whole
    hosting tree; and
  * the per-book step is the PDF text PARSER, not OCR. No tesseract, no worker.mjs,
    no ocr_runtime/, and no classification verdict: these PDFs are already the
    Zawgyi-One and Unicode ones whose text the owner verified survives intact, so
    re-judging them with the old-font gates (WinNwa, Ayar, the legacy layout
    methods) could only throw a verified book away. text_collect.parse_convertable_
    book_direct() extracts and converts; it never decides convertable/scanned/old.

Node is still required - the segmentor bridge is the scraper's own Engine, the same
one the OCR service uses after tesseract.

Hosting originals are read-only: a book is downloaded, parsed, written as one row,
and the LOCAL download is deleted on success. Nothing on the host is ever touched.
A failure keeps its local PDF, is recorded with the reason, and the run carries on.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'crawler' / 'bin'))
from save_remote import load_config, _ssh_argv, sha256_file
from mailer import RunMail
import text_collect
from disk_floor import DISK_FLOOR

STATE = ROOT / 'scan' / 'parse'
DB = STATE / 'state.sqlite3'
CONVERTABLE_DIR = 'convertable'   # the only directory this service reads
LOCAL_STAGING = '.parse'          # downloaded PDFs, deleted on success


class Stopped(BaseException):
    pass


class BookError(Exception):
    pass


def stopped(signum, frame):
    raise Stopped(f'terminated by signal {signum}')


def database():
    STATE.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute('pragma journal_mode=WAL')
    db.execute('pragma synchronous=FULL')
    db.executescript('''
    create table if not exists books (
      source text primary key, local text not null, status text not null,
      digest text, error text, parquet text, updated real not null, book_key text);
    create table if not exists meta (key text primary key, value text not null);
    ''')
    db.commit()
    return db


def book_key(db, source):
    """This book's row identity in the ONE cumulative Parquet.

    Identical to the OCR service's, and it has to be: both services write the same
    file, so the same PDF must resolve to the same key whichever service parsed it,
    or the writer's duplicate guard would see two different books. The key is the
    filename minus .pdf, persisted so a retry replaces its own row; a basename
    already taken by another source is keyed <name>-<first 8 hex of the source>
    instead. convertable/ is flat today, so the clash branch is a safety net rather
    than a routine - but the host is not the only writer of that directory.
    """
    row = db.execute('select book_key from books where source=?', (source,)).fetchone()
    if row and row['book_key']:
        return row['book_key']
    name = PurePosixPath(source).name
    base = name[:-4] if name.lower().endswith('.pdf') else name
    clash = db.execute('select 1 from books where book_key=? and source!=?',
                       (base, source)).fetchone()
    key = base if not clash else base + '-' + hashlib.sha256(source.encode()).hexdigest()[:8]
    db.execute('update books set book_key=? where source=?', (key, source))
    db.commit()
    return key


def update(db, source, status, **values):
    values.update(status=status, updated=time.time())
    db.execute('update books set ' + ','.join(k + '=?' for k in values) + ' where source=?',
               [*values.values(), source])
    db.commit()


def syncfile(path):
    with Path(path).open('rb') as handle:
        os.fsync(handle.fileno())
    fd = os.open(str(Path(path).parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mail_notice(kind, details):
    mail = RunMail('continuous parse')
    if kind == 'failed':
        mail.send_failed('Parse service', '\n'.join(details))
    elif kind == 'started':
        mail.send_started(details)
    else:
        mail.send_finished(details)


def reminders(db):
    """Retained failures, one email an hour, from the 20th book onwards.

    The same rule and the same latch as the OCR service, on its own meta keys in its
    own state.sqlite3 - the two services count their own retained failures, because
    a book that failed OCR and a book that failed parsing are different problems even
    when they are the same file.
    """
    # The timer may run even after the parse loop exits. It never changes source files.
    with (STATE / 'reminder.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        rows = db.execute("select * from books where status='failed'").fetchall()
        active = []
        for row in rows:
            if Path(row['local']).exists():
                active.append(row)
            else:
                update(db, row['source'], 'removed')
        latched = db.execute("select value from meta where key='reminder_latched'").fetchone()
        if not active:
            db.execute("delete from meta where key in ('reminder_latched','last_reminder')")
            db.commit()
            return
        if len(active) < 20 and not latched:
            return
        db.execute("insert or replace into meta values ('reminder_latched','1')")
        last = db.execute("select value from meta where key='last_reminder'").fetchone()
        if not last or time.time() - float(last[0]) >= 3600:
            # Reserve the hour before delivery to prevent duplicate timer/main sends.
            db.execute("insert or replace into meta values ('last_reminder',?)", (str(time.time()),))
            db.commit()
            mail_notice('failed', [
                f'{len(active)} failed convertable PDFs remain on the VPS; no automatic retry.',
                'They are kept, not deleted. Delete them yourself and they drop out of '
                'this count as "removed"; the service keeps running either way.',
            ])


def ssh(config, command):
    return subprocess.run(_ssh_argv(config, command), capture_output=True, check=True).stdout


def discover(config):
    """Every PDF in the host's convertable/ directory, and nothing else.

    The OCR service walks the whole hosting tree and keeps only what sits under a
    scan/scanned directory; this is the mirror image, and it does not walk the tree
    at all - one find over that one directory is the whole queue. NUL delimiters
    preserve spaces, Burmese names and newlines. Symlinks are never followed.

    A missing convertable/ directory makes find exit non-zero, which raises here and
    reaches the owner as a failed email. That is deliberate: a queue that silently
    reads nothing for a month is worse than one loud failure at the start.
    """
    root = config.directory
    script = ('cd -- ' + shlex.quote(root)
              + f' && find ./{CONVERTABLE_DIR} -type f -iname \'*.pdf\' -print0')
    raw = ssh(config, script)
    sources = []
    for name in raw.split(b'\0'):
        if not name:
            continue
        rel = PurePosixPath(os.fsdecode(name))
        if rel.is_absolute() or '..' in rel.parts:
            raise RuntimeError('Unsafe hosting-relative path')
        # The bucket has to be a directory component, not merely part of a filename.
        if CONVERTABLE_DIR not in [p.lower() for p in rel.parts[:-1]]:
            continue
        # find already asked for *.pdf. Filter again anyway: the queue is what gets
        # downloaded, parsed and written into the collection, and trusting a remote
        # shell's glob alone is a thin thing to hang that on - a busybox find, a
        # quoting accident or a hand-edited directory should not be able to put a
        # non-PDF in the collection.
        if not rel.name.lower().endswith('.pdf'):
            continue
        sources.append(str(rel))
    return sorted(set(sources))


def download(config, source, target):
    """Byte-exact pull, verified against the host's own sha256 before and after."""
    remote = str(PurePosixPath(config.directory) / source)
    quoted = shlex.quote(remote)
    expected = ssh(config, 'sha256sum -- ' + quoted).decode().split()[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name('.' + target.name + '.part')
    with part.open('wb') as handle:
        subprocess.run(_ssh_argv(config, 'cat -- ' + quoted), stdout=handle, check=True)
        handle.flush()
        os.fsync(handle.fileno())
    digest = sha256_file(part)
    after = ssh(config, 'sha256sum -- ' + quoted).decode().split()[0]
    if digest != expected or digest != after:
        raise RuntimeError(f'Download checksum mismatch: {source}; partial retained at {part}')
    os.replace(part, target)
    syncfile(target)
    # Each SSH subprocess has exited before returning to the caller.
    return digest


def commit_dataset(db, row, parsed):
    """One row in the ONE cumulative Parquet, then the local download is released.

    The same commit as the OCR service and the same writer, so the duplicate guard,
    the cross-process lock and the five-column schema all behave identically - a book
    the OCR already recorded is denied here rather than written twice, and a book
    whose text this parser reads better updates the row that already holds that PDF.

    There is no journal, no pages file and no segmented temp to clean up: the OCR
    keeps those because tesseract is slow enough that losing the result hurts, while
    a parse is cheap and deterministic, so a crash before 'success' simply re-parses
    on restart. The digest goes in as the row's sha256, straight from the verified
    download.
    """
    source = row['source']
    book, text, segmented = parsed
    final = text_collect.write_parquet_row(
        ROOT / 'pdfs', book_key(db, source), text, segmented,
        author='', sha256=row['digest'] or '', log=print)
    if final is None:
        raise RuntimeError('Existing scraper Parquet writer failed')
    syncfile(final)
    # Deterministic row key + durable ledger make restart reconcile crash windows.
    update(db, source, 'success', parquet=str(final), error='')
    Path(row['local']).unlink(missing_ok=True)
    print(f'[parse-success] {source} -> {final}', flush=True)


def run(db):
    config = load_config()
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'runner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mail_notice('started', [f'Queue: {config.directory}/{CONVERTABLE_DIR}/ on the host, '
                                'read-only. No runtime deadline. Parser only, no OCR.'])
        try:
            # Interrupted parses resume from the retained verified PDF, and the
            # deterministic row key prevents a second dataset row. Reconcile by ROW,
            # because every book shares one cumulative Parquet.
            done = db.execute("select * from books where status='success'").fetchall()
            if done:
                target = ROOT / 'pdfs' / 'parquet' / text_collect.PARQUET_FILE_NAME
                committed = set()
                if target.is_file():
                    import pyarrow.parquet as pq
                    committed = set(pq.read_table(target, columns=['book'])['book'].to_pylist())
                for row in done:
                    if (row['book_key'] or book_key(db, row['source'])) in committed:
                        Path(row['local']).unlink(missing_ok=True)
                        continue
                    # No row in the single Parquet - it was wiped, moved, or this ledger
                    # outlived it. Re-queue rather than brick the service: discover()
                    # re-downloads and the row is appended once. This is exactly how a
                    # lost books.parquet gets rebuilt from the host.
                    update(db, row['source'], 'pending', parquet='',
                           error='re-queued: row absent from ' + target.name)
                    print(f'[migrate] {row["source"]}: no row in {target.name}, re-queued',
                          flush=True)
            while True:
                sources = discover(config)
                for source in sources:
                    local = ROOT / 'pdfs' / LOCAL_STAGING / source
                    db.execute('insert or ignore into books(source,local,status,updated) '
                               'values (?,?,?,?)',
                               (source, str(local), 'pending', time.time()))
                db.commit()
                rows = db.execute("select * from books where status not in "
                                  "('success','failed','removed') order by source").fetchall()
                if not rows:
                    break
                for row in rows:
                    source, local = row['source'], Path(row['local'])
                    if shutil.disk_usage(ROOT).free < DISK_FLOOR:
                        raise RuntimeError('VPS disk below scraper 5 GB safety floor')
                    if not local.exists() or not row['digest'] or sha256_file(local) != row['digest']:
                        update(db, source, 'downloading')
                        digest = download(config, source, local)
                        update(db, source, 'downloaded', digest=digest)
                        row = db.execute('select * from books where source=?', (source,)).fetchone()
                    update(db, source, 'parsing')
                    # The parser logs its own reason and returns None, so collect the log
                    # to keep that reason in the ledger instead of only in the journal.
                    notes = []
                    parsed = text_collect.parse_convertable_book_direct(local, log=notes.append)
                    for note in notes:
                        print(note, flush=True)
                    if parsed is None:
                        reason = next((n for n in reversed(notes) if 'warn' in n),
                                      'parser produced no text and no row')
                        update(db, source, 'failed', error=reason)
                        print(f'[parse-failed] {source}: {reason}', flush=True)
                        reminders(db)
                        continue
                    try:
                        commit_dataset(db, row, parsed)
                    except Exception as error:
                        # Anything past the parse itself - segmentation exit, writer
                        # failure, a disk-full append. Mark the book and carry on:
                        # otherwise the service dies, Restart=no keeps it down, and the
                        # row stays in a status this query re-selects forever, so every
                        # book sorted after it is never reached. Stopped is a
                        # BaseException, so SIGTERM still propagates.
                        update(db, source, 'failed', error=f'commit: {error}')
                        print(f'[commit-failed] {source}: {error}', flush=True)
                        reminders(db)
                        continue
                    reminders(db)
            mail_notice('finished', [
                f'Parse stopped: no eligible PDFs remain in {CONVERTABLE_DIR}/. '
                'Failed PDFs are not retried.',
                'The reminder timer continues checking retained failures.'])
        finally:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--remind', action='store_true')
    parser.add_argument('--failure-hook', action='store_true')
    args = parser.parse_args()
    if args.failure_hook:
        # ExecStopPost: the last chance to tell the owner the service is down, so it
        # must not depend on anything the failure may have broken - including the
        # database. This branch runs before database() on purpose.
        result = os.environ.get('SERVICE_RESULT', '')
        if result and result != 'success':
            mail_notice('failed', ['systemd parse service ended: ' + result,
                                   'Exit status: ' + os.environ.get('EXIT_STATUS', 'unknown')])
        return 0
    db = database()
    if args.remind:
        reminders(db)
        return 0
    signal.signal(signal.SIGINT, stopped)
    signal.signal(signal.SIGTERM, stopped)
    try:
        run(db)
        return 0
    except Stopped as error:
        mail_notice('finished', [f'Parse stopped by owner/system: {error}. '
                                 'Unfinished inputs retained.'])
        return 0
    except BaseException as error:
        mail_notice('failed', [f'{type(error).__name__}: {error}',
                               'Parse stopped; inputs/state retained.'])
        raise
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Scraper integration: serial read-only SSH pull -> persistent OCR -> existing Engine/Parquet."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
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

STATE = ROOT / 'scan' / 'ocr'
DB = STATE / 'state.sqlite3'
WORKER = Path(__file__).with_name('worker.mjs')

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
      digest text, error text, parquet text, updated real not null);
    create table if not exists meta (key text primary key, value text not null);
    ''')
    db.commit()
    return db


def update(db, source, status, **values):
    values.update(status=status, updated=time.time())
    db.execute('update books set ' + ','.join(k+'=?' for k in values) + ' where source=?',
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
    mail = RunMail('continuous OCR')
    if kind == 'failed':
        mail.send_failed('OCR service', '\n'.join(details))
    elif kind == 'started':
        mail.send_started(details)
    else:
        mail.send_finished(details)


def reminders(db):
    # Timer may run even after OCR exits. It never changes source/provider files.
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
            mail_notice('failed', [f'{len(active)} failed OCR PDFs remain on the VPS; no automatic retry.',
                                  *[r['source'] + ': ' + (r['error'] or '')[:250] for r in active]])
        db.commit()


def ssh(config, command):
    return subprocess.run(_ssh_argv(config, command), capture_output=True, check=True).stdout


def discover(config):
    root = config.directory
    # NUL delimiters preserve spaces, Burmese names and newlines. Never follow symlinks.
    script = 'cd -- ' + shlex.quote(root) + " && find . -type f -iname '*.pdf' -print0"
    raw = ssh(config, script)
    sources = []
    for name in raw.split(b'\0'):
        if not name:
            continue
        rel = PurePosixPath(os.fsdecode(name))
        if rel.is_absolute() or '..' in rel.parts:
            raise RuntimeError('Unsafe hosting-relative path')
        # Bucket has to be a directory component, not merely part of a filename.
        if not any(p.lower() in {'scan', 'scanned'} for p in rel.parts[:-1]):
            continue
        if any(p.lower() in {'old', 'convertable'} for p in rel.parts[:-1]):
            continue
        sources.append(str(rel))
    return sorted(set(sources))


def download(config, source, target):
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
    # Each SSH subprocess has exited before returning to the OCR caller.
    return digest


class Ocr:
    def __init__(self):
        env = dict(os.environ, PRASER_ASSET_CACHE_DIR=str(STATE / 'asset-cache'))
        self.proc = subprocess.Popen(['node', str(WORKER)], cwd=ROOT, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        ready = self.read()
        if not ready.get('ready'):
            raise RuntimeError(f'OCR startup failed: {ready}')
        print('[ocr-assets] ' + json.dumps(ready), flush=True)

    def read(self):
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f'OCR worker exited unexpectedly ({self.proc.poll()})')
            if line.startswith('OCR_RESULT '):
                return json.loads(line[len('OCR_RESULT '):])
            print(line.rstrip(), flush=True)

    def run(self, source, output):
        self.proc.stdin.write(json.dumps({'input': str(source), 'output': str(output)}) + '\n')
        self.proc.stdin.flush()
        result = self.read()  # Deliberately no deadline.
        if not result.get('ok'):
            raise BookError(result.get('error', 'OCR failed'))
        return result

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)  # Shutdown cleanup only, not OCR timeout.
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


def commit_dataset(db, row, pages_file, result):
    source = row['source']
    key = hashlib.sha256(source.encode()).hexdigest()
    parquet_dir = ROOT / 'pdfs' / 'parquet' / 'ocr'
    parquet_dir.mkdir(parents=True, exist_ok=True)
    final = parquet_dir / (key + '.parquet')
    journal = STATE / (key + '.json')
    pages = [json.loads(line)['text'] for line in pages_file.read_text().splitlines()]
    record = {'filename': PurePosixPath(source).name, 'author': '', 'sha256': row['digest'],
              'pages': result['pages'], 'encoding': 'OCR_UNICODE', 'text': '\n\n'.join(pages),
              'source_url': source, 'scan_counts': {}}
    temp = journal.with_suffix('.tmp')
    temp.write_text(json.dumps(record, ensure_ascii=False))
    syncfile(temp)
    os.replace(temp, journal)
    syncfile(journal)
    update(db, source, 'segmenting')
    # Reuse the existing scraper bridge/Engine with no segmentor timeout.
    seg = STATE / (key + '.segmented.tmp')
    # Bridge writes argv[2], not stdout. Diagnostics remain in the service journal.
    proc = subprocess.run(['node', str(text_collect.BRIDGE_SCRIPT), str(seg)],
                          input=record['text'].encode(), check=False)
    if proc.returncode:
        raise RuntimeError(f'Scraper Engine segmentation exited {proc.returncode}; input retained')
    segmented = seg.read_text()
    if record['text'].strip() and not segmented.strip():
        raise RuntimeError('Scraper Engine returned empty segmentation')
    # Reuse the actual scraper four-column Parquet writer, not a new schema/Engine.
    batch_dir = STATE / ('batch-' + key)
    written = text_collect._write_parquet([record], [segmented], batch_dir, print)
    if written is None:
        raise RuntimeError('Existing scraper Parquet writer failed')
    syncfile(written)
    import pyarrow.parquet as pq
    table = pq.read_table(written)
    if table.column_names != ['author', 'book', 'text', 'segmented'] or table.num_rows != 1:
        raise RuntimeError('Invalid committed Parquet schema/row count')
    os.replace(written, final)
    syncfile(final)
    # Deterministic filename + durable ledger make restart reconcile crash windows.
    update(db, source, 'success', parquet=str(final), error='')
    Path(row['local']).unlink(missing_ok=True)
    pages_file.unlink(missing_ok=True)
    seg.unlink(missing_ok=True)
    journal.unlink(missing_ok=True)
    print(f'[ocr-success] {source} -> {final}', flush=True)


def run(db):
    config = load_config()
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'runner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mail_notice('started', ['No runtime deadline. Hosting originals are read-only.'])
        worker = None
        try:
            # Interrupted segmentation resumes by re-OCR of its retained verified PDF;
            # deterministic final filename prevents a second dataset row/file.
            for row in db.execute("select * from books where status='success'").fetchall():
                if not row['parquet'] or not Path(row['parquet']).is_file():
                    raise RuntimeError('Committed Parquet missing: ' + row['source'])
                Path(row['local']).unlink(missing_ok=True)
            while True:
                sources = discover(config)
                for source in sources:
                    local = ROOT / 'pdfs' / '.ocr' / source
                    db.execute('insert or ignore into books(source,local,status,updated) values (?,?,?,?)',
                               (source, str(local), 'pending', time.time()))
                db.commit()
                rows = db.execute("select * from books where status not in ('success','failed','removed') order by source").fetchall()
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
                    if worker is None:
                        worker = Ocr()
                    key = hashlib.sha256(source.encode()).hexdigest()
                    pages_file = STATE / (key + '.pages.jsonl')
                    update(db, source, 'ocr')
                    try:
                        result = worker.run(local, pages_file)
                    except BookError as error:
                        update(db, source, 'failed', error=str(error))
                        print(f'[ocr-failed] {source}: {error}', flush=True)
                        reminders(db)
                        continue
                    commit_dataset(db, row, pages_file, result)
                    reminders(db)
            mail_notice('finished', ['OCR stopped: no eligible PDFs remain. Failed PDFs are not retried.',
                                     'The reminder timer continues checking retained failures.'])
        finally:
            if worker:
                worker.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--remind', action='store_true')
    parser.add_argument('--failure-hook', action='store_true')
    args = parser.parse_args()
    db = database()
    if args.failure_hook:
        result = os.environ.get('SERVICE_RESULT', '')
        if result and result != 'success':
            mail_notice('failed', ['systemd OCR service ended: ' + result,
                                   'Exit status: ' + os.environ.get('EXIT_STATUS', 'unknown')])
        return 0
    if args.remind:
        reminders(db)
        return 0
    signal.signal(signal.SIGINT, stopped)
    signal.signal(signal.SIGTERM, stopped)
    try:
        run(db)
        return 0
    except Stopped as error:
        mail_notice('finished', [f'OCR stopped by owner/system: {error}. Unfinished inputs retained.'])
        return 0
    except BaseException as error:
        mail_notice('failed', [f'{type(error).__name__}: {error}', 'OCR stopped; inputs/state retained.'])
        raise
    finally:
        db.close()

if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run from an existing scraper checkout: uv run parse.py (no flags).

Install the continuous convertable-parse service's own systemd units and start it
detached from this SSH session. This is ocr.py's twin for the parser-only service:
same checks, same lock, same "never touch scraper.service" rule, but there is no
OCR runtime to prepare - no tesseract, no worker.mjs, no npm ci - because the only
native step left is the scraper's own Engine segmentor, which install.sh already
provisioned. Existing scraper modules and configuration are imported in place,
never copied over or replaced.

What it runs: every PDF in <SSH_DIR>/convertable/ on the shared host, downloaded
read-only, text extracted and converted (Zawgyi-One or Unicode) with NO
classification verdict, segmented, and appended as one row to
pdfs/parquet/books.parquet. Success deletes the local download; failure keeps it,
records the reason, and mails at 20 retained failures, at most once an hour.
"""
from __future__ import annotations
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SERVICE = 'scraper-parse.service'
REMINDER_UNITS = ['scraper-parse-reminder.timer', 'scraper-parse-reminder.service']


def call(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def main():
    required = ['crawler/bin/save_remote.py', 'crawler/bin/mailer.py',
                'crawler/bin/text_collect.py', 'crawler/bin/pdf_classify.py',
                'crawler/bin/segment_bridge.mjs', 'crawler/bin/disk_floor.py',
                'deploy/engine_ready.py', 'deploy/continuous-parse/install.py',
                'Engine/MonoSyllabism', 'praser/pdf_extract.py', 'praser/detector.py',
                'praser/rabbit.py', 'model/zawgyiUnicodeModel.dat']
    missing = [p for p in required if not (ROOT / p).exists()]
    if missing:
        sys.exit('This must run inside an existing scraper checkout. Missing: '
                 + ', '.join(missing))
    if os.geteuid() != 0 or not Path('/run/systemd/system').is_dir():
        sys.exit('Run uv run parse.py as root on your systemd VPS (same account as scraper.service).')
    for executable in ['uv', 'node', 'npm', 'ssh', 'systemctl']:
        if not shutil.which(executable):
            sys.exit(f'{executable} must be installed and on PATH.')
    node_version = subprocess.check_output(['node', '--version'], text=True).strip()
    if int(node_version.lstrip('v').split('.')[0]) < 22:
        sys.exit('Node 22 or newer is required.')
    state_dir = ROOT / 'scan' / 'parse'
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / 'launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        active = subprocess.run(['systemctl', 'is-active', SERVICE],
                                capture_output=True, text=True).stdout.strip()
        if active in {'active', 'activating', 'reloading'}:
            print(f'The parse service is already running. Logs: journalctl -u {SERVICE} -f')
            return
        if active == 'deactivating':
            sys.exit('The parse service is stopping; run this command again after it stops.')
        # Use the existing SSH loader as-is. No duplicate credentials parser.
        sys.path.insert(0, str(ROOT / 'crawler' / 'bin'))
        from save_remote import load_config
        config = load_config()
        print(f'Using existing scraper SSH settings: {config.user}@{config.host}:{config.port}',
              flush=True)
        print(f'Queue: {config.directory}/convertable/ on the host (read-only)', flush=True)
        # Stop only our own reminder units before refreshing our unit definitions.
        # An absent unit on first run is normal. Never stop scraper.service, and
        # never stop the OCR service - the two are independent.
        subprocess.run(['systemctl', 'stop', *REMINDER_UNITS], check=False)
        call(sys.executable, ROOT / 'deploy' / 'continuous-parse' / 'install.py', cwd=ROOT)
        call('systemctl', 'start', '--no-block', SERVICE, cwd=ROOT)
        print('Parse start requested in the background. You can close SSH.\n'
              f'Status: systemctl status {SERVICE}\n'
              f'Logs: journalctl -u {SERVICE} -f\n'
              f'Stop: systemctl stop {SERVICE}', flush=True)


if __name__ == '__main__':
    main()

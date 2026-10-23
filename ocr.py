#!/usr/bin/env python3
"""Run from an existing scraper checkout: uv run ocr.py (no flags).

Prepare only the additive OCR runtime, install its independent systemd units,
and start OCR detached from this SSH session. Existing scraper modules and
configuration are imported in place, never copied over or replaced.
"""
from __future__ import annotations
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
SERVICE = 'scraper-ocr.service'
REMINDER_UNITS = ['scraper-ocr-reminder.timer', 'scraper-ocr-reminder.service']
UNIT_ENV_KEYS = (
    'SSH_HOST', 'SSH_USER', 'SSH_PORT', 'SSH_KEY', 'SSH_DIR',
    'MAIL_PASSWORD', 'PRASER_MAX_STREAM_MB',
)
STARTUP_WATCH_SECONDS = 5.0
STARTUP_POLL_INTERVAL = 0.25


def call(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def journal_tail(unit: str, lines: int = 50) -> str:
    res = subprocess.run(
        ['journalctl', '-u', unit, '-n', str(lines), '--no-pager', '-o', 'cat'],
        capture_output=True, text=True, check=False,
    )
    return (res.stdout or '').strip()


def service_props(unit: str) -> dict[str, str]:
    res = subprocess.run(
        ['systemctl', 'show', unit,
         '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result', '-p', 'ExecMainStatus'],
        capture_output=True, text=True, check=False,
    )
    props: dict[str, str] = {}
    for line in (res.stdout or '').splitlines():
        if '=' in line:
            key, val = line.split('=', 1)
            props[key.strip()] = val.strip()
    return props


def inspect_unit_config():
    """Validate and report the exact effective config systemd will run with."""
    dotenv_path = ROOT / '.env'
    if dotenv_path.is_dir():
        sys.exit(
            f'{dotenv_path} is a directory instead of a file '
            f'(likely created by uploading to "scraper/.env" instead of "scraper/").\n'
            f'Fix: mv {dotenv_path}/.env {dotenv_path}.tmp && rmdir {dotenv_path} '
            f'&& mv {dotenv_path}.tmp {dotenv_path}'
        )
    sys.path.insert(0, str(ROOT / 'crawler' / 'bin'))
    from mailer import load_mail_config
    from save_remote import RemoteError, load_config
    from site_login import load_dotenv_values

    dotenv = load_dotenv_values(dotenv_path)
    shell_vals = {k: os.environ[k] for k in UNIT_ENV_KEYS if os.environ.get(k, '').strip()}
    file_vals = {k: v for k, v in dotenv.items() if v.strip()}
    effective = {**shell_vals, **file_vals}

    saved = dict(os.environ)
    try:
        for k in UNIT_ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(effective)
        try:
            config = load_config()
        except RemoteError as error:
            sys.exit(str(error))
        mail_cfg, mail_reason = load_mail_config()
    finally:
        os.environ.clear()
        os.environ.update(saved)

    if not dotenv_path.is_file():
        print(
            f'[env-warn] {dotenv_path} not found; staging current shell settings '
            f'into scan/ocr/launch.env so {SERVICE} receives them.',
            flush=True,
        )
    else:
        overridden = sorted(
            k for k in shell_vals
            if k in file_vals and shell_vals[k].strip() != file_vals[k].strip()
        )
        if overridden:
            print(
                f'[env] {dotenv_path} overrides exported shell setting(s): '
                + ', '.join(overridden),
                flush=True,
            )
    print(f'Using existing scraper SSH settings: {config.user}@{config.host}:{config.port}',
          flush=True)
    if mail_cfg is None:
        print(
            f'[mail-warn] Email alerts are OFF ({mail_reason} in both {dotenv_path} '
            f'and shell environment) - NO started/finished/failed emails will be sent! '
            f'Set MAIL_PASSWORD in {dotenv_path} to enable email notices.',
            flush=True,
        )
    else:
        print(
            f'[mail] Email alerts ON: {mail_cfg.user} -> {mail_cfg.recipient} '
            f'via {mail_cfg.host}:{mail_cfg.port}',
            flush=True,
        )
    return config


def start_and_verify(unit: str, label: str) -> None:
    """Start unit, block through ExecStartPre, and confirm it does not die at boot."""
    started = subprocess.run(
        ['systemctl', 'start', unit],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if started.returncode != 0:
        err = (started.stderr or '').strip()
        if err:
            print(err, file=sys.stderr, flush=True)
        logs = journal_tail(unit, 60)
        if logs:
            print(f'\n--- {unit} journal ---\n{logs}', file=sys.stderr, flush=True)
        sys.exit(f'{unit} failed during start (exit {started.returncode}).')

    deadline = time.monotonic() + STARTUP_WATCH_SECONDS
    while time.monotonic() < deadline:
        props = service_props(unit)
        state = props.get('ActiveState', '')
        result = props.get('Result', '')
        code = props.get('ExecMainStatus', '')
        if state == 'failed' or (state == 'inactive' and result and result != 'success'):
            logs = journal_tail(unit, 60)
            if logs:
                print(f'\n--- {unit} journal (full lines, untruncated) ---\n{logs}',
                      file=sys.stderr, flush=True)
            sys.exit(
                f'{unit} exited with failure immediately after start '
                f'(Result={result or "unknown"}, status={code or "unknown"}). '
                f'See untruncated log above.'
            )
        if state == 'inactive' and result == 'success':
            logs = journal_tail(unit, 20)
            if logs:
                print(f'\n--- {unit} journal ---\n{logs}', flush=True)
            print(f'{label} completed immediately with no remaining PDFs to process.',
                  flush=True)
            return
        time.sleep(STARTUP_POLL_INTERVAL)

    logs = journal_tail(unit, 15)
    if logs:
        print(f'\n--- {unit} startup log ---\n{logs}\n---', flush=True)
    print(f'{label} is active and running in the background. You can close SSH.\n'
          f'Status: systemctl status {unit} --no-pager -l\n'
          f'Logs:   journalctl -u {unit} -f\n'
          f'Stop:   systemctl stop {unit}', flush=True)


def main():
    required = ['crawler/bin/save_remote.py', 'crawler/bin/mailer.py',
                'crawler/bin/text_collect.py', 'crawler/bin/segment_bridge.mjs',
                'deploy/engine_ready.py', 'Engine/MonoSyllabism']
    missing = [p for p in required if not (ROOT / p).exists()]
    if missing:
        sys.exit('Extract the OCR overlay directly into your existing scraper directory. Missing: '
                 + ', '.join(missing))
    if os.geteuid() != 0 or not Path('/run/systemd/system').is_dir():
        sys.exit('Run uv run ocr.py as root on your systemd VPS (same account as scraper.service).')
    for executable in ['uv', 'node', 'npm', 'ssh', 'systemctl']:
        if not shutil.which(executable):
            sys.exit(f'{executable} must be installed and on PATH.')
    node_version = subprocess.check_output(['node', '--version'], text=True).strip()
    if int(node_version.lstrip('v').split('.')[0]) < 22:
        sys.exit('Node 22 or newer is required.')
    state_dir = ROOT / 'scan' / 'ocr'
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / 'launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        active = subprocess.run(['systemctl', 'is-active', SERVICE],
                                capture_output=True, text=True).stdout.strip()
        if active in {'active', 'activating', 'reloading'}:
            print(f'OCR is already running. Logs: journalctl -u {SERVICE} -f')
            return
        if active == 'deactivating':
            sys.exit('OCR is stopping; run this command again after it stops.')
        inspect_unit_config()
        runtime = ROOT / 'ocr_runtime' / 'Praser' / 'parser'
        digest = hashlib.sha256(node_version.encode())
        for name in ['package.json', 'package-lock.json']:
            digest.update((runtime / name).read_bytes())
        fingerprint = digest.hexdigest()
        stamp = runtime / 'node_modules' / '.ocr-install-fingerprint'
        if not stamp.exists() or stamp.read_text() != fingerprint:
            print('Preparing additive OCR dependencies (first run or runtime change)...', flush=True)
            call('npm', 'ci', '--prefix', runtime, '--include=dev', '--include=optional',
                 '--no-audit', '--no-fund', cwd=ROOT)
            stamp.write_text(fingerprint)
        # Stop only our reminder units before refreshing our unit definitions.
        # An absent unit on first run is normal. Never stop scraper.service.
        subprocess.run(['systemctl', 'stop', *REMINDER_UNITS], check=False)
        call(sys.executable, ROOT / 'deploy' / 'continuous-ocr' / 'install.py', cwd=ROOT)
        start_and_verify(SERVICE, 'OCR service')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Install independent OCR service and failure-reminder timer; does not start OCR."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
STATE = ROOT / 'scan' / 'ocr'
LAUNCH_ENV = STATE / 'launch.env'

sys.path.insert(0, str(ROOT / 'crawler' / 'bin'))
from site_login import load_dotenv_values  # noqa: E402

ENV_KEYS = (
    'SSH_HOST', 'SSH_USER', 'SSH_PORT', 'SSH_KEY', 'SSH_DIR',
    'MAIL_PASSWORD', 'PRASER_MAX_STREAM_MB',
)


def q(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def stage_launch_env(target: Path, keys: tuple[str, ...]) -> None:
    """Persist shell settings absent from .env in a 0600 file read BEFORE .env."""
    dotenv = load_dotenv_values(ROOT / '.env')
    captured = {
        name: os.environ[name]
        for name in keys
        if os.environ.get(name, '').strip() and not dotenv.get(name, '').strip()
    }
    if not captured:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f'.{target.name}.{os.getpid()}.tmp')
    lines = []
    for name, value in sorted(captured.items()):
        escaped = (
            value.replace('\\', '\\\\')
            .replace('"', '\\"')
            .replace('\n', '\\n')
            .replace('\r', '\\r')
            .replace('\t', '\\t')
        )
        lines.append(f'{name}="{escaped}"\n')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.writelines(lines)
    os.replace(tmp, target)


def main():
    if os.geteuid() != 0 or not Path('/run/systemd/system').is_dir():
        sys.exit('Run as root on the systemd VPS.')
    node, uv = shutil.which('node'), shutil.which('uv')
    if not node or not uv:
        sys.exit('Install Node >=22, npm, and uv first; make them available on PATH.')
    version = subprocess.check_output([node, '--version'], text=True).strip()
    if int(version.lstrip('v').split('.')[0]) < 22:
        sys.exit('Node >=22 is required for the bundled PDF renderer.')
    if not shutil.which('ssh'):
        sys.exit('Install openssh-client first.')
    for name in ['scraper-ocr.service', 'scraper-ocr-reminder.service']:
        state = subprocess.run(['systemctl', 'show', name, '-p', 'ActiveState', '--value'],
                               capture_output=True, text=True).stdout.strip()
        if state in {'active', 'activating', 'deactivating'}:
            sys.exit(f'Stop {name} before installing.')
    stage_launch_env(LAUNCH_ENV, ENV_KEYS)
    path = ':'.join(dict.fromkeys([str(Path(node).parent), str(Path(uv).parent),
                                  '/usr/local/sbin', '/usr/local/bin', '/usr/sbin',
                                  '/usr/bin', '/sbin', '/bin']))
    command = ' '.join(map(q, [uv, 'run', '--project', ROOT, '--', 'python', '-u', HERE / 'run.py']))
    preflight = ' '.join(map(q, [uv, 'run', '--project', ROOT, '--', 'python', ROOT / 'deploy' / 'engine_ready.py', '--fix']))
    root_esc = str(ROOT).replace('%', '%%')
    launch_esc = str(LAUNCH_ENV).replace('%', '%%')
    common = f'''User=root
WorkingDirectory={root_esc}
Environment=PATH={path}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-{launch_esc}
EnvironmentFile=-{root_esc}/.env
StandardOutput=journal
StandardError=journal
'''
    units = {
        'scraper-ocr.service': f'''[Unit]
Description=Continuous scanned PDF OCR into scraper Parquet
Wants=network-online.target scraper-ocr-reminder.timer
After=network-online.target
[Service]
Type=exec
{common}ExecStartPre={preflight}
ExecStart={command}
ExecStopPost={command} --failure-hook
Restart=no
TimeoutStartSec=infinity
TimeoutStopSec=90
RuntimeMaxSec=infinity
KillSignal=SIGINT
KillMode=control-group
[Install]
WantedBy=multi-user.target
''',
        'scraper-ocr-reminder.service': f'''[Unit]
Description=Retained failed OCR PDF reminder check
[Service]
Type=oneshot
{common}ExecStart={command} --remind
TimeoutStartSec=infinity
''',
        'scraper-ocr-reminder.timer': '''[Unit]
Description=Check retained OCR failures independently of OCR queue
[Timer]
# One check an hour, matching the one-mail-an-hour gate in reminders(). The boot
# check fires once per boot so a restart does not wait an hour to be noticed.
# AccuracySec is relaxed because 10s precision on an hourly timer buys nothing and
# stops systemd coalescing the wakeup with other timers.
OnBootSec=2min
OnUnitInactiveSec=1h
AccuracySec=1min
Unit=scraper-ocr-reminder.service
[Install]
WantedBy=timers.target
''',
    }
    for name, content in units.items():
        target = Path('/etc/systemd/system') / name
        target.write_text(content)
        target.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'scraper-ocr-reminder.timer'], check=True)
    if '--from-launcher' in sys.argv[1:]:
        # ocr.py starts and verifies the unit itself right after this returns.
        # Printing "has NOT started" here would contradict its next line.
        print('Installed scraper-ocr.service unit files; starting it now...', flush=True)
        return
    print('Installed. OCR has NOT started.\n'
          'Start: systemctl start scraper-ocr.service\n'
          'Logs: journalctl -u scraper-ocr.service -f\n'
          'Stop OCR: systemctl stop scraper-ocr.service\n'
          'Stop reminders too: systemctl disable --now scraper-ocr-reminder.timer')


if __name__ == '__main__':
    main()

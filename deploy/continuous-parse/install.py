#!/usr/bin/env python3
"""Install the independent convertable-parse service and its reminder timer.

A duplicate of deploy/continuous-ocr/install.py with the same unit shape - same
ExecStartPre engine check, same ExecStopPost failure hook, same Restart=no, same
SIGINT on stop, same hourly reminder timer - under its own three unit names so the
two services can run side by side without touching each other. Installs only; it
does not start the parse service.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

SERVICE = 'scraper-parse.service'
REMINDER_SERVICE = 'scraper-parse-reminder.service'
REMINDER_TIMER = 'scraper-parse-reminder.timer'


def q(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def main():
    if os.geteuid() != 0 or not Path('/run/systemd/system').is_dir():
        sys.exit('Run as root on the systemd VPS.')
    node, uv = shutil.which('node'), shutil.which('uv')
    if not node or not uv:
        sys.exit('Install Node >=22, npm, and uv first; make them available on PATH.')
    version = subprocess.check_output([node, '--version'], text=True).strip()
    if int(version.lstrip('v').split('.')[0]) < 22:
        sys.exit('Node >=22 is required for the segmentor bridge.')
    if not shutil.which('ssh'):
        sys.exit('Install openssh-client first.')
    for name in [SERVICE, REMINDER_SERVICE]:
        state = subprocess.run(['systemctl', 'show', name, '-p', 'ActiveState', '--value'],
                               capture_output=True, text=True).stdout.strip()
        if state in {'active', 'activating', 'deactivating'}:
            sys.exit(f'Stop {name} before installing.')
    path = ':'.join(dict.fromkeys([str(Path(node).parent), str(Path(uv).parent),
                                   '/usr/local/sbin', '/usr/local/bin', '/usr/sbin',
                                   '/usr/bin', '/sbin', '/bin']))
    command = ' '.join(map(q, [uv, 'run', '--project', ROOT, '--', 'python', '-u', HERE / 'run.py']))
    preflight = ' '.join(map(q, [uv, 'run', '--project', ROOT, '--', 'python',
                                 ROOT / 'deploy' / 'engine_ready.py', '--fix']))
    common = f'''User=root
WorkingDirectory={str(ROOT).replace('%', '%%')}
Environment=PATH={path}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-{str(ROOT).replace('%', '%%')}/.env
StandardOutput=journal
StandardError=journal
'''
    units = {
        SERVICE: f'''[Unit]
Description=Continuous convertable PDF parse into scraper Parquet
Wants=network-online.target {REMINDER_TIMER}
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
        REMINDER_SERVICE: f'''[Unit]
Description=Retained failed convertable PDF reminder check
[Service]
Type=oneshot
{common}ExecStart={command} --remind
TimeoutStartSec=infinity
''',
        REMINDER_TIMER: f'''[Unit]
Description=Check retained parse failures independently of the parse queue
[Timer]
# One check an hour, matching the one-mail-an-hour gate in reminders(). The boot
# check fires once per boot so a restart does not wait an hour to be noticed.
# AccuracySec is relaxed because 10s precision on an hourly timer buys nothing and
# stops systemd coalescing the wakeup with other timers.
OnBootSec=2min
OnUnitInactiveSec=1h
AccuracySec=1min
Unit={REMINDER_SERVICE}
[Install]
WantedBy=timers.target
''',
    }
    for name, content in units.items():
        target = Path('/etc/systemd/system') / name
        target.write_text(content)
        target.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', REMINDER_TIMER], check=True)
    print(f'Installed. The parse service has NOT started.\n'
          f'Start: systemctl start {SERVICE}\n'
          f'Logs: journalctl -u {SERVICE} -f\n'
          f'Stop: systemctl stop {SERVICE}\n'
          f'Stop reminders too: systemctl disable --now {REMINDER_TIMER}')


if __name__ == '__main__':
    main()

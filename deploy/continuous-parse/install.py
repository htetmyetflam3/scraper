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
STATE = ROOT / 'scan' / 'parse'
LAUNCH_ENV = STATE / 'launch.env'

sys.path.insert(0, str(ROOT / 'crawler' / 'bin'))
from site_login import load_dotenv_values  # noqa: E402

SERVICE = 'scraper-parse.service'
REMINDER_SERVICE = 'scraper-parse-reminder.service'
REMINDER_TIMER = 'scraper-parse-reminder.timer'
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
        sys.exit('Node >=22 is required for the segmentor bridge.')
    if not shutil.which('ssh'):
        sys.exit('Install openssh-client first.')
    for name in [SERVICE, REMINDER_SERVICE]:
        state = subprocess.run(['systemctl', 'show', name, '-p', 'ActiveState', '--value'],
                               capture_output=True, text=True).stdout.strip()
        if state in {'active', 'activating', 'deactivating'}:
            sys.exit(f'Stop {name} before installing.')
    stage_launch_env(LAUNCH_ENV, ENV_KEYS)
    path = ':'.join(dict.fromkeys([str(Path(node).parent), str(Path(uv).parent),
                                   '/usr/local/sbin', '/usr/local/bin', '/usr/sbin',
                                   '/usr/bin', '/sbin', '/bin']))
    command = ' '.join(map(q, [uv, 'run', '--project', ROOT, '--', 'python', '-u', HERE / 'run.py']))
    preflight = ' '.join(map(q, [uv, 'run', '--project', ROOT, '--', 'python',
                                 ROOT / 'deploy' / 'engine_ready.py', '--fix']))
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
    if '--from-launcher' in sys.argv[1:]:
        # parse.py starts and verifies the unit itself right after this returns.
        # Printing "has NOT started" here would contradict its next line.
        print(f'Installed {SERVICE} unit files; starting it now...', flush=True)
        return
    print(f'Installed. The parse service has NOT started.\n'
          f'Start: systemctl start {SERVICE}\n'
          f'Logs: journalctl -u {SERVICE} -f\n'
          f'Stop: systemctl stop {SERVICE}\n'
          f'Stop reminders too: systemctl disable --now {REMINDER_TIMER}')


if __name__ == '__main__':
    main()

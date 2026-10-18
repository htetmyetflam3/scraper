"""Launch CLI runs as transient systemd services on a root/systemd host.

`main.py` calls this before importing the crawler. It returns None when the
caller should continue directly (help, explicit opt-out, or no system manager).
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"
ENV_DIR = Path("/run/scraper-main-env")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _environment_file(values: dict[str, str], path: Path) -> None:
    """Write systemd EnvironmentFile syntax in a root-only transient file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    lines = []
    for name, value in sorted(values.items()):
        if not ENV_NAME.fullmatch(name):
            continue
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        lines.append(f'{name}="{escaped}"\n')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.writelines(lines)


def launch_under_systemd(args: list[str]) -> int | None:
    """Start one detached transient unit for this exact main.py invocation."""
    if os.environ.get("SCRAPER_SYSTEMD_CHILD") == "1":
        return None
    if os.environ.get("SCRAPER_SYSTEMD_DIRECT") == "1":
        return None
    if not args or "--help" in args or "-h" in args:
        return None
    if os.geteuid() != 0 or not Path("/run/systemd/system").is_dir():
        return None

    systemd_run = shutil.which("systemd-run")
    if systemd_run is None:
        print("[systemd] systemd-run not found; running main.py directly", file=sys.stderr)
        return None

    systemctl = shutil.which("systemctl")
    if systemctl is not None:
        subprocess.run(
            [systemctl, "stop", "scraper-main-*.service", "scraper.service"],
            capture_output=True,
            text=True,
            check=False,
        )

    unit = f"scraper-main-{uuid.uuid4().hex[:12]}.service"
    env_file = ENV_DIR / f"{unit}.env"
    env = dict(os.environ)
    env["SCRAPER_SYSTEMD_CHILD"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        _environment_file(env, env_file)
    except OSError as error:
        print(f"[systemd] cannot stage runtime environment ({error}); refusing to run detached", file=sys.stderr)
        return 1

    cleanup = f"/usr/bin/rm -f {env_file}"
    engine_ready = f"{sys.executable} -u {ROOT / 'deploy' / 'engine_ready.py'} --fix"
    command = [
        systemd_run,
        f"--unit={unit}",
        "--collect",
        "--no-block",
        f"--working-directory={ROOT}",
        "--property=Type=exec",
        "--property=User=root",
        "--property=KillSignal=SIGINT",
        "--property=TimeoutStartSec=1800",
        "--property=TimeoutStopSec=120",
        "--property=StandardOutput=journal",
        "--property=StandardError=journal",
        f"--property=EnvironmentFile=-{env_file}",
        f"--property=ExecStartPre={engine_ready}",
        f"--property=ExecStopPost={cleanup}",
        "--",
        sys.executable,
        "-u",
        str(MAIN),
        *args,
    ]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    except OSError as error:
        env_file.unlink(missing_ok=True)
        print(f"[systemd] could not start {unit}: {error}", file=sys.stderr)
        return 1
    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    if result.returncode != 0:
        env_file.unlink(missing_ok=True)
        if output:
            print(output, file=sys.stdout)
        if error:
            print(error, file=sys.stderr)
        return result.returncode
    if output:
        print(output)
    print(f"[systemd] started {unit}; logs: journalctl -u {unit} -f; stop: systemctl stop {unit}")
    return 0

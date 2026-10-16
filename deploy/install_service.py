#!/usr/bin/env python3
"""Install one systemd job using the supplied main.py arguments (does not start it)."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

SERVICE = "scraper.service"
ROOT = Path(__file__).resolve().parent.parent


def quote(value: str, *, command: bool = False) -> str:
    # systemd unit quoting, not shell quoting. Specifiers expand even in quotes;
    # environment expansion additionally applies to ExecStart arguments.
    value = value.replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"' + value + '"'


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if not args or args == ["--help"]:
        print("Usage: python3 deploy/install_service.py -- <main.py flags>\n"
              "Example: python3 deploy/install_service.py -- --process -l 2 -i 60\n"
              "Installs scraper.service; does not start it or enable it at boot.")
        return 0 if args else 2
    if os.geteuid() != 0:
        sys.exit("Run this installer as root.")
    if not Path("/run/systemd/system").is_dir():
        sys.exit("systemd must be running as the VPS system manager.")
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv is not on PATH. Run this from the same environment as uv run main.py.")
    state = subprocess.run(
        ["systemctl", "show", SERVICE, "--property=ActiveState", "--value"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if state not in {"inactive", "failed"}:
        sys.exit(f"{SERVICE} is {state}; stop it before replacing its flags.")
    command = [uv, "run", "--", "python", "-u", str(ROOT / "main.py"), *args]
    unit = f"""[Unit]
Description=Scraper unattended run
Wants=network-online.target
After=network-online.target

[Service]
Type=exec
User=root
WorkingDirectory={quote(str(ROOT))}
Environment=PYTHONUNBUFFERED=1
ExecStart={' '.join(quote(arg, command=True) for arg in command)}
StandardInput=null
StandardOutput=journal
StandardError=journal
Restart=no
KillSignal=SIGINT
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
"""
    target = Path("/etc/systemd/system") / SERVICE
    temporary = target.with_suffix(".service.tmp")
    temporary.write_text(unit)
    temporary.chmod(0o644)
    temporary.replace(target)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print(f"Installed {target}\n"
          "Start:  systemctl start scraper.service\n"
          "Status: systemctl status scraper.service\n"
          "Logs:   journalctl -u scraper.service -f\n"
          "Stop:   systemctl stop scraper.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Install one systemd job using the supplied main.py arguments (does not start it).

One `systemctl start scraper.service` is the whole automated process: the unit
runs `deploy/engine_ready.py --fix` first, so the run-end JS engine (Node +
`Engine/MonoSyllabism/map/payload.jsc` bytecode + the precomputed syllable
tree) is validated and, if the bytecode was compiled by another Node, rebuilt
before main.py starts. The Node directory found at install time is written
into the unit's PATH, because systemd does not read your shell profile.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

SERVICE = "scraper.service"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def quote(value: str, *, command: bool = False) -> str:
    # systemd unit quoting, not shell quoting. Specifiers expand even in quotes;
    # environment expansion additionally applies to ExecStart arguments.
    value = value.replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"' + value + '"'


def render_unit(uv: str, node: str, args: list[str]) -> str:
    """The unit text. node is used for its directory: systemd has no shell PATH."""
    node_dir = str(Path(node).resolve().parent)
    parts: list[str] = []
    for entry in [node_dir, *DEFAULT_PATH.split(":")]:
        if entry not in parts:
            parts.append(entry)
    unit_path = ":".join(parts)
    engine = [uv, "run", "--", "python", "-u", str(ROOT / "deploy" / "engine_ready.py"), "--fix"]
    command = [uv, "run", "--", "python", "-u", str(ROOT / "main.py"), *args]
    return f"""[Unit]
Description=Scraper unattended run
Wants=network-online.target
After=network-online.target

[Service]
Type=exec
User=root
WorkingDirectory={str(ROOT).replace("%", "%%")}
Environment=PYTHONUNBUFFERED=1
Environment=PATH={unit_path}
ExecStartPre={' '.join(quote(arg, command=True) for arg in engine)}
ExecStart={' '.join(quote(arg, command=True) for arg in command)}
StandardInput=null
StandardOutput=journal
StandardError=journal
Restart=no
KillSignal=SIGINT
TimeoutStartSec=1800
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
"""


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if not args or args == ["--help"]:
        print("Usage: python3 deploy/install_service.py -- <main.py flags>\n"
              "Example: python3 deploy/install_service.py -- --process -l 2 -i 60\n"
              "Installs scraper.service; does not start it or enable it at boot.\n"
              "At start it checks the JS engine (node + bytecode + tree) and rebuilds\n"
              "the bytecode if this machine's Node needs it (deploy/engine_ready.py).")
        return 0 if args else 2
    if os.geteuid() != 0:
        sys.exit("Run this installer as root.")
    if not Path("/run/systemd/system").is_dir():
        sys.exit("systemd must be running as the VPS system manager.")
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv is not on PATH. Run this from the same environment as uv run main.py.")
    node = shutil.which("node")
    if not node:
        sys.exit(
            "Node.js 22+ is not on PATH; select an existing Node 22+ binary. "
            "This installer does not install or replace Node."
        )
    node_version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        node_major = int(node_version.lstrip("v").split(".", 1)[0])
    except ValueError:
        sys.exit(f"Cannot parse Node version {node_version!r} from {node!r}.")
    if node_major < 22:
        sys.exit(
            f"Node.js 22+ is required; found {node_version} at {node}. "
            "Select an existing 22+ binary; no Node installation or downgrade was attempted."
        )
    if not shutil.which("npm"):
        print(
            "warning: npm is not on PATH; if the Engine bytecode must be rebuilt for "
            "this machine's Node, engine_ready.py cannot do it (npm run rebuild).",
            file=sys.stderr,
        )
    state = subprocess.run(
        ["systemctl", "show", SERVICE, "--property=ActiveState", "--value"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if state not in {"inactive", "failed"}:
        sys.exit(f"{SERVICE} is {state}; stop it before replacing its flags.")
    unit = render_unit(uv, node, args)
    target = Path("/etc/systemd/system") / SERVICE
    temporary = target.with_suffix(".service.tmp")
    temporary.write_text(unit)
    temporary.chmod(0o644)
    temporary.replace(target)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print(f"Installed {target}\n"
          f"Node:   {node}\n"
          "Start:  systemctl start scraper.service\n"
          "Status: systemctl status scraper.service\n"
          "Logs:   journalctl -u scraper.service -f\n"
          "Stop:   systemctl stop scraper.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

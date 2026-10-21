"""One-shot file transfer to and from the VPS over the hostingervps ssh alias.

A thin wrapper around ``ssh hostingervps``: connect, run the action the flag
asks for, done. Both directions run from the local container only - never on
the VPS itself.

    uv run vps.py --upload   LOCAL   REMOTE_DIR
    uv run vps.py --download REMOTE  LOCAL_DIR

    uv run vps.py --upload   ./.env                  scraper/
    uv run vps.py --upload   ./pdfs/parquet/         /root/scraper/parquet/
    uv run vps.py --download '~/scraper/.env'        ./
    uv run vps.py --download scraper/pdfs/parquet/   ./parquet/

The flag order is the only difference: --upload is local-then-remote,
--download is remote-then-local. The first argument is the source, the second
is always a directory to save into.

Rules:
- The LOCAL side is relative to where you run the script (your cwd), like any
  other shell command.
- The REMOTE side is relative to the VPS account home, so ``scraper/.env`` and
  ``~/scraper/.env`` both mean ``/root/scraper/.env``; an absolute path is used
  as given. Quote a leading ``~`` - unquoted, your own shell rewrites it to
  your LOCAL home before this script ever sees it.
- A path that ends in ``/`` - or an existing directory - transfers every file
  inside it automatically. Sub-directories are skipped, not followed.
- One-shot on purpose: no resume, no retry waits, no sha256 pass. For the big
  Parquet collection use ./vps-pull.sh, which resumes and verifies.
- Nothing is deleted on either box. A destination file of the same name is
  overwritten.

The private key is hardcoded and pinned with IdentitiesOnly, so only that key
is offered to the VPS and never a general key used for other hosts. HostName,
Port and User come from the ~/.ssh/config "hostingervps" alias.

exit codes: 0 ok, 1 failed, 2 usage error
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import IO

# ── the VPS. Edit these lines. ────────────────────────────────────────────
SSH_HOST = "hostingervps"  # ssh alias in this box's ~/.ssh/config
SSH_KEY = Path.home() / ".ssh" / "mylinux"  # the VPS private key, hardcoded
CONNECT_TIMEOUT = 20


class VpsError(RuntimeError):
    """Unreachable VPS, missing key or bad argument."""


def _log(message: str) -> None:
    print(message, flush=True)


def _ssh(command: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o",
        "ServerAliveInterval=30",
        "-i",
        str(SSH_KEY),
        "-o",
        "IdentitiesOnly=yes",
        SSH_HOST,
        command,
    ]


def _run(
    command: str,
    *,
    stdin=None,
    stdout: int | IO[bytes] | None = subprocess.PIPE,
) -> subprocess.CompletedProcess:
    """Run one command on the VPS. stdout is captured unless a file is given
    (the download path streams straight into the destination file)."""
    try:
        return subprocess.run(
            _ssh(command),
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as error:
        raise VpsError("ssh binary not found; install openssh-client") from error


def _fail(result: subprocess.CompletedProcess, what: str) -> None:
    detail = (result.stderr or "").strip().splitlines()
    raise VpsError(f"{what}: {detail[-1] if detail else f'exit {result.returncode}'}")


def preflight() -> str:
    """Check the key and the connection; return the VPS account home."""
    if not SSH_KEY.is_file():
        raise VpsError(
            f"ssh private key not found: {SSH_KEY}\n"
            f"        copy your VPS key there, or edit SSH_KEY in {Path(__file__).name}"
        )
    result = _run("echo $HOME")  # expanded by the remote shell, not here
    if result.returncode != 0:
        _fail(result, f"cannot connect to {SSH_HOST}")
    home = (result.stdout or "").strip().splitlines()
    if not home or not home[-1].startswith("/"):
        raise VpsError(f"cannot read the account home on {SSH_HOST}: {home!r}")
    return home[-1]


def remote_path(raw: str, home: str) -> str:
    """Absolute path on the VPS: ~ and relative paths resolve in `home`."""
    path = raw.strip()
    if path.startswith("~"):
        path = path[1:].lstrip("/")
    if path.startswith("/"):
        return path
    return f"{home.rstrip('/')}/{path}" if path else home


def wants_directory(raw: str, exists: bool) -> bool:
    """A trailing slash, or an existing directory, means 'the files inside'."""
    return raw.endswith("/") or exists


def expand_local(raw: str) -> list[Path]:
    """One local file, or every file inside a local directory."""
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = Path.cwd() / path  # relative to where you ran the script
    if not wants_directory(raw, path.is_dir()):
        if not path.is_file():
            raise VpsError(f"no such local file: {path}")
        return [path]
    if not path.is_dir():
        raise VpsError(f"no such local directory: {path}")
    files = sorted(entry for entry in path.iterdir() if entry.is_file())
    if not files:
        raise VpsError(f"no files inside {path}")
    return files


def expand_remote(raw: str, home: str) -> list[str]:
    """One remote file, or every file inside a remote directory."""
    target = remote_path(raw, home)
    if not wants_directory(raw, False):
        return [target]
    directory = target.rstrip("/")
    result = _run(f"ls -1p -- {shlex.quote(directory)}")
    if result.returncode != 0:
        _fail(result, f"cannot list {directory} on {SSH_HOST}")
    names = [
        line.strip()
        for line in (result.stdout or "").splitlines()
        if line.strip() and not line.strip().endswith("/")  # skip sub-directories
    ]
    if not names:
        raise VpsError(f"no files inside {SSH_HOST}:{directory}")
    return [f"{directory}/{name}" for name in sorted(names)]


def upload(sources: list[Path], remote_dir: str, home: str) -> None:
    directory = remote_path(remote_dir, home)
    result = _run(f"mkdir -p -- {shlex.quote(directory)}")
    if result.returncode != 0:
        _fail(result, f"cannot create {directory} on {SSH_HOST}")
    for index, local in enumerate(sources, 1):
        target = f"{directory.rstrip('/')}/{local.name}"
        _log(f"[vps] -> {local} ({local.stat().st_size} bytes) to {SSH_HOST}:{target}")
        with local.open("rb") as handle:
            sent = _run(f"cat > {shlex.quote(target)}", stdin=handle)
        if sent.returncode != 0:
            _fail(sent, f"upload of {local.name} failed")
        _log(f"[vps] ok {index}/{len(sources)}")


def download(sources: list[str], local_dir: Path) -> None:
    local_dir.mkdir(parents=True, exist_ok=True)
    for index, remote in enumerate(sources, 1):
        target = local_dir / remote.rstrip("/").rsplit("/", 1)[-1]
        _log(f"[vps] <- {SSH_HOST}:{remote} to {target}")
        with target.open("wb") as handle:
            got = _run(f"cat -- {shlex.quote(remote)}", stdout=handle)
        if got.returncode != 0:
            target.unlink(missing_ok=True)  # our own empty file, nothing else
            _fail(got, f"download of {target.name} failed")
        _log(f"[vps] ok {index}/{len(sources)} ({target.stat().st_size} bytes)")


def local_dir_arg(raw: str) -> Path:
    path = Path(os.path.expanduser(raw))
    return path if path.is_absolute() else Path.cwd() / path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vps.py",
        description=(
            "One-shot file transfer to/from the VPS over the hostingervps ssh "
            "alias. --upload is LOCAL then REMOTE_DIR; --download is REMOTE "
            "then LOCAL_DIR. A path ending in / transfers the files inside it."
        ),
        epilog=(
            "examples:\n"
            "  uv run vps.py --upload ./.env scraper/\n"
            "  uv run vps.py --upload ./pdfs/parquet/ /root/scraper/parquet/\n"
            "  uv run vps.py --download '~/scraper/.env' ./\n"
            "  uv run vps.py --download scraper/pdfs/parquet/ ./parquet/\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    direction = parser.add_mutually_exclusive_group(required=True)
    direction.add_argument(
        "--upload", action="store_true", help="push a local file or directory to a remote directory"
    )
    direction.add_argument(
        "--download", action="store_true", help="pull a remote file or directory to a local directory"
    )
    parser.add_argument(
        "source", metavar="SOURCE", help="local path (--upload) or remote path (--download)"
    )
    parser.add_argument(
        "destination",
        metavar="DEST",
        help="remote directory (--upload) or local directory (--download)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        home = preflight()
        if args.upload:
            sources = expand_local(args.source)
            _log(f"[vps] upload {len(sources)} file(s) -> {SSH_HOST}:{args.destination}")
            upload(sources, args.destination, home)
        else:
            sources = expand_remote(args.source, home)
            dest = local_dir_arg(args.destination)
            _log(f"[vps] download {len(sources)} file(s) -> {dest}")
            download(sources, dest)
        _log("[vps] done; nothing was deleted on either box")
        return 0
    except VpsError as error:
        _log(f"[vps] failed: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

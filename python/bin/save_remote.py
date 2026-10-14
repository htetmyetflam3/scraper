"""Remote saving over SSH: push saved files to a host instead of keeping them.

Used by Dcrawler's remote mode (main.py --download-remote). A download is
still written locally exactly as before; then this module pushes it over
plain ``ssh``, verifies the remote sha256 against the *local* bytes, appends
a line to scan/uploaded-host.txt, and only after that deletes the local copy
(unless keep_local=True).

Design rules, matching the rest of the pipeline:
- Plain ssh binary in BatchMode: it never prompts, so a missing key fails
  fast instead of hanging a crawl. Nothing extra is needed on the host
  (no rsync, no lftp).
- Interrupted uploads resume: the remote file size decides the byte offset
  and the remainder is streamed through ``cat >> file``. Every attempt is
  retried with a delay; a partial upload is never work lost.
- The ledger (scan/uploaded-host.txt) is append-only, keyed by sha256.
  A failed push keeps the local file on disk; the caller records the
  failure in scan/failed.txt.
- Local files are only deleted after a live sha256 verification of the
  remote bytes. Anything unverified stays on disk.

Configuration (environment wins over the repo-root .env):
    SSH_HOST   remote host name or IP          (required)
    SSH_USER   ssh account                     (required)
    SSH_PORT   ssh port                        (default 22)
    SSH_KEY    private key file on this box    (default: ssh's own lookup)
    SSH_DIR    remote directory relative to
               the account's home; keep it out
               of public_html                  (default books)
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
UPLOAD_LEDGER = REPO_ROOT / "scan" / "uploaded-host.txt"
SSH_BIN = "ssh"
RETRY_ATTEMPTS = 5
RETRY_DELAY = 20.0
PROGRESS_STEP = 50 * 1024 * 1024
CONNECT_TIMEOUT = 20
CONTROL_TIMEOUT = 120
HASH_TIMEOUT = 600


class RemoteError(ValueError):
    """Remote archive misconfigured or unreachable (main.py reports it)."""


@dataclass(frozen=True)
class RemoteConfig:
    host: str
    user: str
    port: int = 22
    key: str = ""
    directory: str = "books"

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"


@dataclass
class PushOutcome:
    ok: bool
    detail: str
    remote_path: str = ""
    resumed: bool = False
    skipped: bool = False
    deleted: bool = False


@dataclass
class UploadedLedger:
    by_digest: dict[str, str] = field(default_factory=dict)
    by_name_size: dict[tuple[str, int], str] = field(default_factory=dict)


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def _env_values() -> dict[str, str]:
    """KEY=VALUE pairs from the repo-root .env; blanks and comments skipped."""
    values: dict[str, str] = {}
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        entry = line.strip()
        if entry.startswith("export "):
            entry = entry[7:].lstrip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        name, value = entry.split("=", 1)
        value = value.strip()
        if value[:1] in ('"', "'"):
            closing_quote = value.find(value[0], 1)
            if closing_quote > 0:
                value = value[1:closing_quote]
        else:
            value = value.split("#", 1)[0].strip()
        values[name.strip()] = value
    return values


def load_config() -> RemoteConfig:
    """SSH_* settings from the environment, then the repo-root .env."""
    file_values = _env_values()

    def setting(name: str, default: str = "") -> str:
        return (
            os.environ.get(name, "").strip()
            or file_values.get(name, "").strip()
            or default
        )

    host = setting("SSH_HOST")
    user = setting("SSH_USER")
    if not host or not user:
        raise RemoteError(
            "Remote upload needs SSH_HOST and SSH_USER (environment or the "
            "repo-root .env); see the SSH_* block in .env.example."
        )
    port_text = setting("SSH_PORT", "22")
    try:
        port = int(port_text)
    except ValueError as error:
        raise RemoteError(f"SSH_PORT must be a number, got {port_text!r}") from error
    directory = setting("SSH_DIR", "books").strip().rstrip("/") or "books"
    return RemoteConfig(
        host=host,
        user=user,
        port=port,
        key=setting("SSH_KEY"),
        directory=directory,
    )


def _ssh_argv(config: RemoteConfig, command: str) -> list[str]:
    argv = [
        SSH_BIN,
        "-p",
        str(config.port),
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=6",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT}",
    ]
    if config.key:
        argv += ["-i", os.path.expanduser(config.key), "-o", "IdentitiesOnly=yes"]
    argv += [config.target, command]
    return argv


def _run(
    config: RemoteConfig,
    command: str,
    *,
    timeout: float | None = CONTROL_TIMEOUT,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            _ssh_argv(config, command),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise RemoteError("ssh binary not found; install openssh-client") from error
    except subprocess.TimeoutExpired as error:
        raise RemoteError(f"ssh timed out after {timeout}s: {command}") from error


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remote_size(config: RemoteConfig, remote_path: str) -> int | None:
    result = _run(config, f"stat -c %s {shlex.quote(remote_path)}")
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    return int(text) if text.isdigit() else None


def remote_sha256(config: RemoteConfig, remote_path: str) -> str | None:
    result = _run(
        config,
        f"sha256sum {shlex.quote(remote_path)}",
        timeout=HASH_TIMEOUT,
    )
    if result.returncode != 0:
        return None
    parts = (result.stdout or "").split()
    return parts[0] if parts and len(parts[0]) == 64 else None


def exists(config: RemoteConfig, remote_path: str) -> bool:
    return _run(config, f"test -e {shlex.quote(remote_path)}").returncode == 0


def remove(config: RemoteConfig, remote_path: str) -> None:
    _run(config, f"rm -f {shlex.quote(remote_path)}")


def ensure_dir(config: RemoteConfig, remote_dir: str) -> None:
    result = _run(config, f"mkdir -p {shlex.quote(remote_dir)}")
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"exit {result.returncode}"
        raise RemoteError(f"cannot create remote directory {remote_dir}: {detail}")


def probe(config: RemoteConfig) -> str:
    """Create the target directory and return the host's df line for it."""
    ensure_dir(config, config.directory)
    result = _run(config, f"df -h {shlex.quote(config.directory)}")
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"exit {result.returncode}"
        raise RemoteError(f"ssh check failed for {config.target}: {detail}")
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def read_uploaded(ledger: Path | None = None) -> UploadedLedger:
    """Append-only ledger, one TAB record per uploaded file:
    sha256 <TAB> name <TAB> remote_path <TAB> bytes <TAB> MM-DD HH:MM:SS
    """
    entries = UploadedLedger()
    target = ledger or UPLOAD_LEDGER
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        digest, name, remote_path, size_text = (
            parts[0].strip(),
            parts[1].strip(),
            parts[2].strip(),
            parts[3].strip(),
        )
        if len(digest) != 64 or not remote_path:
            continue
        entries.by_digest[digest] = remote_path
        if size_text.isdigit():
            entries.by_name_size[(name, int(size_text))] = remote_path
    return entries


def remember_uploaded(
    digest: str,
    name: str,
    remote_path: str,
    size: int,
    ledger: Path | None = None,
) -> None:
    target = ledger or UPLOAD_LEDGER
    line = (
        f"{digest}\t{name}\t{remote_path}\t{size}\t"
        f"{time.strftime('%m-%d %H:%M:%S')}"
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as error:
        print(f"[push-warn] cannot record {name}: {error}")


def _reserve_remote_name(config: RemoteConfig, remote_dir: str, name: str) -> str:
    base = Path(name)
    index = 1
    while index < 1000:
        candidate = (
            base.name if index == 1 else f"{base.stem} ({index}){base.suffix}"
        )
        remote_path = f"{remote_dir}/{candidate}"
        if not exists(config, remote_path):
            return remote_path
        index += 1
    raise RemoteError(f"no free remote name for {name}")


def _append_stream(
    config: RemoteConfig,
    remote_path: str,
    local_path: Path,
    offset: int,
    log,
    name: str,
) -> None:
    """Stream local_path's bytes from offset into ``cat >> remote_path``."""
    argv = _ssh_argv(config, f"cat >> {shlex.quote(remote_path)}")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    total = local_path.stat().st_size
    sent = offset
    report_at = offset + PROGRESS_STEP
    try:
        with local_path.open("rb") as handle:
            handle.seek(offset)
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                process.stdin.write(chunk)
                sent += len(chunk)
                if sent >= report_at and sent < total:
                    report_at = sent + PROGRESS_STEP
                    log(
                        f"[push] {name} {sent / 1048576:.1f}/"
                        f"{total / 1048576:.1f} MB"
                    )
    except (BrokenPipeError, OSError) as error:
        with contextlib.suppress(Exception):
            process.kill()
        process.wait()
        raise RemoteError(
            f"connection lost at {sent / 1048576:.1f} MB"
        ) from error
    finally:
        with contextlib.suppress(Exception):
            if process.stdin is not None:
                process.stdin.close()
    stderr = b""
    with contextlib.suppress(Exception):
        stderr = process.stderr.read() or b""
    code = process.wait()
    if code != 0:
        tail = stderr.decode("utf-8", "replace").strip().splitlines()
        message = tail[-1] if tail else f"ssh exit {code}"
        raise RemoteError(f"remote write failed: {message}")


def push_file(
    path: Path,
    config: RemoteConfig,
    *,
    digest: str | None = None,
    ledger: Path | None = None,
    keep_local: bool = False,
    timeline=None,
    log=print,
) -> PushOutcome:
    """Push one saved file, verify it remotely, then delete the local copy.

    Returns PushOutcome; ok=False leaves the local file untouched for the
    next run (the same file is retried and resumes from the remote size).
    """
    path = Path(path)
    name = path.name
    try:
        size = path.stat().st_size
    except OSError as error:
        return PushOutcome(False, f"local file vanished: {error}")
    remote_dir = config.directory
    remote_path = f"{remote_dir}/{name}"
    entries = read_uploaded(ledger)
    if digest is None:
        quick = entries.by_name_size.get((name, size))
        if quick:
            log(f"[push-skip] {name} already in uploaded-host.txt @ {_stamp()}")
            return PushOutcome(
                True,
                "already in uploaded-host.txt",
                remote_path=quick,
                skipped=True,
            )
        digest = sha256_file(path)
    known = entries.by_digest.get(digest)
    if known:
        log(f"[push-skip] {name} already in uploaded-host.txt @ {_stamp()}")
        return PushOutcome(
            True, "already in uploaded-host.txt", remote_path=known, skipped=True
        )
    ensure_dir(config, remote_dir)

    resumed = False
    verified_existing = False
    last_error = "unknown"
    attempt = 0
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            current = remote_size(config, remote_path)
            if current is None:
                offset = 0
            elif current == size:
                if remote_sha256(config, remote_path) == digest:
                    verified_existing = True
                    last_error = ""
                    break
                remote_path = _reserve_remote_name(config, remote_dir, name)
                offset = 0
            elif current < size:
                offset = current
                resumed = True
            else:
                remote_path = _reserve_remote_name(config, remote_dir, name)
                offset = 0
            if offset:
                log(
                    f"[push] {name} resuming at {offset / 1048576:.1f}/"
                    f"{size / 1048576:.1f} MB -> {remote_path} @ {_stamp()}"
                )
            else:
                log(
                    f"[push] {name} ({size / 1048576:.1f} MB) -> "
                    f"{remote_path} @ {_stamp()}"
                )
            _append_stream(config, remote_path, path, offset, log, name)
            if remote_sha256(config, remote_path) != digest:
                raise RemoteError("sha256 mismatch after upload")
            last_error = ""
            break
        except RemoteError as error:
            last_error = str(error)
            if attempt >= RETRY_ATTEMPTS:
                break
            log(
                f"[push] {name} attempt {attempt} failed: {last_error}; "
                f"retrying in {RETRY_DELAY:.0f}s"
            )
            time.sleep(RETRY_DELAY)
    if last_error:
        detail = f"{last_error} (after {attempt} attempt(s))"
        log(f"[push-fail] {name}: {detail} (local copy kept) @ {_stamp()}")
        return PushOutcome(False, detail)

    remember_uploaded(digest, name, remote_path, size, ledger)
    if timeline is not None:
        timeline("push-ok", f"{name} -> {remote_path}")
    if resumed:
        detail = "resumed, sha256 verified"
    elif verified_existing:
        detail = "already complete, sha256 verified"
    else:
        detail = "sha256 verified"
    log(f"[push-ok] {name} {detail} -> {remote_path} @ {_stamp()}")
    deleted = False
    if not keep_local:
        try:
            path.unlink()
            deleted = True
            log(f"[push-free] {name} removed locally @ {_stamp()}")
            if timeline is not None:
                timeline("push-free", name)
        except OSError as error:
            log(f"[push-warn] uploaded but cannot remove {name}: {error}")
    return PushOutcome(
        True,
        detail,
        remote_path=remote_path,
        resumed=resumed,
        deleted=deleted,
    )


def drain_backlog(
    output_dir: Path,
    config: RemoteConfig,
    *,
    ledger: Path | None = None,
    keep_local: bool = False,
    timeline=None,
    log=print,
) -> tuple[int, int, int]:
    """Push files already sitting in output_dir (leftovers, first run, retries).

    Returns (pushed, failed, bytes_moved). Staged temps (.download-*.tmp)
    and anything already in the ledger are skipped or left in place.
    """
    try:
        files = sorted(
            entry
            for entry in Path(output_dir).iterdir()
            if entry.is_file() and not entry.name.startswith(".")
        )
    except OSError:
        return 0, 0, 0
    if not files:
        return 0, 0, 0
    try:
        total_bytes = sum(entry.stat().st_size for entry in files)
    except OSError:
        total_bytes = 0
    log(
        f"[push-backlog] {len(files)} file(s) "
        f"({total_bytes / 1048576:.1f} MB) in {Path(output_dir).name}/"
    )
    pushed = 0
    failed = 0
    moved_bytes = 0
    for file in files:
        try:
            size = file.stat().st_size
        except OSError:
            size = 0
        outcome = push_file(
            file,
            config,
            ledger=ledger,
            keep_local=keep_local,
            timeline=timeline,
            log=log,
        )
        if outcome.ok:
            pushed += 1
            if not outcome.skipped:
                moved_bytes += size
        else:
            failed += 1
    return pushed, failed, moved_bytes

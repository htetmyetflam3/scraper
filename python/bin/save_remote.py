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

import argparse
import contextlib
import hashlib
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
UPLOAD_LEDGER = REPO_ROOT / "scan" / "uploaded-host.txt"
SSH_BIN = "ssh"
RETRY_ATTEMPTS = 5
RETRY_DELAY = 20.0
PROGRESS_STEP = 5 * 1024 * 1024
PROGRESS_SECONDS = 5.0
PROGRESS_FILES = 5
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


def _console_log(message: str) -> None:
    print(message, flush=True)


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
        digest, name, remote_path = (
            parts[0].strip(),
            parts[1].strip(),
            parts[2].strip(),
        )
        if len(digest) != 64 or not remote_path:
            continue
        entries.by_digest[digest] = remote_path
    return entries


def remember_uploaded(
    digest: str,
    name: str,
    remote_path: str,
    size: int,
    ledger: Path | None = None,
) -> None:
    target = ledger or UPLOAD_LEDGER
    if digest in read_uploaded(target).by_digest:
        return
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
    step = min(PROGRESS_STEP, max(256 * 1024, (total - offset) // 4))
    report_at = offset + step
    started = last_report = time.monotonic()
    try:
        with local_path.open("rb") as handle:
            handle.seek(offset)
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                process.stdin.write(chunk)
                sent += len(chunk)
                now = time.monotonic()
                if sent >= report_at or now - last_report >= PROGRESS_SECONDS or sent == total:
                    report_at = sent + step
                    last_report = now
                    elapsed = max(now - started, 0.001)
                    log(
                        f"[push-progress] {name} {sent / total:.0%} "
                        f"({sent / 1048576:.1f}/{total / 1048576:.1f} MB), "
                        f"{(sent - offset) / 1048576 / elapsed:.2f} MB/s "
                        f"streamed @ {_stamp()}"
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


def _delete_local(path: Path, timeline, log) -> bool:
    """Remove a local file whose bytes are verified on the remote."""
    try:
        path.unlink()
    except OSError as error:
        log(f"[push-warn] cannot remove {path.name}: {error}")
        return False
    log(f"[push-free] {path.name} removed locally @ {_stamp()}")
    if timeline is not None:
        timeline("push-free", path.name)
    return True


def push_file(
    path: Path,
    config: RemoteConfig,
    *,
    digest: str | None = None,
    ledger: Path | None = None,
    keep_local: bool = False,
    timeline=None,
    log=_console_log,
    remote_name: str | None = None,
) -> PushOutcome:
    """Push one saved file, verify it remotely, then delete the local copy.

    ``remote_name`` keeps sub-directory structure when the local tree has
    depth (the drain walks pdfs/ recursively); it defaults to the file name.
    A file already in the ledger is trusted only after a live size + sha256
    check on the host: an entry pointing at a missing or mismatched remote
    file is re-uploaded instead of deleting anything locally.
    """
    path = Path(path)
    name = path.name
    rel = (remote_name or name).replace("\\", "/").lstrip("/")
    try:
        size = path.stat().st_size
    except OSError as error:
        return PushOutcome(False, f"local file vanished: {error}")
    remote_parent = config.directory
    if "/" in rel:
        remote_parent = f"{remote_parent}/{rel.rsplit('/', 1)[0]}"
    remote_path = f"{config.directory}/{rel}"
    entries = read_uploaded(ledger)
    if digest is None:
        digest = sha256_file(path)
    known = entries.by_digest.get(digest)
    if (
        known
        and remote_size(config, known) == size
        and remote_sha256(config, known) == digest
    ):
        log(f"[push-skip] {name} already archived and verified @ {_stamp()}")
        deleted = False
        if not keep_local:
            deleted = _delete_local(path, timeline, log)
        return PushOutcome(
            True,
            "already archived, sha256 verified",
            remote_path=known,
            skipped=True,
            deleted=deleted,
        )
    if known:
        log(
            f"[push-warn] {name} is in uploaded-host.txt but not verified on "
            f"the host; re-uploading @ {_stamp()}"
        )
    ensure_dir(config, remote_parent)

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
                remote_path = _reserve_remote_name(config, remote_parent, name)
                offset = 0
            elif current < size:
                offset = current
                resumed = True
            else:
                remote_path = _reserve_remote_name(config, remote_parent, name)
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
            log(f"[push-check] {name} upload sent; verifying remote sha256 @ {_stamp()}")
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
        deleted = _delete_local(path, timeline, log)
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
    log=_console_log,
) -> tuple[int, int, int]:
    """Mirror-and-prune one pass over output_dir, depth included.

    Every file is pushed (or recognised as already archived) with the same
    verify-then-delete rule as push_file, so this doubles as the standalone
    watcher's single step. Staged temps (.download-*.tmp), dotfiles and
    dot-directories are never touched. Returns (archived, failed, bytes
    freed locally); files that fail stay on disk for the next pass.
    """
    root = Path(output_dir)
    try:
        files = sorted(
            entry
            for entry in root.rglob("*")
            if entry.is_file()
            and not any(
                part.startswith(".") or part == "__pycache__"
                for part in entry.relative_to(root).parts
            )
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
        f"({total_bytes / 1048576:.1f} MB) in {root.name}/ @ {_stamp()}"
    )
    archived = 0
    failed = 0
    freed = 0
    started = time.monotonic()
    for completed, file in enumerate(files, 1):
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
            remote_name=str(file.relative_to(root)),
        )
        if outcome.ok:
            archived += 1
            if outcome.deleted:
                freed += size
        else:
            failed += 1
        if completed % PROGRESS_FILES == 0 or completed == len(files):
            elapsed = max(time.monotonic() - started, 0.001)
            log(
                f"[push-progress] {completed}/{len(files)} files "
                f"({completed / len(files):.0%}), {archived} archived, "
                f"{failed} failed, {freed / 1048576:.1f} MB freed; "
                f"{completed * 60 / elapsed:.1f} files/min, "
                f"elapsed {elapsed:.0f}s @ {_stamp()}"
            )
    log(
        f"[push-backlog] done: {archived} archived, {failed} failed, "
        f"{freed / 1048576:.1f} MB freed @ {_stamp()}"
    )
    return archived, failed, freed


def _default_local_dir() -> Path:
    """Same folder Dcrawler saves into: <repo>/python/pdfs."""
    return Path(__file__).resolve().parents[1] / "pdfs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Standalone SSH archiver: push saved files to the host from the "
            "SSH_* settings, verify each one, then free the local copy. Runs "
            "independently of the crawler; a file only ever leaves the disk "
            "after its bytes are verified on the host."
        )
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="local folder to archive (default: <repo>/python/pdfs)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="keep going forever: drain, sleep, drain again (Ctrl+C stops)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="with --watch: pause between passes (default 60)",
    )
    parser.add_argument(
        "--keep-local",
        action="store_true",
        help="archive without deleting anything locally",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Keep status lines visible even when stdout is redirected through tee.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = build_parser().parse_args(argv)
    local = (args.dir or _default_local_dir()).expanduser()
    try:
        config = load_config()
        if not local.is_dir():
            print(f"error: local folder not found: {local}", file=sys.stderr)
            return 1
        print(
            f"[remote] {config.user}@{config.host}:{config.port} "
            f"dir={config.directory} @ {_stamp()}"
        )
        print("[upload-limit] unlimited", flush=True)
        info = probe(config)
        if info:
            print(f"[remote] {info}")
        if not args.watch:
            archived, failed, freed = drain_backlog(
                local,
                config,
                keep_local=args.keep_local,
                timeline=_cli_timeline,
            )
            return 1 if failed else 0
        total_freed = 0
        while True:
            archived, failed, freed = drain_backlog(
                local,
                config,
                keep_local=args.keep_local,
                timeline=_cli_timeline,
            )
            total_freed += freed
            if archived or failed:
                print(
                    f"[watch] archived {archived}, failed {failed}, "
                    f"freed {freed / 1048576:.1f} MB this pass "
                    f"({total_freed / 1048576:.1f} MB total); next check in "
                    f"{args.interval:.0f}s @ {_stamp()}"
                )
            else:
                print(
                    f"[watch] nothing new; next check in {args.interval:.0f}s "
                    f"@ {_stamp()}"
                )
            time.sleep(max(args.interval, 1.0))
    except RemoteError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "\n[paused] watcher stopped; unarchived files stay local. "
            f"@ {_stamp()}"
        )
        return 130


def _cli_timeline(event: str, detail: str = "") -> None:
    """Timeline rows for standalone runs: same file the crawler writes."""
    target = REPO_ROOT / "scan" / "timeline.log"
    line = f"{time.strftime('%m-%d %H:%M:%S')} {event}"
    if detail:
        line += f" {detail}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as error:
        print(f"[timeline-warn] cannot record {event}: {error}")


if __name__ == "__main__":
    raise SystemExit(main())

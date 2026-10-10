#!/usr/bin/env python3
"""Linearize every PDF in a directory, in place, and delete duplicates.

"Linearized" (a.k.a. "fast web view") PDFs are byte-ordered so a reader can
paint the first page before the whole file arrives, which is what Scribd-style
viewers and browser PDF plugins want.

    python linearize.py              # ../pdfs, the sibling of python/
    python linearize.py /path/to/dir
    python linearize.py --dry-run    # show what would happen

Each file is replaced by its linearized version in place: no backups, no
copies, nothing but the linearized PDF left behind. The original is only
removed once the new file is written and verified readable, so a file that
fails to linearize keeps its original rather than being destroyed.

Encrypted PDFs are unlocked, not preserved: the rewrite saves without an
encryption dictionary, so print/copy restrictions do not survive. Pass
--password for files that need one (most "encrypted" PDFs are
restrictions-only and open with an empty password, which needs no flag).

Duplicate detection runs first, on content (sha256), so two copies with
different names collapse to one before any rewriting happens.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pikepdf
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("Missing dependency. Run:  uv sync  (or pip install pikepdf)")

PDF_MAGIC = b"%PDF"
READ_SIZE = 1 << 20  # 1 MiB, for hashing and sniffing
TMP_SUFFIX = ".linearizing"


def is_pdf(path: Path) -> bool:
    """A real PDF starts with %PDF. Catches HTML error pages saved as .pdf."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(PDF_MAGIC)) == PDF_MAGIC
    except OSError:
        return False


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(READ_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def iter_files(directory: Path, recursive: bool) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    return sorted(p for p in directory.glob(pattern) if p.is_file())


def choose_keeper(paths: list[Path]) -> tuple[Path, list[Path]]:
    """Keep the tidiest name: shortest, then alphabetically first."""
    ordered = sorted(paths, key=lambda p: (len(p.name), str(p).casefold()))
    return ordered[0], ordered[1:]


def find_duplicates(files: list[Path]) -> dict[str, list[Path]]:
    by_hash: dict[str, list[Path]] = {}
    for path in files:
        try:
            by_hash.setdefault(sha256_of(path), []).append(path)
        except OSError as error:
            print(f"  ! could not read {path.name}: {error}")
    return {digest: paths for digest, paths in by_hash.items() if len(paths) > 1}


# --------------------------------------------------------------------------- #
# Linearizing
# --------------------------------------------------------------------------- #

@dataclass
class Outcome:
    path: Path
    state: str            # linearized | already | skipped | broken | encrypted | failed
    before: int = 0
    after: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state in ("linearized", "already")


def open_pdf(path: Path, passwords: tuple[str, ...] = ()) -> tuple["pikepdf.Pdf", str | None]:
    """Open `path`, trying each password in turn.

    Most "encrypted" PDFs in the wild are restrictions-only: they carry an owner
    password that stops printing or copying, but no user password, so they open
    with an empty one. Returns (pdf, password that worked).
    """
    last_error: Exception | None = None
    for candidate in (None, "", *passwords):
        try:
            if candidate is None:
                return pikepdf.open(path), None
            return pikepdf.open(path, password=candidate), candidate
        except pikepdf.PasswordError as error:
            last_error = error
    raise last_error or pikepdf.PasswordError(str(path))


def linearize(path: Path, dry_run: bool = False, passwords: tuple[str, ...] = ()) -> Outcome:
    """Rewrite `path` as a linearized PDF, replacing it only if the result is sound.

    Encrypted PDFs are unlocked: saving without an `encryption=` argument drops
    the encryption, so copy/print restrictions do not survive the rewrite. That
    is the point -- a locked book is no use to anyone.

    With dry_run the file is opened and judged but not written, so a dry run
    reports the same verdict a real run would.
    """
    info = path.stat()
    before = info.st_size
    mode = info.st_mode & 0o777  # mkstemp creates 0600; keep what the file had
    was_encrypted = False
    try:
        try:
            pdf, _password = open_pdf(path, passwords)
        except pikepdf.PasswordError as error:
            return Outcome(path, "locked", before, before,
                           "encrypted and none of the passwords fit; pass --password")
        with pdf:
            pages = len(pdf.pages)
            was_encrypted = bool(pdf.is_encrypted)
            if not was_encrypted and b"/Linearized" in path.open("rb").read(4096):
                return Outcome(path, "already", before, before)
            if dry_run:
                return Outcome(path, "linearized", before, before, "would unlock" if was_encrypted else "")
            tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=TMP_SUFFIX)
            os.close(tmp_fd)
            tmp = Path(tmp_name)
            try:
                pdf.save(tmp, linearize=True)  # no encryption= argument: the output is unlocked
                with pikepdf.open(tmp) as check:
                    if len(check.pages) != pages:
                        raise ValueError(f"page count changed ({pages} -> {len(check.pages)})")
                    if check.is_encrypted:
                        raise ValueError("output is still encrypted")
                os.replace(tmp, path)
                os.chmod(path, mode)
            finally:
                if tmp.exists():
                    tmp.unlink()
    except Exception as error:  # noqa: BLE001 - a bad file must not stop the run
        return Outcome(path, "broken", before, before, str(error) or type(error).__name__)

    after = path.stat().st_size
    if not is_pdf(path):
        return Outcome(path, "failed", before, after, "output is not a valid PDF")
    return Outcome(path, "linearized", before, after, "unlocked" if was_encrypted else "")


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

@dataclass
class Report:
    removed: list[Path] = field(default_factory=list)
    freed: int = 0
    outcomes: list[Outcome] = field(default_factory=list)

    def add(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)

    def count(self, state: str) -> int:
        return sum(1 for outcome in self.outcomes if outcome.state == state)

    @property
    def failures(self) -> list[Outcome]:
        return [outcome for outcome in self.outcomes if not outcome.ok]


def sweep(directory: Path, recursive: bool = False, dedupe: bool = True,
          delete_broken: bool = False, dry_run: bool = False, quiet: bool = False,
          passwords: tuple[str, ...] = ()) -> Report:
    report = Report()

    if not directory.is_dir():
        raise SystemExit(f"Not a directory: {directory}")

    files = iter_files(directory, recursive)
    if not quiet:
        print(f"{directory}: {len(files)} file(s)")

    # 1. Duplicates first, so we do not linearize the same file twice.
    doomed: set[Path] = set()
    if dedupe:
        for digest, paths in sorted(find_duplicates(files).items()):
            keeper, duplicates = choose_keeper(paths)
            if not quiet:
                names = ", ".join(p.name for p in duplicates)
                print(f"  duplicate {digest[:12]}: keeping {keeper.name}, removing {names}")
            for path in duplicates:
                size = path.stat().st_size
                if dry_run:
                    doomed.add(path)
                    report.removed.append(path)
                    report.freed += size
                    continue
                try:
                    path.unlink()
                    report.removed.append(path)
                    report.freed += size
                except OSError as error:
                    print(f"  ! could not remove {path.name}: {error}")
        files = [p for p in files if p.exists()]

    # 2. Linearize whatever is left.
    for path in files:
        if path in doomed:
            if not quiet:
                print(f"  would be removed as a duplicate: {path.name}")
            continue
        if not is_pdf(path):
            report.add(Outcome(path, "skipped", path.stat().st_size, path.stat().st_size,
                               "not a PDF (wrong magic bytes)"))
            if not quiet:
                print(f"  skip {path.name}: not a PDF")
            continue

        outcome = linearize(path, dry_run=dry_run, passwords=passwords)
        report.add(outcome)
        if quiet:
            continue
        if outcome.state == "linearized":
            delta = outcome.after - outcome.before
            sign = "+" if delta >= 0 else "-"
            note = f", {outcome.detail}" if outcome.detail else ""
            print(f"  linearized {path.name}{note} ({outcome.before:,} -> {outcome.after:,} bytes, {sign}{abs(delta):,})")
        elif outcome.state == "already":
            print(f"  already linearized {path.name}")
        else:
            print(f"  ! {outcome.state}: {path.name}: {outcome.detail}")

    # 3. Files that could not be linearized are the only thing that can leave
    #    a non-linearized PDF behind, and only with permission.
    if delete_broken:
        for outcome in report.failures:
            if outcome.state in ("broken", "failed", "locked", "skipped"):
                if dry_run:
                    print(f"  would delete {outcome.path.name}")
                    continue
                try:
                    outcome.path.unlink()
                    report.removed.append(outcome.path)
                    print(f"  deleted unreadable {outcome.path.name}")
                except OSError as error:
                    print(f"  ! could not remove {outcome.path.name}: {error}")

    # 4. Never leave a half-written temp file behind.
    if not dry_run:
        for leftover in directory.glob(f"**/*{TMP_SUFFIX}" if recursive else f"*{TMP_SUFFIX}"):
            leftover.unlink(missing_ok=True)

    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_dir = Path(__file__).resolve().parent.parent / "pdfs"
    parser = argparse.ArgumentParser(description="Linearize PDFs in place and delete duplicates")
    parser.add_argument("directory", nargs="?", type=Path, default=default_dir,
                        help=f"directory to sweep (default: {default_dir})")
    parser.add_argument("--recursive", action="store_true", help="descend into subdirectories")
    parser.add_argument("--no-dedupe", action="store_true", help="keep duplicate files")
    parser.add_argument("--delete-broken", action="store_true",
                        help="delete files that cannot be read as PDFs (off by default: nothing is lost)")
    parser.add_argument("--password", action="append", default=[], metavar="PW",
                        help="password to try on encrypted PDFs (repeatable)")
    parser.add_argument("--password-file", type=Path, default=None, metavar="FILE",
                        help="file of passwords to try, one per line")
    parser.add_argument("--dry-run", action="store_true", help="report without changing anything")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    passwords = tuple(args.password)
    if args.password_file:
        try:
            passwords += tuple(
                line.strip() for line in args.password_file.read_text(encoding="utf8").splitlines() if line.strip()
            )
        except OSError as error:
            raise SystemExit(f"Could not read {args.password_file}: {error}") from error
    report = sweep(
        args.directory,
        recursive=args.recursive,
        dedupe=not args.no_dedupe,
        delete_broken=args.delete_broken,
        dry_run=args.dry_run,
        quiet=args.quiet,
        passwords=passwords,
    )

    if not args.quiet:
        print()
    if args.dry_run:
        print("Dry run, nothing was changed.")
    print(f"linearized:         {report.count('linearized')}")
    print(f"already linearized: {report.count('already')}")
    print(f"duplicates removed: {len(report.removed)} ({report.freed:,} bytes freed)")
    problems = report.failures
    if problems:
        print(f"left as-is:         {len(problems)}")
        for outcome in problems:
            print(f"  - {outcome.path.name}: {outcome.state}"
                  f"{': ' + outcome.detail if outcome.detail else ''}")
        if report.count("locked"):
            print(f"  ({report.count('locked')} encrypted; pass --password=... to unlock them)")
        if not args.delete_broken and not args.dry_run:
            print("  (re-run with --delete-broken to remove the rest)")

    return 1 if problems and not args.delete_broken else 0


if __name__ == "__main__":
    sys.exit(main())

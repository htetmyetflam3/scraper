#!/usr/bin/env python3
"""Delete `name (N).ext` duplicates whose plain `name.ext` sits beside them.

Dry run by default; pass --delete to actually remove files. A file counts
as a duplicate only when ALL hold:

- the name ends with ` (digits)` immediately before the extension
  (numbers elsewhere, e.g. `vol (3) final.pdf`, are never touched);
- the number is below 1000, so edition years like `Book (2024).pdf`
  are never mistaken for dup markers;
- the plain sibling exists in the SAME directory (stem matched exactly,
  case-sensitively; extension matched case-insensitively).

Stdlib only; safe to run from anywhere (defaults to this repo's pdfs/).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PDFS_DIR = REPO_ROOT / "pdfs"

DUP_PATTERN = re.compile(r"^(?P<stem>.*) \((?P<num>\d+)\)(?P<ext>\.[^.]+)$")
YEAR_CUTOFF = 1000


def collect(directory: Path) -> dict:
    """Scan recursively; return delete-list plus skip counters."""
    found: dict = {"delete": [], "scanned": 0, "no_parent": 0, "year": 0}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        found["scanned"] += 1
        match = DUP_PATTERN.match(path.name)
        if match is None:
            continue
        if int(match.group("num")) >= YEAR_CUTOFF:
            found["year"] += 1
            continue
        stem, ext = match.group("stem"), match.group("ext").lower()
        parent_exists = any(
            sibling.is_file()
            and not sibling.is_symlink()
            and sibling.stem == stem
            and sibling.suffix.lower() == ext
            for sibling in path.parent.iterdir()
        )
        if not parent_exists:
            found["no_parent"] += 1
            continue
        found["delete"].append(path)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        default=str(PDFS_DIR),
        help="Directory to scan (default: this repo's pdfs/).",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually remove the duplicates (default is a dry run).",
    )
    args = parser.parse_args(argv)
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"error: not a directory: {directory}", file=sys.stderr)
        return 2
    found = collect(directory)
    errors = 0
    for path in found["delete"]:
        shown = path.relative_to(directory)
        if not args.delete:
            print(f"[would delete] {shown}")
            continue
        try:
            path.unlink()
        except OSError as error:
            print(f"[error] {shown}: {error}")
            errors += 1
        else:
            print(f"[deleted] {shown}")
    verb = "deleted" if args.delete else "would delete"
    print(
        f"{len(found['delete'])} {verb}, "
        f"{found['no_parent']} without original kept, "
        f"{found['year']} year-like kept, "
        f"{found['scanned']} file(s) scanned."
    )
    if errors:
        print(f"{errors} deletion(s) failed.", file=sys.stderr)
        return 1
    if not args.delete and found["delete"]:
        print("Nothing was deleted (dry run). Rerun with --delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

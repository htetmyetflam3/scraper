#!/usr/bin/env python3
"""Standalone auditor for Dcrawler history: failed URLs and zip coverage.

Standard library only; never needs the crawler's dependencies. It reads
what Dcrawler writes under scan/ and only ever appends to the two ledgers
below (or writes nothing at all with --dry-run).

Default mode - backfill failed URLs
    crawled URLs (every page and link in scan/crawl-*.jsonl)
    - downloaded URLs (identities in scan/downloaded.txt)
    = failed URLs
    New failed download-looking URLs (.pdf/.docx links and known file-host
    links) are appended to scan/blocked.txt so later runs skip them, and
    recorded with a timestamp in scan/failed.txt. Idempotent: reruns add
    nothing that is already in either ledger. Use --all to count every
    unmatched crawled URL, not only download-looking ones.

    python3 python/bin/failed_urls.py [--scan-dir DIR] [--dry-run] [--all]

Zip mode - what a batch zip actually contains, plus a deletion manifest
    Prints the member count, when the zip was made, which PDFs were saved
    after that moment (from scan/timeline.log => not inside the zip), and
    which PDFs on disk near the zip are missing from it.
    Also writes ``<zip>.manifest.txt`` next to the zip: header comments,
    then one zip-internal path per line for every file inside, so exactly
    those files can be deleted after the upload is verified. The tool
    itself never deletes anything; re-running --zip rewrites the manifest
    so it always matches the current zip.

    python3 python/bin/failed_urls.py --zip batch1.zip [--pdfs-dir DIR]
                                       [--manifest PATH]

Delete mode - separate step, run ONLY after the upload is verified
    Deletes exactly the files listed in ``<zip>.manifest.txt``. Refuses to
    run when the manifest does not match the zip's current contents, and
    never touches absolute paths or ``..`` escapes. Dry run by default;
    ``--yes`` performs the deletions. ``--base DIR`` when the zip was
    built somewhere other than the zip's own folder.

    python3 python/bin/failed_urls.py --delete-zip batch1.zip [--base DIR] [--yes]

Dcrawler itself keeps scan/failed.txt up to date automatically now; this
helper exists for history written before that, and for zip audits.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import urllib.parse
import zipfile
from pathlib import Path

DOC_EXTENSION = re.compile(r"\.(pdf|docx)$", re.I)
PROVIDER_HOSTS = (
    "drive.google.com",
    "docs.google.com",
    "drive.usercontent.google.com",
    "drive.googleusercontent.com",
    "mega.nz",
    "mega.co.nz",
    "mediafire.com",
    "dropbox.com",
    "dropboxusercontent.com",
    "terabox.com",
    "teraboxapp.com",
    "1024terabox.com",
    "1024tera.com",
    "terabox.app",
)
TRACKING_KEYS = frozenset({"fbclid", "gclid", "dclid", "msclkid", "yclid"})
TIMELINE_NAME = "timeline.log"


def repo_scan_dir() -> Path:
    """Same resolution as Dcrawler: <repo>/scan, one level above python/."""
    return Path(__file__).resolve().parents[2] / "scan"


def canonical(url: str) -> str | None:
    """Lowercase scheme+host, drop fragment and default port; else None."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    netloc = parts.hostname.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and port not in (80, 443):
        netloc += f":{port}"
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), netloc, parts.path, parts.query, "")
    )


def variants(url: str) -> set[str]:
    """Canonical forms to try when matching against recorded identities."""
    base = canonical(url)
    if base is None:
        return set()
    parts = urllib.parse.urlsplit(base)
    bare = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    out = {base, bare}
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    kept = [(name, value) for name, value in pairs if name.lower() not in TRACKING_KEYS]
    if kept != pairs:
        out.add(
            urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), "")
            )
        )
    return {value for value in out if value}


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def journal_urls(scan_dir: Path) -> tuple[list[str], list[str], int]:
    """(page urls, link urls, journal file count) from every crawl journal."""
    pages: list[str] = []
    links: list[str] = []
    files = 0
    for path in sorted(scan_dir.glob("crawl-*.jsonl")):
        files += 1
        for line in read_lines(path):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            url = record.get("u")
            if isinstance(url, str):
                pages.append(url)
            children = record.get("c")
            if not isinstance(children, list):
                continue
            for link in children:
                if isinstance(link, str):
                    links.append(link)
    return pages, links, files


def is_download_like(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    if DOC_EXTENSION.search(parts.path):
        return True
    host = (parts.hostname or "").lower()
    return any(host == name or host.endswith("." + name) for name in PROVIDER_HOSTS)


def in_blob(url: str, blob: str) -> bool:
    return any(value in blob for value in variants(url))


def page_has_download(page: str, downloaded_blob: str) -> bool:
    """A page counts as satisfied when its URL - or its long numeric id run -
    appears in the downloaded identities (form posts record uid=NNN too)."""
    if in_blob(page, downloaded_blob):
        return True
    runs = re.findall(r"\d{4,}", urllib.parse.urlsplit(page).path)
    return any(run in downloaded_blob for run in runs)


def backfill(scan_dir: Path, dry_run: bool, include_all: bool) -> int:
    if not scan_dir.is_dir():
        print(f"[audit] scan dir not found: {scan_dir}")
        return 1
    pages, links, journal_files = journal_urls(scan_dir)
    downloaded = read_lines(scan_dir / "downloaded.txt")
    blocked = read_lines(scan_dir / "blocked.txt")
    failed = read_lines(scan_dir / "failed.txt")
    downloaded_blob = "\n".join(downloaded)
    blocked_blob = "\n".join(blocked)
    failed_blob = "\n".join(failed)

    print(f"[audit] scan dir: {scan_dir}")
    print(
        f"[audit] journals: {journal_files}, crawled page(s): {len(pages):,}, "
        f"crawled link(s): {len(links):,}"
    )
    print(
        f"[audit] downloaded: {len(downloaded)} line(s), "
        f"blocked: {len(blocked)}, failed ledger: {len(failed)}"
    )

    seen: set[str] = set()
    new_failed: list[str] = []
    for url in links + pages:
        key = canonical(url)
        if key is None or key in seen:
            continue
        seen.add(key)
        if not include_all and not is_download_like(url):
            continue
        if in_blob(url, downloaded_blob):
            continue
        if in_blob(url, blocked_blob) or in_blob(url, failed_blob):
            continue
        new_failed.append(key)

    print(f"[audit] failed (crawled, never downloaded): {len(new_failed)} new")
    for key in new_failed[:20]:
        print(f"[audit]   + {key}")
    if len(new_failed) > 20:
        print(f"[audit]   ... and {len(new_failed) - 20} more")

    book_like = [
        page
        for page in pages
        if re.search(r"\d{4,}", urllib.parse.urlsplit(page).path)
        and not page_has_download(page, downloaded_blob)
    ]
    print(
        f"[id-check] {len(book_like):,} page(s) with a numeric id in the URL "
        "have no matching download record (report only)"
    )
    for page in book_like[:5]:
        print(f"[id-check]   ? {page}")

    if not new_failed:
        print("[audit] nothing to append")
        return 0
    if dry_run:
        print(
            f"[audit] dry run: would append {len(new_failed)} URL(s) to "
            "blocked.txt and record them in failed.txt"
        )
        return 0

    stamp = dt.datetime.now().strftime("%m-%d %H:%M:%S")
    try:
        with (scan_dir / "blocked.txt").open("a", encoding="utf-8") as handle:
            for key in new_failed:
                handle.write(key + "\n")
        with (scan_dir / "failed.txt").open("a", encoding="utf-8") as handle:
            for key in new_failed:
                handle.write(
                    f"{stamp}\tfailed\t{key}\t"
                    "crawled but never downloaded (helper backfill)\n"
                )
    except OSError as error:
        print(f"[audit] cannot write: {error}")
        return 1
    print(
        f"[audit] appended {len(new_failed)} URL(s) to blocked.txt and "
        "recorded them in failed.txt"
    )
    return 0


def parse_timeline(scan_dir: Path) -> list[tuple[dt.datetime, str, str]]:
    """(when, event, detail) rows from scan/timeline.log (year assumed now)."""
    events: list[tuple[dt.datetime, str, str]] = []
    pattern = re.compile(r"^(\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (\S+)(?: (.*))?$")
    for line in read_lines(scan_dir / TIMELINE_NAME):
        match = pattern.match(line.strip())
        if not match:
            continue
        try:
            when = dt.datetime.strptime(match.group(1), "%m-%d %H:%M:%S").replace(
                year=dt.datetime.now().year
            )
        except ValueError:
            continue
        events.append((when, match.group(2), (match.group(3) or "").strip()))
    return events


def zip_audit(
    zip_path: Path,
    scan_dir: Path,
    pdfs_dir: Path | None,
    manifest_path: Path | None = None,
) -> int:
    zip_path = zip_path.resolve()
    if not zip_path.is_file():
        print(f"[zip] not found: {zip_path}")
        return 1
    try:
        with zipfile.ZipFile(zip_path) as bundle:
            names = bundle.namelist()
    except (OSError, zipfile.BadZipFile) as error:
        print(f"[zip] cannot read {zip_path.name}: {error}")
        return 1
    made = dt.datetime.fromtimestamp(zip_path.stat().st_mtime)
    file_members = [name for name in names if not name.endswith("/")]
    members = [Path(name).name for name in file_members]
    pdf_members = {name for name in members if name.lower().endswith(".pdf")}
    print(
        f"[zip] {zip_path.name}: {len(file_members):,} member(s), "
        f"{len(pdf_members):,} .pdf, made {made.strftime('%m-%d %H:%M:%S')}"
    )

    manifest = (
        manifest_path
        if manifest_path is not None
        else zip_path.with_name(zip_path.name + ".manifest.txt")
    )
    listed = [normalize_member(name) for name in file_members]
    header = [
        f"# zip: {zip_path.name}",
        f"# made: {made.strftime('%m-%d %H:%M:%S')}",
        f"# files: {len(file_members):,} ({len(pdf_members):,} .pdf)",
        f"# written: {dt.datetime.now().strftime('%m-%d %H:%M:%S')} "
        "by failed_urls.py --zip",
    ]
    try:
        manifest.write_text("\n".join(header + listed) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"[zip] cannot write manifest {manifest}: {error}")
        return 1
    print(f"[zip] manifest written: {manifest} ({len(listed):,} path(s))")
    hint = f"python/bin/failed_urls.py --delete-zip {zip_path}"
    if manifest_path is not None:
        hint += f" --manifest {manifest}"
    print(
        "[zip] manifest creation deletes nothing; when the upload is "
        f"verified, run separately (dry run first): {hint}"
    )

    saved = [
        (when, detail)
        for when, event, detail in parse_timeline(scan_dir)
        if event == "saved" and detail
    ]
    if saved:
        before = [(when, detail) for when, detail in saved if when <= made]
        after = [
            (when, detail)
            for when, detail in saved
            if when > made
            and detail.split(" ", 1)[0] not in pdf_members
            and not any(member in detail for member in pdf_members)
        ]
        if before:
            last_when, last_detail = before[-1]
            print(
                f"[zip] covers saves up to: "
                f"{last_detail.split(' ', 1)[0]} @ {last_when.strftime('%H:%M:%S')}"
            )
        else:
            print("[zip] no save recorded before the zip was made")
        print(f"[zip] saved after the zip (not inside it): {len(after)}")
        for when, detail in after[:15]:
            print(f"[zip]   + {detail} @ {when.strftime('%H:%M:%S')}")
        if len(after) > 15:
            print(f"[zip]   ... and {len(after) - 15} more")
    else:
        print(
            f"[zip] no saved events in {scan_dir / TIMELINE_NAME} yet; "
            "crawl first for exact coverage"
        )

    root = (pdfs_dir or zip_path.parent).resolve()
    disk_pdfs: list[Path] = []
    try:
        for path in root.rglob("*.pdf"):
            relative = path.relative_to(root)
            if any(
                part.startswith(".") or part in {"__pycache__", "node_modules"}
                for part in relative.parts[:-1]
            ):
                continue
            disk_pdfs.append(path)
    except OSError:
        disk_pdfs = []
    missing = [path for path in disk_pdfs if path.name not in pdf_members]
    print(f"[zip] .pdf on disk not in the zip: {len(missing):,} (scanning {root})")
    for path in missing[:10]:
        print(f"[zip]   - {path.name}")
    if len(missing) > 10:
        print(f"[zip]   ... and {len(missing) - 10} more")
    return 0


def normalize_member(name: str) -> str:
    """Zip member path with any leading ./ removed."""
    while name.startswith("./"):
        name = name[2:]
    return name


def delete_from_manifest(
    zip_path: Path,
    manifest_path: Path | None,
    base_dir: Path | None,
    apply_delete: bool,
) -> int:
    """Delete exactly the manifest-listed files, after an upload is verified.

    The manifest must match the zip's current contents exactly; a stale
    manifest refuses to run. Paths resolve under base_dir (default: the
    zip's own folder). Absolute paths and ``..`` escapes are never touched;
    symlinks are skipped. Dry run unless apply_delete is true.
    """
    zip_path = zip_path.resolve()
    if not zip_path.is_file():
        print(f"[delete] zip not found: {zip_path}")
        return 1
    manifest = (
        manifest_path
        if manifest_path is not None
        else zip_path.with_name(zip_path.name + ".manifest.txt")
    )
    if not manifest.is_file():
        print(f"[delete] manifest not found: {manifest} (run --zip first)")
        return 1
    base = (base_dir if base_dir is not None else zip_path.parent).resolve()
    if not base.is_dir():
        print(f"[delete] base directory not found: {base} (pass --base DIR)")
        return 1
    try:
        with zipfile.ZipFile(zip_path) as bundle:
            zip_members = {
                normalize_member(name)
                for name in bundle.namelist()
                if not name.endswith("/")
            }
    except (OSError, zipfile.BadZipFile) as error:
        print(f"[delete] cannot read {zip_path.name}: {error}")
        return 1

    listed: list[str] = []
    for line in read_lines(manifest):
        text = line.strip()
        if text and not text.startswith("#"):
            listed.append(normalize_member(text))
    listed_set = set(listed)
    if listed_set != zip_members:
        print(
            f"[delete] manifest does not match {zip_path.name}: "
            f"{len(listed_set)} listed vs {len(zip_members)} inside the zip; "
            "re-run --zip first; nothing deleted"
        )
        for name in sorted(listed_set - zip_members)[:3]:
            print(f"[delete]   only in manifest: {name}")
        for name in sorted(zip_members - listed_set)[:3]:
            print(f"[delete]   only in zip: {name}")
        return 1

    targets: list[Path] = []
    missing: list[str] = []
    symlinks: list[str] = []
    unsafe: list[str] = []
    seen: set[str] = set()
    for entry in listed:
        if entry in seen:
            continue
        seen.add(entry)
        if Path(entry).is_absolute() or ".." in Path(entry).parts:
            unsafe.append(entry)
            continue
        candidate = base / entry
        if candidate.is_symlink():
            symlinks.append(entry)
        elif candidate.is_file():
            targets.append(candidate)
        else:
            missing.append(entry)

    print(
        f"[delete] {zip_path.name}: {len(listed_set)} listed path(s), "
        f"base {base}"
    )
    for entry in unsafe:
        print(f"[delete]   unsafe, never touched: {entry}")
    for entry in symlinks:
        print(f"[delete]   symlink, skipped: {entry}")

    if not apply_delete:
        print(f"[delete] dry run: {len(targets)} file(s) would be deleted")
        for path in targets[:10]:
            print(f"[delete]   - {path.relative_to(base)}")
        if len(targets) > 10:
            print(f"[delete]   ... and {len(targets) - 10} more")
        if missing:
            print(f"[delete] {len(missing)} listed path(s) not found under base")
            for entry in missing[:5]:
                print(f"[delete]   ? {entry}")
            print("[delete] if the zip was built in another folder, pass --base DIR")
        print(
            "[delete] nothing was deleted; re-run with --yes only after the "
            "upload is verified on the other side"
        )
        return 0

    deleted = 0
    errors = 0
    for path in targets:
        try:
            path.unlink()
        except OSError as error:
            errors += 1
            print(f"[delete] cannot delete {path}: {error}")
        else:
            deleted += 1
    print(
        f"[delete] deleted {deleted} file(s); missing {len(missing)}, "
        f"symlink(s) {len(symlinks)}, unsafe {len(unsafe)}, error(s) {errors}"
    )
    if missing:
        print("[delete] if the zip was built in another folder, pass --base DIR")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill failed URLs from crawl/download history, or audit a batch zip."
    )
    parser.add_argument(
        "--scan-dir",
        type=Path,
        default=None,
        help="scan directory (default: <repo>/scan)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview the failed URLs; write nothing",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="count every unmatched crawled URL, not only download-looking ones",
    )
    parser.add_argument(
        "--zip",
        type=Path,
        default=None,
        help="audit this zip against timeline.log and the PDFs on disk",
    )
    parser.add_argument(
        "--pdfs-dir",
        type=Path,
        default=None,
        help="with --zip: where to look for PDFs missing from it (default: the zip's folder)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help=(
            "with --zip: where to write the member list; with --delete-zip: "
            "which list to read (default: <zip>.manifest.txt next to the zip)"
        ),
    )
    parser.add_argument(
        "--delete-zip",
        type=Path,
        default=None,
        metavar="ZIP",
        help=(
            "separate step after the upload is verified: delete exactly the "
            "files listed in the zip's manifest (dry run without --yes)"
        ),
    )
    parser.add_argument(
        "--base",
        type=Path,
        default=None,
        help=(
            "with --delete-zip: directory the zip was built in "
            "(default: the zip's own folder)"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="with --delete-zip: actually delete (without it nothing is removed)",
    )
    args = parser.parse_args(argv)
    if args.zip is not None and args.delete_zip is not None:
        parser.error("choose one of --zip or --delete-zip")
    if (args.yes or args.base is not None) and args.delete_zip is None:
        parser.error("--yes and --base need --delete-zip")
    scan_dir = (args.scan_dir or repo_scan_dir()).resolve()
    if args.delete_zip is not None:
        return delete_from_manifest(
            args.delete_zip, args.manifest, args.base, args.yes
        )
    if args.zip is not None:
        return zip_audit(args.zip, scan_dir, args.pdfs_dir, args.manifest)
    return backfill(scan_dir, dry_run=args.dry_run, include_all=args.all)


if __name__ == "__main__":
    raise SystemExit(main())

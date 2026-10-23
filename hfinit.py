#!/usr/bin/env python3
"""hfinit.py -- log in to Hugging Face, then push parquet files into ONE dataset repo.

    uv run hfinit.py                             log in only
    uv run hfinit.py pdfs/parquet                the collection parquet, up to HF
    uv run hfinit.py pdfs/parquet/books.parquet  the same, named explicitly
    uv run hfinit.py --download pulled           ...and back down again, the round trip

Local paths are relative to the current directory. `pdfs/parquet` is not a guess: it is
where crawler/bin/text_collect.py writes, now that REPO_ROOT resolves to the repository
root in every module instead of `crawler/` in seven of them. Not recursive, and dot-files
are skipped, so the ".books.parquet.tmp" half-write is never picked up mid-commit.

How the name check works
------------------------
    name already in the repo -> UPDATE that same path in the repo
    name not in the repo     -> ADD the whole file as a new dataset file

There is ONE parquet: pdfs/parquet/books.parquet, five columns (author | book | text |
segmented | sha256). text_collect.py appends each book into it and rewrites it atomically, so the
local copy is always cumulative - every old row plus the new ones. The first push is an
ADD; every push after that has the same name, so it is an UPDATE.

UPDATE replaces the file on HF with the local copy. It never downloads anything and never
merges rows on this machine, and it does not have to: the local file already holds every
row, which is exactly what makes re-uploading it the append.

The one thing to watch: if you ever point this at a file holding ONLY new rows, under a
name that already exists, UPDATE would drop whatever rows are on HF.

Everything in one run goes into ONE commit, because the Hub allows roughly 320 commit
creations per hour and a single file upload is a single commit.

--cdc
---
Rewrites each parquet with Parquet Content-Defined Chunking before uploading (needs
pyarrow>=21). Without it, appended rows shift every page boundary and column header, so
Xet re-transfers most of the file. With it, only the changed ~64 kB chunks travel. The
first --cdc run pays one full re-upload, because every chunk boundary moves at once.
The rewrite keeps the file's existing codec -- zstd level 19, what text_collect.py
chose -- rather than falling back to pyarrow's default.

Environment
-----------
    HG_TOKEN   Hugging Face access token (the "hg_full" one). If unset, login prompts
               and the token is written to the HF cache, so later runs need nothing.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Sequence

# ------------------------------------------------------------------ edit these lines
HF_USER = "htetmyetflam3"  # your Hugging Face user name (same as your git user name)
HF_REPO_NAME = "scraper"  # the one dataset repo this script ever touches
PARQUET_PATH = "pdfs/parquet"  # where text_collect.py writes the collection, cwd-relative
# -----------------------------------------------------------------------------------

REPO_ID = f"{HF_USER}/{HF_REPO_NAME}"
REPO_TYPE = "dataset"
PRIVATE = True
TOKEN_ENV = "HG_TOKEN"


class HfInitError(RuntimeError):
    """Anything that should stop the run with a readable message instead of a traceback."""


def _fail(message: str) -> "NoReturn":  # noqa: F821 - NoReturn only needed for typing
    raise HfInitError(message)


def _import_hub():
    try:
        from huggingface_hub import (CommitOperationAdd, HfApi, hf_hub_download,
                                     login, whoami)
    except ImportError as exc:  # pragma: no cover - environment problem
        _fail(f"huggingface_hub is not installed ({exc}). Try: uv sync")
    return HfApi, CommitOperationAdd, hf_hub_download, login, whoami


# --------------------------------------------------------------------------- login

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pull(dest: str) -> None:
    """Return leg of the round trip: pull the parquet back down out of the repo.

    Writes into the folder given, keeping the name the file has on HF. A file already
    sitting there that is byte-identical is left alone and reported as [same], so this
    doubles as the proof that what came back is what went up.
    """
    HfApi, _, hf_hub_download, _, whoami = _import_hub()

    api = HfApi()
    token, hub_user = _identity_or_die(whoami)
    print(f"[INFO] acting as {hub_user} -> {REPO_TYPE}s/{REPO_ID}")

    try:
        names = [
            rel for rel in api.list_repo_files(REPO_ID, repo_type=REPO_TYPE, token=token)
            if rel.lower().endswith(".parquet") and not _hidden(Path(rel))
        ]
    except Exception as exc:
        _fail(f"cannot list the files in {REPO_TYPE}s/{REPO_ID}: {exc}")

    if not names:
        _fail(f"{REPO_TYPE}s/{REPO_ID} holds no parquet - push one first")

    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)

    for rel in sorted(names):
        try:
            got = hf_hub_download(repo_id=REPO_ID, repo_type=REPO_TYPE,
                                  filename=rel, token=token)
        except Exception as exc:
            _fail(f"downloading {rel}: {exc}")
        source = Path(got)
        target = out / Path(rel).name
        if target.exists() and _sha256(target) == _sha256(source):
            print(f"[same] {rel} -> {target} (already byte-identical)")
            continue
        shutil.copyfile(source, target)
        print(f"[pull] {rel} -> {target} ({row_count(target)} row(s))")

    print(f"[OK] {len(names)} parquet pulled into {out} - nothing on HF was changed")


def do_login() -> None:
    """No arguments: authenticate and confirm who we are. Nothing is uploaded."""
    _, _, _, login, whoami = _import_hub()

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        token = getpass.getpass(f"Hugging Face token ({TOKEN_ENV} is not set): ").strip()
    if not token:
        _fail("no token given, nothing to log in with")

    try:
        # skip_if_logged_in=False so an explicit token always replaces whatever was cached
        login(token=token, add_to_git_credential=False, skip_if_logged_in=False)
    except Exception as exc:
        _fail(f"login failed: {exc}")

    try:
        me = whoami(token=token)
    except Exception as exc:
        _fail(f"the token was stored but the Hub rejected it: {exc}")

    print(f"[OK] logged in as {me.get('name')} ({me.get('fullname') or 'no display name'})")
    print(f"[INFO] the token is cached, so later runs do not need {TOKEN_ENV}")
    print(f"[INFO] repo this script uses: {REPO_TYPE}s/{REPO_ID} (private={PRIVATE})")


def _identity_or_die(whoami) -> tuple[str | None, str]:
    """Return (token, hub user name); token is None when the cached one should be used.

    The token is validated here, before any upload work, because create_repo(exist_ok=True)
    deliberately swallows 401/402/403 -- it assumes such an error just means the repo
    already exists. Without this check a wrong token would only fail much later, part way
    through a commit, with a message that does not mention authentication at all.
    """
    token = os.environ.get(TOKEN_ENV, "").strip() or None
    try:
        me = whoami(token=token) if token else whoami()
    except Exception as exc:
        if token:
            _fail(f"the Hub rejected ${TOKEN_ENV}: {exc}")
        _fail(f"not logged in. Set {TOKEN_ENV}, or run: uv run hfinit.py")
    return token, str(me.get("name") or "?")


# ------------------------------------------------------------------- local file pick

def _hidden(rel: Path) -> bool:
    """True when any path component is a dot-name.

    Skips the ".books.parquet.tmp" half-write text_collect.py leaves behind mid-commit,
    and the ".cache" folder hf_hub_download creates inside a local_dir.
    """
    return any(part.startswith(".") for part in rel.parts)


def local_files(raw: str) -> tuple[Path, list[Path]]:
    """Resolve the argument to (base, files). Repo paths are relative to base.

    Not recursive: the collection is ONE parquet, pdfs/parquet/books.parquet, written by
    crawler/bin/text_collect.py. Dot-files are skipped so the ".books.parquet.tmp"
    half-write is never picked up mid-commit.
    """
    path = Path(raw)
    if not path.exists():
        _fail(f"no such file or folder: {raw}")

    if path.is_file():
        return path.parent, [path]

    found = sorted(p for p in path.glob("*.parquet")
                   if p.is_file() and not _hidden(p.relative_to(path)))
    if not found:
        _fail(f"no *.parquet in {raw} (expected {PARQUET_PATH})")
    return path, found


def row_count(path: Path) -> int | None:
    """Row count straight from the parquet footer -- cheap, never reads the data."""
    try:
        import pyarrow.parquet as pq

        return pq.ParquetFile(path).metadata.num_rows
    except Exception:
        return None  # not a parquet we can read; the upload still goes ahead


# ----------------------------------------------------------------------------- cdc

def source_compression(path: Path) -> dict:
    """The codec this parquet was written with, so a CDC rewrite does not change it.

    crawler/bin/text_collect.py writes compression="zstd", compression_level=19 on
    purpose - measured ~40% smaller than pyarrow's default on segmented Burmese text.
    The level is not recorded inside the file, so the pipeline's own value is reused.
    Without this, rewriting for CDC silently fell back to snappy: a 10,487-byte batch
    came back as 28,599 bytes, 2.7x larger, with the data unchanged.
    """
    try:
        import pyarrow.parquet as pq

        codec = pq.ParquetFile(path).metadata.row_group(0).column(0).compression
    except Exception:
        return {}
    codec = (codec or "").upper()
    if codec == "ZSTD":
        return {"compression": "zstd", "compression_level": 19}
    if codec in ("", "UNCOMPRESSED"):
        return {}
    return {"compression": codec.lower()}


def rewrite_with_cdc(path: Path, workdir: Path) -> Path:
    """Re-write one parquet with Content-Defined Chunking + page index.

    Returns the path to upload. Fails loudly rather than silently uploading the
    un-optimised original, because a silent no-op would cost a full re-transfer
    every single run.
    """
    import pyarrow.parquet as pq

    target = workdir / path.name
    if target.resolve() == path.resolve():
        _fail(f"refusing to rewrite {path.name} over itself - the CDC work dir is the source dir")
    before = row_count(path)
    try:
        table = pq.read_table(path)
        pq.write_table(
            table,
            target,
            use_content_defined_chunking=True,
            write_page_index=True,
            **source_compression(path),
        )
    except TypeError as exc:
        _fail(
            f"--cdc needs pyarrow>=21 for use_content_defined_chunking; this pyarrow "
            f"refused it ({exc}). Upload pyarrow or drop --cdc."
        )
    except Exception as exc:
        _fail(f"could not rewrite {path.name} with CDC: {exc}")

    after = row_count(target)
    if before is not None and after != before:
        _fail(f"CDC rewrite of {path.name} changed the row count ({before} -> {after}); not uploading")
    return target


# ---------------------------------------------------------------------------- push

def push(base: Path, files: Sequence[Path], use_cdc: bool) -> None:
    HfApi, CommitOperationAdd, _, _, whoami = _import_hub()

    api = HfApi()
    token, hub_user = _identity_or_die(whoami)
    print(f"[INFO] acting as {hub_user} -> {REPO_TYPE}s/{REPO_ID}")

    # Creates the repo the first time, a no-op afterwards.
    try:
        api.create_repo(
            REPO_ID,
            repo_type=REPO_TYPE,
            private=PRIVATE,
            exist_ok=True,
            token=token,
        )
    except Exception as exc:
        _fail(f"cannot create or reach {REPO_TYPE}s/{REPO_ID}: {exc}")

    try:
        existing = set(api.list_repo_files(REPO_ID, repo_type=REPO_TYPE, token=token))
    except Exception as exc:
        _fail(f"cannot list the files already in {REPO_TYPE}s/{REPO_ID}: {exc}")

    workdir_ctx = tempfile.TemporaryDirectory(prefix="hfinit-cdc-") if use_cdc else None
    try:
        workdir = Path(workdir_ctx.name) if workdir_ctx else None
        operations = []
        updated: list[str] = []
        added: list[str] = []

        for path in files:
            # Relative to the folder given, so pdfs/parquet/books.parquet -> books.parquet.
            rel = path.relative_to(base).as_posix()
            rows = row_count(path)
            rows_txt = f"{rows} row(s)" if rows is not None else "row count unreadable"
            upload_path = rewrite_with_cdc(path, workdir) if use_cdc else path

            if rel in existing:
                updated.append(rel)
                print(f"[UPDATE] {rel} <- {path} ({rows_txt}) replaces the copy on HF")
            else:
                added.append(rel)
                print(f"[NEW]    {rel} <- {path} ({rows_txt}) added as a new dataset file")

            operations.append(CommitOperationAdd(path_in_repo=rel, path_or_fileobj=upload_path))

        parts = []
        if added:
            parts.append(f"add {', '.join(added)}")
        if updated:
            parts.append(f"update {', '.join(updated)}")
        message = f"hfinit: {'; '.join(parts)}"

        print(f"[INFO] committing {len(operations)} file(s) to {REPO_TYPE}s/{REPO_ID}")
        try:
            info = api.create_commit(
                repo_id=REPO_ID,
                operations=operations,
                commit_message=message,
                repo_type=REPO_TYPE,
                token=token,
            )
        except Exception as exc:
            _fail(f"the commit was rejected by the Hub: {exc}")
    finally:
        if workdir_ctx is not None:
            workdir_ctx.cleanup()

    url = f"https://huggingface.co/{REPO_TYPE}s/{REPO_ID}"
    print(f"[OK] {len(added)} new, {len(updated)} updated, in 1 commit -> {url}")
    print(f"[OK] nothing on this machine was deleted or modified")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hfinit.py",
        description="Log in to Hugging Face (no arguments), or push parquet files into "
        f"{REPO_TYPE}s/{REPO_ID}. A file whose name is already in the repo is updated in "
        "place; a new name is added as a new dataset file.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="a .parquet file, or a folder whose .parquet files are pushed "
        "(relative to the current directory, not recursive). Omit to log in only.",
    )
    parser.add_argument(
        "--cdc",
        action="store_true",
        help="rewrite each parquet with Content-Defined Chunking first (needs pyarrow>=21) "
        "so later row updates transfer only the changed chunks",
    )
    parser.add_argument(
        "--download",
        metavar="DEST",
        help=f"return leg of the round trip: pull the parquet back down out of "
        f"{REPO_TYPE}s/{REPO_ID} into the folder DEST",
    )
    args = parser.parse_args(argv)

    try:
        if args.download is not None:
            if args.path is not None:
                _fail("give a path to push OR --download DEST, not both")
            if args.cdc:
                _fail("--cdc applies to pushing, not to --download")
            pull(args.download)
        elif args.path is None:
            if args.cdc:
                _fail("--cdc does nothing without a file or folder to push")
            do_login()
        else:
            push(*local_files(args.path), use_cdc=args.cdc)
    except KeyboardInterrupt:
        print("\n[ERROR] interrupted", file=sys.stderr)
        return 1
    except HfInitError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

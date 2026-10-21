#!/usr/bin/env python3
"""hfinit.py -- log in to Hugging Face, then push parquet files into ONE dataset repo.

    uv run hfinit.py                              log in only
    uv run hfinit.py ./parquet                    every *.parquet in that folder
    uv run hfinit.py ./parquet/books.parquet      just that file

Local paths are relative to the current directory. A folder is NOT walked recursively,
and dot-files are skipped -- which conveniently ignores the ".books.parquet.tmp"
half-written files that crawler/bin/text_collect.py leaves behind mid-write.

How the name check works
------------------------
    name already in the repo -> UPDATE that same path in the repo
    name not in the repo     -> ADD the whole file as a new dataset file

UPDATE replaces the file on HF with your local copy; it does not download anything and
does not merge rows on this machine. The dataset still "gains the new rows" because your
local parquet is cumulative -- crawler/bin/text_collect.py appends every book into
books.parquet and rewrites it atomically, so the local copy already holds old rows + new
rows. Re-uploading it is the append.

That assumption is the one thing to watch: if you ever point this at a file holding ONLY
new rows, UPDATE would drop the rows already on HF.

Everything in one run goes into ONE commit, because the Hub allows roughly 320 commit
creations per hour and one upload_file call is one commit.

--cdc
---
Rewrites each parquet with Parquet Content-Defined Chunking before uploading (needs
pyarrow>=21). Without it, appended rows shift every page boundary and column header, so
Xet re-transfers most of the file. With it, only the changed ~64 kB chunks travel. The
first --cdc run pays one full re-upload, because every chunk boundary moves at once.

Environment
-----------
    HG_TOKEN   Hugging Face access token (the "hg_full" one). If unset, login prompts
               and the token is written to the HF cache, so later runs need nothing.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import tempfile
from pathlib import Path
from typing import Sequence

# ---------------------------------------------------------------- edit these two lines
HF_USER = "htetmyetflam3"  # your Hugging Face user name (same as your git user name)
HF_REPO_NAME = "scraper"  # the one dataset repo this script ever touches
# -------------------------------------------------------------------------------------

REPO_ID = f"{HF_USER}/{HF_REPO_NAME}"
REPO_TYPE = "dataset"
PRIVATE = True
TOKEN_ENV = "HG_TOKEN"


class HfInitError(RuntimeError):
    """Anything that should stop the run with a readable message instead of a traceback."""


def _fail(message: str) -> "NoReturn":  # noqa: F821 - NoReturn only needed for typing
    print(f"[ERROR] {message}", file=sys.stderr)
    raise SystemExit(1)


def _import_hub():
    try:
        from huggingface_hub import CommitOperationAdd, HfApi, login, whoami
    except ImportError as exc:  # pragma: no cover - environment problem
        _fail(f"huggingface_hub is not installed ({exc}). Try: uv sync")
    return HfApi, CommitOperationAdd, login, whoami


# --------------------------------------------------------------------------- login

def do_login() -> None:
    """No arguments: authenticate and confirm who we are. Nothing is uploaded."""
    _, _, login, whoami = _import_hub()

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

def local_files(raw: str) -> list[Path]:
    """Resolve the argument to the list of files that will be pushed."""
    path = Path(raw)
    if not path.exists():
        _fail(f"no such file or folder: {raw}")

    if path.is_file():
        return [path]

    found = sorted(p for p in path.glob("*.parquet") if p.is_file())
    if not found:
        _fail(f"no *.parquet files directly inside {raw} (sub-folders are not walked)")
    return found


def row_count(path: Path) -> int | None:
    """Row count straight from the parquet footer -- cheap, never reads the data."""
    try:
        import pyarrow.parquet as pq

        return pq.ParquetFile(path).metadata.num_rows
    except Exception:
        return None  # not a parquet we can read; the upload still goes ahead


# ----------------------------------------------------------------------------- cdc

def rewrite_with_cdc(path: Path, workdir: Path) -> Path:
    """Re-write one parquet with Content-Defined Chunking + page index.

    Returns the path to upload. Fails loudly rather than silently uploading the
    un-optimised original, because a silent no-op would cost a full re-transfer
    every single run.
    """
    import pyarrow.parquet as pq

    target = workdir / path.name
    before = row_count(path)
    try:
        table = pq.read_table(path)
        pq.write_table(
            table,
            target,
            use_content_defined_chunking=True,
            write_page_index=True,
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

def push(files: Sequence[Path], use_cdc: bool) -> None:
    HfApi, CommitOperationAdd, _, whoami = _import_hub()

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
            rows = row_count(path)
            rows_txt = f"{rows} row(s)" if rows is not None else "row count unreadable"
            upload_path = rewrite_with_cdc(path, workdir) if use_cdc else path

            if path.name in existing:
                updated.append(path.name)
                print(f"[UPDATE] {path.name} <- {path} ({rows_txt}) replaces the copy on HF")
            else:
                added.append(path.name)
                print(f"[NEW]    {path.name} <- {path} ({rows_txt}) added as a new dataset file")

            operations.append(CommitOperationAdd(path_in_repo=path.name, path_or_fileobj=upload_path))

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
    args = parser.parse_args(argv)

    try:
        if args.path is None:
            if args.cdc:
                _fail("--cdc does nothing without a file or folder to push")
            do_login()
        else:
            push(local_files(args.path), use_cdc=args.cdc)
    except KeyboardInterrupt:
        print("\n[ERROR] interrupted", file=sys.stderr)
        return 1
    except HfInitError as exc:
        _fail(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Commands — every flag, in one place

One document for every command in this repo: what it does, every flag it takes, and
what it refuses to do. Everything here is taken from the code, not from memory — the
`argparse`/`sys.argv` block of each file is the source of truth, and `--help` on any of
them prints the same thing.

Python commands run through `uv` from the repo root:

```
uv run main.py --process
```

Shell scripts run directly: `./install.sh`, `./git.sh "message"`.

---

## Which command do I want?

| I want to… | Run |
| --- | --- |
| Search for new sites, then crawl and download | `uv run main.py --main -l 2` |
| Download overnight while uploading | `uv run main.py --process` |
| Only push what's already in `pdfs/` to the host | `uv run main.py --upload` |
| Download Scribd documents | `uv run main.py --scribd` |
| Download the phatmal library | `uv run main.py --phatmal` |
| Fill the parquet's `sha256` column from the host | `uv run shafill.py` |
| Push the book collection to Hugging Face | `uv run hfinit.py pdfs/parquet` |
| Pull it back to prove the round trip | `uv run hfinit.py --download ./verify` |
| Move one file to or from the VPS | `uv run vps.py --upload SRC DEST` |
| Start the continuous OCR service | `uv run ocr.py` |
| Rebuild the collection from the host's `convertable/` PDFs (parser, no OCR) | `uv run parse.py` |
| Install `scraper.service` (the download pipeline) | `sudo python3 deploy/install_service.py -- --process -l 2` |
| Find duplicate PDFs on disk | `uv run crawler/bin/dedup_pdfs.py` |
| Backfill URLs that failed | `uv run crawler/bin/failed_urls.py --dry-run` |

---

## `main.py` — the scrape pipeline

One command per run; with no command it prints help and runs nothing. The seven
commands are mutually exclusive.

| Flag | Argument | What it does |
| --- | --- | --- |
| `--findentry` | `[KEYWORD]` | Search `KEYWORD` with SerpApi, or the built-in keywords when empty. Results always go to `scan/search_results.json`; keywords already saved there skip the API. Then crawl only those keywords' results and append found sites to `scan/entry.txt`. |
| `--main` | — | Search (built-in keywords) → entry crawl (all saved results) → download from `scan/entry.txt`, in order. |
| `--download` | `[URL_OR_FILE]` | Download from the sites in `scan/entry.txt`, or from `URL_OR_FILE`: one site URL or another entries file. Ctrl+C pauses; rerun resumes. |
| `--upload` | — | Push every file in `pdfs/` to the SSH host (`SSH_*` in `.env`), verify its sha256, then delete the local copy. When the folder is empty it mails "finished" and exits. |
| `--process` | `[URL]` | Night run in one terminal: download from `scan/entry.txt`, or from `URL` (one site URL, so no `scan/entry.txt` needed), while uploading to the SSH host. Stops at the 6-hour mark and mails a report. |
| `--scribd` | `[URL_OR_FILE]` | Download Scribd documents from `scan/scribd.txt`, or from `URL_OR_FILE`: one Scribd URL or a links file. Any non-Scribd URL stops the run before downloading. |
| `--phatmal` | `[URL]` | Download the whole phatmal library (`PHATMAL_*` in `.env`), or one book from a `https://phatmal.com/ebook/<slug>` URL. Any other URL is an error. |

Numbers, both optional:

| Flag | Argument | What it does |
| --- | --- | --- |
| `-l` | `MB_PER_SEC` | With `--download`/`--main`/`--process`: download speed cap in MB/s (1 MB = 1048576 bytes). Default 0 = unlimited. |
| `-i` | `SECONDS` | With `--process`: seconds between upload folder checks. Default 60. |

```
uv run main.py --findentry
uv run main.py --findentry "pdfများ"
uv run main.py --main -l 2
uv run main.py --download https://example.com -l 2
uv run main.py --download my-entries.txt
uv run main.py --process https://example.com -i 60 -l 5
uv run main.py --scribd https://www.scribd.com/document/123/title
uv run main.py --phatmal https://phatmal.com/ebook/<slug>
```

**Exit codes** — 0 ok (also `--process` stopping at its 6-hour mark), 1 failed,
2 usage error, 130 paused (Ctrl+C; rerun resumes).

**Email** — every command except `--findentry` mails a started notice, then a finished
(`--upload`: when `pdfs/` is empty) or failed one naming the part that failed.
`--process` mails started, then one report at the 6-hour mark (or when all is done)
listing every problem with its part and time; nothing in between unless it quits with
an error, in which case the failed email goes right away with the problems so far.

**Upload retries** — a file that fails is retried after 15 s, 30 s, 45 s, 1 min,
1 min 30 s, 2 min, 2 min 30 s and 3 min (`UPLOAD_WAITS` in `crawler/bin/save_remote.py`).
Then uploading stops instead of looping forever: with an error when under 5 GB is free,
otherwise normally, and the files stay in `pdfs/`.

**Disk** — 5 GB is kept free for the system (`DISK_FLOOR` in `crawler/bin/disk_floor.py`).
Every download command checks it before each file and quits with an error under it; free
space, then rerun to resume.

**Detached runs** — `main.py` re-launches itself under systemd so a dropped SSH session
cannot kill a night run. `SCRAPER_SYSTEMD_CHILD=1` and `SCRAPER_SYSTEMD_DIRECT=1` are set
by `scripts/systemd_launcher.py` and are not meant to be set by hand.

---

## `shafill.py` — fill the parquet's `sha256` column (round one)

Rows written before the `sha256` column existed carry no hash, so the writer's duplicate
guard cannot match them on their file. This connects to the shared host with the same
`SSH_*` settings `save_remote.py` uses, hashes every PDF in `<SSH_DIR>/convertable/` in
**one** ssh round trip, matches each hash to a row by its `book` value, and writes it into
that row's column. Books whose PDF is no longer on the host fall back to
`scan/uploaded-host.txt`, which recorded the hash at upload time; the report says which
hashes came from a live file and which from history.

| Flag | Argument | What it does |
| --- | --- | --- |
| *(positional)* | `[pdfs]` | The `pdfs/` dir holding `parquet/books.parquet`. Default: this repo's own. |
| `--dry-run` | — | Report what would be written; write nothing. |
| `--force` | — | Overwrite a hash that disagrees with the host. |

```
uv run shafill.py --dry-run     # look first
uv run shafill.py               # fill the column
uv run shafill.py --force       # also correct hashes that disagree
uv run shafill.py /other/pdfs   # a different collection
```

**What it will not do:** touch `author`, `book`, `text` or `segmented`; add or remove a
row; delete anything on either box; or guess a hash it cannot find — that row is reported
and left empty. An existing hash that disagrees is **kept** and reported as a conflict
unless `--force`, because a disagreement means the file on the host changed or two books
were matched to one file. Running it twice is a no-op the second time. An unreachable host
is not fatal: it says why and still fills what the ledger can.

---

## `hfinit.py` — the book collection on Hugging Face

With no argument it only logs in. With a path it pushes every `*.parquet` directly inside
that folder into the one hardcoded private dataset repo, `datasets/htetmyetflam3/scraper`,
created on first run. A file whose name is already in the repo is **updated in place**; a
new name is **added** as a new dataset file. Every file in a run goes into a single commit,
because the Hub allows only about 320 commit creations per hour.

| Flag | Argument | What it does |
| --- | --- | --- |
| *(positional)* | `[path]` | A `.parquet` file, or a folder whose `.parquet` files are pushed. Relative to the current directory, **not recursive**. Omit to log in only. |
| `--download` | `DEST` | The return leg of the round trip: pull the parquet back down out of the repo into the folder `DEST`. |
| `--cdc` | — | Rewrite each parquet with Content-Defined Chunking first (needs `pyarrow>=21`) so later row updates transfer only the changed chunks. |

```
uv run hfinit.py                        # log in only
uv run hfinit.py pdfs/parquet           # push the collection
uv run hfinit.py --download ./verify    # pull it back and compare
uv run hfinit.py pdfs/parquet --cdc     # chunk-optimised push
```

The token is read from `HG_TOKEN` (the access token named `hg_full`), or prompted for, and
is validated with `whoami` **before** any upload work. There is deliberately no repo listing
and no repo picker. `--download` is mutually exclusive with a push path and with `--cdc`;
an empty repo exits 1 rather than creating an empty folder. Dot-files are skipped, so the
`.books.parquet.tmp` half-write is never uploaded mid-commit. A file already at `DEST` that
is byte-identical is reported `[same]` and left alone.

The three constants are obvious lines at the top of the file: `HF_USER`, `HF_REPO_NAME`,
`PARQUET_PATH`.

---

## `vps.py` — one-shot file transfer to and from the VPS

A thin wrapper around `ssh hostingervps`. Both directions run from the local machine only,
never on the VPS. The flag order is the only difference: `--upload` is local-then-remote,
`--download` is remote-then-local. One of the two is **required**.

| Flag | Argument | What it does |
| --- | --- | --- |
| `--upload` | — | Push a local file or directory to a remote directory. |
| `--download` | — | Pull a remote file or directory to a local directory. |
| *(positional)* | `SOURCE` | Local path (`--upload`) or remote path (`--download`). |
| *(positional)* | `DEST` | Remote directory (`--upload`) or local directory (`--download`). |

```
uv run vps.py --upload   ./.env                scraper/
uv run vps.py --upload   ./pdfs/parquet/       /root/scraper/parquet/
uv run vps.py --download '~/scraper/.env'      ./
uv run vps.py --download scraper/pdfs/parquet/ ./parquet/
```

The local side is relative to your cwd, like any shell command. The remote side is relative
to the VPS account home, so `scraper/.env` and `~/scraper/.env` both mean
`/root/scraper/.env`; an absolute path is used as given. **Quote a leading `~`** — unquoted,
your own shell rewrites it to your local home before this script sees it. A path that ends
in `/`, or an existing directory, transfers every file inside it; sub-directories are
skipped, not followed. Nothing is deleted on either box. One-shot on purpose: no resume, no
retry waits, no sha256 pass — for the big Parquet collection use `hfinit.py`.

The VPS key is hardcoded to `~/.ssh/mylinux` and pinned with `IdentitiesOnly=yes`, so only
that key is ever offered.

---

## `ocr.py` — install and start the continuous OCR

```
uv run ocr.py          # no flags
```

Prepares only the additive OCR runtime, installs its independent systemd units, and starts
the OCR detached from your SSH session. Existing scraper modules and configuration are
imported in place, never copied over or replaced. It gates on the presence of the modules it
needs and runs `npm ci` for the parser runtime.

The OCR is a **standalone service** — it is not wired into the download or upload path. It
downloads from the shared host, records, and OCRs: success writes one parquet row and
deletes the downloaded file; failure records the book, **keeps** the file, and continues
whether or not you delete it. At 20 retained failures it mails you, at most once an hour.

It installs three units:

| Unit | What it is |
| --- | --- |
| `scraper-ocr.service` | The OCR loop. `systemctl start`/`stop`; logs with `journalctl -u scraper-ocr.service -f`. |
| `scraper-ocr-reminder.service` | One reminder check, invoked by the timer. |
| `scraper-ocr-reminder.timer` | Hourly (`OnBootSec=2min`, `OnUnitInactiveSec=1h`), enabled and started by the installer. |

`deploy/continuous-ocr/run.py` is what those units execute. Its two flags are for systemd,
not for you:

| Flag | What it does |
| --- | --- |
| `--remind` | Run one reminder check and exit: mark failures whose PDF is gone as `removed`, and mail if 20 or more remain and an hour has passed since the last mail. |
| `--failure-hook` | The service's `ExecStopPost` — the last chance to say the OCR is down. Reads `SERVICE_RESULT`/`EXIT_STATUS` and mails unless the result is `success` or unset. It runs **before** anything opens `state.sqlite3`, so a full disk or a corrupt database cannot silence it. |

Stopping it yourself (`systemctl stop`) sends `SIGINT`, which mails one *finished* notice —
"OCR stopped by owner/system … Unfinished inputs retained." — and exits 0, so the failure
hook correctly stays silent instead of doubling it. A hard failure (OOM, `SIGKILL`, segfault,
timeout, non-zero exit, start-limit) mails from the hook.

---

## `parse.py` — the continuous convertable parse

```
uv run parse.py          # no flags
```

`ocr.py`'s twin for the parser-only service. It installs that service's own three
systemd units and starts it detached from your SSH session, so you can close the
terminal. There is no OCR runtime to prepare — no tesseract, no `worker.mjs`, no
`npm ci` — because the only native step left is the scraper's own Engine segmentor.
It runs as root on the systemd VPS, in the same account as `scraper.service`, and
never stops `scraper.service` or the OCR service.

What it does: every PDF in `<SSH_DIR>/convertable/` on the shared host, downloaded
read-only, its text extracted and converted (Zawgyi-One or Unicode) with **no
classification verdict**, segmented, and appended as one row to
`pdfs/parquet/books.parquet`. Success deletes the local download; failure keeps it,
records the reason, and mails at 20 retained failures, at most once an hour. Nothing
on the host is ever written to or deleted.

This is the command that **rebuilds a lost collection**: if `books.parquet` is wiped,
its ledger rows no longer match anything in the file, so the service re-queues every
one of them and re-parses the host's `convertable/` directory back into a fresh
parquet. The writer's duplicate guard is what makes that safe to run twice.

Why no verdict: the books in `convertable/` are already the Zawgyi-One and Unicode
ones whose text survives intact, and the old-font gates — image dominance, Myanmar
characters per page, the structural scan rate that judges WinNwa / Ayar / legacy
layout decodes — can only throw a verified book away. `classify_pdf` still uses them
for PDFs nobody has looked at; this path deliberately does not. On a real fixture the
gates reject a 41-page book as `old` with zero characters, and this path reads all
52,209 of them. A PDF with no text layer at all still fails and is kept, so an empty
row is never written.

It installs three units, mirroring the OCR's:

| Unit | What it is |
| --- | --- |
| `scraper-parse.service` | The parse loop. `systemctl start`/`stop`; logs with `journalctl -u scraper-parse.service -f`. |
| `scraper-parse-reminder.service` | One reminder check, invoked by the timer. |
| `scraper-parse-reminder.timer` | Hourly (`OnBootSec=2min`, `OnUnitInactiveSec=1h`), enabled and started by the installer. |

`deploy/continuous-parse/run.py` is what those units execute. Its two flags are for
systemd, not for you, and behave exactly like the OCR's:

| Flag | What it does |
| --- | --- |
| `--remind` | Run one reminder check and exit: mark failures whose PDF is gone as `removed`, and mail if 20 or more remain and an hour has passed since the last mail. |
| `--failure-hook` | The service's `ExecStopPost`. Reads `SERVICE_RESULT`/`EXIT_STATUS` and mails unless the result is `success` or unset. Runs **before** anything opens `state.sqlite3`, so a full disk or a corrupt database cannot silence it. |

Its ledger is `scan/parse/state.sqlite3`, separate from the OCR's `scan/ocr/`, and
each service counts its own retained failures. Both resolve the same PDF to the same
`book_key`, so a book the OCR already recorded is denied by the guard rather than
written twice — and if this parser reads the same file better, the row that already
holds that PDF is updated in place instead of a second book being opened for it.

---

## `deploy/install_service.py` — install `scraper.service`

```
sudo python3 deploy/install_service.py -- --process -l 2 -i 60
sudo python3 deploy/install_service.py --help
```

Everything after `--` is passed straight to `main.py` as the service's command. It installs
`scraper.service` but does **not** start it or enable it at boot. At start the service checks
the JS engine (node + bytecode + tree) and rebuilds the bytecode if this machine's Node needs
it. Requires root, a running systemd, `uv` on `PATH`, and Node.js 22+ — it does not install or
replace Node.

## `deploy/engine_ready.py` — the engine pre-start check

```
uv run deploy/engine_ready.py          # check only
uv run deploy/engine_ready.py --fix    # rebuild the Engine bytecode if needed
uv run deploy/engine_ready.py --help   # print its docstring (-h also works)
```

`scraper.service` runs it as `ExecStartPre`, so one `systemctl start` is the whole automated
process and nobody has to run `npm run rebuild` by hand on the VPS.

---

## Maintenance and diagnostic tools

| Command | Flags | What it does |
| --- | --- | --- |
| `uv run crawler/bin/dedup_pdfs.py` | `[directory]`, `--delete` | Scan for duplicate PDFs. `directory` defaults to this repo's `pdfs/`. **Dry run by default** — `--delete` actually removes them. Exit 2 if the directory does not exist. |
| `uv run crawler/bin/failed_urls.py` | `--scan-dir DIR`, `--dry-run`, `--all`, `--zip ZIP`, `--pdfs-dir DIR`, `--manifest FILE`, `--delete-zip ZIP`, `--base DIR`, `--yes` | Backfill failed URLs from crawl/download history, or audit a batch zip. `--all` counts every unmatched crawled URL, not only download-looking ones. `--zip` and `--delete-zip` are mutually exclusive. `--delete-zip` is a separate step after the upload is verified and deletes exactly the files in the zip's manifest — **dry run unless `--yes`**. |
| `uv run crawler/bin/pdf_classify.py` | `FILE.pdf [FILE.pdf …]` | Print one PDF's category, encoding, page count, decode result, text/image bytes and scan counts. Exit 2 with a usage line if given no file. |
| `uv run crawler/bin/text_collect.py` | `[OUTPUT_DIR]` | Manual run-end flush of `scan/collected.jsonl` into the collection: one segmentor pass, then the parquet write. `OUTPUT_DIR` defaults to the pipeline's own. Exit 1 if the flush failed. Note this is currently dormant — `append_document()`, the only writer of that journal, has no callers, so the flush reports "no convertable text collected". |
| `uv run praser/scan.py` | `file`, `-o/--out`, `-n/--per-rule N`, `-d/--dict` | Scan decoded text for the damage a quality score cannot see: a mark with no base, a pair Burmese cannot write, a syllable printed three times. `file` is a parsed `.txt` produced by the praser; the log defaults to `<file>.scan.txt`; `-n` is examples per rule (default 25). |
| `uv run wasm/box/loader.py` | `[sample]` | Host loader for `wasm/box/box.wasm`: boots the QuickJS engine in memory and exposes the three normalizer modes. The optional argument is the text to normalize (default `မြန်မာစာ`). |

---

## Shell scripts

| Script | Arguments | What it does |
| --- | --- | --- |
| `./install.sh` | none | System installer for the pipeline (see `INSTALL.md`): the Python dependencies and the Engine's one npm requirement, then prints how to verify. Safe to re-run. Preflights Node.js 22+ via `scripts/check-node.mjs` and refuses to run without it; uses `uv sync` when `uv` is present, otherwise `pip3`. |
| `./git.sh` | `[commit message]` | Reads `package.json`, bumps the patch version, stages everything recursively, commits with your message or a default one, and records commit history into `.commit`. |
| `./gitinit.sh` | none — **interactive** | One-time repo identity setup. Prompts for the SSH private key, git user name and email, and the remote URL, with defaults inline at the top (`DEFAULT_REMOTE`, `DEFAULT_USER`, `DEFAULT_EMAIL`, `DEFAULT_BRANCH`, `DEFAULT_SSH_KEY`). Press enter to accept each default. |

## npm scripts

| Command | What it does |
| --- | --- |
| `npm run rebuild` | Check Node (`scripts/check-node.mjs`, minimum major 22), install the mapbuilder's two npm requirements if missing, restore the Engine master, then rebuild the obfuscated bytecode. |
| `npm run rebuild:wasm` | Check Node, then `sh ./wasm/rebuild-box.sh`. |

---

## Environment

All of it lives in the repo-root `.env`; `.env.example` documents every key. The environment
wins over the file.

**Required for uploading** — `SSH_HOST`, `SSH_USER`. Optional: `SSH_PORT` (default 22),
`SSH_DIR` (default `pdfs`) — the remote directory that *contains* `convertable/`, and `scan/` or `scanned/` for the OCR service; it mirrors this checkout's own `pdfs/`. A relative value is resolved by ssh against the account's home, so the default means `<home>/pdfs/convertable/`; write an absolute path only if the files really live outside it. The value is used verbatim — no `~` expansion, no spaces. `SSH_KEY`.

**Email** — `MAIL_PASSWORD`. Without it the mailer logs `off` and every notice is silently
skipped, including the OCR failure mail and the 20-failure reminder.

**Hugging Face** — `HG_TOKEN`, the access token named `hg_full`. If unset, `hfinit.py`
prompts for it.

**Search and logins** — `SERPAPI` (`--findentry`), `PHATMAL_EMAIL`, `PHATMAL_PASSWORD`,
`PHATMAL_API_KEY` (`--phatmal`), `LOGIN_EMAIL`, `LOGIN_PASSWORD`, `LOGIN_URL`,
`LOGIN_USER_FIELD`, `LOGIN_PASS_FIELD`.

**Commented out in `.env.example`, off by default** — `PRASER_MAX_STREAM_MB` (256), the eight
`SCRIBD_*` tuning keys, and `LOGIN_SCRIBD_COM_EMAIL`/`LOGIN_SCRIBD_COM_PASSWORD`.

**Set by the launcher, not by hand** — `SCRAPER_SYSTEMD_CHILD`, `SCRAPER_SYSTEMD_DIRECT`.

---

## The collection, and the guard on it

There is **one** parquet: `pdfs/parquet/books.parquet`, zstd level 19, five columns —
`author | book | text | segmented | sha256`. `author` is deliberately left empty: PDF metadata
authorship is untrustworthy, most Burmese authors never embed their name, and online tools
overwrite the field. `book` is the filename minus `.pdf`.

Three writers reach that one file — `save_remote.py` during upload, the continuous-OCR service,
and the run-end flush — so appends are serialised across processes by a lock file beside the
parquet. Every write is a dot-temp inside `pdfs/parquet/` renamed within that same directory,
so a reader never sees a half-written file and `os.replace` can never hit `EXDEV`.

The writer refuses to record one book twice, matching on **both** identities a book has:

| Incoming row | Result |
| --- | --- |
| Same file (`sha256`) **and** same text | **Denied** — a plain re-add |
| Same text under a **different name** | **Denied** — "already in the collection as `'<name>'`" |
| Same name and same text, from a different file | Row kept, its hash corrected (`[parquet-fix]`) |
| Same file, **different** text | The row holding that PDF is updated in place (`[parquet-update]`) — no second book opened for one file |
| A genuinely different book with similar text | Both kept — only an exact match denies |

The second arm is what catches the same book fetched from a different source: a different file
with a different hash, but identical words, which the hash alone cannot see. It needs no hash,
so it already protects rows written before the column existed — before `shafill.py` has run.
Text shorter than 200 characters (`TEXT_DEDUPE_MIN_CHARS`) is matched on the hash only, so two
unrelated books whose parses both collapsed into the same stub are not mistaken for duplicates.
A denied row changes nothing, and a batch in which every row is denied does not rewrite the file.

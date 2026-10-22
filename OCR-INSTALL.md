# OCR overlay for your existing scraper directory

Unzip **scraper-ocr-overlay.zip directly into your existing scraper directory**, then run:

```sh
uv run ocr.py
```

No flags. Run as root on the systemd VPS, as with the existing scraper service. Keep Node 22+, npm, uv and ssh on that account's PATH.

The launcher prepares the added OCR dependencies once, installs its own background service/reminder timer, and requests OCR startup. **You can then close SSH.** It never starts or stops `scraper.service`.

## What is added — and what is not

`ocr.py`, `deploy/continuous-ocr/` and the private `ocr_runtime/` namespace are part of this repository. Installing them copies over or replaces no existing scraper module: the OCR imports `save_remote`, `mailer`, `text_collect` and the Engine bridge in place.

The runner imports your existing SSH configuration/download utilities, mailer, disk-floor rule, Engine bridge and Parquet writer **in place**. Your `.env`, SSH key, `crawler/`, `Engine/`, `pyproject.toml`, main command and existing scraper deployment files are not replaced. No site Engine, embedded-font outline-repair module, outline witness corpus, credentials, downloaded PDFs, node_modules or production ledger is bundled.

The runtime contains only the Linga modules/assets needed for raster OCR, scanner/dictionary repair and pixel-to-SVG region matching; the vector match module has an explicit whole-index cache addition. `ocr_runtime/Praser/parser/package-lock.json` is tracked, so the `npm ci` that `ocr.py` runs is reproducible, while `node_modules/` is not.

## Existing SSH/settings

Use your existing root-level `.env` or environment: `SSH_HOST`, `SSH_USER`, `SSH_PORT`, `SSH_KEY`, `SSH_DIR`, `MAIL_PASSWORD`. They are read by **your existing scraper loaders**, not a second parser — and the systemd units get them through `EnvironmentFile=-<repo root>/.env`, so the service sees the same values an interactive run does (the leading `-` means a missing `.env` is not fatal, since the environment may supply them instead). Copy `.env.example` to `.env` for the full key list. Keep the existing VPS key file and known_hosts; nothing here ships an SSH key or disables host-key checking.

`SSH_DIR` should be the hosting archive root containing `scan/` or `scanned/`. PDFs under either spelling are discovered recursively; `old/` and `convertable/` are excluded. Hosting originals are read-only. The VPS downloads mirror hosting-relative directories under `pdfs/.ocr/`.

The launcher checks required existing scraper files and Node version before setup. First-run npm setup needs internet. The service uses the existing scraper Engine preflight/rebuild to ensure its bytecode can run under the installed Node version; that is your existing Engine, never the site Engine.

## Status and stop

```sh
systemctl status scraper-ocr.service
journalctl -u scraper-ocr.service -f
systemctl stop scraper-ocr.service
```

`uv run ocr.py` again does not launch a duplicate while OCR is active. After it stops, the same command resumes eligible unfinished work. Recorded failed books are not automatically retried.

OCR has `Restart=no` and is not enabled at boot. There is no six-hour or month-end deadline. If desired, deliberately enable boot startup with `systemctl enable scraper-ocr.service`.

The independent reminder timer continues checking retained failed PDFs after OCR finishes. To stop reminders too:

```sh
systemctl disable --now scraper-ocr-reminder.timer
systemctl stop scraper-ocr-reminder.service
```

## Processing and output

List hosting → download one PDF → verify SHA256 → close SSH → record download → OCR → existing scraper Engine segmentation → existing Parquet writer → verify/fsync dataset → durable success record → delete **only the VPS PDF**.

Books are appended as single rows to ONE cumulative Parquet, `pdfs/parquet/books.parquet`, with the existing **author | book | text | segmented | sha256** schema — the same file and the same writer (`text_collect.write_parquet_row`) that the scraper's own convertable-book path uses, so the whole collection stays one table and `hfinit.py` pushes one file. Source identity, status and row key remain in `scan/ocr/state.sqlite3`. `sha256` is the hash of the PDF the text came from, and the writer's duplicate guard matches a book on **both** of its identities: a row whose hash is already in the file carrying the same text is refused, and so is a row whose exact text is already in the file under a different name - the same book fetched from another source is a different file with a different hash, which the hash alone cannot see. So one book cannot be recorded twice, under its own name or under a second one. The text arm needs no hash, so it already holds on rows written before the column existed; `./shafill.py` fills those hashes in from the PDFs on the shared host so the file arm covers them too. Text shorter than 200 characters is matched on the hash only, so two unrelated books whose parses both collapsed into the same stub are not mistaken for duplicates.

The row key is the filename minus `.pdf`. Because the OCR walks the hosting directories recursively, two different sources can share a basename, so a source whose basename is already taken by another source is keyed `<name>-<first 8 hex of the source hash>` instead — equal basenames in different directories cannot overwrite each other. The key is persisted per source, so a retry replaces its own row rather than appending a duplicate. Appends are serialised across *processes* by a lock file beside the Parquet, because `save_remote` and the OCR service are separate processes writing the same file and a threading lock cannot see the other one.

Hosting paths are treated as immutable book identities; a changed file at an already-successful path is not automatically enqueued.

Expected book-level OCR failures: record once, retain the downloaded PDF, continue to the next book. Transfer/checksum failures, worker death, Engine/dataset errors, disk-floor or runner failures stop OCR and attempt an email. On explicit restart interrupted nonfailed work may be recomputed; its deterministic output filename prevents duplicate dataset files for that source.

At 20 distinct unresolved failed books, send an immediate reminder, then hourly reminders until all retained failed PDFs are removed. Reminders remain latched even if the count drops below 20. Removing a failed VPS PDF marks it removed; its hosting original is not redownloaded. The timer checks once per minute, independently of the main loop.

The existing mail transport is best-effort. SIGINT/SIGTERM and systemd failures attempt stop/failure notices. Nothing can deliver mail while the whole VPS is powered off or mail connectivity is unavailable.

## Correct OCR repair path and caching

This runner uses **raster pixels → labelled SVG reference matching**, through the same raster helpers found on site. It does **not** use embedded-font glyph IDs or digital-PDF outline repair. The shared site/current helper implementation was compared byte-for-byte.

First worker startup loads the full Tesseract Myanmar model, full scan dictionary, and prepares all **2,598 raster-to-SVG reference entries** with masks/distance fields. Those assets stay resident across books. The prepared whole vector index is stored atomically in `scan/ocr/asset-cache/vector-index.v8` and reused on future service starts. Asset/preparation-code/V8 content fingerprints invalidate stale caches; corrupt caches rebuild. No TTL, per-word result cache or per-book asset reload is used.

Each book opens one PDF.js document and renders/cleans one page at a time. Scanner-flagged Tesseract regions may be matched against the cached reference index; replacements require matcher acceptance plus dictionary validation. Numeric/letter homoglyph-only changes are dictionary-arbitrated, never shape guesses. Output is normalized and dictionary-repaired. No extraction-only ZawCode unmixing is applied to OCR Unicode. Region-selection wiring is new integration code, not a claim that site already had this automatic orchestrator.

There is **no overall, per-book, per-page, cache-build or segmentation deadline**, and no web Manager 50/500-page ceiling. The existing per-page pixel safety cap and scraper 5 GB disk floor remain. SSH connection/mail delivery/setup preflight limits and the service's shutdown-cleanup allowance are not OCR processing deadlines. Without an OCR watchdog a hung job requires you to stop the service; this is intentional.

## Recovery and safety

Back up `scan/ocr/` and `pdfs/parquet/books.parquet` together. SQLite uses WAL/FULL synchronous commits. The Parquet is rewritten to a dot-temp inside `pdfs/parquet/` and renamed within that same directory, then fsynced, before success is recorded and the input deleted — so there is no cross-device move that could fail. Crash before success retains the PDF; restart recomputes it and replaces the same row.

On restart every source marked success is checked for its *row* in `books.parquet`, not for a per-book file. A row that is absent — for example a `state.sqlite3` left over from the older per-book `pdfs/parquet/ocr/<hash>.parquet` output — is re-queued and re-OCR'd once, with a `[migrate]` line in the journal, rather than stopping the service.

OCR does not share or modify scraper's collected-text journal. It reuses the actual bridge/writer with its own local state. Local locks prevent duplicate OCR runners and duplicate reminder checks in this deployment. Run only one deployment against these paths.

To reset only reference caching, stop OCR and delete `scan/ocr/asset-cache/`. **Do not delete state.sqlite3**: it is the queue/success/failure history, not a disposable cache.

No 6,000-book throughput, completion date or OCR accuracy guarantee is claimed.

# OCR overlay verification — 2026-10-21

## Copy checks (not a repeat of the existing components' tests)

- ZIP extraction into a temporary original scraper snapshot produced **zero filename collisions** and did not alter any original file.
- **34 unchanged OCR runtime files/assets** matched the committed Linga source byte-for-byte. One runtime file, `shape/match.mjs`, intentionally contains the new whole-index cache extension.
- The raster/OCR helper block in `codepage/vote.mjs` matches site byte-for-byte (SHA256 `3e1d79791d2314d0c46c7e2a4d72424993ea28a46481bc3331d49d8f02acd0cd`). It compares region pixels to labelled SVG references, not embedded-font glyph IDs.
- The ZIP does not contain original scraper files, Engine files, SSH/email configuration, keys, embedded-font outline repair, or its witness corpus.
- Existing scraper SSH, mail, disk-floor, collection writer, Engine bridge and preflight sources remained byte-identical after the integration check. Their reference hashes are in `OCR-MANIFEST.json`. **That statement is about that check, not about today** — see the next section for what has since changed and when the hashes were recomputed.

## Added integration checks

One end-to-end overlay check used simulated hosting transport, real file transfers/SHA256 checks, real Tesseract OCR, the actual existing scraper Engine bridge, and its actual Parquet writer:

- 20 invalid downloaded PDFs were marked failed and retained; the following valid PDF still completed.
- The valid PDF produced one readable **author | book | text | segmented** row: 1,324 stored text characters and 2,239 segmented characters.
- Only its VPS download was deleted, after dataset commit. All 22 simulated hosting originals remained untouched, including an excluded old/ PDF.
- Reinvocation neither retried failures nor duplicated the dataset file.
- The twentieth failure triggered one reminder. Immediate reinvocation did not send another.
- Advancing the recorded reminder time demonstrated a reminder with 19 remaining failed PDFs (latched behavior). Removing all retained failures cleared the latch and did not redownload originals.

The no-flag launcher and installer were exercised with **simulated systemd dispatch**: first-run dependency preparation, dependency-stamp reuse, detached start request, duplicate-start avoidance, independent unit generation, `Restart=no`, and unlimited processing runtime. No real system service was installed in this sandbox.

The added cache had previously passed cold/warm reload, Map/typed-array preservation, same-process index reuse, matching from the reloaded index, no warm-cache rewrite, and corrupt-cache rebuild. It caches all 2,598 raster reference entries, not matched words.

The persistent-worker integration previously completed a 501-page fixture (one real page plus 500 tiny blank pages), followed by a second book in the same worker. That establishes absence of the web Manager's page ceiling, not realistic 501-full-page resource consumption. The unchanged code was not retested for the overlay rename.

Python/Node syntax checks and ZIP integrity/manifest checks passed. Temporary scraper copies and the superseded full-deployment ZIP were discarded after final packaging; no verification harness, downloaded book, test dataset, dependency directory or production state is included.


## After the merge into this branch — 2026-10-22

The overlay is no longer an addition laid over an untouched scraper: it now lives in this branch, and the scraper modules it reuses were changed by work that landed after the check above. `OCR-MANIFEST.json` content hashes were **recomputed** at commit `ddf9a41`; `scraper_reference` still records the original overlay base (`26779d8`) and `hashes_recomputed_at` records the recomputation. Eight hashes moved:

- `crawler/bin/save_remote.py`, `crawler/bin/text_collect.py` — changed by this branch's fixes (stale path prose; a cross-process append lock).
- `crawler/bin/segment_bridge.mjs`, `deploy/engine_ready.py` — **not edited by that work**; they differ because this branch's copies had already diverged from `main`'s before the overlay was merged.
- `deploy/continuous-ocr/run.py`, `deploy/continuous-ocr/install.py`, `deploy/continuous-ocr/README.md`, `OCR-INSTALL.md` — changed by the fixes below.

The 35 `ocr_runtime/` files were **not** edited: every one is still byte-identical to what `main` shipped, and `copied_runtime[*].source_sha256` (the upstream Linga source) is unchanged.

What changed in the integration itself:

- **One cumulative Parquet.** `commit_dataset` no longer writes `pdfs/parquet/ocr/<source-hash>.parquet` per book; it appends one row to `pdfs/parquet/books.parquet` through `text_collect.write_parquet_row`, the same writer and file the scraper's convertable-book path uses. This removes the `ocr/` sub-folder, removes the `batch-<key>` staging directory, and removes the cross-device `os.replace` entirely — the dot-temp is written inside `pdfs/parquet/` and renamed within that same directory, so `EXDEV` cannot occur if `pdfs/` is a separate mount.
- **Row identity.** Rows are keyed by filename minus `.pdf`. Because the OCR walks hosting directories recursively, a source whose basename is already taken by another source is keyed `<name>-<first 8 hex of sha256(source)>`, persisted in a new `books.book_key` column added by migration, so equal basenames cannot overwrite each other and a retry replaces its own row.
- **Cross-process lock.** `write_parquet_row` now holds an `fcntl.flock` beside the Parquet as well as its threading lock, because `save_remote` and this service are separate processes appending to one file.
- **A commit failure no longer stops the service.** `commit_dataset` was called bare, so a non-`BookError` failure propagated out of `run()`; with `Restart=no` the unit stayed down and the row stayed in a status the queue query re-selects forever, so every book sorted after it was never reached. It is now caught, the book is marked `failed` with `commit: <reason>`, and the loop continues. `Stopped` is a `BaseException`, so SIGTERM still propagates.
- **Restart reconciles by row**, not by per-book file, and re-queues a source whose row is absent from `books.parquet` (with a `[migrate]` journal line) instead of raising.
- **The units now receive `.env`** via `EnvironmentFile=-<repo root>/.env`; previously they set only `PATH` and `PYTHONUNBUFFERED`, while `save_remote` and `mailer` read the repo-root `.env`.

Re-verified by executing the real code, not by inspection — 39 checks, including the reminder rule recorded above, which behaves identically: 19 retained failures produce no mail and no latch; the 20th produces exactly one `failed` mail; five further checks inside the hour produce nothing; backdating `last_reminder` by 3601 s produces a second mail; a failure whose PDF is gone is marked `removed`; clearing every retained failure drops the latch without an extra mail. Also verified: one row appended per book with no `ocr/` sub-folder and no staging leftovers, ZSTD preserved, two same-basename sources both kept with the second disambiguated and no text lost, a retry reusing its key without duplicating, two concurrent processes appending 12 rows each with none lost, `run()` surviving a commit failure with the later book still processed, and the legacy-row self-heal. End to end: three rows via the scraper writer plus two via the real `commit_dataset` land in one five-row four-column `books.parquet`, which `hfinit.py` pushes in one commit and `--download` returns byte-identical.

Still unverified, unchanged from above and not re-tested here: actual hosting SSH, real SMTP/IMAP delivery, real systemd start/stop on the VPS, `npm ci`, representative scanned-book OCR accuracy through Tesseract, forced power-loss recovery and 6,000-book throughput. The Hub transport in the round-trip check was faked, because `huggingface.co` is unreachable from this sandbox.

## Still unverified in production

Actual hosting SSH access, root's existing key/known_hosts/.env, actual SMTP/IMAP delivery, real systemd start/stop on the VPS, representative scanned-book OCR accuracy, forced power-loss recovery and 6,000-book throughput. No processing completion date or accuracy guarantee is claimed.

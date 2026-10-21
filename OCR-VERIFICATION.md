# OCR overlay verification — 2026-10-21

## Copy checks (not a repeat of the existing components' tests)

- ZIP extraction into a temporary original scraper snapshot produced **zero filename collisions** and did not alter any original file.
- **34 unchanged OCR runtime files/assets** matched the committed Linga source byte-for-byte. One runtime file, `shape/match.mjs`, intentionally contains the new whole-index cache extension.
- The raster/OCR helper block in `codepage/vote.mjs` matches site byte-for-byte (SHA256 `3e1d79791d2314d0c46c7e2a4d72424993ea28a46481bc3331d49d8f02acd0cd`). It compares region pixels to labelled SVG references, not embedded-font glyph IDs.
- The ZIP does not contain original scraper files, Engine files, SSH/email configuration, keys, embedded-font outline repair, or its witness corpus.
- Existing scraper SSH, mail, disk-floor, collection writer, Engine bridge and preflight sources remained byte-identical after the integration check. Their reference hashes are in `OCR-MANIFEST.json`.

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

## Still unverified in production

Actual hosting SSH access, root's existing key/known_hosts/.env, actual SMTP/IMAP delivery, real systemd start/stop on the VPS, representative scanned-book OCR accuracy, forced power-loss recovery and 6,000-book throughput. No processing completion date or accuracy guarantee is claimed.

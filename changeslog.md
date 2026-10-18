# Changelog — v1.0.0 (2026-10-17)

## Changes — 2026-10-18
- Route operational `main.py` invocations on root/systemd hosts into unique, background transient units by default; preserve environment overrides in a root-only `/run` file, send output to journald, and document direct-run/help behavior and journal access.
- Require Node.js 22+ for builds, systemd installation, Engine readiness, and wasm rebuilding. Added a no-install/no-switch preflight and skip Engine npm dependency installation when its build packages are already present.
- Added a `javascript-obfuscator` pass to the wasm payload builder for all four bundled modules before AES-GCM encryption; moved it to runtime build dependencies and rebuilt `box.wasm`. Added an archived regression assertion that checks every packed module has obfuscator-generated identifiers. Wasm round trips and the fake-download pipeline pass. This is source obfuscation, not bytecode compilation or cryptographic secrecy.
- Consolidated the duplicate `scraper/` tree into `crawler/`; moved the unique `dedup_pdfs.py` into `crawler/bin/` before removing `scraper/`.
- Repointed runtime/deployment/bootstrap paths to `crawler/bin/` and `crawler/scribdl/`, and corrected root-relative `praser/` model/map paths.
- Corrected the Engine mapbuilder imports from the nonexistent `./_function.js` to `./function.js`. Verified the Engine build in isolated before/after copies: before, the imports were treated as external; after, that warning disappeared and the build completed.
- Added `/ssh/` to `.gitignore` to preserve the repo-root SSH directory layout used on `main`.
- Updated the wasm payload pack key to `function.js` to match the corrected mapbuilder imports. Verified archived wasm tests (loader, payload, box, Python/wasmtime), `deploy/engine_ready.py`, the live segment bridge, and `main.py --help`.
- Refreshed `test.zip`'s scraper pipeline and wasm tests for the current `crawler/` and `wasm/box/` paths; the fake-download end-to-end test passed all 15 assertions against this checkout. Preserved the archive's unrelated historical `R_Site` utilities unchanged.


## Overview & Requirements

Single-entry Burmese PDF crawler, classifier, segmentor, and Parquet archiver (`main.py`), paired with the `Engine/` MonoSyllabism segmentor and the standalone `wasm/box/box.wasm` normalizer blackbox.

### Core Runtime & Build Requirements
- **Python 3.11+ (`uv` / `pyproject.toml`)**: `httpx`, `pikepdf`, `pyarrow`, `pypdf`, `cryptography`, `beautifulsoup4`, `tenacity`, `selenium`, `requests`, `trio`, `trio-websocket`, `websocket-client` (plus `wasmtime` when hosting `wasm/box/box.wasm` via `wasm/box/loader.py`).
- **Node.js 22+ & `npm`**:
  - Run-end segmentation subprocess (`python/bin/segment_bridge.mjs`) loads `Engine/MonoSyllabism/map/loader.js` (`payload.jsc` V8 bytecode, regenerated with all 12 `IMPOSTER` entries) and the precomputed syllable tree in `Engine/.cache-tree/`.
  - `npm run rebuild`: installs `Engine/MonoSyllabism/mapbuilder` deps (`bytenode`, `javascript-obfuscator`), restores `Engine/knowledge/isTreasure/master.json` from `Engine/MonoSyllabism/map/master.json.enc` (5,285 B `mapData`-only container) via `Engine/Bridge/restore-master.js`, and compiles host-bound V8 bytecode (`payload.jsc` + `loader.js`) via `Engine/MonoSyllabism/mapbuilder/obfuscator.js`.
  - `npm run rebuild:wasm` (`sh wasm/rebuild-box.sh`): restores `master.json` if absent, builds `wasm/box/payload.bin` (19,025 B, inlining only compact `mapData` = 2,280 B via `wasm/box/build-payload.mjs`, no fallback paths), and packages `wasm/box/box.wasm` (5,128,492 B = `engine.wasm` + `linga.loader` + `linga.payload`) via `wasm/box/tools/pack-box.mjs`.
- **Wasm Loaders (`wasm/box/loader.mjs`, `wasm/box/loader.py`)**:
  - `wasm/box/loader.mjs` + `wasm/box/aes.mjs`: in-box JS bootstrap module.
  - `wasm/box/loader.py`: Python/wasmtime host loader (`LingaBox`, `normalize`, `mapper_normalize`, `my_normalize`).
- **Systemd Installer & Self-Healing Pre-Start (`deploy/install_service.py`, `deploy/engine_ready.py`, `install.sh`)**:
  - `ExecStartPre` runs `deploy/engine_ready.py --fix` to verify Node, `.cache-tree/`, `Engine/MonoSyllabism/map/{payload.jsc,loader.js}`, `wasm/box/box.wasm`, and `segment_bridge.mjs` boot (automatically running `npm run rebuild` / `npm run rebuild:wasm` when needed) before starting `main.py`.

### Pipeline Architecture & Footprint (v1.0.0)
1. **Crawl & Save (`python/bin/Dcrawler.py`, `Scrawler.py`, `save_local.py`)**: unlocks, repairs, and linearizes PDFs without `/ObjStm`; enforces a 5 GB free-disk floor.
2. **Classify & Extract (`python/bin/pdf_classify.py`, `python/praser/`)**: routes books into `convertable/` (staged in `.convert/` with extracted Unicode/Zawgyi-One text journaled to `scan/collected.jsonl`), `scanned/` (image-only), or `old/` (legacy pre-Unicode fonts).
3. **Run-End Flush (`python/bin/text_collect.py`, `segment_bridge.mjs`)**: normalizes via `mapperNormalize` (12-entry `IMPOSTER` table including NFC twin `ဥ့် -> ဉ့်`), segments via `Engine/.cache-tree/`, writes one ZSTD-19 Parquet batch (`author | book | text | segmented`, `author` intentionally empty), and releases staged PDFs to `convertable/`.
4. **Upload & Report (`python/bin/save_remote.py`, `mailer.py`)**: uploads categories + Parquet batches over SSH, verifies SHA-256 before local deletion, and sends Hostinger SMTP/IMAP status notices.
5. **Tree footprint (~12.6 MB total, down from ~176 MB)**: removed `scraper-drop/` (87 MB), `python/fonts/` (33 MB), `model/mya.traineddata` + `model/secure.js` (25 MB), `map/vector/` + unused map JSONs (18 MB), root `MonoSyllabism/` duplicate (88 KB), `wasm/normalize/` (12 KB), and loose test folders from `HEAD`.

---

## Things to Improve (Technical Overview)

1. **`wasm/box/box.wasm` payload is source text, not QuickJS bytecode (`wasm/box/REVERSE-ENGINEERING.md`)**:
   - `wasm/box/build-payload.mjs` encrypts the 4 `mapbuilder/base/*.js` module sources as JSON text, and `linga.loader` exports `assembleKey()` in the clear (obfuscated ESM). Anyone holding `box.wasm` can call `assembleKey()`, decrypt `linga.payload`, and read the original JS sources.
   - **Improvement**: compile the bundled JS to QuickJS bytecode (`JS_WriteObject` with `JS_WRITE_OBJ_BYTECODE`) before AES-256-GCM encryption, and move key material out of the exported loader surface (or supply the key from the host environment).
2. **Node V8 bytecode coupling (`Engine/MonoSyllabism/map/payload.jsc`)**:
   - `bytenode` artifacts are tied to the exact Node/V8 version; `npm run rebuild` (or `deploy/engine_ready.py --fix`) must run on the target VPS whenever Node changes.
3. **Mirrored crawler directory (`crawler/bin` vs `python/bin`)**:
   - `crawler/` mirrors `python/bin` + `python/scribdl` for layout parity while `main.py` executes from `python/bin`. Consolidating to one directory will prevent divergence.
4. **Cross-file symbol coupling in `obfuscator.js`**:
   - `Engine/MonoSyllabism/mapbuilder/base/{normalize,sourceMapper}.js` import `./_function.js` while the file on disk is `function.js`; flattening relies on alphabetical ordering placing `function.js` before its consumers.

---

## Test Scripts Archive (`tests.zip` — in git history only)

The 5 test scripts (`python/tests/fake_download.py`, `wasm/box/test-loader.mjs`, `wasm/box/test-payload.mjs`, `wasm/box/test-box.mjs`, `wasm/box/test-box-py.py` — 8.8 KB zipped; `fake_download.py` fetches its PDF fixtures from git history automatically) are archived in `tests.zip` at commit **`7615165930edbaf4b840081aa86644dbe84d98b9`** (`7615165`) and removed from `HEAD`.

To restore and unpack the test scripts in the future:

```sh
git show 7615165930edbaf4b840081aa86644dbe84d98b9:tests.zip > tests.zip
unzip -o tests.zip
```

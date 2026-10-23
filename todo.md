# Burmese PDF Scraper & Parser — Architecture and To-Do

This document consolidates the architecture and parser documentation collected across all branches (`arena/c7f80d48-scraper`, `release`, `main`, `staging`, and tag/PR `linga-vector-eval-2026-10-14` / `origin/pr/4`), and tracks project status across four sections:

1. [Architecture Overview](#architecture-overview)
2. [What to Finish](#what-to-finish)
3. [What Is Finished](#what-is-finished)
4. [What Isn't Finished](#what-isnt-finished)
5. [What Should Be Made as Improvement](#what-should-be-made-as-improvement)

---

## Architecture Overview

The repository is an end-to-end Burmese PDF discovery, download, classification, text extraction, normalization, syllable segmentation, and remote archival pipeline.

### 1. Repository Layout

```text
scraper/
├── main.py                              # CLI entry point + transient systemd unit launcher
├── scripts/
│   ├── systemd_launcher.py              # Spawns transient systemd unit per operational main.py run
│   └── check-node.mjs                   # Node.js >= 22 runtime guard
├── deploy/
│   ├── engine_ready.py                  # Preflight checks: Node 22+, Engine payload.jsc, .cache-tree
│   └── install_service.py               # Installs/updates persistent scraper.service on Debian 12
├── crawler/
│   ├── bin/
│   │   ├── Scrawler.py                  # Direct HTML site crawler
│   │   ├── Dcrawler.py                  # PDF download, qpdf normalization, classification, staging
│   │   ├── pdf_classify.py              # 3-way PDF classifier (convertable / scanned / old)
│   │   ├── text_collect.py              # Per-book Burmese parser + Engine segmentor + Parquet writer
│   │   ├── segment_bridge.mjs           # Node 22+ bridge into Engine mapperNormalize + runSegmentor
│   │   ├── save_local.py                # Local staging into convertable/, scanned/, old/
│   │   ├── save_remote.py               # Background uploader: Parquet on VPS + SSH/SFTP book upload
│   │   ├── pdf_support.py               # qpdf password removal, repair, linearization, /ObjStm unpack
│   │   ├── dedup_pdfs.py                # SHA-256 content deduplication across staged/remote PDFs
│   │   ├── mediafire.py / scribd.py     # External host resolvers
│   │   └── serpapi.py                   # SerpApi search discovery
│   └── scribdl/                         # Vendored Scribd downloader
├── praser/
│   ├── pdf_extract.py                   # Pure-Python PDF object/stream parser, TrueType cmap & /ToUnicode reader, TJ/Tj line reconstructor
│   ├── pdf_fonts.py                     # Per-(page_resources, font_name) FontDecoder + DocumentDecoding
│   ├── detector.py                      # Google myanmar-tools Markov Zawgyi/Unicode detector
│   ├── rabbit.py                        # Rule-based Zawgyi-One <-> Myanmar Unicode converter
│   ├── cleanup.py                       # Myanmar canonical mark ordering, imposter fixes, visual cleanup
│   ├── scan.py                          # Structural validator for impossible Burmese mark sequences
│   ├── mm_score.py                      # Structural Burmese base/mark legitimacy scorer for legacy font arbitration
│   └── paths.py                         # Shared resolver for root map/ and model/ assets
├── map/
│   ├── cipher-map.json                  # Post-ToUnicode PUA/presentation-form substitution map
│   ├── cleanup.json                     # Ordered post-conversion regex cleanup rules
│   ├── mm-score.json                    # Dependent mark set and canonical mark priority table
│   ├── scan.json                        # Structural invalid-Burmese regex rules (lone marks, bad medials, etc.)
│   └── zg2uni-rules.json                # Ordered Zawgyi-to-Unicode and Unicode-to-Zawgyi regex tables
├── model/
│   └── zawgyiUnicodeModel.dat           # Binary Markov model for Zawgyi vs Unicode detection
├── Engine/
│   ├── Bridge/                          # Path/logger shims and master.json restore helper
│   ├── .cache-tree/                     # Precomputed syllable trie (syllable.tree.json, rtl-tree.json, ref-syllables.json)
│   └── MonoSyllabism/
│       ├── main/grapheme/               # Syllable graph traversal (Tree.loaded.js, traverser.js, reverse.js)
│       ├── main/phoneme/                # Phonemic lexer (lexer.js)
│       ├── map/                         # V8 bytecode loader (loader.js, payload.jsc) + AES-GCM master.json.enc
│       └── mapbuilder/                  # Obfuscator + Bytenode compiler + AES-256-GCM map encryptor
└── wasm/
    ├── rebuild-box.sh                   # Rebuilds both Engine/MonoSyllabism/map/payload.jsc and wasm/box/box.wasm
    └── box/                             # Portable QuickJS Wasm blackbox (box.wasm) with custom sections linga.loader + linga.payload
```

---

### 2. Parser Architecture (`praser/` + `crawler/bin/pdf_classify.py`)

The Python parser in `praser/` is a zero-external-PDF-library extractor tailored specifically for Burmese PDFs. Prior to reaching `praser/`, `crawler/bin/pdf_support.py` runs `qpdf` (`--decrypt --stream-data=uncompress --object-streams=disable --linearize`) so that every object and content stream is directly inspectable in memory.

#### Step A: Low-Level PDF Object, Font & Stream Parsing (`praser/pdf_extract.py`)
1. **Object Decomposition (`parse_pdf_objects`)**: Parses all `N 0 obj ... endobj` blocks and decompresses `/FlateDecode` streams (trying standard zlib and raw deflate `-15` fallback).
2. **Page Tree Traversal (`find_root_pages`, `collect_pages`)**: Walks `/Catalog -> /Pages -> /Kids` recursively, propagating inherited `/Resources` dictionaries down to leaf `/Page` objects so fonts declared on ancestor `/Pages` nodes remain visible on every page.
3. **Font Discovery (`find_font_streams`, `pick_embedded_font`, `parse_ttf_cmap`)**:
   - Locates `/FontDescriptor` objects and extracts embedded `/FontFile2` (TrueType) or `/FontFile3` streams.
   - Parses TrueType `cmap` subtables (`format 4`, `format 6`, `format 12`) to build `glyph_id -> unicode_codepoint` tables.
   - Because Burmese PDFs frequently embed Latin body fonts (Times/Arial) alongside a smaller Myanmar font, `pick_embedded_font` first selects any embedded font whose `cmap` contains Myanmar code points (`U+1000..U+109F`), falling back to the largest embedded font stream only when none do.
4. **CMap Extraction (`find_tounicode_maps`, `parse_tounicode`)**:
   - Extracts `/ToUnicode` streams per `(page_resource_obj, font_name)` key (`F1`, `F2`, etc. are scoped per `/Resources` dictionary rather than globally, preventing collisions across merged PDFs or Form XObjects).
   - Handles both `beginbfchar` and `beginbfrange` (including array ranges and multi-codepoint UTF-16BE ligature targets).
   - Guards against malformed multi-byte `bfrange` entries where `(end - start)` exceeds `0xFFFF` (`_MAX_BFRANGE_SPAN`) and clamps single-codepoint additions above `0x10FFFF`.
5. **Content Stream Line Reconstruction (`extract_page_lines`)**:
   - Evaluates graphics/text state operators (`cm`, `Tm`, `Td`, `TD`, `T*`, `TL`, `Tf`, `Tj`, `TJ`, `'`, `"`), tracking effective `(x, y)` coordinates in page space and active font (`Tf`) across nested Form XObjects (`/Do`).
   - Splits intra-array `TJ` kerning adjustments narrower than `-180` thousandths of a text space unit into separate horizontal spans so multi-column gaps are not collapsed into a single span.
   - Groups spans into lines by vertical proximity (`y_tolerance = 3.0`), sorts spans left-to-right (`x`), and inserts spaces when horizontal gaps exceed `char_gap = 8.0`.

#### Step B: Two-Stage Per-Font Decoding (`praser/pdf_fonts.py`)
`DocumentDecoding` resolves each text span using the font active at its `(page_resources_obj, font_name)` pair via `FontDecoder.decode_char(cid)`:
1. **Stage 1 — Per-Font `/ToUnicode`**: Applies the font's own `/ToUnicode` map if present, falling back to `fallback_cmap` (embedded TrueType `cmap` or identity probe) for unmapped CIDs.
2. **Stage 2 — Sidecar `cipher-map.json`**: Applies post-`ToUnicode` substitutions for Private Use Area (`U+E000..U+F8FF`) and Arabic Presentation Forms (`U+FB50..U+FDFF`, `U+FE70..U+FEFF`) used by Zawgyi-One and Masterpiece Uni Sans to encode contextual medial/stack variants (`map/cipher-map.json`).

#### Step C: 3-Way PDF Classification & Decoding Ladder (`crawler/bin/pdf_classify.py`)
`classify_pdf(pdf_bytes)` classifies each downloaded PDF in memory into one of three categories (`convertable`, `scanned`, `old`) using two inspection layers:

1. **Image vs. Text Routing Probe**:
   - Measures total image stream bytes vs. file size (`IMAGE_RATIO_HEAVY = 0.85`, `IMAGE_RATIO_MODERATE = 0.50`) and extrapolates total character count from up to 32 sampled pages (`TEXT_COUNT_THRESHOLD = 1000`, `CHARS_PER_PAGE_MIN = 150`).
   - Image-dominant PDFs without a real text layer are immediately classified as **`scanned`**.
2. **Three-Rung Decoding Ladder**:
   - **Rung 1 — Identity Probe (`CmapIdentityMap`)**: Decodes raw character codes directly as Unicode code points on up to 8 sampled pages. If Myanmar code points are present and `ZawgyiDetector` returns probability `< 0.5` (genuine Unicode), extracts the full book via identity mapping.
   - **Rung 2 — Embedded TrueType `cmap`**: Parses the chosen embedded Myanmar TrueType font's `cmap` and checks `ZawgyiDetector` probability:
     - `< 0.05`: Unicode embedded font -> full extraction.
     - `> 0.95`: Zawgyi embedded font -> full extraction + `Rabbit.zg2uni` conversion.
   - **Rung 3 — Per-Font `/ToUnicode` + Sidecar `cipher-map.json`**: Extracts using per-resource `/ToUnicode` tables (with embedded `cmap` fallback) plus `map/cipher-map.json`:
     - If `ZawgyiDetector` probability `> 0.5`, converts via `Rabbit.zg2uni`.
3. **Post-Decode Normalization & Structural Gate**:
   - Every candidate reading passes through:
     - `cleanup_text()` (`praser/cleanup.py`): applies regex rules from `map/cleanup.json` and canonical Burmese combining-mark priority ordering (`order_text()`).
     - **Coverage & Myanmar Ratio Gate**: requires at least 200 non-whitespace characters, `>= 15%` Myanmar script ratio, and at least 30 non-whitespace characters per text-bearing page (`MIN_CHARS_PER_TEXT_PAGE = 30`).
     - **Broken-Text Structural Scan (`praser/scan.py`)**: checks 7 structural rules from `map/scan.json` (`lone_vowel`, `lone_mark`, `bad_medial`, `dup_vowel`, `dup_medial`, `sterile_medial`, `unconverted_zg`). If non-zero errors exceed `MAX_SCAN_ERROR_RATE = 0.02` (2 errors per 100 Myanmar characters), the decoding is rejected as **`old`** (pre-Unicode legacy encoding such as Win Innwa / WinResearcher with masquerading Latin/byte codes or corrupted glyph tables).
   - Only books passing all gates become **`convertable`**.

#### Step D: Myanmar Normalization, Mark Ordering & Syllable Segmentation (`praser/cleanup.py` + `Engine/`)
1. **Python Cleanup (`praser/cleanup.py`)**:
   - Executes ordered regex substitutions from `map/cleanup.json` (zero-width space removal, composite vowel normalization, kinzi reordering, double-mark deduplication, tall-aa normalization).
   - Applies imposter fixes (`ဥ် -> ဉ်` and NFC-composed `ဥ့် -> ဉ့်`).
   - Reorders dependent marks after each consonant base according to the canonical priority table in `map/mm-score.json`.
2. **Engine Normalization & Syllable Segmentation (`crawler/bin/segment_bridge.mjs` -> `Engine/`)**:
   - `text_collect.py` streams convertable book text into `segment_bridge.mjs` on Node.js 22+.
   - `Engine/MonoSyllabism/map/loader.js` loads `payload.jsc` (V8 bytecode compiled via Bytenode from obfuscated `function.js`, `normalize.js`, `sourceMap.js`, `sourceMapper.js`) and decrypts `master.json.enc` in memory using AES-256-GCM.
   - Each document is normalized by `mapperNormalize` (which reorders marks and wraps non-Myanmar blocks in `{...}` so English/digits are preserved) and segmented by `runSegmentor` against the precomputed syllable trie in `Engine/.cache-tree/`.

#### Step E: Portable Wasm Blackbox (`wasm/box/`)
For portable Python/Node environments where V8 version-locked `.jsc` bytecode cannot be shared, `wasm/box/` packages the same four Engine normalization/mapping modules inside a standalone QuickJS WebAssembly binary (`box.wasm`):
- `engine.wasm` is compiled from `wasm/box/c/shim.c` + QuickJS (`2024-01-13`) via WASI SDK.
- `build-payload.mjs` bundles the four `Engine/MonoSyllabism/mapbuilder/base/*.js` modules, runs `javascript-obfuscator`, and encrypts the bundle with AES-256-GCM (`aes.mjs`).
- `tools/pack-box.mjs` strips `engine.wasm` and injects two custom Wasm sections: `linga.loader` (obfuscated JS loader) and `linga.payload` (IV + GCM tag + ciphertext).

#### Step F: Legacy Vector / SVG Contour Recovery Evaluation (Branch/Tag `linga-vector-eval-2026-10-14` / `origin/pr/4`)
On legacy pre-Unicode PDFs without `/ToUnicode` or usable TrueType `cmap` (such as 2001 `bms.pdf` typeset with 8-bit `WinResearcher` Type1C fonts), byte codes represent raw glyph slots rather than Unicode or Zawgyi characters. Branch `linga-vector-eval-2026-10-14` (`6daf988`, `experiments/linga-vector/`) evaluated a 3-route contour-matching architecture:
1. **PDF.js Native Vector Extraction (`canvas: null`)**: Overrides `CanvasGraphics.showText` in PDF.js (`5.4.149`) to capture `font.compiledGlyphs[charCode]` outline commands directly into per-page SVGs, avoiding `dvisvgm`/`Ghostscript` subprocess overhead (~3.57s for 9 pages vs. ~124s).
2. **Coordinate Alignment**: Aligns PDF.js Letter coordinates (`612x792`) to reference A4 (`595.28x841.89`) via exact uniform scaling `x' = 0.9726732026143791 * x`, `y' = 0.9726732026143791 * y + 35.76641176470588`.
3. **3-Tier Curve Witness Identification**:
   - *Tier 1 — Exact Curve Signature (`curve.mjs`)*: Normalizes each glyph contour (`M/L/C/Z`) into a scale-invariant signature hashed against `map/curve-witness.json` (built from 339 legacy Myanmar font faces; 34,512 unambiguous signatures kept, 550 ambiguous cross-letter collisions excluded).
   - *Tier 2 — Per-Font Vote Lock*: Accumulates `(fontTag, slot) -> Unicode` votes across the document so a slot resolved on earlier pages decodes consistently on later pages.
   - *Tier 3 — Structural Legitimacy Scoring (`mm_score.py`)*: Ranks competing candidate code-page decodings by orphan-mark rate, duplicate-mark rate, and mark-order violation rate.

---

### 3. Linear Per-Book Download, Classification, Parquet & Upload Pipeline

1. **Launch (`main.py` + `scripts/systemd_launcher.py`)**:
   - Every operational `main.py` invocation stops any running `scraper-main-*.service` unit and launches a fresh background transient systemd unit (`scraper-main-<id>.service`), returning the unit name and `journalctl` commands immediately.
2. **Download & Immediate Staging (`crawler/bin/Dcrawler.py` + `crawler/bin/save_local.py`)**:
   - `Dcrawler.py` downloads each PDF, normalizes it in memory with `qpdf`, classifies it via `classify_pdf()`, and saves it directly into its category directory under `--save`:
     - `<save>/convertable/<book>.pdf`
     - `<save>/scanned/<book>.pdf`
     - `<save>/old/<book>.pdf`
   - Downloading continues linearly without waiting for an end-of-run batch flush.
3. **Background Per-Book Processing & Upload (`crawler/bin/save_remote.py` + `crawler/bin/text_collect.py`)**:
   - `RemoteUploader` polls `convertable/`, `scanned/`, and `old/` continuously (`POLL_INTERVAL = 2.0s`).
   - **For `convertable/` books**:
     1. Calls `process_convertable_pdf()`, which runs `classify_pdf()` + `segment_bridge.mjs` (`Engine`) and appends **1 row per book** (`book_id`, `author`, `text`, `syllables`) to `<parquet_dir>/part-NNNNN.parquet` on the VPS (rotating at 300 MB).
     2. Uploads the PDF over persistent SSH ControlMaster SFTP to `<remote_path>/convertable/` and deletes the local staged PDF.
   - **For `scanned/` and `old/` books**:
     1. Uploads the PDF directly over SSH ControlMaster SFTP to `<remote_path>/scanned/` or `<remote_path>/old/` and deletes the local staged PDF.
   - **Parquet locality**: Parquet files (`books_parquet/`) remain on the VPS; only PDF books are sent to remote shared storage.

---

## What to Finish

1. **Sync Pending Imposter Rules into Sealed Engine & Wasm Artifacts**:
   - Add `"ဥ်": "ဉ်"` and NFC `"ဥ့်": "ဉ့်"` into `mapData.IMPOSTER` inside `Engine/MonoSyllabism/map/master.json` (restored via `node Engine/Bridge/restore-master.js`), then run `bash wasm/rebuild-box.sh` so `Engine/MonoSyllabism/map/master.json.enc`, `Engine/MonoSyllabism/map/payload.jsc`, and `wasm/box/box.wasm` carry the same imposter rules already enforced in `praser/cleanup.py`.
2. **Compile `wasm/box` Payload Modules to QuickJS Bytecode Before Encryption**:
   - Update `wasm/box/c/shim.c` (`box_eval` -> `JS_ReadObject` + `JS_EvalFunction` with fallback to `JS_Eval`) and `wasm/box/build-payload.mjs` (compile obfuscated bundle via `qjs -b` / `JS_WriteObject` before AES-256-GCM encryption) so `linga.payload` holds QuickJS bytecode rather than plaintext JS source inside the AES-GCM envelope.
3. **Complete Legacy Vector/SVG Extraction Pipeline (`linga-vector`) and Wire Optional Recovery for `old/` PDFs**:
   - Port the two verified parser patches (`svg.mjs` nested-path loop fix and `ttf-cmap.mjs` format-4 `idRangeOffset` pointer arithmetic fix) and wire the PDF.js SVG outline extractor + 339-face `curve-witness.json` + `mm_score` arbitration into a standalone or post-processing recovery pass for `old/` PDFs that lack `/ToUnicode`.
4. **Populate `author` Metadata Column in Parquet Output**:
   - Currently `crawler/bin/text_collect.py` writes `author = ""` for every Parquet row. Extract author metadata from PDF `/Info` dictionaries, crawler page metadata, or title-page heuristics when available.

---

## What Is Finished

1. **Pure-Python Burmese PDF Extractor (`praser/`)**:
   - Object/stream parser with Flate decompression (`pdf_extract.py`).
   - Page tree walker with inherited `/Resources` propagation (`collect_pages`).
   - Embedded TrueType `cmap` parser (`format 4`, `format 6`, `format 12`) and Myanmar-aware embedded font selector (`pick_embedded_font`).
   - Per-resource `(res_obj, font_name)` `/ToUnicode` parser with `bfrange` span guards (`_MAX_BFRANGE_SPAN = 0xFFFF`) and Unicode scalar clamping (`<= 0x10FFFF`) to prevent `OverflowError: Python int too large to convert to C ssize_t` on malformed PDFs.
   - Two-stage `FontDecoder` combining per-font `/ToUnicode`, fallback TrueType `cmap`, and `map/cipher-map.json` PUA/presentation-form substitutions (`pdf_fonts.py`).
   - Pure-Python Google `myanmar-tools` binary Markov model detector (`detector.py` + `model/zawgyiUnicodeModel.dat`).
   - Rule-based Zawgyi-One <-> Unicode converter (`rabbit.py` + `map/zg2uni-rules.json`).
   - Canonical Burmese combining-mark priority sorter and visual/imposter cleanup (`cleanup.py` + `map/cleanup.json` + `map/mm-score.json`).
   - Structural broken-Burmese scanner (`scan.py` + `map/scan.json`) and legacy decoding legitimacy scorer (`mm_score.py`).
2. **3-Way In-Memory PDF Classifier (`crawler/bin/pdf_classify.py`)**:
   - Whole-file image-byte ratio + 32-page sampled text extrapolation to separate `scanned` PDFs from text-bearing PDFs.
   - 3-rung decoding ladder (Identity probe -> Embedded TrueType `cmap` -> Per-font `/ToUnicode` + sidecar `cipher-map.json`) with structural coverage and `scan_text` validation separating `convertable` from `old`.
3. **Engine Obfuscation, Bytecode Compilation & Node 22+ Segmentor Bridge**:
   - `Engine/MonoSyllabism/mapbuilder/obfuscator.js` obfuscates all four payload modules (`function.js`, `normalize.js`, `sourceMap.js`, `sourceMapper.js`) before compiling `payload.jsc` with Bytenode and encrypting `master.json.enc` with AES-256-GCM.
   - `wasm/box/build-payload.mjs` and `wasm/box/tools/obfuscate-loader.mjs` obfuscate both the payload bundle and loader before packing `wasm/box/box.wasm`.
   - `crawler/bin/segment_bridge.mjs`, `deploy/engine_ready.py`, and `crawler/bin/text_collect.py` enforce and auto-discover Node.js 22+ across standard system, fnm, nvm, Volta, and NodeSource paths even under restricted systemd `PATH`.
4. **Linear Per-Book Download, Classification, Parquet & Remote Upload Workflow**:
   - `Dcrawler.py` classifies each downloaded PDF immediately and saves it to `<save>/{convertable,scanned,old}/`.
   - `RemoteUploader` (`save_remote.py`) watches all three category folders: for `convertable/` books it parses the PDF, segments syllables via `Engine`, appends 1 row (`book_id`, `author`, `text`, `syllables`) to local VPS Parquet (`books_parquet/part-NNNNN.parquet`), uploads the PDF to `<remote_path>/convertable/`, and removes the staged copy; for `scanned/` and `old/` books it uploads directly to remote storage.
   - SSH ControlMaster connection multiplexing (`-o ControlMaster=auto -o ControlPersist=300`) and single-pass SHA-256 hashing eliminate per-file SSH handshake overhead and duplicate disk reads.
5. **Transient Systemd Background Execution (`main.py` + `scripts/systemd_launcher.py`)**:
   - Operational `main.py` runs automatically stop any prior `scraper-main-*.service` unit, launch a new transient systemd service unit in the background, and print the unit name and `journalctl` follow commands.

---

## What Isn't Finished

1. **End-to-End Legacy Font Vector Recovery (`linga-vector-eval-2026-10-14` / `origin/pr/4`)**:
   - On branch/tag `linga-vector-eval-2026-10-14`, the vector/SVG evaluation established PDF.js native outline extraction, Letter-to-A4 scaling, and the rebuilt 339-face `curve-witness.json`, but its handoff status is **NOT GREEN**:
     - The two patches (`svg.mjs` nested contour loop fix and `ttf-cmap.mjs` format-4 `2 * segCount` offset fix) were shipped as patch files in `experiments/linga-vector/portable/patches/` rather than merged into the live parser.
     - Full 9-page extraction on `bms.pdf` with Unicode-labelled SVG candidates wired into cluster/line context and `scan.py` validation was not completed.
     - `crawler/bin/pdf_classify.py` intentionally routes legacy non-Unicode/non-Zawgyi PDFs to `old/` without running vector outline recovery.
2. **QuickJS Bytecode Compilation Inside `wasm/box/box.wasm`**:
   - While `Engine/MonoSyllabism/map/payload.jsc` compiles to native V8 bytecode, `wasm/box/box.wasm` currently encrypts obfuscated JavaScript source inside `linga.payload` because `shim.c` calls `JS_Eval` rather than `JS_ReadObject`.
3. **In-Engine `master.json.enc` Synchronization for `ဥ် -> ဉ်`**:
   - The `ဥ် -> ဉ်` and `ဥ့် -> ဉ့်` imposter replacements are active in `praser/cleanup.py` (so `pdf_classify.py` and `text_collect.py` output already have the fix), but have not yet been baked into `Engine/MonoSyllabism/map/master.json.enc` and `wasm/box/box.wasm`.
4. **Author Metadata Extraction**:
   - The Parquet schema includes an `author` column (`pa.field("author", pa.string())`), which is currently populated with an empty string `""`.

---

## What Should Be Made as Improvement

1. **Long-Lived `segment_bridge.mjs` Worker Process**:
   - Currently, linear per-book processing in `text_collect.py` (`process_convertable_pdf`) spawns a fresh `node segment_bridge.mjs` process per convertable book. Keeping a persistent Node.js 22 worker process alive over stdin/stdout IPC (or Unix domain socket) in `RemoteUploader` will avoid repeated V8 startup, `master.json.enc` decryption, and `Engine/.cache-tree/syllable.tree.json` deserialization on every book.
2. **Eliminate Duplicate `classify_pdf()` Execution on Convertable Books**:
   - `Dcrawler.py` runs `classify_pdf(data)` during download to decide whether a PDF belongs in `convertable/`, `scanned/`, or `old/`. When `RemoteUploader` picks up a file from `convertable/`, `process_convertable_pdf()` reads the PDF and runs `classify_pdf()` a second time to get `res.text`. Persisting the extracted `.txt` sidecar alongside `<save>/convertable/<book>.pdf` (and deleting it after Parquet write) would cut CPU work per convertable book in half.
3. **Parquet Compaction / Row-Group Buffering**:
   - Writing one row per book via `ParquetWriter` or reading/rewriting existing `.parquet` shards on each convertable book causes write amplification as shard sizes approach 300 MB. Holding an open `pq.ParquetWriter` handle per active shard (flushing row groups periodically and closing cleanly on uploader shutdown) will reduce disk I/O on the VPS.
4. **TrueType `cmap` Format 4 Pointer Arithmetic Guard in `praser/pdf_extract.py`**:
   - Ensure `parse_ttf_cmap` in `praser/pdf_extract.py` includes the exact `idRangeOffset` byte-offset calculation (`id_range_offset_pos + 2 * i + ro + 2 * (code - start)`) verified in `linga-vector` so embedded fonts using non-zero `idRangeOffset` arrays decode every glyph accurately.
5. **Automated Offline Legacy (`old/`) Reprocessing CLI**:
   - Once the `linga-vector` curve-witness + `mm_score` pipeline is finalized, provide a dedicated batch CLI tool that scans archived `old/` PDFs, recovers high-confidence Burmese Unicode text, and appends verified books into the VPS Parquet dataset.

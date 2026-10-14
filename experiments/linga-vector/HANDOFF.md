# Continuation contract — read before changing the acquisition pipeline

## Recovery notice (read first)

A sandbox transition removed the external scratch folders described below.
The core experiment was reconstructed from pinned sources and its results were
reproduced: all 339 font identities, nine page contour counts, corrected witness
inventory, raw page votes and the recorded closest cluster scores. Fresh recovery
suite: 17/17 passed. This does NOT recover every older isolated-glyph diagnostic
verbatim and does NOT make extraction green.

The portable source now lives in `experiments/linga-vector/portable/`; the ZIP
installs under `Parsed/parser/tools/vector-evaluation/` in the user's Linga tree.
Read that package's README for runnable relative paths and recovery inventory.
Historical absolute paths and prior test counts below are evidence/history,
not assertions that those scratch files currently exist. The old session's branch
name below records that session, not a direction to switch a future session's
branch.

## Why this file exists

The user must not have to spend another session explaining the design. Their
specific concern is shipping wrong extraction logic over 10,000 documents, or
turning separately developed components into repeated download/upload stages.
This file records the existing task and evidence. It is NOT approval to ship,
merge, integrate acquisition, or implement a new transfer architecture.

## Shipping gate

**NOT GREEN. Do not integrate Linga processing into scraper acquisition yet.**

A successful SVG export, passing regression tests, a nearest text hypothesis,
raw witness-byte agreement, or zero dictionary unknowns is NOT extraction
validation. Keep the independent PDF, raw inputs, unresolved results, and
provenance. No deletion/replacement of source documents is authorized. Existing
manifest behavior must remain non-destructive; deletion requires explicit user
instruction after verification.

The intended acquisition integration must avoid reacquiring documents between
processing components. Multiple internal functions do not imply multiple
network transfer stages. Exactly where local/remote processing and uploads occur
still needs implementation/verification; do not describe it as already built.
Preserve the existing no-automatic-unresolved-download-retry rule. Do not claim
all 10,000 PDFs will be recognized merely because one independent PDF succeeds.

## Active experiment — fixed acceptance constraints

- Processing repository: `/home/user/linga`, `site` revision
  `712728395276b4fb57f25b8c3b05757f990a55ca`.
- Coding-session repository/branch: `/home/user/scraper`,
  `arena/891e3856-scraper`. Never switch the session branch.
- Independent input: 2001 `bms.pdf`, 9 pages, old WinResearcher Type1C fonts,
  no ToUnicode and no page images. Source currently `/tmp/linga-unseen/bms.pdf`.
- Digital documents: vector/SVG only. Raster is for scanned-input OCR only.
- Actual PDF -> full-page SVG -> shipped SVG contour/line helpers, not merely
  importing those helpers into an isolated-glyph experiment.
- A4 standard and ONE uniform scale; never independently stretch width/height.
- Preserve full-page/cluster placement, free-standing marks, and raw holes.
- Contour matching; morphology/impossible rules in scanner, not invented gates.
- Font swapping and the shipped 339-face/~17 MB dependency, not Pyidaungsu alone.
- Reuse existing aliases, overrides, dictionary and arbitration mechanisms.
  Do not invent replacement rules before tracing existing behavior.
- Remove document keyboard-layout candidates from this glyph-only experiment.
  Font reference encodings/provenance still matter; witness bytes are not
  automatically Unicode. Do not silently choose a document layout to name votes.
- Preserve intentional asat-before-dot representation. Dictionary warnings are
  not synonymous with extraction errors.

## Executed and established

1. PDF.js native outlines -> actual positioned full-page SVG works on all nine
   pages. `canvas:null` is essential: providing a canvas made PDF.js bypass the
   proxy context. No pixel calls or auxiliary surfaces were recorded. Matrices
   are baked into paths, with raw source bytes/font IDs as metadata only.
2. Both shipped specimen sheets are A4, 595.276 x 841.89 pt. Input is Letter,
   612 x 792 pt. Executed mapping for every page:
   `x' = 0.9726732026143791*x`,
   `y' = 0.9726732026143791*y + 35.76641176470588`.
   All contours remain and their bounds satisfy the same mapping.
3. Shipped `svg.mjs` nesting loop uses `j = i += 1`; isolated correction is
   `j = i, i += 1`. Closed-ring/free-mark tests establish the discrepancy.
   Historical trace: 893 raw contours; 617 ink with correction, NOT the historical
   comment's 587. Do not claim a golden match.
4. Shipped format-4 cmap reader omitted `2*segCount` from the nonzero-range
   glyph-array address. `cmap-format4.patch` supplies the minimal correction.
   WRESB code 114: old GID 61, corrected/HarfBuzz GID 85. 24 selected real-font
   lookups agree with HarfBuzz; independent synthetic tests reproduce the bug.
5. Rebuilt evaluation-only curve witness copy from all 339 verified font files.
   Explicit Windows-symbol F000 byte aliases recover symbol-font coverage.
   34,512 kept signatures; 550 mismatched/ambiguous lookups logged and excluded.
   No shipped witness/alias/threshold/override files were overwritten.
6. Actual A4 SVG paths for the problem word vote to raw bytes
   117,114,199,109. Existing provisional exact-witness override handles dissent
   on 114 and 199. This is NOT a Unicode extraction verdict.
7. Closest exploratory whole-word hypothesis: `ကမ္ဘာ`. It was not accepted.
   The exploratory whole-word metric/bar and enumerated hypotheses are not the
   complete shipped extraction path. Do not use their rejection counts to
   report original matcher accuracy.
8. Existing sealed dictionary, `repairText` and `scanText` were executed on 28
   targeted hypotheses. Correct/corrupted title variants can both have zero
   unknown segments and unchanged repairs. This does not establish correctness
   or disprove a complete candidate-arbitration path. Do not speculate that the
   dictionary is Zawgyi: `NewMap` is internal category encoding.
9. 15 evaluation/input/nesting/address tests and 6 portable patch tests passed.
   Tests establish input/lookup invariants, not semantic extraction accuracy.

## Still unfinished — the real next work

- Consistently use the verified reader/nesting corrections and corrected curve
  references throughout the evaluation path, keeping production changes separate.
- Trace/connect Unicode-labelled SVG candidates and font-swapped evidence to the
  existing alias/override/arbitration path. Do not name raw-byte votes using a
  silently selected document keyboard table.
- Carry real line/cluster context into candidate assembly and scanner. Whole-page
  line grouping is not certified; source contours/counters must stay available
  even when ink-only contours are used to group lines.
- Run the resulting complete extractor on all nine pages, provide the ACTUAL
  Unicode output, inspect known failures and scanner diagnostics, and distinguish
  unresolved spans from accepted text. No blanket unanimity/.03-em baseline gates.
- Only then decide whether independent validation is green enough to integrate
  acquisition. One qualifying PDF cannot certify every legacy-font family.

This is unfinished engineering, not a request for the user to restate the design
or supply another library. Core vector availability has already been proved.

## Workspace evidence

All detailed scripts/output are under
`/home/user/linga-evaluation/vector-only/` (outside Git to avoid large generated
artifacts):

- `page-svg.mjs`, `page-svg.test.mjs`, `svg-fixed.mjs`
- `a4-frame.mjs`, `page-input/bms-page{1..9}-a4.svg`, `*-a4-geometry.json`
- `ttf-cmap-fixed.mjs`, `address-frame.test.mjs`, `address-frame-tests.log`
- `rebuild-corrected-witnesses.mjs`, `corrected-witnesses.json`,
  `corrected-witness-audit.json`
- `a4-page-witness-probe.mjs`, `a4-page-witness-results.json`
- `a4-cluster-probe.mjs`, `a4-cluster-results.json`, `a4-cluster.log`
- `title-dictionary-audit.json`

Tracked small artifacts in this folder: `README.md`, `cmap-format4.patch`,
`cmap-format4.test.mjs`, this handoff. The patch passed `git apply --check` against
Linga site but is not applied to production. Production parser/vector assets and
scraper pipeline remain untouched.

Scratch dependency/font paths are under `/tmp`; do not assume they survive a
new sandbox. The historic font source is Linga revision
`f9cddc495fb107ebd2c95059390dc9083bab7fa9`, recovered through GitHub tarball;
339 font files were verified against the old witness records. Native npm deps
currently live at `/tmp/linga-eval-deps/node_modules`. Do not install pnpm or
leave a root node_modules symlink. Recheck files before rerunning generation.

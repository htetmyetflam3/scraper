# START HERE — Linga vector evaluation recovery bundle

**NOT GREEN. This archive does not enable extraction in the downloader.**

## Unpack the included payload first (one download, no archive parts)

The release ZIP uses a solid-compressed `PAYLOAD.tar.xz` to avoid storing similar
font data repeatedly. After unzipping at your Linga root, run:

```
cd Parsed/parser/tools/vector-evaluation
node restore.mjs
```

This requires Node and tar with XZ support. It makes no network requests, verifies
the included payload, restores files only in this evaluation directory, and
refuses to overwrite an existing `work/`. Once restored, skip this step on later
runs. The unchanged OCR-only `model/mya.traineddata` baseline asset is excluded;
it was never an input or modified output of this digital-vector experiment.

## Install without overwriting production

Extract the ZIP at your Linga repository root. It adds only:

```
Parsed/parser/tools/vector-evaluation/
  work/                 portable evaluation code + regenerated results
  patches/              two separate verified one-line fixes; NOT auto-applied
  fixtures/             independent PDF + historical tested SVG
  snapshot/linga/        pinned baseline parser, reference assets, fonts and log helper
  assets/legacy-fonts/   recovered historical font files
  notes/                preserved design/evidence history
  package.json          pinned npm dependencies (no native binaries bundled)
  package-lock.json
  MANIFEST.json         file sizes + SHA-256 for the payload
```

No top-level production parser file, source PDF, crawler or configuration is
replaced. Review patches before applying them to the live Linga tree; do not copy
`snapshot/` over your live files. Current production integration remains prohibited
until complete extraction is independently validated.

## Verify and run (Node >=22.22, npm; never pnpm)

```
cd Parsed/parser/tools/vector-evaluation
node verify.mjs
npm ci --no-audit --no-fund
npm test
```

Regenerate in this order; these commands overwrite only evaluation results in
this directory, never production parser data:

```
npm run pages
npm run witnesses
npm run clusters
npm run probe
npm test
```

`npm run pages` uses native SVG-only surfaces and `canvas:null`, forbids pixel
operations/auxiliary surfaces, bakes transforms, then applies A4 with ONE uniform
scale plus translation. It is not a generic arbitrary-SVG converter: unsupported
stroke/current-path operations fail rather than disappear silently.

`npm run witnesses` verifies each original packed reference record before using
the recovered font. It produces both the curve-only and corrected-cmap shadows.
All 339 faces reproduced; 34,512 corrected signatures are kept; 550 ambiguous
lookups are excluded and logged. Windows-symbol aliases are explicit font cmap
metadata, not a silently selected document keyboard layout.

`npm run clusters` is an explicitly enumerated targeted regression hypothesis
probe, NOT complete blind extraction and NOT a calibrated production classifier.
`npm run probe` queries actual A4 SVG paths; raw byte winners are NOT Unicode
text authority. Do not claim `ကမ္ဘာ` as accepted text from a nearest hypothesis.

The recorded fresh recovery run passed 17 tests. Those prove input/lookup
invariants and the targeted evidence path, not semantic recognition accuracy.
`node verify.mjs` checks the original published payload; regeneration changes
files and therefore can legitimately invalidate that original manifest.

## What was recovered and what was not

A sandbox transition removed the old external `/home/user/linga-evaluation` and
`/tmp` scratch folders before packaging. The repository's handoff, minimal cmap
patch and six regression tests survived. The release is therefore an honestly
labelled **recovery**, not a byte-for-byte backup of every earlier scratch file.

Recovered from pinned upstream sources and regenerated:

- Linga site baseline `712728395276b4fb57f25b8c3b05757f990a55ca`.
- Historical fonts `f9cddc495fb107ebd2c95059390dc9083bab7fa9`.
- Shipped two SVG sheets, aliases, overrides, thresholds, original 339-face witness
  JSON and sealed dictionary, preserved inside the baseline snapshot.
- Independent 2001 PDF, native + A4 SVGs for all nine pages, historical trace SVG.
- Full 339-face font-identity audit, curve-only and corrected witness JSON/audits.
- Reconstructed portable A4/full-page/whole-cluster/witness probes and fresh tests,
  with new run logs and targeted dictionary/scanner results.
- Original surviving handoff/notes/patch/test sources in `notes/`.

Not recovered verbatim: the earlier isolated-glyph experiment/policy scripts,
its strict TXT/match JSON files, intermediate raw/control-polygon diagnostic
files, earlier production-extraction reports, and earlier test logs. Their known
limitations and results are recorded as history in the notes, not substituted
with fabricated files or claimed as fresh tests. The reconstructed exporter omits
empty path records: native contour counts match all nine recorded pages, while
nonempty path counts differ from the older exporter. Full page fidelity and
recognition are not certified by that agreement.

Read `notes/HANDOFF.md` for the design, completed evidence and precise unwired
work. Its absolute scratch paths and historical counts describe the prior
session; use this portable directory's files and fresh logs now. Do not ask the
user to re-explain the design or mistake historical paths for present files.

The next work remains actual Unicode-candidate wiring through existing
aliases/overrides/arbitration and scanner, followed by real nine-page output.
Do not add blanket unanimity/baseline gates or silently restore document keyboard
layout selection. No complete validated layout-free extraction is included.

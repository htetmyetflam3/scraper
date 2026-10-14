# Linga vector evaluation: A4 and format-4 address regression

**Current packaging/recovery:** see `portable/README.md` and `HANDOFF.md`'s
recovery notice. Earlier external scratch folders no longer exist; core results
were reconstructed and reproduced, with 17 fresh passing tests. The release ZIP
uses `Parsed/parser/tools/vector-evaluation/`, with portable relative paths and
separate unapplied patches. Historical paths/test counts below describe the
original run, not files guaranteed present now. Missing older diagnostics are
listed explicitly in the portable README.

Status: **NOT GREEN**. No scraper/download integration and no Linga production
source or shipped vector assets have been changed. This folder preserves a
reviewable, minimal reader patch and Node-only regression tests; large PDFs,
fonts, witness JSON and generated page SVGs remain outside Git.

## Verified A4 input

Independent `bms.pdf` has native Letter pages, 612 × 792 pt. Both shipped
Pyidaungsu specimen sheets declare 595.276 × 841.89 (A4 at 72 dpi).

All nine actual full-page SVG inputs were transformed with exactly:

```
x' = 0.9726732026143791 * x
y' = 0.9726732026143791 * y + 35.76641176470588
```

The extra height is letterboxing, not vertical stretch. Matrices are baked into
path coordinates. No image/text elements, unbaked transforms, raster operations
or discarded contours; every contour's bounds obey the same mapping. The actual
`svg.mjs` loader/ink/line helpers run on these inputs. This proves input invariants,
not segmentation or recognition accuracy.

Artifacts in the shared workspace:

- `/home/user/linga-evaluation/vector-only/a4-frame.mjs`
- `.../page-input/bms-page{1..9}-a4.svg` and `-a4-geometry.json`
- `.../address-frame.test.mjs` and `address-frame-tests.log`: 15/15 tests,
  including the previous five page/nesting tests.

## Confirmed reader discrepancy

In `Parsed/parser/core/ttf-cmap.mjs`, format-4's `idx` is relative to the glyph-ID
array, but `ro` points to the earlier idRangeOffset array. The address omitted
`2 * segCount` bytes. This corrupts nonzero-range GID lookups; no new GID adapter
is required.

Example: `WRESB.TTF`, code 114 (`0x72`): shipped reader **61**, corrected reader
**85**, HarfBuzz **85**. Twenty-four selected lookups in WRES, WRESB and WININNWA
agree with HarfBuzz after the fix. Synthetic tests independently establish the
required address and preserve zero-GID, delta, bounds, format-0/6/12 behavior.

```
LINGA_ROOT=/home/user/linga node --test experiments/linga-vector/cmap-format4.test.mjs
```

Six tests pass. They apply the patch in memory, leaving the checkout unchanged.
`cmap-format4.patch` also passes `git apply --check` against Linga site 7127283.
It has **not** been applied to production.

## Corrected dependency shadow and actual page votes

The shipped 339-face / ~17 MB dependency was consumed, and all 339 verified font
files were rebuilt into an evaluation-only, sampled-curve witness copy. Windows
symbol fonts use their explicitly declared F000 byte aliases; this is font cmap
encoding metadata, not a chosen PDF keyboard layout. Every retained lookup agrees
with HarfBuzz. 34,512 signatures are retained; 550 ambiguous/mismatched lookups
are logged and excluded, not silently interpreted. No shipped aliases, overrides,
thresholds or witness JSON are overwritten.

Actual positioned **A4 page SVG** queries for the four-path problem word now
produce raw witness bytes 117, 114, 199, 109. The existing provisional exact-witness
override handles the dissent for 114 and 199; no new unanimity rule was added.
These are raw-byte diagnostics, **not Unicode text authority**.

Artifacts: `.../corrected-witnesses.json`, `corrected-witness-audit.json`,
`a4-page-witness-probe.mjs`, `a4-page-witness-results.json`.

## Recognition remains unfinished

Whole-cluster font-swap probes preserve the page positions and title's floating
asat/dots. These probes use an explicitly enumerated regression hypothesis set,
not a blind, complete extractor. Closest world-word hypothesis is `ကမ္ဘာ`; it is
not an accepted extraction. Word-scale contour bars in this probe are exploratory
and must not be reported as the original matcher's accuracy.

The shipped sealed dictionary, `repairText`, and `scanText` were executed on all
28 targeted hypotheses. Both intended and corrupted title variants can have
zero unknown segments and unchanged repair in this checkout. That does not prove
lexical correctness, nor disprove a different complete candidate-arbitration path.
No complete layout-free nine-page Unicode output has been validated. Next work:
connect semantic glyph candidates and actual cluster context to the existing
arbitration/scanner, rather than translating raw witness byte winners through a
silently selected document keyboard table.

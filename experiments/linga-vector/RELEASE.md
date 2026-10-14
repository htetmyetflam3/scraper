# Linga vector evaluation recovery — 2026-10-14

**Experimental handoff; NOT GREEN for extraction or acquisition integration.**

Release/tag: `linga-vector-eval-2026-10-14`

Download:
https://github.com/htetmyetflam3/scraper/releases/download/linga-vector-eval-2026-10-14/linga-vector-evaluation-recovery.zip

Checksums:
https://github.com/htetmyetflam3/scraper/releases/download/linga-vector-eval-2026-10-14/SHA256SUMS

- ZIP: 85,940,948 bytes (~82 MiB), 828 entries including the manifest.
- ZIP SHA-256: `3be6d9809d0746f34f8902fc65574e548a2c7374925759dd0e1b0655376a5001`
- Extract at the Linga root. Only `Parsed/parser/tools/vector-evaluation/` is added.
- No live parser files are replaced; verified fixes are separate unapplied patches.
- Includes pinned baseline parser/assets/dictionary, recovered fonts, independent
  PDF and historical trace, rebuilt native/A4 pages, both witness shadows,
  relative-path evaluation scripts, fresh logs, handoff and recovery inventory.
- All 339 font identities reproduced; all nine native contour counts reproduced.
- 17 fresh tests passed, including after extracting into a different directory.
- Every one of 827 payload files verified by SHA-256 before and after extraction.
- `node verify.mjs`, then `npm ci --no-audit --no-fund`, then `npm test` in the
  extracted directory. npm dependencies are pinned; platform binaries are not bundled.

## Honest recovery boundary

External scratch folders disappeared across a sandbox transition. The surviving
repository notes, patch and tests were preserved. Core inputs/assets were
recovered from their pinned GitHub revisions, evaluation scripts reconstructed,
and the main recorded results reproduced. Some older isolated-glyph scripts,
strict-output JSON/TXT, intermediate probes and historical logs were not recovered
verbatim. The ZIP explicitly inventories that gap; it is not labelled a complete
byte-for-byte backup of all prior scratch work.

Complete layout-free nine-page Unicode extraction is still unfinished. Raw-byte
votes, closest hypotheses, unit tests and zero dictionary unknowns are not a green
extraction verdict. Merging scraper PR #4 preserves this handoff; it does not wire
or enable experimental Linga processing in the crawler/download path.

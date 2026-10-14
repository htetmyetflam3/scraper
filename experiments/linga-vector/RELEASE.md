# Linga vector evaluation recovery — 2026-10-14

**Experimental handoff; NOT GREEN for extraction or acquisition integration.**

Release/tag: `linga-vector-eval-2026-10-14`

[Download the single ZIP](https://github.com/htetmyetflam3/scraper/raw/linga-vector-eval-2026-10-14/release-assets/linga-vector-evaluation-recovery.zip)

[SHA256SUMS](https://github.com/htetmyetflam3/scraper/raw/linga-vector-eval-2026-10-14/release-assets/SHA256SUMS)

- ZIP: **36,124,451 bytes (~34.5 MiB)**. No archive parts or extra font downloads.
- ZIP SHA-256: `823c2d562264a3c4b68e183680cd88eee7ce55c2a80ae5c6642aba1ad8a33a68`
- Extract at the Linga root; it creates `Parsed/parser/tools/vector-evaluation/`.
- The ZIP uses an included solid-compressed payload to keep similar font data
  small. Run **`node restore.mjs`** in that directory to restore and verify it
  locally. Requires Node >=22.22 and tar with XZ support. No network is used by
  restoration, and an existing evaluation `work/` will not be overwritten.
- Then run `npm ci --no-audit --no-fund` and `npm test`.
- No live parser file is replaced; the two verified fixes are separate unapplied
  patches. Snapshot data must not be copied over production files.
- Includes pinned baseline code, vector assets, sealed dictionary, all recovered
  fonts, independent PDF/historical trace, rebuilt native/A4 pages, witness shadows,
  relative-path scripts, fresh logs, handoff and an honest recovery inventory.
- The unchanged 25 MB **OCR-only traineddata** asset is excluded; it was never
  used or modified in this digital-vector experiment. No experimental asset was
  removed to achieve the smaller package.
- All 339 font identities and nine recorded native contour counts reproduced.
- **17 fresh tests passed after restoring into a different directory.**
- **827 payload files verified by SHA-256**, before and after the relocated test.

## Honest recovery boundary

External scratch folders disappeared across a sandbox transition. The surviving
repository notes, patch and tests were preserved. Core inputs/assets were
recovered from pinned GitHub revisions, scripts reconstructed, and the main
recorded results reproduced. Some older isolated-glyph scripts, strict-output
JSON/TXT, intermediate probes and historical logs were not recovered verbatim.
The restored README inventories that gap; this is not a byte-for-byte backup of
every prior scratch file.

Complete layout-free nine-page Unicode extraction is still unfinished. Raw-byte
votes, closest hypotheses, unit tests and zero dictionary unknowns are not green
extraction verdicts. Merging scraper PR #4 preserves this handoff; it does not
wire or enable experimental Linga processing in the crawler/download path.

## Release storage

GitHub's release-asset upload endpoint returned EOF even for the small checksum
file; the API-host alternative returned 404. No uploaded binary release asset is
claimed. The ZIP and checksum are preserved **in the tagged Git commit** and
linked above from the release page. The automatic release source archive also
contains this ZIP. The original larger Git push failed; only this compact package
is being published, with the historical recovery limits explicitly documented.

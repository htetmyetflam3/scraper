previous changes logs are wiped.

## 2026-10-13 — Scrawler single-branch document-tree crawl + S/D rename
- Renamed crawl modules to match S=search / D=download: `search.py` -> `Scrawler.py` (search-result crawler), old `Scrawler.py` (file saver) -> `Dcrawler.py`, old `Dcrawler.py` (static link guards) -> `download_links.py`; fixed `Dcrawler.py` usage string.
- `Scrawler.py` now walks one guided branch per domain: from the site root down the search-result path ancestors, then deeper prefix children only (siblings like `/latest/`, `/author/` never visited).
- Removed the 10-page-per-site limit; the chain runs as deep as prefix children exist, guarded by strictly-longer-path + visited checks.
- Leaf with no PDF/DOCX link or download button gives up the whole domain (`failed_sites`) and moves to the next site; `entry.txt` gains the site root only on a find.
- Added same-site extensionless download-button detection alongside `extract_file_links`.
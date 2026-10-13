previous changes logs are wiped.

## 2026-10-13 — Scrawler single-branch document-tree crawl + S/D rename
- Renamed crawl modules to match S=search / D=download: `search.py` -> `Scrawler.py` (search-result crawler), old `Scrawler.py` (file saver) -> `Dcrawler.py`, old `Dcrawler.py` (static link guards) -> `download_links.py`; fixed `Dcrawler.py` usage string.
- `Scrawler.py` now walks one guided branch per domain: from the site root down the search-result path ancestors, then deeper prefix children only (siblings like `/latest/`, `/author/` never visited).
- Removed the 10-page-per-site limit; the chain runs as deep as prefix children exist, guarded by strictly-longer-path + visited checks.
- Leaf with no PDF/DOCX link or download button gives up the whole domain (`failed_sites`) and moves to the next site; `entry.txt` gains the site root only on a find.
- Added same-site extensionless download-button detection alongside `extract_file_links`.

## 2026-10-13 — Scrawler query-child support (MAX_QUERY_PAGES=5)
- Same-path/different-query links (`/?p=124`, `/books/?file=9`) now qualify as children, so query-navigated sites work; path-deepening children are still preferred and unlimited.
- Query hops capped at 5 per chain: a real file-hosting site shows a download within a few, endless `?page=N` pagination gives up the site instead of hanging.
- Fixed first-segment lock to distinguish "no lock yet" from "locked to root path" for query seeds.

## 2026-10-13 — Split SerpApi fetch from Scrawler crawl
- New `python/bin/fetch_results.py`: runs the four keyword searches once and saves every raw result link (`url`, `title`, `keyword`, `page`) to `python/search_results.json`; owns the API key, keywords and paging.
- `Scrawler.py` no longer calls the search API: `find_entry(results_file=...)` reads the saved JSON, so crawl testing burns zero quota; missing file errors point at `fetch_results.py`. Hand-written `{url}`-only files also work for targeted tests.
- Added `/python/search_results.json` to `.gitignore` (refreshed each fetch; stays local).

## 2026-10-13 — main.py pipeline executor
- Root `main.py` is now the single entry point (`uv run main.py ...`): `--search` fetches SerpApi results only, `--entry` crawls saved results into `entry.txt`, `--download` downloads from `entry.txt` or from a given site URL, `--main` runs all three in order; flags also combine.
- Rebased onto owner's `move toml` + `add main.py` commits; root `pyproject.toml` already carries all six runtime deps so no change was needed there.
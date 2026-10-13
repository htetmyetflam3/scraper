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

## 2026-10-13 — Wise fetch: skip saved keywords, JSON under scan/
- `search_results.json` moved to `scan/` (both writer and Scrawler updated); obsolete ignore rule removed since `/scan/` already covers it.
- `fetch_results()` now reads the existing JSON first and skips keywords already recorded — new keywords append without re-burning quota on old ones. Corrupt files error out instead of being overwritten.
- One-time migration: if only the old `python/search_results.json` exists, it is used as the base and merged output goes to `scan/`.
- `main.py --search` takes optional keyword(s): `--search "pdfများ"` fetches just that keyword (2 pages), bare `--search` uses defaults; multiple keywords allowed in one call.

## 2026-10-13 — Scribd stage: vendored exporter, links file, --scribd
- Reviewed owner's `modified gitignore` commit (adds `python/scribdl/setup.py` only): purely additive, touches none of my logic, so nothing reverted; fast-forwarded.
- Replaced all old scribdl scripts (`scribdl.py`, `__init__.py`, both legacy `setup.py`) with themrsami/scribd-downloader @0396d95, vendored byte-identical as `python/scribdl/scribd-downloader.py` (+ upstream README); driven via subprocess through stdin, never modified.
- New `python/bin/scribd.py` driver (stdlib-only): reads `scan/scribd.txt`, exports each URL to `python/pdfs/` one process per doc, skips non-scribd URLs without spawning, continues past failures.
- Scrawler now writes scribd result URLs (deduped) to `scan/scribd.txt` instead of mixing them into `entry.txt`.
- Root + python `pyproject.toml` gain the exporter deps: selenium, pypdf, trio, trio-websocket, typing_extensions, urllib3 pin, websocket-client. System requirement: Google Chrome installed.
- `main.py --scribd` downloads from the links file; `--scribd "URL"` downloads one document; exit 1 when links were attempted but none succeeded. `--main` still runs search/entry/download only.

## 2026-10-13 — API key reads repo-root .env
- `fetch_results.py` looked for `.env` under `python/`; it now reads the repo-root `.env` next to `main.py` (env var still wins). Only reader, one-line fix.

## 2026-10-13 — entry.txt moved under scan/
- Scrawler writes and Dcrawler reads `scan/entry.txt` (was `python/entry.txt`); stale empty `python/entry.txt` removed so exactly one exists. Help texts and logs updated to the new path.

## 2026-10-13 — Append to entry.txt / scribd.txt instead of wiping
- `find_entry` now preloads existing `scan/entry.txt` and `scan/scribd.txt` (comments and blanks preserved) and appends only new roots/links, deduplicated; re-runs log `already in` plus `[done] X new, Y total` lines.
- `search_results.json` already appended (`existing + collected`, verified by test) — no change; if a wipe was seen there it came from a stale checkout before the wise-fetch update.
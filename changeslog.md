previous changes logs are wiped.

## 2026-10-13 — Per-site login via .env (site_login.py, form-based sites)
- New shared `python/bin/site_login.py`: reads `LOGIN_EMAIL`/`LOGIN_PASSWORD` (generic default) or `LOGIN_<HOST>_EMAIL`/`_PASSWORD` (host-specific, dots/dashes to `_`, uppercased) from the environment or repo-root `.env` (same quoting rules as the SERPAPI parser); optional per-host `_URL`/`_USER_FIELD`/`_PASS_FIELD` overrides.
- Auto-login finds the best username+password form (single-password login preferred over register, email/autocomplete/name hints for the user field), preserves hidden CSRF tokens + checked boxes, allows off-site SSO actions, and verifies via login-form-gone/logout-marker on a re-fetched seed. Fail-open: any failure logs and the crawl continues without the session; silent when a host has no credentials.
- Wired once-per-host into both crawlers on their shared clients (cookies persist for the run): `ensure_logged_in_sync` before Scrawler's site crawl, `ensure_logged_in_async` before Dcrawler's.
- New tracked `.env.example` template (`.gitignore` gains a `!.env.example` exception; real `.env` stays ignored).
- Note: JS-rendered SPA sites (e.g. Expo web apps serving an empty shell) carry no static form, so this covers classic form logins; SPA/API driving is the pending follow-up.
- Verified: 34/34 new tests (`/tmp/test_site_login.py`); regressions 189/189 green.

## 2026-10-13 — Dcrawler POST download forms + POST-returns-URL flow (mbookshelf)
- New `_extract_download_forms`: same-site download forms become candidates carrying (action, method, fields), replicating the page's own `$.post(action, $(form).serialize())` — hidden inputs, selects, textareas, checked boxes; CSRF tokens included in the POST. GET forms fold fields into the URL; file actions become direct GETs; `formaction`/`formmethod` submitter overrides become variants.
- `_resolve_candidate` submits form candidates with POST on the same cookie session (Laravel `_token` flow works), and follows bare-URL response bodies with a bounded second GET — the mbookshelf `/download-book` answers with a file-URL string, not the PDF. JSON-quoted and relative URLs handled; HTML/JSON blobs/binary rejected.
- Shared action URLs no longer collapse: candidate keys and `scan/blocked.txt` use a stable `POST <action> uid=…` identity minus session-rotating CSRF fields, so each book is its own candidate and stays skippable across runs despite fresh tokens.
- A form action is submitted to, never GET: duplicate GET control-candidates for POST actions are suppressed (no more doomed 419/405 `[unresolved]` noise). `[download]` logs POSTs as `POST <url> (N fields)`.
- Verified Scrawler needs no change: its control scan already sees the `download-book` action (`[found]`), so the site reaches Dcrawler; the iOS `naingdroidapps://` link is ignored everywhere (non-http scheme).
- Verified: 38/38 new mocked tests (`/tmp/test_d_form_download.py`, incl. end-to-end POST→URL→save and token-proof blocked skip); regressions 151/151 green.

## 2026-10-13 — Dcrawler search-loop skip + honest no-download verdict + blocked memory
- Site-search URLs (`?s=`, `?q=`, `/search/...`, Joomla `searchword`, `keyword(s)`, …) are never scheduled as crawl pages: every search page links more fresh-query searches, an infinite space the visited-set can never close. Post links (`?p=`), pagination (`?page=`) and files are unaffected.
- Download controls pointing at already-scheduled pages are no longer re-candidated: the page visit itself handles their files, so this kills the re-fetch plus the bogus "failed download" verdict (e.g. blogspot self-links).
- `_resolve_candidate` now returns a `_NO_DOWNLOAD` sentinel for plain pages with no file link, file-host link or download control (recover attempt still runs first; dead `.pdf` links and pages with buttons stay `[unresolved]`, providers never get the sentinel). `[no-download]` log replaces the misleading "provider did not return a valid PDF/DOCX" warning for such pages.
- Download-less URLs are remembered in `scan/blocked.txt` (git-ignored runtime memory): later runs skip them pre-resolution with `[blocked]` and zero network. Genuine failures are never recorded, so they keep retrying.
- `Finished:` line now also reports `blocked-skipped` and `page(s) without download` counts.
- Verified: 30/30 new mocked tests (`/tmp/test_d_search_blocked.py`); regressions 17/17 BFS+dedupe, 14/14 archive, 21/21 app, 7/7 append, 3/3 envloc.

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

## 2026-10-13 — Dcrawler root-expanding BFS + duplicate guards
- `_crawl_site` restructured from stack-DFS to breadth-first root expansion: seeds first, then every sibling branch level by level to its leaves, each URL fetched exactly once, 3000-URL cap with a one-time `[cap]` log, `[page] depth=N` progress lines. Sitemap pre-scan/guard and per-page downloads unchanged.
- Identical file bytes now saved once per run (sha256 dedupe in `download_one`): repeats log `[duplicate]` and count in the `Finished:` line instead of writing `name (2).pdf`.
- `guard_download_url` canonicalizes scheme/host (lowercase + IDNA; path case, userinfo, port preserved), so `http`/`https` and `WWW`/`www` variants share one identity across all three pipeline stages.

## 2026-10-13 — API key reads repo-root .env
- `fetch_results.py` looked for `.env` under `python/`; it now reads the repo-root `.env` next to `main.py` (env var still wins). Only reader, one-line fix.

## 2026-10-13 — entry.txt moved under scan/
- Scrawler writes and Dcrawler reads `scan/entry.txt` (was `python/entry.txt`); stale empty `python/entry.txt` removed so exactly one exists. Help texts and logs updated to the new path.

## 2026-10-13 — Append to entry.txt / scribd.txt instead of wiping
- `find_entry` now preloads existing `scan/entry.txt` and `scan/scribd.txt` (comments and blanks preserved) and appends only new roots/links, deduplicated; re-runs log `already in` plus `[done] X new, Y total` lines.
- `search_results.json` already appended (`existing + collected`, verified by test) — no change; if a wipe was seen there it came from a stale checkout before the wise-fetch update.

## 2026-10-13 — Exclude archive.org (shared guard) + Scrawler ignores excluded file links
- `domain_guard.py` gains `ARCHIVE_DOMAINS` (`archive.org`, all subdomains incl. `*.us.archive.org` download hosts): archive.org was never guarded, nothing was lost. Both crawlers honor it — Dcrawler skips its pages and files via its existing 7 guard call sites, no code change there.
- Scrawler no longer counts file links on excluded domains (archive.org, wiki, video, telegram, github, search) toward a `[found]` verdict; seeds/children on those domains were already never fetched.

## 2026-10-13 — Exclude app-download sites (shared guard)
- New `APP_DOMAINS` in `domain_guard.py`, honored by both crawlers: apple.com, appstore.com, softonic.com, apkpure.com, uptodown.com, apkmirror.com, aptoide.com, f-droid.org, filehippo.com, softpedia.com, apps.microsoft.com (subdomains included). play.google.com was already excluded via the google-label rule.
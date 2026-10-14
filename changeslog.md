previous changes logs are wiped.

## 2026-10-14 — --download-remote: save_local split out + save_remote SSH archive
- Saving split: `python/bin/save_local.py` now holds the local saving trio (`filename_hint`, `reserve_unique_path`, `save_payload`) moved out of Dcrawler byte-for-byte; Dcrawler imports them back under the original private names (`_save_payload` etc.), so every call site behaves exactly as before (verified: aliases identical, suites green). `linearize`/`Message`/`tempfile` imports moved with the code.
- New `python/bin/save_remote.py` (stdlib only, plain `ssh`): pushes a saved file, resumes from the remote byte size through `cat >>`, retries with a delay, verifies the remote sha256 against the local bytes, appends to append-only `scan/uploaded-host.txt` (digest, name, remote path, bytes, time), then deletes the local copy. `BatchMode=yes` so a missing key fails fast instead of prompting; `ServerAliveInterval=30` beats idle drops. Failures keep the local file; nothing is deleted without a live verification. No rsync/lftp needed on the host.
- `main.py --download-remote [SITE_URL]` (+ `--keep-local`): same crawl as `--download` but every saved file is archived to the SSH host. Dcrawler also accepts `--remote`/`--keep-local` directly. Remote runs first drain the backlog already sitting in `pdfs/` (leftovers or a first run, `[push-backlog] pushed N, failed M (X MB)`), then push each new file inline as it is saved.
- Wiring: push failures append status `upload` to `scan/failed.txt`, count in the new `upload_failed` summary field (timeline `summary` row and the `Finished:` line), and keep the local copy for the next run. Timeline gains `push-ok`/`push-free` rows; console lines `[remote]`, `[push]`, `[push-ok]`, `[push-skip]`, `[push-free]`, `[push-fail]`, all stamped.
- Config: `SSH_HOST`, `SSH_USER` (required), `SSH_PORT`, `SSH_KEY`, `SSH_DIR` — environment first, then repo-root `.env` (same rule as SERPAPI); `.env.example` documents the block. `SSH_DIR` is relative to the host account's home; keep it out of public_html. `--keep-local` without `--download-remote` exits 2.
- Verified: 60/60 (`/tmp/test_d_remote.py` — fake ssh binary: fresh push, resume from partial, sha256-mismatch detection, collision naming `name (2).ext`, ledger skips by digest and by name+size, retry-then-fail, drain backlog, plus full Dcrawler remote runs for success, failure (local kept + failed.txt), keep-local, and backlog drain) + 19/19 (`/tmp/test_d_core.py` — split aliases, local-mode behaviour unchanged, CLI flag guards) + a real `main.py --download-remote` CLI run through the probe/crawl/exit path. Test scripts kept out of the repo per agents.md.

## 2026-10-14 — accidental tdl README reverted (owner push fe4005d)
- The pushed commit `fe4005d Readme.md` added `README.md` + `README_zh.md` — iyear/tdl's own project README (Telegram Downloader badges, docs links), not this repo's docs. Copy/paste accident, not a file-location move, so reverted (`55fde0a`); both files are 101 lines of upstream text, nothing of the scraper was touched by their commit or by the revert.
- Note for the box: pulling the revert deletes those two files locally. If a real scraper README is wanted, write one for this repo instead of importing another project's.

## 2026-10-14 — deletion is its own step: failed_urls.py --delete-zip
- `--zip` now only audits and writes the manifest; it no longer prints any delete command, and says so: "manifest creation deletes nothing".
- New separate mode `--delete-zip batch1.zip [--base DIR] [--yes]`: reads the zip's manifest and deletes exactly those files. Dry run and nothing removed without `--yes`; refuses to run when the manifest does not match the zip's current contents (stale manifest), never touches absolute paths or `..` escapes, skips symlinks, reports counts (deleted/missing/symlink/unsafe/error) and hints `--base DIR` when the listed paths are not under the default base (the zip's own folder). `--yes`/`--base` without `--delete-zip`, and `--zip` together with `--delete-zip`, exit 2.
- Verified: 60/60 (`/tmp/test_d_tracking.py`: dry run deletes nothing, `--yes` removes exactly the members and leaves non-members/zip/manifest, rerun reports missing + base hint, stale-manifest refusal, zip-slip `../` canary untouched while the safe member is deleted, flag validation, missing zip/manifest exits) + 10/10 regression. Test scripts stay out of the repo.

## 2026-10-14 — stray scan/gitkeep reverted (owner push 3b9f021)
- The pushed commit `3b9f021 updated` added an empty `scan/gitkeep` — same intent as `scan/.gitkeep` but missing the dot, which already exists and is the only trackable path under the new `/scan/*` ignore rule. Not a file-location move, so reverted per the kept/reverted rule; the file was 0 bytes, nothing lost.
- `git ls-files scan/` lists exactly `scan/.gitkeep` again. Any box that pulled it loses the stray file on its next pull (it is tracked-and-deleted upstream, so the pull removes it cleanly).

## 2026-10-14 — failed_urls.py --zip writes a deletion manifest
- `--zip batch1.zip` now also writes `<zip>.manifest.txt` next to the zip (`--manifest PATH` to override): four `#` header lines (zip name, made time, member/pdf counts, written time) then one zip-internal path per line for every file inside, `./` prefixes normalized. Rewritten on every `--zip` run so it always matches the current zip; the tool never deletes anything itself.
- The run prints a ready-to-run delete loop to execute from the directory the zip was built in, and only after the upload is verified: `grep -v '^#' <manifest> | while IFS= read -r f; do rm -f -- "$f"; done`.
- Verified: 50/50 total (`/tmp/test_d_tracking.py` gains 7 manifest checks: default placement, header, body + `./` strip, printed hint, custom path, write-failure exit 1) + 10/10 regression; end-to-end simulate deleted exactly the manifest-listed members (including a name with spaces), leaving non-member files, the manifest and the zip untouched.

## 2026-10-14 — scan/ keeps a .gitkeep; empty entry.txt untracked (fast-forward safety)
- `scan/entry.txt` was the only tracked file inside `scan/` and it was an empty placeholder; a fast-forward could have tried to overwrite a box's local crawl history in it. It is now untracked (`git rm --cached`, file kept on disk byte-for-byte) and `scan/.gitkeep` replaces it so the directory still exists in fresh clones.
- `.gitignore`: `/scan/` -> `/scan/*` + `!/scan/.gitkeep`. The negation requires the parent directory not be ignored itself, which is why `/scan/*` is used; everything inside scan/ (entry.txt, downloaded.txt, blocked.txt, failed.txt, timeline.log, crawl journals) stays local-only, `.gitkeep` is the only trackable path.
- Box-side one-time step when pulling this commit: `git rm --cached scan/entry.txt` then `git pull`. A plain pull first aborts safely ("would be overwritten by merge") whenever entry.txt is modified locally, and that file was 0 bytes in every commit ever, so nothing can be lost either way.
- Verified in a throwaway clone of the repo: dirty entry.txt -> pull refused, content intact; after `git rm --cached` -> pull fast-forwards, content intact, `git ls-files scan/` lists only `scan/.gitkeep`, other scan/ files stay ignored across further pulls.
- Checked every tracked blob at HEAD: `scan/entry.txt` was the only empty one. No behavioral code touched.

## 2026-10-14 — Timestamps + one failed-download ledger + failed_urls.py helper
- Small `HH:MM:SS` stamps so parallel zip/upload/download work stays trackable: `[start] <source> @ HH:MM:SS` at run start, both `[resume]` lines, every `[saved]` line, the final `Finished:` line, and `[paused]` on Ctrl+C (Dcrawler and main.py) — the interrupt time and the resume time are now always visible.
- New append-only `scan/timeline.log`, one row per event: `MM-DD HH:MM:SS event [detail]`. Events: start, resume (pages=N), live (continuing from URL), saved (name + size), pause, summary (saved/downloaded/no_download/unresolved/pages). Oldest first, never rewritten; a timeline write failure only warns and the run continues.
- New append-only `scan/failed.txt` ledger, one TAB record per failure: `time<TAB>status<TAB>url<TAB>detail`. Blocked pages (no download button) and failed downloads (unresolved = provider returned nothing, rejected = save/validation refused) are all recorded now — previously the failed ones existed only as console lines. Deduped on (status, url); skipping still comes from `scan/blocked.txt` only, so the ledger records without silently skipping.
- New standalone `python/bin/failed_urls.py` (stdlib only, runnable from anywhere): crawled URLs (every crawl journal) minus downloaded URLs (`downloaded.txt` identities) = failed URLs, appended to `blocked.txt` and recorded in `failed.txt`; idempotent reruns, `--dry-run`, `--all` (all crawled URLs, not just download-looking ones), `--scan-dir`. Its `[id-check]` report lists crawl pages whose numeric URL id has no matching download record. `--zip batch1.zip` audits a batch zip: member + .pdf counts, when it was made, saves after that moment (=> not inside), and PDFs on disk missing from it — answers "up to which pdf the zip contains".
- Verified: 43/43 (`/tmp/test_d_tracking.py`: stamps, timeline rows, ledger dedupe/shape, replay-resume stamping without network, run wiring for unresolved/blocked/saved, helper backfill/dry-run/idempotence/--all/--zip) + 10/10 (`/tmp/test_d_regress.py`: blocked-skip behaviour untouched, CLI entry paths, helper default scan dir). Test scripts kept out of the repo per agents.md.

## 2026-10-13 — [seed-empty] warning + [known] hash count (dead-seed UX)
- `_crawl_site` prints `[seed-empty] <url>: entry page gave no links or downloads; check the seed URL` when a depth-0 page yields zero children and zero candidates — a dead/soft-404 seed now explains itself instead of ending the run in silence. Errored pages keep their `[page-error]` and never trigger it.
- `[known]` now shows both counts (`N remembered URL(s), M known file-hash(es)`) so hash-only bootstrapped history no longer reads as "0 remembered".
- No crawl-logic change: queueing/expansion untouched; both edits are logging/display only.
- Verified: 6/6 new (`/tmp/test_d_seedwarn.py`: empty/soft-404 seeds warn, linked/download/errored seeds quiet); 23/23 + 9/9 suites still green.

## 2026-10-13 — dedup_pdfs standalone utility (name-based (N) cleanup)
- New `python/bin/dedup_pdfs.py` (stdlib only, executable, runnable from anywhere, defaults to the repo's `pdfs/`): deletes `name (N).ext` files whose plain `name.ext` exists in the same directory.
- Safety rules: dry run by default (`--delete` to act); only a trailing ` (digits)` immediately before the extension counts (middle numbers, non-digit parens untouched); numbers >= 1000 skipped as edition years; plain sibling must exist (orphans kept); symlinks skipped; stem matched case-sensitively, extension case-insensitively; per-file log + summary counts.
- Verified: 14/14 (`/tmp/test_dedup_names.py`: dry/delete, ext case, middle numbers, year guard, chains, orphans, stem case, same-dir recursion, missing dir).

## 2026-10-13 — History bootstrap from disk on startup
- `_load_downloaded_memory` now appends hash-only lines (sorted, flushed at once) for on-disk files missing from `scan/downloaded.txt`, then loads — so pre-history downloads are listed automatically and archiving the disk right after a run never orphans them. Announces `[history] listed N file(s) from disk`; idempotent and append-only (existing lines untouched).
- Bare files yield hashes, not URL identities, so bootstrapped lines skip by hash on first meeting and self-heal the URL identity then (later runs pre-skip with zero network). In-memory index stays hash sets (O(1)); only the file is kept sorted for humans.
- Workflow: run once WITH files on disk (bootstrap lists them), THEN move to drive — files already gone cannot be listed.
- Verified: 9/9 new (`/tmp/test_d_bootstrap.py`: sorted/flushed bootstrap, idempotence, append-only, hash-only parsing, malformed ignored, e2e self-heal + post-archive pre-skip); 23/23 downloaded suite still green.

## 2026-10-13 — Downloaded-history is permanent (rotate files off disk freely)
- `_load_downloaded_memory` no longer intersects history with disk state: once a download is recorded in `scan/downloaded.txt` (URL identity + sha256) it is never fetched again, even after the file is moved to an external drive to free space. The startup disk scan now only adds pre-existing/hand-placed files to the known hashes.
- Rationale: 10k books x 10-20 MB far exceeds local spare space, so files must rotate off disk; "deletion re-arms the download" would have re-downloaded the archive. To force a re-download, delete its line from `scan/downloaded.txt`.
- Post-fetch skip message reworded to "already downloaded on a previous run" (the file may live on the archive drive, not on disk).
- No disk guard added per user request; no other behavior changed (URL-memory still skips pre-fetch with zero network, hash still backstops same-bytes arrivals).
- Verified: 23/23 (`/tmp/test_d_downloaded.py` rewritten after a `/tmp` wipe: trust-without-disk, moved-away zero-network skip, archived-bytes dedupe with no disk write, plus all prior checks).

## 2026-10-13 — Single-site runs uncapped (no 3000 limit, no size skip)
- `_crawl_site` takes `max_pages` (default 3000); the caller passes `None` when the run holds exactly one site — a URL arg or a 1-line entry file — so one specified site grinds to its last page. Multi-site file runs stay capped per site per run.
- Uncapped mode also disables the sitemap-size skip (`large_site_reason` gets 0, its built-in off switch) and logs `[nocap] <site>: single-site run, no page limit`.
- Verified: 11/11 new tests (`/tmp/test_d_nocap.py`: uncapped 8-chain vs 3000-exact default proven with a 3005-chain, gate on/off, forwarder, caller URL/1-line/multi-line); full Dcrawler regression re-green 21/21 + 22/22 + 15/15 (test fakes updated for the new kwarg).

## 2026-10-13 — Dcrawler pause/resume crawl journal
- New per-site journal `scan/crawl-<site>.jsonl` (git-ignored): every finished page appends `{"u": url, "c": [discovered children]}`, flushed per line, so Ctrl+C / git-pull kills lose at most the page in flight.
- Reruns replay journaled pages with zero network (`[resume] … replaying them`, then `[resume] … continuing live from <url>`) and continue where the last run stopped; Ctrl+C now prints `[paused] … rerun to resume` (exit 130) instead of a traceback.
- The 3000-URL cap is now a per-run fresh-page budget (`[cap] … this run; rerun to continue`): big sites grind forward a further 3000 pages each run instead of re-crawling the same head forever. Cap-cut branches are journaled as discovered (not lost).
- Retry rules: non-200 pages and page-errors are never journaled (retried next run); redirects/doc-pages journal normally; corrupt journal lines are skipped.
- New `--fresh` (`main.py --download --fresh`, `--main --fresh`, `Dcrawler.py --fresh [src]`): wipes journals to revisit every page — saved files are still skipped via `scan/downloaded.txt`, never re-downloaded.
- Verified: 21/21 new tests (`/tmp/test_d_resume.py`: journal round-trip/corruption/clear units plus real-`_crawl_site` two-run e2e: pure replay, cap-continue with zero refetch overlap, 500-retry, doc-page, redirect chain).

## 2026-10-13 — Dcrawler cross-run downloaded-memory (no re-downloads)
- New `scan/downloaded.txt` memory (`{sha256} {identity}` lines, git-ignored): every save/duplicate/hash-hit records the bytes' hash plus the canonical candidate identity (form-safe: whitespace-collapsed, CSRF-proof).
- Pre-fetch skip: remembered identities are skipped with `[downloaded] … saved on a previous run` and zero network; skips don't count as candidates.
- Disk-hash backstop: each run sha256-scans `python/pdfs/` (dotfiles/subdirs ignored) and skips same-bytes arrivals with `[downloaded] … identical file already on disk` — covers rotated CDN URLs and hand-placed files, never writes `(2)` dupes.
- Disk is ground truth: a remembered URL is trusted only while its hash still exists, so deleting a file re-arms its download; failures/rejections are never recorded.
- `Finished:` gains an `already-downloaded` count; startup logs `[known] N file(s) (M MB) on disk, K remembered download(s)`.
- Verified: 22/22 new tests (`/tmp/test_d_downloaded.py`: identity/trust/disk-scan units plus multi-run e2e incl. rotation, deletion, manual adds, in-run `[duplicate]` preserved).

## 2026-10-13 — Blacklist apkpure.net; Scrawler logs skipped domains
- The APKPure mirror `apkpure.net` (not just `apkpure.com`) sailed through the guard, got crawled, matched "Download APK" buttons, and polluted `scan/entry.txt`. Added to `APP_DOMAINS`, so both crawlers refuse it everywhere the shared `excluded_site_reason` applies.
- Scrawler's `find_entry` used to drop excluded seeds silently; it now logs `[skip-domain] <url>`, matching Dcrawler's vocabulary, so blacklist hits are visible in the crawl log.
- Verified: 15/15 new tests (`/tmp/test_apkpure_net.py`: net/com variants, subdomains, case, lookalike non-matches, Scrawler never-crawls/never-enters, Dcrawler entry gate).

## 2026-10-13 — Phatmal full crawler: library sweep + S3 EPUB downloads + --phatmal
- Contract captured from the live API: `GET /ebooks?page&limit` (236 books, `hasMore` pages), `GET /ebooks/{slug}`, per-book `files[{url,type,size}]` with `fileUrl`/`fileType` fallback; files are EPUBs on S3 (`phatmal-production…amazonaws.com`).
- `phatmal.py` now crawls the whole library (or one slug/URL): login → verify → paginate → prefer epub over pdf per book → headerless S3 GET (auth headers would be rejected) → content-sniff (new tolerant `is_epub`, plus pdf/docx) → save as `Title - Author.epub` with unique-name guard into `python/pdfs/`.
- `scan/phatmal.txt` remembers `slug|filename` (append-only, pipeline convention): re-runs skip books whose file still exists and re-fetch missing ones; drafts and file-less books log `[skip]`; non-book bytes `[rejected]`.
- `main.py` gains `--phatmal [SLUG_OR_URL]` (bare = whole library; exit 1 only when nothing saved and something failed).
- Verified: 31/31 new tests (`/tmp/test_phatmal_crawl.py`); auth 22/22 + probe 24/24 still green.

## 2026-10-13 — Phatmal probe: endpoint sweep + shape logging + --phatmal-probe
- `phatmal.py` gains content discovery: `probe_content` logs in, verifies `/auth/me`, then sweeps 6 detail-path guesses + 4 list paths with the real session, printing each hit's JSON SHAPE (keys/previews, secrets redacted, strings truncated) instead of full bodies.
- `slug_from_input` accepts bare slugs or full `/ebook/<slug>` URLs (validated charset); `describe_shape` caps depth/keys/length for pastable logs.
- `main.py` gains `--phatmal-probe SLUG_OR_URL` (exit 0 on any hit); `.env.example` documents `PHATMAL_*`.
- Verified: 24/24 new tests (`/tmp/test_phatmal_probe.py`); auth suite still 22/22.

## 2026-10-13 — Phatmal API driver v1: login + session verify + --phatmal-check
- New `python/bin/phatmal.py`: the Expo SPA serves an empty shell, so this drives `https://api.phatmal.com/api` directly. `api_login` POSTs `PHATMAL_EMAIL`/`PHATMAL_PASSWORD` with the `x-api-key` header (from `PHATMAL_API_KEY`), tolerant-extracts the Bearer JWT from any common response shape, and `api_me` proves the session via `GET /api/auth/me`. App-like headers (Accept/Referer/mobile UA) on every call; secrets never logged, misses log key names for diagnosis.
- `main.py` gains `--phatmal-check` (exit 0/1) so the login can be tested from the real environment.
- Verified: 22/22 new tests (`/tmp/test_phatmal_auth.py`, MockTransport). Note: the /tmp regression suites were wiped by a sandbox reset, so old suites could not be re-run; this turn's change surface is additive (new module + default-off flag + import).

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
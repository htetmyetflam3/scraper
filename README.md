# Myanmar book link scraper

This repository has two independent crawlers (Python and Node) and one list-based
downloader. The Python pair lives in `python/` and is the one under active work:

## 0. Search-engine crawler — Python (recommended)

`python/crawler.py` is the crawler to use. It reads results with CSS selectors
instead of hand-written HTML parsing, keeps cookies across requests (the usual
cause of a 403 on page two), and can drive a **real headless Chromium** when an
engine refuses plain HTTP.

```sh
cd python
uv sync                       # requests, beautifulsoup4, httpx, tenacity, tqdm

uv run crawler.py                         # Mojeek, default terms
uv run crawler.py --engine=searx
uv run crawler.py --fetcher=chromium      # real headless browser
uv run crawler.py --reset-state           # scan everything again
uv run pytest                             # 35 tests, no network needed
```

### Headless browser: one distro package, no Python wheel

`--fetcher=chromium` shells out to the browser already on your system and reads
the DOM with `chromium --headless --dump-dom`. There is **no Python browser
package to install**, which is deliberate:

- Playwright publishes no wheel for musl, so `uv add playwright` fails on
  Alpine/aarch64 with *"only has wheels for the following platforms"*.
- Even where the wheel installs, `playwright install chromium` downloads a
  Chromium linked against glibc, which will not run on musl either.

So install the browser with your package manager and the crawler finds it
automatically (`--browser-path`, `$CHROMIUM_PATH`, then `$PATH`):

```sh
apk add chromium          # Alpine / postmarketOS
pacman -S chromium        # Arch (incl. Arch Linux ARM)
apt install chromium      # Debian / Ubuntu
brew install --cask chromium   # macOS
```

On a glibc machine Playwright also works, and is used when it is installed
(`uv pip install playwright && playwright install chromium`). Nothing depends on
it, so no documented command can fail on musl.

No browser and no package manager? `--browser-path` also takes a whole command,
so a containerised browser works:

```sh
docker run --rm -p 53333:53333 jacoblincool/playwright:chromium-light-server
uv run crawler.py --fetcher=chromium \
  --browser-path="docker run --rm --entrypoint chromium jacoblincool/playwright:chromium-light"
```

`jacoblincool/playwright` publishes Alpine ARMv8 images, which is the one
combination PyPI cannot serve. If the entrypoint differs, `--browser-path` takes
any command that accepts Chromium flags and ends with the URL.

| Flag | Notes |
| --- | --- |
| `--engine` | `mojeek` (default), `searx`, `brave`, `bing`, `google`, `duckduckgo` |
| `--fetcher` | `chromium` (system browser, no Python package), `playwright` (its bundled browser), `http` (requests), `auto` (first one available, else HTTP) |
| `--browser-path` | path to a Chromium/Chrome binary |
| `--base-url` | point at your own SearXNG instance |
| `--delay`, `--jitter` | seconds between requests (default 2 + up to 0.5 random) |
| `--pages` | result pages per query (default 10) |
| `--reset-state` | clear the page history, keep found links |
| `--debug-dir` | save HTML of pages that could not be read |

It writes `search_entry_list.txt`, `search_scribd_links.txt`,
`search_crawled_pages.txt` and `search_pending_pages.txt` next to the script.

If an engine answers `HTTP 403`, run with `--fetcher=chromium`: a real browser
executes JavaScript, keeps cookies and has a genuine browser fingerprint.

## 0d. Site crawler and downloader — two scripts

Two scripts, run in order. Neither one needs the other's code to run.

- **`python/dsite.py` — crawler (search, no downloads).** Finds sites and records
  where the files are. It does not download anything.
- **`python/dsite_download.py` — downloader.** Reads the entry list the crawler
  wrote, downloads the files, and crawls the listed sites for more files.

```sh
cd python
uv run dsite.py                              # SerpApi searches, then record each site
uv run dsite.py https://example.org/books/   # or start from one URL
uv run dsite_download.py                     # download from scan/site_entry_list.txt
uv run dsite_download.py my_links.txt        # or from any list you give it
```

### The crawler (`dsite.py`)

- Without a URL, it searches with **SerpApi** (Google results). The key is read
  from `SERPAPI` in `python/.env` (create that file yourself; it is gitignored),
  or from the `SERPAPI` environment variable. The environment wins if both are set.
- The default search terms are the four approved keywords (`--search-terms`,
  comma-separated): `myanmar books download`, `myanmar ebooks download`,
  `Myanmar PDF free download`, `free မြန်မာ pdf စာအုပ်များ`. Each keyword is one
  search, run in order. Two result pages per keyword by default, so a full run
  is **8 searches** (4 keywords × 2 pages). `--search-pages=1` makes it 4.
  Each page is one SerpApi request, and each request counts against the budget.
- Searches are spaced at least `--search-gap` seconds apart (default 60).
- **One method, stop at the first download link.** For each site result, pages are
  read until the first PDF or DOCX link. Then the crawl of that site stops, and the site's **main link** (scheme + host + `/`) is written to
  `scan/site_entry_list.txt`. A site with no hit is capped at `--max-pages`
  (default 10).
- **Crawler-only site guard.** Wikipedia/Wikimedia, YouTube, and common search
  engine domains (including regional Google result pages) are skipped before
  any site page is requested. For other sites, `dsite.py` counts URLs from a
  sitemap named in `robots.txt` or a conventional sitemap path; if it can prove
  there are more than 3,000 entries, it skips that site. Change the threshold
  with `--max-site-pages` (`0` disables only the sitemap-size check). There is
  no universal exact page-count API: if a sitemap is missing/incomplete, size is
  unknown. `--max-pages` still caps requests, and an enabled size limit also
  caps that crawl at `--max-site-pages`. The guard does not apply to
  `dsite_download.py`.
- A result that is a PDF or DOCX file itself goes to the same entry list as a file URL.
- A Scribd result goes to `scan/site_scribd_links.txt`. Its key is the Scribd
  document URL, and its Source is the **search engine index link**
  (`https://www.google.com/search?q=<keyword>`), not the site link.
- Every PDF/DOCX link on a page is kept. There is no file-name or Burmese-text
  check: the searches are already Burmese.
- **Monthly budget.** The free SerpApi plan allows 250 successful searches a
  month. The tool counts its own searches in `scan/serpapi_usage.json`
  (gitignored), refuses a run whose planned searches exceed what is left, and
  stops on a refused key, rate limit or SerpApi error. The count only covers
  searches made by this tool, so check your SerpApi dashboard for the real figure.
- `robots.txt` is honoured. A site that blocks or rate-limits is abandoned at once.
- Pages are paced by `--delay` (default 1 s) plus up to `--jitter` (default 0.5 s).

### The downloader (`dsite_download.py`)

- Reads the list given on the command line (default `scan/site_entry_list.txt`).
  A list you write by hand works: one `http(s)` URL per line, with or without the
  header, UTF-8 or UTF-16. A file URL is downloaded; a site URL is crawled.
- The downloader and the crawler are separate entrypoints. The downloader
  shares low-level page-fetch/link-parsing code but does not run `dsite.py` or
  search, and it never writes the entry list. It also does not use the crawler's
  Wikipedia/search-engine/YouTube or sitemap-size guard.
- Each file is handled one at a time, in this order:
  **download -> decrypt (unlock) -> linearize -> save to disk.** PDFs are unlocked
  and linearized by `linearize.linearize()` (from `linearize.py`). DOCX files are
  saved as they are. A PDF with a user password stays on disk as downloaded and is
  reported as `locked`; pass `--password` or `--password-file` to unlock it.
- A **file row** (a PDF/DOCX URL) is handled directly.
- A **site row** (a main link) is crawled: no page limit by default (`--max-pages 0`; a positive number sets one), depth 10
  (`--max-depth`). Each file is handled **as soon as the crawler finds it**, before
  the crawl moves to the next link.
- WordPress share-button variants such as `?share=telegram` and
  `?share=jetpack-whatsapp` are not page targets; tracking parameters are
  removed, and a same-site URL that redirects off-site is stopped before the
  external host is requested. This avoids the Telegram/WhatsApp redirect loop.
  The Wikipedia/search-engine/YouTube and 3k sitemap guard above remains
  crawler-only and is not applied to downloader entry-list rows.
- Files already in `download.py`'s history are skipped, so a file is never
  downloaded twice, even across runs.
- Pages are read again on a rerun. There is no page-level resume; only the
  downloaded files are remembered.
- Output: the files go to `python/downloaded_files/` (`--out-dir` changes it).

**Download buttons.** A button counts as a link when the page HTML names its
file: an `<a href>` around the button, `onclick="...file.pdf..."`, `data-href` or
`data-url`, `formaction`, or `<form action>`. A button whose file URL is only
created by JavaScript at run time is not visible to the crawler.

### Output (all in `scan/` at the repo root, gitignored)

- `search_results.txt`: every result of every search (keyword, start, rank,
  title, URL), written **before** any site is crawled.
- `search_cache.json`: the saved search responses. A rerun reuses them, so
  repeating a run does not spend searches. `--fresh-search` ignores them.
- `site_entry_list.txt`: the entry list, one row per file URL or site main link.
  It is written with a header row at the start and grows during the run. The
  crawler merges its rows into the file on disk, so rows you add by hand are kept.
- `site_scribd_links.txt`: Scribd documents, Source = search index link.
- `serpapi_usage.json`: the monthly search counter.

### Order of work

1. `uv run dsite.py` — searches and the entry list are complete when it exits.
2. `uv run dsite_download.py` — downloads, unlocks and linearizes from the entry list.

Stop a run with Ctrl+C. Files already downloaded are kept, and a rerun skips them.

## 0c. PDF linearizer — `python/linearize.py`

Sweeps the `pdfs/` folder that sits next to `python/` and rewrites every PDF so
a browser or Scribd-style viewer can show page 1 before the whole file has
arrived ("fast web view").

```sh
cd python
uv run linearize.py --dry-run       # report only, nothing touched
uv run linearize.py                 # do it
uv run linearize.py --password=xyz  # also unlock files that need a password
```

What it does, in order:

1. **Duplicates** (same sha256) — all but one copy are deleted. The kept copy is
   the one with the shortest name, so `book.pdf` beats `book (1).pdf`.
2. **Linearize in place** — the new file is written to a temp file beside the
   original, its page count is checked against the original, and only then does
   it replace the original. **No backup and no copy is left behind.**
3. **Encrypted PDFs are unlocked, not kept as-is.** Restriction-only PDFs (no
   user password) open automatically; password-protected ones need `--password`
   (repeatable) or `--password-file`. The rewrite saves without encryption, so
   print/copy restrictions do not survive.
4. **Broken, non-PDF and still-locked files are left byte-for-byte alone** and
   listed at the end. Add `--delete-broken` to remove them.

It is safe to run again: files already linearized are skipped, so you can drop
PDFs into `pdfs/` one at a time and re-run. Tests: `uv run pytest` (no network).

## 1. Search-engine crawler (general discovery)

This searches result pages from Mojeek by default. It does **not** start from or crawl `dhammadownload.com`; it collects matching direct PDF/DOCX results and records Scribd document URLs separately for manual use.

```sh
node js/searchbooks.js
node js/download.js search_entry_list.txt
```

Supported providers are `mojeek` (default), `searx`, `brave`, `bing`, `google`, and `duckduckgo`:

```sh
node js/searchbooks.js --engine=searx
node js/searchbooks.js --engine=google --delay=5000
```

| Engine | Notes |
| --- | --- |
| `mojeek` | Default, but **not reliable**. Its search pages have returned HTTP 403 after page 1 and an ALTCHA "Verification required" CAPTCHA page (seen 2026-10-10 from the sandbox). The crawler now reports these as `blocked` instead of `0 matches`. Do not assume it is crawler-friendly. |
| `searx` | Any SearXNG instance — a meta-search proxy, so it queries Google/Bing/Brave *for* you and returns plain HTML. Best coverage if the instance allows you. Point it at your own with `--base-url=https://your-instance/search`. |
| `brave`, `bing`, `google`, `duckduckgo` | Bigger indexes, but all of them throttle or CAPTCHA automated clients — Brave answers `HTTP 429` within the first few pages. |

Switching engines is safe mid-project: pages queued for a different engine are dropped from the queue, and the links already found are kept.

Default keywords include the requested broad Burmese and English searches: `Burmese book PDF download link`, `Burmese PDF`, `Myanmar ဝတ္ထု`, `မြန်မာစာ`, `ဝတ္ထု`, `ရသ`, `ကဗျာများ free download`, `မြန်မာစာပေ`, both `မြန်မာဝတ္တု`/`မြန်မာဝတ္ထု` spellings, `သုတစာပေ`, `ရသစာပေ`, `အချစ်ဝတ္ထု`, `စိတ်ကူးယဉ်ဝတ္ထု`, `နာမည်ကြီးစာရေးဆရာများ၏ PDF download linkများ`, `နာမည်ကြီးစာရေးဆရာများ`, and related book/novel/poetry download phrases. For every keyword, it searches broadly, adds PDF- and DOCX-focused searches, and searches Scribd separately. A result enters `search_entry_list.txt` when it is a PDF/DOCX that looks Burmese-related: with the default `loose` matching the file name, URL path, result title or full URL may carry `myanmar`/`burmese`/`burma` or a Myanmar character in U+1000–U+1041; set `SEARCH_MATCH_MODE=filename` to require it in the file name alone. Scribd `/doc/`, `/document/` and `/book/` result URLs are stored separately in `search_scribd_links.txt` as title/URL/source-page TSV rows.

To use exact search queries, pass them as arguments or set `SEARCH_QUERIES` (separate multiple values with newlines or semicolons):

```sh
node js/searchbooks.js "Abhidhamma Myanmar filetype:pdf" "site:scribd.com Abhidhamma"
```

Useful settings:

- `SEARCH_ENGINE` / `--engine=` — `brave` (default), `bing`, `google`, or `duckduckgo`.
- `SEARCH_TERMS` — comma-separated search terms, replacing the defaults.
- `SEARCH_PAGES_PER_QUERY` — result pages per query (default `10`, roughly up to 100 results per query on Bing/Google; actual provider limits vary).
- `SEARCH_DELAY_MS` — delay between page requests (default `1000`).
- `SEARCH_MATCH_MODE` — `loose` (default) or `filename`.
- `SEARCH_USER_AGENT` — override the `User-Agent` header (engines serve a reduced page to obvious bots).
- `SEARCH_DEBUG_DIR` — save the HTML of pages whose results could not be parsed.
- `SEARCH_MAX_CONSECUTIVE_BLOCKED` — stop after this many blocked pages in a row (default `5`).
- `SEARCH_MAX_CONSECUTIVE_RATE_LIMITED` — stop after this many `HTTP 429`s in a row (default `3`).
- `SEARCH_BASE_URL` / `--base-url=` — override the engine URL, e.g. your own SearXNG instance.
- `MAX_PAGES` — pages per internal batch; the crawler automatically continues into further batches.
- `SEARCH_ENTRY_LIST_OUT`, `SEARCH_SCRIBD_LIST_OUT`, `SEARCH_CRAWLED_PAGES_OUT`, `SEARCH_PENDING_PAGES_OUT` — output/state paths.

Search progress persists in `search_crawled_pages.txt` and `search_pending_pages.txt`. Completed result pages are skipped on later runs, and failed pages remain pending for retry. Search-engine HTML and rate limits can vary; switch `SEARCH_ENGINE` if a provider blocks automated requests.

### Why a run can find nothing, and how to tell

Every result page is classified, and the run ends with a breakdown so an empty
result is never silent:

```
Processed 32 search page attempt(s):
  pages with results:            16
  pages the engine said were empty: 8
  pages blocked by the engine:   0
  pages with unparseable HTML:   8
```

- **engine said were empty** — the query really matched nothing. Recorded as done.
- **blocked** — a CAPTCHA / `HTTP 403` / JavaScript-required page, Google's cookie
  consent wall, or Brave's Cloudflare interstitial. Left pending for retry; after
  `SEARCH_MAX_CONSECUTIVE_BLOCKED` in a row the run stops.
- **rate limited (HTTP 429)** — see below.
- **unparseable HTML** — the page loaded but no results could be read from it,
  which means the engine changed its markup. These are **not** recorded as done,
  so they are retried instead of being lost. Set `SEARCH_DEBUG_DIR` to keep the
  HTML when this happens.

### Rate limiting

The big engines answer scripted access with `HTTP 429`, sometimes on the very
first page. The crawler now treats that as a signal to slow down rather than an
error to retry through:

- it honours the `Retry-After` header, with a minimum backoff of 5s, then 10s;
- every rate-limited page doubles the delay between requests for the rest of the
  run (capped at 60s);
- after `SEARCH_MAX_CONSECUTIVE_RATE_LIMITED` (default `3`) in a row the run
  stops, and every unfinished page stays queued for the next run.

So a run that hits a wall costs a few slow requests instead of hammering the
engine hundreds of times. To get going again: wait for the throttle to expire,
raise `--delay` (or `SEARCH_DELAY_MS`), lower `SEARCH_PAGES_PER_QUERY`, or
switch to `--engine=searx` / the default `mojeek`.

### Re-reading pages after a parser change

```sh
node js/searchbooks.js --reset-state
```

This clears `search_crawled_pages.txt` and `search_pending_pages.txt` (found
links are kept) and scans every result page again with the current parser. Use
it after upgrading the crawler, otherwise pages recorded by an older parser stay
marked as done.

### Tests

```sh
npm test
```

The Python tests in `python/test/` run offline and need no network access.

## 2. Dhammadownload site crawler (separate, site-specific case)

This follows pages on the supplied site, starting at the Abhidhamma page by default. It writes its own `entry_list.txt` and `scribd_links.txt` files:

```sh
node node/crawl_books.js
# or choose another starting page
node node/crawl_books.js https://www.dhammadownload.com/AbhidhammaInMyanmar.htm
node node/download.js entry_list.txt
```

Its progress is persisted separately in `crawled_pages.txt` and `pending_pages.txt`.

## 0b. Downloader — Python (recommended)

`python/download.py` consumes a crawler-generated TSV or a plain URL list:

```sh
cd python
uv run download.py                        # search_entry_list.txt
uv run download.py entry_list.txt         # a plain URL list
uv run download.py --concurrency=8 --verify-pdf
```

It downloads concurrently with `httpx`, retries with `tenacity` (exponential
backoff + jitter), shows progress with `tqdm`, and skips URLs already in
`.download-history.json`.

The useful part: **it rejects files that only pretend to be PDFs.** Plenty of
"PDF" links answer `200 OK` with an HTML login or error page, which is worse
than a failure because it looks like a success. A PDF header is accepted within
the first 1 KiB (some legacy files have a short preamble); if the header is
missing, `pikepdf` must be able to open the file. `dsite_download.py` then
linearizes/rewrites readable legacy PDFs. DOCX files must have the ZIP signature,
and `--verify-pdf` asks `pikepdf` to open every PDF. If a MediaFire share page
returns HTML with a usable Download button, the downloader follows its same-site
or MediaFire file link and validates the actual response. HTML with no usable
file link (for example, an expired-link or verification page) is still rejected.
Failures are listed at the end and stay downloadable on the next run.

`uv run download.py --help` for concurrency, timeout, delay and retry
options. Tests: `uv run pytest` (no network needed).

Scribd documents are not direct files — they need the vendored `scribdl`:

```sh
uv run python -m scribdl.scribdl "https://www.scribd.com/document/123/Title"
```

The Node downloader below still works, but the Python one is where new work happens.

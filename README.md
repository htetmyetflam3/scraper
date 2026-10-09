# Myanmar book link scraper

This repository has two independent crawlers (Python and Node) and one list-based downloader:

## 0. Search-engine crawler — Python (recommended)

`python/search_crawl.py` is the crawler to use. It reads results with CSS
selectors instead of hand-written HTML parsing, keeps cookies across requests
(the usual cause of a 403 on page two), and can drive a **real headless
Chromium** through Playwright when an engine refuses plain HTTP.

```sh
uv sync                                   # requests + beautifulsoup4
uv sync --extra browser                   # + playwright for --fetcher=browser
uv run playwright install chromium        # download the browser once

uv run python/search_crawl.py                          # Mojeek, default terms
uv run python/search_crawl.py --engine=searx
uv run python/search_crawl.py --fetcher=browser        # headless Chromium
uv run python/search_crawl.py --reset-state            # scan everything again
```

| Flag | Notes |
| --- | --- |
| `--engine` | `mojeek` (default), `searx`, `brave`, `bing`, `google`, `duckduckgo` |
| `--fetcher` | `http` (requests), `browser` (headless Chromium), `auto` (browser if available, else HTTP) |
| `--base-url` | point at your own SearXNG instance |
| `--delay`, `--jitter` | seconds between requests (default 2 + up to 0.5 random) |
| `--pages` | result pages per query (default 10) |
| `--reset-state` | clear the page history, keep found links |
| `--debug-dir` | save HTML of pages that could not be read |

It writes `search_entry_list.txt`, `search_scribd_links.txt`,
`search_crawled_pages.txt` and `search_pending_pages.txt` next to the script.
The row format is identical to the Node crawler's, so the downloader reads
either. Run the tests with `uv run python -m pytest python/test_search_crawl.py`
(no network needed — they use saved result pages).

If an engine answers `HTTP 403`, run with `--fetcher=browser`: a real browser
executes JavaScript, keeps cookies and has a genuine browser fingerprint. That
is the whole reason this crawler moved to Python — Playwright is far better at
this than anything in the Node setup.

The Node crawler below still works and shares the same fixtures and output
format, but the Python one is where new work happens.

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
| `mojeek` | Default. Independent index, no CAPTCHA for ordinary clients, tolerates polite scripted access. |
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
  HTML and add a fixture under `js/test/fixtures/` when this happens.

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

The parser is tested offline against saved result pages in
`js/test/fixtures/`, covering Mojeek, SearXNG, Brave, both Bing result layouts,
DuckDuckGo and Google (see `js/test/fixtures/README.md`). No network access is
needed.

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
uv run python/download.py                          # search_entry_list.txt
uv run python/download.py entry_list.txt           # the other crawler's output
uv run python/download.py --concurrency=8 --verify-pdf
```

It downloads concurrently with `httpx`, retries with `tenacity` (exponential
backoff + jitter), shows progress with `tqdm`, and skips URLs already in
`.download-history.json`.

The useful part: **it rejects files that only pretend to be PDFs.** Plenty of
"PDF" links answer `200 OK` with an HTML login or error page, which is worse
than a failure because it looks like a success. Every download is checked — a
PDF must start with `%PDF`, a DOCX must be a zip — and optionally opened with
`pikepdf` (`--verify-pdf`). Failures are listed at the end and stay downloadable
on the next run.

`python/download.py --help` for concurrency, timeout, delay and retry options.
Tests: `uv run pytest` (no network needed).

Scribd documents are not direct files — they need the vendored `scribdl`:

```sh
cd python && uv run python -m scribdl.scribdl "https://www.scribd.com/document/123/Title"
```

The Node downloader below still works, but the Python one is where new work happens.

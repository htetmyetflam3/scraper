# Myanmar book link scraper

This repository has two independent crawlers and one list-based downloader:

## 1. Search-engine crawler (general discovery)

This searches result pages from Bing by default. It does **not** start from or crawl `dhammadownload.com`; it collects matching direct PDF/DOCX results and records Scribd document URLs separately for manual use.

```sh
SEARCH_ENGINE=bing node node/search_crawl_books.js
node node/download.js search_entry_list.txt
```

Supported providers are `bing`, `google`, and `duckduckgo`:

```sh
SEARCH_ENGINE=google node node/search_crawl_books.js
SEARCH_ENGINE=duckduckgo node node/search_crawl_books.js
```

Default searches are generated from `Myanmar`, `Burmese`, `Burma`, and `မြန်မာ`, with PDF, DOCX, and Scribd-document searches for each term. A result enters `search_entry_list.txt` only when its **filename** is a PDF/DOCX and contains Myanmar, Burmese, Burma, or a Myanmar character in U+1000–U+1041. Scribd `/doc/` and `/document/` result URLs are stored separately in `search_scribd_links.txt` as title/URL/source-page TSV rows.

To use exact search queries, pass them as arguments or set `SEARCH_QUERIES` (separate multiple values with newlines or semicolons):

```sh
node node/search_crawl_books.js "Abhidhamma Myanmar filetype:pdf" "site:scribd.com/doc Abhidhamma"
```

Useful settings:

- `SEARCH_ENGINE` — `bing` (default), `google`, or `duckduckgo`.
- `SEARCH_TERMS` — comma-separated search terms, replacing the defaults.
- `SEARCH_PAGES_PER_QUERY` — result pages per query (default `5`).
- `SEARCH_DELAY_MS` — delay between page requests (default `1000`).
- `MAX_PAGES` — pages per internal batch; the crawler automatically continues into further batches.
- `SEARCH_ENTRY_LIST_OUT`, `SEARCH_SCRIBD_LIST_OUT`, `SEARCH_CRAWLED_PAGES_OUT`, `SEARCH_PENDING_PAGES_OUT` — output/state paths.

Search progress persists in `search_crawled_pages.txt` and `search_pending_pages.txt`. Completed result pages are skipped on later runs, and failed pages remain pending for retry. Search-engine HTML and rate limits can vary; switch `SEARCH_ENGINE` if a provider blocks automated requests.

## 2. Dhammadownload site crawler (separate, site-specific case)

This follows pages on the supplied site, starting at the Abhidhamma page by default. It writes its own `entry_list.txt` and `scribd_links.txt` files:

```sh
node node/crawl_books.js
# or choose another starting page
node node/crawl_books.js https://www.dhammadownload.com/AbhidhammaInMyanmar.htm
node node/download.js entry_list.txt
```

Its progress is persisted separately in `crawled_pages.txt` and `pending_pages.txt`.

## Downloader

`node/download.js` consumes a crawler-generated TSV or a plain URL list. With no argument it uses `search_entry_list.txt`; pass `entry_list.txt` to use the Dhammadownload crawl output. Downloaded files and URL history are stored in `dhammadownload_files/` by default. Already-downloaded URLs and existing same-named files are skipped.

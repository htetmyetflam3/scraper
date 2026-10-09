import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import {
  SEARCH_ENGINE_BASE_URLS,
  RESULTS_PER_PAGE_BY_ENGINE,
  buildQueries,
  classifySearchPage,
  extractSearchResults,
  fallbackBookName,
  isConsentRedirect,
  isCurrentEnginePage,
  isScribdDocumentUrl,
  makeSearchPageUrl,
  matchesBookCandidate,
} from "./search_results.js";

const args = process.argv.slice(2);
const flags = new Set(args.filter((arg) => arg.startsWith("--")));
const positionalArgs = args.filter((arg) => !arg.startsWith("--"));
const optionValue = (name) => {
  const match = args.find((arg) => arg.startsWith(`--${name}=`));
  return match ? match.slice(name.length + 3).trim() : "";
};

if (flags.has("--help") || flags.has("-h")) {
  console.log(`Search-engine book link crawler

Usage: node js/searchbooks.js [options] [query ...]

Options:
  --engine=NAME   Search engine: brave (default), bing, google, duckduckgo.
  --reset-state   Clear the crawled/pending page history and scan everything
                  again (found links are kept). Use this after upgrading: pages
                  recorded by an older parser are re-read with the current one.
  --help          Show this message.

Environment:
  SEARCH_ENGINE            brave (default), bing, google, or duckduckgo
  SEARCH_TERMS             comma-separated terms, replacing the defaults
  SEARCH_QUERIES           exact queries, newline or ; separated
  SEARCH_PAGES_PER_QUERY   result pages per query (default 10)
  SEARCH_DELAY_MS          delay between requests (default 1000)
  SEARCH_MATCH_MODE        loose (default) or filename
  SEARCH_USER_AGENT        override the User-Agent header
  SEARCH_DEBUG_DIR         save HTML of pages whose markup could not be parsed
  MAX_PAGES                attempts per internal batch (default 500)
`);
  process.exit(0);
}

const SEARCH_ENGINE = (optionValue("engine") || process.env.SEARCH_ENGINE || "brave").trim().toLowerCase();
if (!SEARCH_ENGINE_BASE_URLS[SEARCH_ENGINE]) {
  throw new Error(
    `Unsupported search engine "${SEARCH_ENGINE}". Use brave, bing, google, or duckduckgo.`,
  );
}
const SEARCH_BASE_URL = process.env.SEARCH_BASE_URL || SEARCH_ENGINE_BASE_URLS[SEARCH_ENGINE];
const SEARCH_PAGES_PER_QUERY = Math.max(
  1,
  Number.parseInt(process.env.SEARCH_PAGES_PER_QUERY || "10", 10) || 10,
);
const MAX_PAGES_PER_BATCH = Math.max(
  1,
  Number.parseInt(process.env.MAX_PAGES || "500", 10) || 500,
);
const DELAY_MS = Math.max(0, Number.parseInt(process.env.SEARCH_DELAY_MS || "1000", 10) || 0);
// Stop hammering the engine once it starts answering with interstitials.
const MAX_CONSECUTIVE_BLOCKED = Math.max(
  1,
  Number.parseInt(process.env.SEARCH_MAX_CONSECUTIVE_BLOCKED || "5", 10) || 5,
);
const TIMEOUT_MS = 60_000;
const RETRIES = 3;
const RESULTS_PER_PAGE = Number.parseInt(
  process.env.SEARCH_RESULTS_PER_PAGE || String(RESULTS_PER_PAGE_BY_ENGINE[SEARCH_ENGINE]),
  10,
) || RESULTS_PER_PAGE_BY_ENGINE[SEARCH_ENGINE];
const MATCH_MODE = (process.env.SEARCH_MATCH_MODE || "loose").trim().toLowerCase();
if (MATCH_MODE !== "loose" && MATCH_MODE !== "filename") {
  throw new Error(`Unsupported SEARCH_MATCH_MODE "${MATCH_MODE}". Use loose or filename.`);
}
// Search engines serve a reduced "lite" SERP to obvious bot agents, so ask for
// the ordinary desktop page. Override with SEARCH_USER_AGENT if you prefer to
// identify the crawler.
const USER_AGENT = (
  process.env.SEARCH_USER_AGENT ||
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
).trim();
const DEBUG_DIR = process.env.SEARCH_DEBUG_DIR ? path.resolve(process.env.SEARCH_DEBUG_DIR) : "";
const MAX_DEBUG_DUMPS = 20;
const DEFAULT_SEARCH_KEYWORDS = [
  "Burmese book PDF download link",
  "Burmese PDF",
  "Myanmar ဝတ္ထု",
  "မြန်မာစာ",
  "ဝတ္ထု",
  "ရသ",
  "ကဗျာများ free download",
  "မြန်မာစာပေ",
  "မြန်မာဝတ္တု",
  "မြန်မာဝတ္ထု",
  "သုတစာပေ",
  "ရသစာပေ",
  "အချစ်ဝတ္ထု",
  "စိတ်ကူးယဉ်ဝတ္ထု",
  "နာမည်ကြီးစာရေးဆရာများ၏ PDF download linkများ",
  "နာမည်ကြီးစာရေးဆရာများ",
  "မြန်မာစာအုပ် PDF download",
  "မြန်မာကဗျာများ free download",
  "Myanmar book PDF free download",
  "Burmese novel PDF download",
  "မြန်မာစာအုပ်များ download",
  "Myanmar",
  "Burmese",
  "Burma",
  "မြန်မာ",
  "Myanmar PDF free download",
  "Burma book PDF download",
  "Burmese book free download",
];

const ENTRY_LIST_PATH = path.resolve(process.env.SEARCH_ENTRY_LIST_OUT || "search_entry_list.txt");
const SCRIBD_LIST_PATH = path.resolve(process.env.SEARCH_SCRIBD_LIST_OUT || "search_scribd_links.txt");
const CRAWLED_PAGES_PATH = path.resolve(
  process.env.SEARCH_CRAWLED_PAGES_OUT || "search_crawled_pages.txt",
);
const PENDING_PAGES_PATH = path.resolve(
  process.env.SEARCH_PENDING_PAGES_OUT || "search_pending_pages.txt",
);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function cleanTsvCell(value) {
  return String(value || "").replace(/[\t\r\n]+/g, " ").trim();
}

function recordFromUrl(url, title, source) {
  return {
    name: cleanTsvCell(title) || fallbackBookName(url),
    url,
    source: cleanTsvCell(source),
  };
}

function readBookList(filePath) {
  const records = new Map();
  if (!existsSync(filePath)) return records;
  const lines = readFileSync(filePath, "utf8").split(/\r?\n/).filter((line) => line.trim());
  if (!lines.length) return records;
  const header = lines[0].split("\t").map((cell) => cell.trim().toLowerCase());
  const urlIndex = header.indexOf("url");
  const nameIndex = header.indexOf("book name");
  const sourceIndex = header.indexOf("source page");
  if (urlIndex < 0 || nameIndex < 0) return records;
  for (const line of lines.slice(1)) {
    const columns = line.split("\t");
    const url = columns[urlIndex]?.trim();
    if (!url) continue;
    records.set(url, recordFromUrl(url, columns[nameIndex], columns[sourceIndex]));
  }
  return records;
}

function writeBookList(filePath, records) {
  mkdirSync(path.dirname(filePath), { recursive: true });
  const rows = [...records.values()].sort((a, b) => a.url.localeCompare(b.url));
  const lines = ["Book Name\tURL\tSource Page"];
  for (const record of rows) {
    lines.push(`${cleanTsvCell(record.name)}\t${record.url}\t${cleanTsvCell(record.source)}`);
  }
  writeFileSync(filePath, `${lines.join("\n")}\n`, "utf8");
}

function readUrlList(filePath) {
  if (!existsSync(filePath)) return [];
  return readFileSync(filePath, "utf8")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
}

function writeUrlList(filePath, urls) {
  mkdirSync(path.dirname(filePath), { recursive: true });
  const unique = [...new Set(urls)].sort((a, b) => a.localeCompare(b));
  writeFileSync(filePath, unique.length ? `${unique.join("\n")}\n` : "", "utf8");
}

// Older runs stored the result's site line ("scribd.com https://… › document")
// as the book name. A real title is better, so those get replaced on re-crawl.
function isWeakName(name, url) {
  const value = String(name || "").trim();
  return !value || value === fallbackBookName(url) || value.includes("://") || value.includes("›");
}

function upsertRecord(records, url, title, source) {
  const next = recordFromUrl(url, title, source);
  const previous = records.get(url);
  if (!previous || (!isWeakName(next.name, url) && isWeakName(previous.name, url))) {
    records.set(url, next);
  }
}

function queriesToSearch() {
  const commandLineQueries = positionalArgs.map((query) => query.trim()).filter(Boolean);
  if (commandLineQueries.length) return [...new Set(commandLineQueries)];

  const explicit = (process.env.SEARCH_QUERIES || "")
    .split(/\r?\n|;/)
    .map((query) => query.trim())
    .filter(Boolean);
  if (explicit.length) return [...new Set(explicit)];

  const terms = process.env.SEARCH_TERMS
    ? process.env.SEARCH_TERMS.split(",").map((term) => term.trim()).filter(Boolean)
    : DEFAULT_SEARCH_KEYWORDS;
  // Broad discovery, format-focused searches, and a Scribd search per term.
  return buildQueries(terms);
}

async function fetchWithRetry(url) {
  let lastError;
  for (let attempt = 1; attempt <= RETRIES; attempt++) {
    try {
      const response = await fetch(url, {
        headers: {
          "User-Agent": USER_AGENT,
          Accept: "text/html,application/xhtml+xml,*/*;q=0.8",
          "Accept-Language": "en-US,en;q=0.9",
        },
        redirect: "follow",
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`);
      return response;
    } catch (error) {
      lastError = error;
      if (attempt < RETRIES) await sleep(800 * attempt);
    }
  }
  throw lastError;
}

let debugDumps = 0;
function saveDebugHtml(pageUrl, html) {
  if (!DEBUG_DIR || debugDumps >= MAX_DEBUG_DUMPS) return null;
  mkdirSync(DEBUG_DIR, { recursive: true });
  const label = pageUrl.replace(/[^a-z\d]+/gi, "_").slice(-80) || "page";
  const filePath = path.join(DEBUG_DIR, `${String(debugDumps).padStart(2, "0")}-${label}.html`);
  writeFileSync(filePath, html, "utf8");
  debugDumps++;
  return filePath;
}

async function crawlSearchResults() {
  const queries = queriesToSearch();
  if (!queries.length) throw new Error("No search queries were configured.");
  const generatedPages = queries.flatMap((query) =>
    Array.from({ length: SEARCH_PAGES_PER_QUERY }, (_, pageNumber) =>
      makeSearchPageUrl(query, pageNumber, {
        engine: SEARCH_ENGINE,
        baseUrl: SEARCH_BASE_URL,
        resultsPerPage: RESULTS_PER_PAGE,
      }),
    ),
  );

  console.log(`Search engine: ${SEARCH_ENGINE} (${SEARCH_BASE_URL})`);
  console.log(`Queries: ${queries.length}; pages per query: ${SEARCH_PAGES_PER_QUERY}.`);
  console.log(`Book match mode: ${MATCH_MODE}.`);
  console.log("Collecting matching PDF/DOCX results and Scribd document links; not crawling result websites.");

  const fileLinks = readBookList(ENTRY_LIST_PATH);
  const scribdLinks = readBookList(SCRIBD_LIST_PATH);
  const resetState = flags.has("--reset-state");
  const completedPages = resetState ? new Set() : new Set(readUrlList(CRAWLED_PAGES_PATH));
  // A reset drops the queued pages too: they belong to the previous query set,
  // and pages recorded by an older parser are exactly what we want to re-read.
  const pendingPages = resetState
    ? []
    : [...new Set(readUrlList(PENDING_PAGES_PATH))]
      .filter((url) => isCurrentEnginePage(url, { engine: SEARCH_ENGINE, baseUrl: SEARCH_BASE_URL }))
      .filter((url) => !completedPages.has(url));
  const pendingSet = new Set(pendingPages);
  for (const url of generatedPages) {
    if (!completedPages.has(url) && !pendingSet.has(url)) {
      pendingSet.add(url);
      pendingPages.push(url);
    }
  }
  if (resetState) {
    console.log("Reset requested: previously recorded search pages will be re-read.");
  }

  const failedThisRun = new Set();
  const stats = {
    withResults: 0,
    noResults: 0,
    blocked: 0,
    unparsed: 0,
    fetchFailed: 0,
    nonHtml: 0,
    fallbackParses: 0,
    rejectedFiles: 0,
  };
  let totalAttempts = 0;
  let batchAttempts = 0;
  let consecutiveBlocked = 0;
  writeUrlList(PENDING_PAGES_PATH, pendingPages);

  while (pendingPages.length) {
    let pageIndex = pendingPages.findIndex((url) => !completedPages.has(url) && !failedThisRun.has(url));
    if (batchAttempts >= MAX_PAGES_PER_BATCH) {
      if (pageIndex < 0) break;
      console.log(`Completed a ${MAX_PAGES_PER_BATCH}-page batch; continuing automatically.`);
      batchAttempts = 0;
      pageIndex = pendingPages.findIndex((url) => !completedPages.has(url) && !failedThisRun.has(url));
    }
    if (pageIndex < 0) break;

    const pageUrl = pendingPages[pageIndex];
    totalAttempts++;
    batchAttempts++;
    console.log(`Search page attempt ${totalAttempts}: ${pageUrl}`);

    try {
      const response = await fetchWithRetry(pageUrl);
      const contentType = (response.headers.get("content-type") || "").toLowerCase();
      const finalPageUrl = response.url || pageUrl;
      if (contentType && !contentType.includes("html") && !contentType.includes("xhtml")) {
        await response.body?.cancel();
        stats.nonHtml++;
        completedPages.add(pageUrl);
        completedPages.add(finalPageUrl);
        pendingPages.splice(pageIndex, 1);
        writeUrlList(CRAWLED_PAGES_PATH, completedPages);
        writeUrlList(PENDING_PAGES_PATH, pendingPages);
        console.log(`  Skipped: not an HTML response (${contentType || "unknown content type"}).`);
        await sleep(DELAY_MS);
        continue;
      }

      const html = await response.text();
      const { results, usedFallback } = extractSearchResults(html, finalPageUrl, SEARCH_ENGINE);
      // Only interesting when the fallback actually rescued a page: pages with
      // no results at all always end up in the fallback path.
      if (usedFallback && results.length) stats.fallbackParses++;

      let fileCount = 0;
      let scribdCount = 0;
      let rejectedCount = 0;
      for (const result of results) {
        if (isScribdDocumentUrl(result.url)) {
          if (!scribdLinks.has(result.url)) scribdCount++;
          upsertRecord(scribdLinks, result.url, result.title, finalPageUrl);
        } else if (matchesBookCandidate(result, MATCH_MODE)) {
          if (!fileLinks.has(result.url)) fileCount++;
          upsertRecord(fileLinks, result.url, result.title, finalPageUrl);
        } else if (/\.(pdf|docx)(?:[?#]|$)/i.test(result.url)) {
          rejectedCount++;
        }
      }
      stats.rejectedFiles += rejectedCount;

      const status = classifySearchPage(html, results.length, {
        consentRedirect: isConsentRedirect(finalPageUrl, SEARCH_ENGINE),
      });
      console.log(
        `  Results parsed: ${results.length}; new files: ${fileCount}; new Scribd: ${scribdCount}` +
          `${rejectedCount ? `; PDF/DOCX skipped as unrelated: ${rejectedCount}` : ""}.`,
      );

      if (status === "results") {
        stats.withResults++;
        consecutiveBlocked = 0;
      } else if (status === "no-results") {
        stats.noResults++;
        consecutiveBlocked = 0;
        console.log("  Engine reported no results for this query page.");
      } else {
        // "empty" or "blocked": nothing usable was fetched, so do not record
        // the page as done. It stays pending and is retried on the next run,
        // instead of being silently lost like a query that found nothing.
        if (status === "blocked") stats.blocked++;
        else stats.unparsed++;
        const dumpPath = saveDebugHtml(finalPageUrl, html);
        console.warn(
          `  No results could be parsed (${status}); page left pending for retry.` +
            `${dumpPath ? ` HTML saved to ${dumpPath}` : " Set SEARCH_DEBUG_DIR to save the HTML for inspection."}`,
        );
        failedThisRun.add(pageUrl);
        writeUrlList(PENDING_PAGES_PATH, pendingPages);
        if (status === "blocked") {
          consecutiveBlocked++;
          if (consecutiveBlocked >= MAX_CONSECUTIVE_BLOCKED) {
            console.warn(
              `\n${consecutiveBlocked} consecutive blocked pages: ${SEARCH_ENGINE} is refusing requests. ` +
                "Stopping this run; the remaining pages stay queued. Raise SEARCH_DELAY_MS, switch " +
                "SEARCH_ENGINE, or try again later.",
            );
            break;
          }
        } else {
          consecutiveBlocked = 0;
        }
        await sleep(DELAY_MS);
        continue;
      }

      completedPages.add(pageUrl);
      completedPages.add(finalPageUrl);
      pendingPages.splice(pageIndex, 1);
      writeBookList(ENTRY_LIST_PATH, fileLinks);
      writeBookList(SCRIBD_LIST_PATH, scribdLinks);
      writeUrlList(CRAWLED_PAGES_PATH, completedPages);
      writeUrlList(PENDING_PAGES_PATH, pendingPages);
    } catch (error) {
      stats.fetchFailed++;
      failedThisRun.add(pageUrl);
      writeUrlList(PENDING_PAGES_PATH, pendingPages);
      console.warn(`Search page failed: ${pageUrl} (${error.message})`);
    }

    await sleep(DELAY_MS);
  }

  writeBookList(ENTRY_LIST_PATH, fileLinks);
  writeBookList(SCRIBD_LIST_PATH, scribdLinks);
  writeUrlList(CRAWLED_PAGES_PATH, completedPages);
  writeUrlList(PENDING_PAGES_PATH, pendingPages);

  console.log(`\nProcessed ${totalAttempts} search page attempt(s):`);
  console.log(`  pages with results:            ${stats.withResults}`);
  console.log(`  pages the engine said were empty: ${stats.noResults}`);
  console.log(`  pages blocked by the engine:   ${stats.blocked}`);
  console.log(`  pages with unparseable HTML:   ${stats.unparsed}`);
  console.log(`  non-HTML responses skipped:    ${stats.nonHtml}`);
  console.log(`  pages that failed to download: ${stats.fetchFailed}`);
  if (stats.fallbackParses) {
    console.log(
      `  ${stats.fallbackParses} page(s) parsed with the generic link fallback: the engine's result markup may have changed.`,
    );
  }
  if (stats.rejectedFiles) {
    console.log(
      `  ${stats.rejectedFiles} PDF/DOCX link(s) were skipped as not Burmese-related (match mode: ${MATCH_MODE}).`,
    );
  }
  console.log(`Matching PDF/DOCX links: ${fileLinks.size} (${ENTRY_LIST_PATH})`);
  console.log(`Scribd document links: ${scribdLinks.size} (${SCRIBD_LIST_PATH})`);
  console.log(`Completed search pages: ${completedPages.size} (${CRAWLED_PAGES_PATH})`);
  if (pendingPages.length) {
    const retryCount = pendingPages.filter((url) => failedThisRun.has(url)).length;
    console.warn(`${retryCount} failed search page(s) remain in ${PENDING_PAGES_PATH}; they will be retried next run.`);
  }
}

crawlSearchResults().catch((error) => {
  console.error(error.message);
  console.error("Usage: node js/searchbooks.js [--engine=brave|bing|google|duckduckgo] [query ...]");
  process.exitCode = 1;
});

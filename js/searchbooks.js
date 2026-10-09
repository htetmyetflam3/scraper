import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const SEARCH_ENGINE = (process.env.SEARCH_ENGINE || "bing").trim().toLowerCase();
const SEARCH_BASE_URLS = {
  bing: "https://www.bing.com/search",
  duckduckgo: "https://html.duckduckgo.com/html/",
  google: "https://www.google.com/search",
};
if (!SEARCH_BASE_URLS[SEARCH_ENGINE]) {
  throw new Error(`Unsupported SEARCH_ENGINE "${SEARCH_ENGINE}". Use bing, duckduckgo, or google.`);
}
const SEARCH_BASE_URL = process.env.SEARCH_BASE_URL || SEARCH_BASE_URLS[SEARCH_ENGINE];
const SEARCH_PAGES_PER_QUERY = Math.max(
  1,
  Number.parseInt(process.env.SEARCH_PAGES_PER_QUERY || "10", 10) || 10,
);
const MAX_PAGES_PER_BATCH = Math.max(
  1,
  Number.parseInt(process.env.MAX_PAGES || "500", 10) || 500,
);
const DELAY_MS = Math.max(0, Number.parseInt(process.env.SEARCH_DELAY_MS || "1000", 10) || 0);
const TIMEOUT_MS = 60_000;
const RETRIES = 3;
const RESULTS_PER_PAGE = SEARCH_ENGINE === "duckduckgo" ? 30 : 10;
const USER_AGENT = "Mozilla/5.0 (compatible; dhamma-search-results-crawler/1.0)";
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

function decodeHtmlEntities(value) {
  const namedEntities = {
    amp: "&",
    apos: "'",
    gt: ">",
    lt: "<",
    nbsp: " ",
    quot: '"',
  };
  return value.replace(
    /&(?:#x([\da-f]+);?|#(\d+);?|([a-z][a-z\d]+);)/gi,
    (match, hex, decimal, named) => {
      if (hex || decimal) {
        const codePoint = Number.parseInt(hex || decimal, hex ? 16 : 10);
        return Number.isFinite(codePoint) && codePoint <= 0x10ffff
          ? String.fromCodePoint(codePoint)
          : match;
      }
      return namedEntities[named.toLowerCase()] ?? match;
    },
  );
}

function getAttribute(tag, name) {
  const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = tag.match(
    new RegExp(`(?:^|\\s)${escapedName}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s"'=<>\\x60]+))`, "i"),
  );
  return match ? (match[1] ?? match[2] ?? match[3] ?? "") : null;
}

function cleanLabel(value) {
  return decodeHtmlEntities(value
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " "))
    .trim();
}

function parseAnchorTags(markup) {
  const results = [];
  const anchorPattern = /<a\b[^>]*>[\s\S]*?<\/a\s*>/gi;
  for (const match of markup.matchAll(anchorPattern)) {
    const tag = match[0].match(/^<a\b[^>]*>/i)?.[0];
    if (!tag) continue;
    const href = getAttribute(tag, "href");
    if (!href) continue;
    const title = cleanLabel(match[0].slice(tag.length).replace(/<\/a\s*>$/i, ""));
    results.push({ href: decodeHtmlEntities(href.trim()), title, tag, markup: match[0] });
  }
  return results;
}

function decodeBingRedirect(value) {
  const encoded = value.startsWith("a1") ? value.slice(2) : value;
  try {
    const decoded = Buffer.from(encoded, "base64url").toString("utf8");
    return /^https?:\/\//i.test(decoded) ? decoded : null;
  } catch {
    return null;
  }
}

function unwrapResultUrl(rawHref, pageUrl) {
  let url;
  try {
    url = new URL(decodeHtmlEntities(rawHref), pageUrl);
  } catch {
    return null;
  }

  for (let depth = 0; depth < 3; depth++) {
    const host = url.hostname.toLowerCase();
    let target = null;
    if (host.includes("duckduckgo.com") || (url.pathname.endsWith("/l/") && url.searchParams.has("uddg"))) {
      target = url.searchParams.get("uddg");
    } else if (
      host === "google.com" || host.endsWith(".google.com") ||
      (url.pathname === "/url" && (url.searchParams.has("url") || url.searchParams.has("q")))
    ) {
      target = url.searchParams.get("url") || url.searchParams.get("q");
    } else if ((host === "bing.com" || host.endsWith(".bing.com")) && url.searchParams.has("u")) {
      target = decodeBingRedirect(url.searchParams.get("u"));
    }
    if (!target) break;
    try {
      url = new URL(decodeHtmlEntities(target), url);
    } catch {
      return null;
    }
  }

  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  url.hash = "";
  return url.href;
}

function extractSearchResults(html, pageUrl) {
  let anchors = [];
  if (SEARCH_ENGINE === "duckduckgo") {
    anchors = parseAnchorTags(html).filter(({ tag }) =>
      /\bresult__a\b/i.test(getAttribute(tag, "class") || ""),
    );
  } else if (SEARCH_ENGINE === "bing") {
    const blocks = [...html.matchAll(
      /<li\b[^>]*class\s*=\s*(?:"[^"]*\bb_algo\b[^"]*"|'[^']*\bb_algo\b[^']*')[^>]*>[\s\S]*?<\/li\s*>/gi,
    )].map((match) => match[0]);
    anchors = blocks.flatMap((block) => parseAnchorTags(block).slice(0, 1));
  } else {
    anchors = parseAnchorTags(html).filter(({ markup }) => /<h3\b/i.test(markup));
  }

  const seen = new Set();
  const results = [];
  for (const anchor of anchors) {
    const url = unwrapResultUrl(anchor.href, pageUrl);
    if (!url || seen.has(url)) continue;
    seen.add(url);
    results.push({ url, title: anchor.title });
  }
  return results;
}

function getFilename(url) {
  let pathname = new URL(url).pathname;
  try {
    pathname = decodeURIComponent(pathname);
  } catch {
    // Keep the encoded path if it contains a malformed escape.
  }
  return path.posix.basename(pathname);
}

function fallbackBookName(url) {
  return getFilename(url).replace(/\.(pdf|docx)$/i, "") || url;
}

function matchesBookFilename(url) {
  const filename = getFilename(url);
  const extension = path.posix.extname(filename).toLowerCase();
  if (extension !== ".pdf" && extension !== ".docx") return false;
  const stem = filename.slice(0, -extension.length);
  return /myanmar|burmese|burma/i.test(stem) || /[\u1000-\u1041]/u.test(stem);
}

function isScribdDocumentUrl(url) {
  const parsed = new URL(url);
  const hostname = parsed.hostname.toLowerCase().replace(/^www\./, "");
  return (hostname === "scribd.com" || hostname.endsWith(".scribd.com")) &&
    /^\/(?:doc|document)\//i.test(parsed.pathname);
}

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

function upsertRecord(records, url, title, source) {
  const next = recordFromUrl(url, title, source);
  const previous = records.get(url);
  if (!previous || previous.name === fallbackBookName(url)) records.set(url, next);
}

function queriesToSearch() {
  const commandLineQueries = process.argv.slice(2).map((query) => query.trim()).filter(Boolean);
  if (commandLineQueries.length) return [...new Set(commandLineQueries)];

  const explicit = (process.env.SEARCH_QUERIES || "")
    .split(/\r?\n|;/)
    .map((query) => query.trim())
    .filter(Boolean);
  if (explicit.length) return [...new Set(explicit)];

  const terms = process.env.SEARCH_TERMS
    ? process.env.SEARCH_TERMS.split(",").map((term) => term.trim()).filter(Boolean)
    : DEFAULT_SEARCH_KEYWORDS;
  const queries = [];
  for (const term of terms) {
    // Include broad discovery as well as format-focused and Scribd searches.
    queries.push(term);
    queries.push(`${term} filetype:pdf`);
    queries.push(`${term} filetype:docx`);
    queries.push(`site:scribd.com/doc ${term}`);
  }
  return [...new Set(queries)];
}

function makeSearchPageUrl(query, pageNumber) {
  const url = new URL(SEARCH_BASE_URL);
  url.searchParams.set("q", query);
  if (SEARCH_ENGINE === "bing") {
    url.searchParams.set("count", String(RESULTS_PER_PAGE));
    url.searchParams.set("first", String(pageNumber * RESULTS_PER_PAGE + 1));
  } else if (SEARCH_ENGINE === "duckduckgo") {
    url.searchParams.set("s", String(pageNumber * RESULTS_PER_PAGE));
  } else {
    url.searchParams.set("start", String(pageNumber * RESULTS_PER_PAGE));
  }
  return url.href;
}

function isCurrentEnginePage(value) {
  try {
    return new URL(value).hostname.toLowerCase() === new URL(SEARCH_BASE_URL).hostname.toLowerCase();
  } catch {
    return false;
  }
}

async function fetchWithRetry(url) {
  let lastError;
  for (let attempt = 1; attempt <= RETRIES; attempt++) {
    try {
      const response = await fetch(url, {
        headers: {
          "User-Agent": USER_AGENT,
          Accept: "text/html,application/xhtml+xml,*/*;q=0.8",
        },
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

async function crawlSearchResults() {
  const queries = queriesToSearch();
  if (!queries.length) throw new Error("No search queries were configured.");
  const generatedPages = queries.flatMap((query) =>
    Array.from({ length: SEARCH_PAGES_PER_QUERY }, (_, pageNumber) =>
      makeSearchPageUrl(query, pageNumber),
    ),
  );

  console.log(`Search engine: ${SEARCH_ENGINE} (${SEARCH_BASE_URL})`);
  console.log(`Queries: ${queries.length}; pages per query: ${SEARCH_PAGES_PER_QUERY}.`);
  console.log("Collecting matching PDF/DOCX results and Scribd document links; not crawling result websites.");

  const fileLinks = readBookList(ENTRY_LIST_PATH);
  const scribdLinks = readBookList(SCRIBD_LIST_PATH);
  const completedPages = new Set(readUrlList(CRAWLED_PAGES_PATH));
  const pendingPages = [...new Set(readUrlList(PENDING_PAGES_PATH))]
    .filter((url) => isCurrentEnginePage(url) && !completedPages.has(url));
  const pendingSet = new Set(pendingPages);
  for (const url of generatedPages) {
    if (!completedPages.has(url) && !pendingSet.has(url)) {
      pendingSet.add(url);
      pendingPages.push(url);
    }
  }

  const failedThisRun = new Set();
  let totalAttempts = 0;
  let batchAttempts = 0;
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
        completedPages.add(pageUrl);
        completedPages.add(finalPageUrl);
        pendingPages.splice(pageIndex, 1);
        writeUrlList(CRAWLED_PAGES_PATH, completedPages);
        writeUrlList(PENDING_PAGES_PATH, pendingPages);
        continue;
      }

      const html = await response.text();
      let fileCount = 0;
      let scribdCount = 0;
      for (const result of extractSearchResults(html, finalPageUrl)) {
        if (isScribdDocumentUrl(result.url)) {
          if (!scribdLinks.has(result.url)) scribdCount++;
          upsertRecord(scribdLinks, result.url, result.title, finalPageUrl);
        } else if (matchesBookFilename(result.url)) {
          if (!fileLinks.has(result.url)) fileCount++;
          upsertRecord(fileLinks, result.url, result.title, finalPageUrl);
        }
      }

      completedPages.add(pageUrl);
      completedPages.add(finalPageUrl);
      pendingPages.splice(pageIndex, 1);
      writeBookList(ENTRY_LIST_PATH, fileLinks);
      writeBookList(SCRIBD_LIST_PATH, scribdLinks);
      writeUrlList(CRAWLED_PAGES_PATH, completedPages);
      writeUrlList(PENDING_PAGES_PATH, pendingPages);
      console.log(`  Matching files added: ${fileCount}; Scribd document links added: ${scribdCount}.`);
    } catch (error) {
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
  console.log(`\nProcessed ${totalAttempts} search page attempt(s).`);
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
  console.error("Usage: SEARCH_ENGINE=bing|google|duckduckgo node node/search_crawl_books.js [query ...]");
  process.exitCode = 1;
});

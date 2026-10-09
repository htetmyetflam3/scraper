import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const DEFAULT_ENTRY_URL =
  "https://www.dhammadownload.com/AbhidhammaInMyanmar.htm";
const ENTRY_URL = process.argv[2] || DEFAULT_ENTRY_URL;
const DELAY_MS = 500;
const TIMEOUT_MS = 120_000;
const RETRIES = 3;
// Page attempts per batch; the crawler automatically continues in more batches.
const MAX_PAGES = Math.max(1, Number.parseInt(process.env.MAX_PAGES || "500", 10) || 500);
// Default to the entry page's topic to avoid unrelated navigation; set to 1 for the whole site.
const CRAWL_ALL_SITE = process.env.CRAWL_ALL_SITE === "1";
const USER_AGENT = "Mozilla/5.0 (compatible; dhamma-book-link-crawler/1.0)";

const ENTRY_LIST_PATH = path.resolve(process.env.ENTRY_LIST_OUT || "entry_list.txt");
const SCRIBD_LIST_PATH = path.resolve(process.env.SCRIBD_LIST_OUT || "scribd_links.txt");
const CRAWLED_PAGES_PATH = path.resolve(process.env.CRAWLED_PAGES_OUT || "crawled_pages.txt");
const PENDING_PAGES_PATH = path.resolve(process.env.PENDING_PAGES_OUT || "pending_pages.txt");
const SKIP_PAGE_EXTENSIONS = new Set([
  ".7z", ".apk", ".avi", ".bmp", ".css", ".csv", ".doc", ".docx", ".epub",
  ".exe", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".js", ".json", ".m4a",
  ".m4v", ".mid", ".midi", ".mkv", ".mov", ".mp3", ".mp4", ".mpeg", ".mpg",
  ".odp", ".ods", ".odt", ".ogg", ".ogv", ".pdf", ".png", ".ppt", ".pptx",
  ".rar", ".rss", ".rtf", ".svg", ".tar", ".tgz", ".txt", ".wav", ".webm",
  ".webp", ".woff", ".woff2", ".xls", ".xlsx", ".xml", ".zip",
]);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function parseHttpUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`Invalid URL: ${value}`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`Only http:// and https:// URLs are supported: ${value}`);
  }
  url.hash = "";
  return url;
}

function decodeURIComponentSafely(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function decodeHtmlEntities(value) {
  const namedEntities = { amp: "&", apos: "'", gt: ">", lt: "<", nbsp: " ", quot: '"' };
  return value.replace(
    /&(?:#x([\da-f]+);?|#(\d+);?|([a-z][a-z\d]+);)/gi,
    (match, hex, decimal, named) => {
      const codePoint = hex
        ? Number.parseInt(hex, 16)
        : decimal
          ? Number.parseInt(decimal, 10)
          : null;
      if (codePoint !== null) {
        return Number.isFinite(codePoint) && codePoint <= 0x10ffff
          ? String.fromCodePoint(codePoint)
          : match;
      }
      return namedEntities[named.toLowerCase()] ?? match;
    },
  );
}

function getAttribute(tag, attributeName) {
  const match = tag.match(
    new RegExp(`(?:^|\\s)${attributeName}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s"'=<>\\x60]+))`, "i"),
  );
  return match ? (match[1] ?? match[2] ?? match[3] ?? "") : null;
}

function cleanLabel(value) {
  return decodeHtmlEntities(value.replace(/<[^>]*>/g, " "))
    .replace(/\s+/g, " ")
    .trim();
}

function labelFromAnchor(markup, tag, startAfterTag) {
  const title = getAttribute(tag, "title") || getAttribute(tag, "aria-label");
  const closeIndex = markup.toLowerCase().indexOf("</a", startAfterTag);
  const inner = closeIndex >= 0 ? markup.slice(startAfterTag, closeIndex) : "";
  const text = cleanLabel(inner.replace(/<br\b[^>]*>/gi, " "));
  if (text) return text;
  if (title) return cleanLabel(title);

  const imageTag = inner.match(/<img\b[^>]*>/i)?.[0];
  const imageAlt = imageTag && getAttribute(imageTag, "alt");
  return imageAlt ? cleanLabel(imageAlt) : "";
}

function getDocumentBase(html, pageUrl) {
  const baseTag = html.match(/<base\b[^>]*>/i)?.[0];
  const href = baseTag && getAttribute(baseTag, "href");
  if (!href) return pageUrl;
  try {
    return new URL(decodeHtmlEntities(href.trim()), pageUrl).href;
  } catch {
    return pageUrl;
  }
}

function extractLinks(html, pageUrl) {
  const markup = html
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, "");
  const baseUrl = getDocumentBase(markup, pageUrl);
  const links = new Map();
  const tags = /<(a|area|iframe|frame|embed|object|source)\b[^>]*>/gi;

  for (const match of markup.matchAll(tags)) {
    const tag = match[0];
    const tagName = match[1].toLowerCase();
    const attribute = tagName === "object"
      ? "data"
      : ["iframe", "frame", "embed", "source"].includes(tagName)
        ? "src"
        : "href";
    const raw = getAttribute(tag, attribute);
    if (!raw) continue;

    try {
      const url = new URL(decodeHtmlEntities(raw.trim()), baseUrl);
      if (url.protocol !== "http:" && url.protocol !== "https:") continue;
      url.hash = "";
      const name = tagName === "a"
        ? labelFromAnchor(markup, tag, match.index + tag.length)
        : getAttribute(tag, "title") || getAttribute(tag, "aria-label") || "";
      links.set(url.href, { url: url.href, name: cleanLabel(name) });
    } catch {
      // Ignore malformed and non-HTTP links.
    }
  }

  return [...links.values()];
}

function decodedPathname(url) {
  return decodeURIComponentSafely(url.pathname).toLowerCase();
}

function filenameFromUrl(url) {
  return path.posix.basename(decodeURIComponentSafely(url.pathname));
}

function fallbackBookName(url) {
  return filenameFromUrl(url).replace(/\.(pdf|docx)$/i, "") || url.href;
}

function normalizedHostname(hostname) {
  return hostname.toLowerCase().replace(/^www\./, "");
}

function isSameSite(url, entryUrl) {
  return normalizedHostname(url.hostname) === normalizedHostname(entryUrl.hostname);
}

function inferTopic(entryUrl) {
  if (CRAWL_ALL_SITE) return null;
  const filename = path.posix.basename(decodeURIComponentSafely(entryUrl.pathname));
  const stem = filename.replace(/\\.[^.]+$/, "");
  const words = stem.match(/[A-Z]+(?=[A-Z][a-z]|\\b)|[A-Z]?[a-z]+|\\d+/g) || [];
  const ignored = new Set(["default", "home", "index", "in", "page", "the"]);
  return words.map((word) => word.toLowerCase()).find((word) => word.length >= 6 && !ignored.has(word)) || null;
}

function isCrawlablePage(url, entryUrl, topic) {
  if (!isSameSite(url, entryUrl)) return false;
  const pathname = decodedPathname(url);
  const extension = path.posix.extname(pathname);
  if (extension && SKIP_PAGE_EXTENSIONS.has(extension)) return false;
  return !topic || pathname.includes(topic);
}

function isScribdUrl(url) {
  const hostname = url.hostname.toLowerCase();
  return hostname === "scribd.com" || hostname.endsWith(".scribd.com");
}

function matchesBookFilename(url) {
  const filename = filenameFromUrl(url);
  const extension = path.posix.extname(filename).toLowerCase();
  if (extension !== ".pdf" && extension !== ".docx") return false;

  const stem = filename.slice(0, -extension.length);
  const matchesLanguageName = /myanmar|burmese|burma/i.test(stem);
  // Match the supplied Myanmar Unicode range anywhere in the filename stem.
  const containsMyanmarCharacter = /[\u1000-\u1041]/u.test(stem);
  return matchesLanguageName || containsMyanmarCharacter;
}

async function fetchWithRetry(url, referer) {
  let lastError;
  for (let attempt = 1; attempt <= RETRIES; attempt++) {
    try {
      const headers = { "User-Agent": USER_AGENT, Accept: "text/html,application/xhtml+xml,*/*;q=0.8" };
      if (referer) headers.Referer = referer;
      const response = await fetch(url, {
        headers,
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

function cleanTsvCell(value) {
  return String(value || "").replace(/[\t\r\n]+/g, " ").trim();
}

function recordFromUrl(url, name = "", source = "") {
  return {
    name: cleanTsvCell(name) || fallbackBookName(new URL(url)),
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
  const headerUrlIndex = header.indexOf("url");
  if (headerUrlIndex >= 0 && header.includes("book name")) {
    const nameIndex = header.indexOf("book name");
    const sourceIndex = header.indexOf("source page");
    for (const line of lines.slice(1)) {
      const columns = line.split("\t");
      const url = columns[headerUrlIndex]?.trim();
      if (!url) continue;
      records.set(url, recordFromUrl(url, columns[nameIndex], columns[sourceIndex]));
    }
    return records;
  }

  // Also load older one-URL-per-line entry lists.
  for (const line of lines) {
    if (line.trim().startsWith("#")) continue;
    const columns = line.split(/\s+\|\s+|\t+/).map((cell) => cell.trim());
    const urlIndex = columns.findIndex((cell) => /^https?:\/\//i.test(cell));
    if (urlIndex < 0) continue;
    const url = columns[urlIndex];
    records.set(url, recordFromUrl(url, columns[urlIndex + 1], columns[urlIndex + 2]));
  }
  return records;
}

function readUrlList(filePath) {
  if (!existsSync(filePath)) return [];
  return readFileSync(filePath, "utf8")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
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

function writeUrlList(filePath, urls) {
  mkdirSync(path.dirname(filePath), { recursive: true });
  const lines = [...new Set(urls)].sort((a, b) => a.localeCompare(b));
  writeFileSync(filePath, lines.length ? `${lines.join("\n")}\n` : "", "utf8");
}

function upsertRecord(records, url, name, source) {
  const next = recordFromUrl(url, name, source);
  const previous = records.get(url);
  if (!previous || (previous.name === fallbackBookName(new URL(url)) && next.name)) {
    records.set(url, next);
  }
}

async function crawl(entryUrl) {
  const topic = inferTopic(entryUrl);
  console.log(`Crawl scope: ${topic ? `same-site pages containing "${topic}"` : "all same-site pages"}.`);
  const fileLinks = readBookList(ENTRY_LIST_PATH);
  const scribdLinks = readBookList(SCRIBD_LIST_PATH);
  const completedPages = new Set(readUrlList(CRAWLED_PAGES_PATH));
  const pendingPages = [...new Set(readUrlList(PENDING_PAGES_PATH))].filter((url) => {
    if (completedPages.has(url)) return false;
    try {
      return isCrawlablePage(new URL(url), entryUrl, topic);
    } catch {
      return false;
    }
  });
  if (!completedPages.has(entryUrl.href) && !pendingPages.includes(entryUrl.href)) {
    pendingPages.unshift(entryUrl.href);
  }

  const queuedPages = new Set(pendingPages);
  const newlyCompleted = new Set();
  const attemptedThisBatch = new Set();
  const failedThisRun = new Set();
  let totalAttempts = 0;
  let batchAttempts = 0;
  writeUrlList(PENDING_PAGES_PATH, pendingPages);

  while (pendingPages.length) {
    let pageIndex = pendingPages.findIndex((url) =>
      !completedPages.has(url) &&
      !failedThisRun.has(url) &&
      !attemptedThisBatch.has(url),
    );

    if (batchAttempts >= MAX_PAGES) {
      if (pageIndex < 0) break;
      console.log(`Completed a ${MAX_PAGES}-page batch; continuing automatically with the remaining queue.`);
      batchAttempts = 0;
      attemptedThisBatch.clear();
      pageIndex = pendingPages.findIndex((url) =>
        !completedPages.has(url) && !failedThisRun.has(url),
      );
    }
    if (pageIndex < 0) break;

    const pageUrl = pendingPages[pageIndex];
    attemptedThisBatch.add(pageUrl);
    totalAttempts++;
    batchAttempts++;
    console.log(`Page attempt ${totalAttempts}: ${pageUrl}`);

    try {
      const response = await fetchWithRetry(pageUrl);
      const contentType = (response.headers.get("content-type") || "").toLowerCase();
      const finalPageUrl = response.url || pageUrl;

      if (contentType && !contentType.includes("html") && !contentType.includes("xhtml")) {
        await response.body?.cancel();
        completedPages.add(pageUrl);
        completedPages.add(finalPageUrl);
        newlyCompleted.add(pageUrl);
        pendingPages.splice(pageIndex, 1);
        writeUrlList(CRAWLED_PAGES_PATH, completedPages);
        writeUrlList(PENDING_PAGES_PATH, pendingPages);
        await sleep(DELAY_MS);
        continue;
      }

      const html = await response.text();
      let fileCount = 0;
      let scribdCount = 0;
      for (const link of extractLinks(html, finalPageUrl)) {
        const url = new URL(link.url);
        const name = link.name || fallbackBookName(url);

        if (isScribdUrl(url)) {
          if (!scribdLinks.has(url.href)) scribdCount++;
          upsertRecord(scribdLinks, url.href, name, finalPageUrl);
          continue;
        }

        if (matchesBookFilename(url)) {
          if (!fileLinks.has(url.href)) fileCount++;
          upsertRecord(fileLinks, url.href, name, finalPageUrl);
          continue;
        }

        if (
          isCrawlablePage(url, entryUrl, topic) &&
          !completedPages.has(url.href) &&
          !queuedPages.has(url.href)
        ) {
          queuedPages.add(url.href);
          pendingPages.push(url.href);
        }
      }

      completedPages.add(pageUrl);
      completedPages.add(finalPageUrl);
      newlyCompleted.add(pageUrl);
      pendingPages.splice(pageIndex, 1);
      writeBookList(ENTRY_LIST_PATH, fileLinks);
      writeBookList(SCRIBD_LIST_PATH, scribdLinks);
      writeUrlList(CRAWLED_PAGES_PATH, completedPages);
      writeUrlList(PENDING_PAGES_PATH, pendingPages);
      console.log(`  Matched files: ${fileCount}; Scribd links: ${scribdCount}.`);
    } catch (error) {
      // Leave failures in the saved queue, but don't retry them endlessly this run.
      failedThisRun.add(pageUrl);
      writeUrlList(PENDING_PAGES_PATH, pendingPages);
      console.warn(`Page fetch failed: ${pageUrl} (${error.message})`);
    }

    await sleep(DELAY_MS);
  }

  writeBookList(ENTRY_LIST_PATH, fileLinks);
  writeBookList(SCRIBD_LIST_PATH, scribdLinks);
  writeUrlList(CRAWLED_PAGES_PATH, completedPages);
  writeUrlList(PENDING_PAGES_PATH, pendingPages);

  console.log(`\nProcessed ${totalAttempts} page attempt(s); ${newlyCompleted.size} new page(s) completed.`);
  console.log(`Total matched PDF/DOCX links: ${fileLinks.size} (${ENTRY_LIST_PATH})`);
  console.log(`Total Scribd links: ${scribdLinks.size} (${SCRIBD_LIST_PATH})`);
  if (scribdLinks.size === 0) {
    console.log("No Scribd URLs found in the pages scanned so far; the Scribd TSV contains its header only.");
  }
  console.log(`Completed page history: ${completedPages.size} (${CRAWLED_PAGES_PATH})`);
  if (pendingPages.length) {
    const retryCount = pendingPages.filter((url) => failedThisRun.has(url)).length;
    console.warn(`${retryCount} failed page(s) remain in ${PENDING_PAGES_PATH}; they will be retried on the next run.`);
  }
}

async function main() {
  await crawl(parseHttpUrl(ENTRY_URL));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

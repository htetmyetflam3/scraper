import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const DEFAULT_ENTRY_URL =
  "https://www.dhammadownload.com/AbhidhammaInMyanmar.htm";
const ENTRY_URL = process.argv[2] || DEFAULT_ENTRY_URL;
const DELAY_MS = 500;
const TIMEOUT_MS = 120_000;
const RETRIES = 3;
const MAX_PAGES = Number.parseInt(process.env.MAX_PAGES || "500", 10);
const USER_AGENT = "Mozilla/5.0 (compatible; dhamma-book-link-crawler/1.0)";

const ENTRY_LIST_PATH = path.resolve(process.env.ENTRY_LIST_OUT || "entry_list.txt");
const SCRIBD_LIST_PATH = path.resolve(process.env.SCRIBD_LIST_OUT || "scribd_links.txt");
const CRAWLED_PAGES_PATH = path.resolve(process.env.CRAWLED_PAGES_OUT || "crawled_pages.txt");
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
  const links = new Set();
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
      links.add(url.href);
    } catch {
      // Ignore malformed and non-HTTP links.
    }
  }

  return [...links];
}

function decodedPathname(url) {
  return decodeURIComponentSafely(url.pathname).toLowerCase();
}

function normalizedHostname(hostname) {
  return hostname.toLowerCase().replace(/^www\./, "");
}

function isSameSite(url, entryUrl) {
  return normalizedHostname(url.hostname) === normalizedHostname(entryUrl.hostname);
}

function isCrawlablePage(url, entryUrl) {
  if (!isSameSite(url, entryUrl)) return false;
  const extension = path.posix.extname(decodedPathname(url));
  return !extension || !SKIP_PAGE_EXTENSIONS.has(extension);
}

function isScribdUrl(url) {
  const hostname = url.hostname.toLowerCase();
  return hostname === "scribd.com" || hostname.endsWith(".scribd.com");
}

function matchesBookFilename(url) {
  const filename = path.posix.basename(decodeURIComponentSafely(url.pathname));
  const extension = path.posix.extname(filename).toLowerCase();
  if (extension !== ".pdf" && extension !== ".docx") return false;

  const stem = filename.slice(0, -extension.length);
  const matchesMyanmarName = /myanmar/i.test(stem);
  const matchesBurmeseName = /burmese/i.test(stem);
  // Interprets the supplied range as Myanmar code points U+1000 through U+1040.
  const startsWithMyanmarCharacter = /^[\u1000-\u1040]/u.test(stem);
  return matchesMyanmarName || matchesBurmeseName || startsWithMyanmarCharacter;
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

function writeUrlList(filePath, urls) {
  mkdirSync(path.dirname(filePath), { recursive: true });
  const lines = [...urls].sort((a, b) => a.localeCompare(b));
  writeFileSync(filePath, lines.length ? `${lines.join("\n")}\n` : "", "utf8");
}

async function crawl(entryUrl) {
  const pendingPages = [{ url: entryUrl.href, referer: null }];
  const queuedPages = new Set([entryUrl.href]);
  const visitedPages = new Set();
  const fileLinks = new Set();
  const scribdLinks = new Set();

  while (pendingPages.length && visitedPages.size < MAX_PAGES) {
    const { url: pageUrl, referer } = pendingPages.shift();
    if (visitedPages.has(pageUrl)) continue;
    visitedPages.add(pageUrl);
    console.log(`Page ${visitedPages.size}/${MAX_PAGES}: ${pageUrl}`);

    try {
      const response = await fetchWithRetry(pageUrl, referer);
      const contentType = (response.headers.get("content-type") || "").toLowerCase();
      if (contentType && !contentType.includes("html") && !contentType.includes("xhtml")) {
        await response.body?.cancel();
        await sleep(DELAY_MS);
        continue;
      }

      const html = await response.text();
      const finalPageUrl = response.url || pageUrl;
      let pageFileCount = 0;
      let pageScribdCount = 0;
      let pageCount = 0;

      for (const link of extractLinks(html, finalPageUrl)) {
        const url = new URL(link);
        if (isScribdUrl(url)) {
          if (!scribdLinks.has(url.href)) {
            scribdLinks.add(url.href);
            pageScribdCount++;
          }
          continue;
        }

        if (matchesBookFilename(url)) {
          if (!fileLinks.has(url.href)) {
            fileLinks.add(url.href);
            pageFileCount++;
          }
          continue;
        }

        if (
          isCrawlablePage(url, entryUrl) &&
          !visitedPages.has(url.href) &&
          !queuedPages.has(url.href)
        ) {
          queuedPages.add(url.href);
          pendingPages.push({ url: url.href, referer: finalPageUrl });
          pageCount++;
        }
      }

      console.log(
        `  Matching files: ${pageFileCount}; Scribd links: ${pageScribdCount}; related pages queued: ${pageCount}.`,
      );
    } catch (error) {
      console.warn(`Page fetch failed: ${pageUrl} (${error.message})`);
    }

    await sleep(DELAY_MS);
  }

  if (pendingPages.length) {
    console.warn(`Reached the ${MAX_PAGES}-page safety limit; ${pendingPages.length} page(s) remain.`);
  }

  return { fileLinks, scribdLinks, visitedPages };
}

async function main() {
  const entryUrl = parseHttpUrl(ENTRY_URL);
  const { fileLinks, scribdLinks, visitedPages } = await crawl(entryUrl);

  writeUrlList(ENTRY_LIST_PATH, fileLinks);
  writeUrlList(SCRIBD_LIST_PATH, scribdLinks);
  writeUrlList(CRAWLED_PAGES_PATH, visitedPages);

  console.log(`\nVisited ${visitedPages.size} page(s).`);
  console.log(`Matched ${fileLinks.size} PDF/DOCX link(s): ${ENTRY_LIST_PATH}`);
  console.log(`Found ${scribdLinks.size} Scribd link(s): ${SCRIBD_LIST_PATH}`);
  console.log(`Visited page list: ${CRAWLED_PAGES_PATH}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

import { mkdirSync, createWriteStream, existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { pipeline } from "node:stream/promises";
import crypto from "node:crypto";

// One entry page is enough: the crawler follows relevant pages from it.
const DEFAULT_ENTRY_URL =
  "https://www.dhammadownload.com/AbhidhammaInMyanmar.htm";
const ENTRY_URL = process.argv[2] || DEFAULT_ENTRY_URL;
const OUT_DIR = path.resolve(
  process.env.DOWNLOAD_DIR || process.env.PDF_OUT_DIR || "dhammadownload_files",
);
const TIMEOUT_MS = 120_000;
const RETRIES = 3;
const DELAY_MS = 500;
const MAX_PAGES = 200;
const USER_AGENT = "Mozilla/5.0 (compatible; dhamma-pdf-crawler/1.0)";

const NON_PAGE_EXTENSIONS = new Set([
  ".7z",
  ".apk",
  ".avi",
  ".bmp",
  ".css",
  ".csv",
  ".doc",
  ".docx",
  ".epub",
  ".exe",
  ".gif",
  ".gz",
  ".ico",
  ".jpeg",
  ".jpg",
  ".js",
  ".json",
  ".m4a",
  ".m4v",
  ".mid",
  ".midi",
  ".mkv",
  ".mov",
  ".mp3",
  ".mp4",
  ".mpeg",
  ".mpg",
  ".odp",
  ".ods",
  ".odt",
  ".ogg",
  ".ogv",
  ".pdf",
  ".png",
  ".ppt",
  ".pptx",
  ".rar",
  ".rss",
  ".rtf",
  ".svg",
  ".tar",
  ".tgz",
  ".txt",
  ".wav",
  ".webm",
  ".webp",
  ".woff",
  ".woff2",
  ".xls",
  ".xlsx",
  ".xml",
  ".zip",
]);

mkdirSync(OUT_DIR, { recursive: true });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function parseEntryUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`Invalid entry URL: ${value}`);
  }

  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("The entry URL must use http:// or https://.");
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
      if (hex) {
        const codePoint = Number.parseInt(hex, 16);
        return Number.isFinite(codePoint) && codePoint <= 0x10ffff
          ? String.fromCodePoint(codePoint)
          : match;
      }
      if (decimal) {
        const codePoint = Number.parseInt(decimal, 10);
        return Number.isFinite(codePoint) && codePoint <= 0x10ffff
          ? String.fromCodePoint(codePoint)
          : match;
      }
      return namedEntities[named.toLowerCase()] ?? match;
    },
  );
}

function getAttribute(tag, attributeName) {
  const escapedName = attributeName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(
    `(?:^|\\s)${escapedName}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s"'=<>\\x60]+))`,
    "i",
  );
  const match = tag.match(pattern);
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
  // Strip comments and script/style contents so markup-like strings are not
  // mistaken for real links. The target is a static .htm site, so reading the
  // relevant URL-bearing elements is sufficient and avoids a runtime package.
  const markup = html
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, "");
  const documentBase = getDocumentBase(markup, pageUrl);
  const links = new Set();
  const tagPattern = /<(a|area|iframe|frame|embed|object|source)\b[^>]*>/gi;

  for (const match of markup.matchAll(tagPattern)) {
    const tag = match[0];
    const tagName = match[1].toLowerCase();
    const attributeName = tagName === "object" ? "data" :
      tagName === "iframe" || tagName === "frame" || tagName === "embed" || tagName === "source"
        ? "src"
        : "href";
    const rawLink = getAttribute(tag, attributeName);
    if (!rawLink) continue;

    try {
      const url = new URL(decodeHtmlEntities(rawLink.trim()), documentBase);
      if (url.protocol !== "http:" && url.protocol !== "https:") continue;
      url.hash = "";
      links.add(url.href);
    } catch {
      // Ignore malformed or intentionally non-URL hrefs.
    }
  }

  return [...links];
}

function getDecodedPathname(url) {
  return decodeURIComponentSafely(url.pathname).toLowerCase();
}

function isPdfUrl(url) {
  return getDecodedPathname(url).endsWith(".pdf");
}

function isDocxUrl(url) {
  return getDecodedPathname(url).endsWith(".docx");
}

function isDownloadableUrl(url) {
  return isPdfUrl(url) || isDocxUrl(url);
}

function normalizedHostname(hostname) {
  return hostname.toLowerCase().replace(/^www\./, "");
}

function isSameSite(url, entryUrl) {
  // The target site serves its pages on www and its PDFs from the apex host.
  return normalizedHostname(url.hostname) === normalizedHostname(entryUrl.hostname);
}

function inferPageScope(entryUrl) {
  const filename = path.posix.basename(decodeURIComponentSafely(entryUrl.pathname));
  const stem = filename.replace(/\.[^.]+$/, "");
  const words = stem.match(/[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+/g) || [];
  const ignoredWords = new Set([
    "default",
    "home",
    "index",
    "in",
    "page",
    "the",
  ]);
  const topic = words
    .map((word) => word.toLowerCase())
    .find((word) => word.length >= 6 && !ignoredWords.has(word));

  // For a descriptive entry-page filename, follow pages about that subject
  // without restricting the crawl to any particular language.
  return topic ? { topic } : null;
}

function isRelevantPage(url, entryUrl, scope) {
  if (!isSameSite(url, entryUrl)) return false;

  const extension = path.posix.extname(getDecodedPathname(url));
  if (extension && NON_PAGE_EXTENSIONS.has(extension)) return false;

  if (scope && !getDecodedPathname(url).includes(scope.topic)) return false;

  return true;
}

function safeFilename(name) {
  return name
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, "_")
    .replace(/\s+/g, " ")
    .replace(/[. ]+$/g, "")
    .trim();
}

function guessFilenameFromUrl(fileUrl) {
  const base = decodeURIComponentSafely(fileUrl.pathname.split("/").pop() || "download.pdf");
  return safeFilename(base) || "download.pdf";
}

function parseContentDispositionFilename(contentDisposition) {
  if (!contentDisposition) return null;

  const encoded = contentDisposition.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
  if (encoded) {
    return safeFilename(decodeURIComponentSafely(encoded[1].trim().replace(/^"|"$/g, "")));
  }

  const filename =
    contentDisposition.match(/filename\s*=\s*"([^"]+)"/i) ||
    contentDisposition.match(/filename\s*=\s*([^;]+)/i);
  return filename ? safeFilename(filename[1].trim()) : null;
}

function filenameFromResponse(response, requestUrl) {
  const dispositionName = parseContentDispositionFilename(
    response.headers.get("content-disposition"),
  );
  return dispositionName || guessFilenameFromUrl(new URL(response.url || requestUrl));
}

async function fetchWithRetry(url, { referer } = {}) {
  let lastError;

  for (let attempt = 1; attempt <= RETRIES; attempt++) {
    try {
      const headers = {
        "User-Agent": USER_AGENT,
        Accept: "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
      };
      if (referer) headers.Referer = referer;

      const response = await fetch(url, {
        headers,
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status} ${response.statusText}`);
      }
      return response;
    } catch (error) {
      lastError = error;
      if (attempt < RETRIES) await sleep(800 * attempt);
    }
  }

  throw lastError;
}

function supportedContentExtension(contentType) {
  if (contentType.includes("pdf")) return ".pdf";
  if (contentType.includes("officedocument.wordprocessingml.document")) return ".docx";
  return "";
}

async function saveFileResponse(response, fileUrl) {
  const contentType = (response.headers.get("content-type") || "").toLowerCase();
  const filename = filenameFromResponse(response, fileUrl);
  const filenameExtension = path.extname(filename).toLowerCase();
  const namedExtension = filenameExtension === ".pdf" || filenameExtension === ".docx"
    ? filenameExtension
    : "";
  const url = new URL(fileUrl);
  const urlExtension = isPdfUrl(url) ? ".pdf" : isDocxUrl(url) ? ".docx" : "";
  const extension = namedExtension || urlExtension || supportedContentExtension(contentType);

  if (!extension || contentType.includes("text/html")) {
    console.warn(`Skip (response is not a PDF/DOCX): ${fileUrl} -> ${contentType || "unknown type"}`);
    await response.body?.cancel();
    return false;
  }

  const safeName = namedExtension
    ? safeFilename(filename)
    : `${safeFilename(filename.replace(/\.[^.]+$/, "")) || "download"}${extension}`;
  let outPath = path.join(OUT_DIR, safeName);

  if (existsSync(outPath)) {
    const hash = crypto.createHash("sha1").update(fileUrl).digest("hex").slice(0, 8);
    const outputExtension = path.extname(safeName) || extension;
    const outputBasename = safeName.slice(0, safeName.length - outputExtension.length);
    outPath = path.join(OUT_DIR, `${outputBasename}__${hash}${outputExtension}`);
    if (existsSync(outPath)) {
      console.log(`Already downloaded: ${outPath}`);
      await response.body?.cancel();
      return false;
    }
  }

  if (!response.body) {
    console.warn(`Skip (empty response body): ${fileUrl}`);
    return false;
  }

  await pipeline(response.body, createWriteStream(outPath, { flags: "wx" }));
  console.log(`Saved: ${outPath}`);
  return true;
}

async function downloadFile(fileUrl, referer) {
  const response = await fetchWithRetry(fileUrl, { referer });
  return saveFileResponse(response, fileUrl);
}

async function crawl(entryUrl, initialReferer = null) {
  const pageScope = inferPageScope(entryUrl);
  // Keep the source page with each link for sites that check the Referer header.
  const downloadUrls = new Map();
  const pendingPages = [{ url: entryUrl.href, referer: initialReferer }];
  const queuedPages = new Set([entryUrl.href]);
  const visitedPages = new Set();

  while (pendingPages.length > 0 && visitedPages.size < MAX_PAGES) {
    const { url: pageUrl, referer } = pendingPages.shift();
    if (visitedPages.has(pageUrl)) continue;
    visitedPages.add(pageUrl);

    console.log(`Page ${visitedPages.size}/${MAX_PAGES}: ${pageUrl}`);

    try {
      const response = await fetchWithRetry(pageUrl, { referer });
      const contentType = (response.headers.get("content-type") || "").toLowerCase();

      // A page-like URL can still be a PDF endpoint (for example, download.php).
      const responseFilename = filenameFromResponse(response, pageUrl).toLowerCase();
      const isDocxResponse = contentType.includes("officedocument.wordprocessingml.document");
      if (
        contentType.includes("pdf") ||
        isDocxResponse ||
        /\.(pdf|docx)$/i.test(responseFilename) ||
        isDownloadableUrl(new URL(pageUrl))
      ) {
        await saveFileResponse(response, pageUrl);
        await sleep(DELAY_MS);
        continue;
      }

      if (contentType && !contentType.includes("html") && !contentType.includes("xhtml")) {
        await response.body?.cancel();
        await sleep(DELAY_MS);
        continue;
      }

      const html = await response.text();
      const finalPageUrl = new URL(response.url || pageUrl);
      const pageLinks = extractLinks(html, finalPageUrl.href);
      let pageFileCount = 0;
      let queuedPageCount = 0;

      for (const link of pageLinks) {
        const url = new URL(link);
        if (isDownloadableUrl(url)) {
          if (!downloadUrls.has(url.href)) {
            downloadUrls.set(url.href, finalPageUrl.href);
            pageFileCount++;
          }
          continue;
        }

        if (
          isRelevantPage(url, entryUrl, pageScope) &&
          !visitedPages.has(url.href) &&
          !queuedPages.has(url.href)
        ) {
          queuedPages.add(url.href);
          pendingPages.push({ url: url.href, referer: finalPageUrl.href });
          queuedPageCount++;
        }
      }

      console.log(`  Found ${pageFileCount} PDF/DOCX link(s); queued ${queuedPageCount} related page(s).`);
    } catch (error) {
      console.warn(`Page fetch failed: ${pageUrl} (${error.message})`);
    }

    await sleep(DELAY_MS);
  }

  if (pendingPages.length > 0) {
    console.warn(`Reached the ${MAX_PAGES}-page safety limit; ${pendingPages.length} page(s) remain.`);
  }

  console.log(`\nFound ${downloadUrls.size} unique PDF/DOCX link(s). Downloading...\n`);
  let downloaded = 0;

  for (const [fileUrl, fileReferer] of downloadUrls) {
    console.log(`Downloading: ${fileUrl}`);
    try {
      if (await downloadFile(fileUrl, fileReferer)) downloaded++;
    } catch (error) {
      console.warn(`Download failed: ${fileUrl} (${error.message})`);
    }
    await sleep(DELAY_MS);
  }

  console.log(`\nDone. Visited ${visitedPages.size} page(s); saved ${downloaded} file(s) to ${OUT_DIR}.`);
}

function readEntryItems(input) {
  if (!/^https?:\/\//i.test(input) && existsSync(input)) {
    const lines = readFileSync(input, "utf8")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#"));
    if (lines.length === 0) throw new Error(`No URL entries found in ${input}`);

    const header = lines[0].split("\t").map((cell) => cell.trim().toLowerCase());
    const urlIndex = header.indexOf("url");
    if (urlIndex >= 0) {
      const sourceIndex = header.indexOf("source page");
      const firstDataRow = lines[0].includes("\t") ? 1 : 0;
      const items = lines.slice(firstDataRow).map((line) => {
        const columns = line.split("\t");
        return {
          url: columns[urlIndex]?.trim(),
          referer: (sourceIndex >= 0 ? columns[sourceIndex] : "")?.trim() || DEFAULT_ENTRY_URL,
        };
      }).filter((item) => item.url);
      if (items.length === 0) throw new Error(`No URL entries found in ${input}`);
      return items;
    }

    return lines.map((line) => {
      const columns = line.split(/\s+\|\s+|\t+/).map((cell) => cell.trim());
      const entryIndex = columns.findIndex((cell) => /^https?:\/\//i.test(cell));
      if (entryIndex < 0) throw new Error(`No URL found in entry: ${line}`);
      const possibleReferer = columns[entryIndex + 1];
      return {
        url: columns[entryIndex],
        referer: /^https?:\/\//i.test(possibleReferer || "") ? possibleReferer : DEFAULT_ENTRY_URL,
      };
    });
  }
  return [{ url: input, referer: null }];
}

async function main() {
  const entries = readEntryItems(ENTRY_URL);
  const pageUrls = new Map();
  const fileUrls = new Map();

  for (const item of entries) {
    const url = parseEntryUrl(item.url);
    if (isDownloadableUrl(url)) {
      if (!fileUrls.has(url.href)) fileUrls.set(url.href, item.referer);
    } else if (!pageUrls.has(url.href)) {
      pageUrls.set(url.href, item.referer);
    }
  }

  for (const [fileUrl, referer] of fileUrls) {
    console.log(`Downloading listed file: ${fileUrl}`);
    try {
      await downloadFile(fileUrl, referer);
    } catch (error) {
      console.warn(`Download failed: ${fileUrl} (${error.message})`);
    }
    await sleep(DELAY_MS);
  }

  for (const [pageUrl, referer] of pageUrls) {
    await crawl(parseEntryUrl(pageUrl), referer);
  }
}

main().catch((error) => {
  console.error(error.message);
  console.error("Usage: node download.js [entry-url|entry-list.txt]");
  process.exitCode = 1;
});

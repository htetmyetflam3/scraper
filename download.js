import { mkdirSync, createWriteStream, existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pipeline } from "node:stream/promises";
import crypto from "node:crypto";

// Consume a crawler-generated URL list by default. Pass entry_list.txt to download
// the separate Dhammadownload-site crawl results instead.
const DEFAULT_ENTRY_LIST = process.env.DOWNLOAD_LIST || "search_entry_list.txt";
const ENTRY_INPUT = process.argv[2] || DEFAULT_ENTRY_LIST;
const OUT_DIR = path.resolve(
  process.env.DOWNLOAD_DIR || process.env.PDF_OUT_DIR || "dhammadownload_files",
);
const TIMEOUT_MS = 120_000;
const RETRIES = 3;
const DOWNLOAD_CONCURRENCY = Math.max(1, Number.parseInt(process.env.DOWNLOAD_CONCURRENCY || "4", 10) || 4);
const DOWNLOAD_DELAY_MS = Math.max(0, Number.parseInt(process.env.DOWNLOAD_DELAY_MS || "200", 10) || 0);
const USER_AGENT = "Mozilla/5.0 (compatible; dhamma-book-list-downloader/1.0)";


mkdirSync(OUT_DIR, { recursive: true });
const DOWNLOAD_HISTORY_PATH = path.join(OUT_DIR, ".download-history.json");

function normalizeDownloadUrl(value) {
  const url = new URL(value);
  url.hash = "";
  return url.href;
}

function loadDownloadHistory() {
  try {
    const entries = JSON.parse(readFileSync(DOWNLOAD_HISTORY_PATH, "utf8"));
    return new Map(Array.isArray(entries) ? entries : []);
  } catch {
    return new Map();
  }
}

const downloadedFiles = loadDownloadHistory();
const outputOwners = new Map();
for (const [url, filename] of downloadedFiles) {
  if (!outputOwners.has(path.basename(filename))) outputOwners.set(path.basename(filename), url);
}
const reservedOutPaths = new Map();
const inFlightDownloads = new Map();

function persistDownloadHistory() {
  writeFileSync(DOWNLOAD_HISTORY_PATH, JSON.stringify([...downloadedFiles], null, 2), "utf8");
}

function wasDownloaded(fileUrl) {
  const key = normalizeDownloadUrl(fileUrl);
  const filename = downloadedFiles.get(key);
  if (!filename) return false;
  if (existsSync(path.join(OUT_DIR, path.basename(filename)))) return true;
  downloadedFiles.delete(key);
  if (outputOwners.get(path.basename(filename)) === key) outputOwners.delete(path.basename(filename));
  return false;
}

function rememberDownloaded(fileUrl, responseUrl, filename) {
  const cleanFilename = path.basename(filename);
  const primaryKey = normalizeDownloadUrl(fileUrl);
  downloadedFiles.set(primaryKey, cleanFilename);
  if (responseUrl) downloadedFiles.set(normalizeDownloadUrl(responseUrl), cleanFilename);
  if (!outputOwners.has(cleanFilename)) outputOwners.set(cleanFilename, primaryKey);
  persistDownloadHistory();
}

function recordDownloaded(fileUrl, responseUrl, outputPath) {
  rememberDownloaded(fileUrl, responseUrl, outputPath);
}

function findLegacyDownloadedPath(fileUrl) {
  const key = normalizeDownloadUrl(fileUrl);
  const filename = guessFilenameFromUrl(new URL(fileUrl));
  const extension = path.extname(filename).toLowerCase();
  if (extension !== ".pdf" && extension !== ".docx") return null;

  const initialPath = path.join(OUT_DIR, filename);
  const initialOwner = outputOwners.get(filename);
  if (existsSync(initialPath) && (!initialOwner || initialOwner === key)) return initialPath;

  const outputExtension = path.extname(filename);
  const basename = filename.slice(0, filename.length - outputExtension.length);
  const hash = crypto.createHash("sha1").update(key).digest("hex").slice(0, 10);
  const hashedName = `${basename}__${hash}${outputExtension}`;
  const hashedPath = path.join(OUT_DIR, hashedName);
  const hashedOwner = outputOwners.get(hashedName);
  if (existsSync(hashedPath) && (!hashedOwner || hashedOwner === key)) return hashedPath;
  return null;
}

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

function getDecodedPathname(url) {
  return decodeURIComponentSafely(url.pathname).toLowerCase();
}

function isPdfUrl(url) {
  return getDecodedPathname(url).endsWith(".pdf");
}

function isDocxUrl(url) {
  return getDecodedPathname(url).endsWith(".docx");
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

function reserveOutputPath(filename, fileUrl, extension) {
  const key = normalizeDownloadUrl(fileUrl);
  const initialPath = path.join(OUT_DIR, filename);
  const initialOwner = outputOwners.get(filename) || reservedOutPaths.get(initialPath);

  if (existsSync(initialPath)) {
    // Legacy output without a URL-history record: prefer skipping it over
    // downloading the same-looking file again under a new name.
    if (!initialOwner || initialOwner === key) {
      return { path: initialPath, alreadyDownloaded: true, legacy: !initialOwner };
    }
  } else if (reservedOutPaths.has(initialPath)) {
    if (initialOwner === key) return { path: initialPath, alreadyDownloaded: true };
  } else {
    reservedOutPaths.set(initialPath, key);
    return { path: initialPath, alreadyDownloaded: false };
  }

  const outputExtension = path.extname(filename) || extension;
  const basename = filename.slice(0, filename.length - outputExtension.length);
  const hash = crypto.createHash("sha1").update(key).digest("hex").slice(0, 10);
  let suffix = 0;
  while (true) {
    const extra = suffix === 0 ? hash : `${hash}_${suffix}`;
    const candidateName = `${basename}__${extra}${outputExtension}`;
    const candidate = path.join(OUT_DIR, candidateName);
    const owner = outputOwners.get(candidateName) || reservedOutPaths.get(candidate);

    if (existsSync(candidate)) {
      if (owner === key || (!owner && suffix === 0)) {
        return { path: candidate, alreadyDownloaded: true, legacy: !owner };
      }
      suffix++;
      continue;
    }
    if (reservedOutPaths.has(candidate)) {
      if (owner === key) return { path: candidate, alreadyDownloaded: true };
      suffix++;
      continue;
    }

    reservedOutPaths.set(candidate, key);
    return { path: candidate, alreadyDownloaded: false };
  }
}

async function saveFileResponse(response, fileUrl) {
  if (wasDownloaded(fileUrl)) {
    const filename = downloadedFiles.get(normalizeDownloadUrl(fileUrl));
    console.log(`Already downloaded: ${path.join(OUT_DIR, path.basename(filename))}`);
    await response.body?.cancel();
    return false;
  }

  const responseUrl = response.url || fileUrl;
  if (responseUrl !== fileUrl && wasDownloaded(responseUrl)) {
    const filename = downloadedFiles.get(normalizeDownloadUrl(responseUrl));
    rememberDownloaded(fileUrl, responseUrl, filename);
    console.log(`Already downloaded (redirect alias): ${path.join(OUT_DIR, path.basename(filename))}`);
    await response.body?.cancel();
    return false;
  }

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

  if (!response.body) {
    console.warn(`Skip (empty response body): ${fileUrl}`);
    return false;
  }

  const safeName = namedExtension
    ? safeFilename(filename)
    : `${safeFilename(filename.replace(/\.[^.]+$/, "")) || "download"}${extension}`;
  const reservation = reserveOutputPath(safeName, fileUrl, extension);
  if (reservation.alreadyDownloaded) {
    if (reservation.legacy) {
      rememberDownloaded(fileUrl, response.url, reservation.path);
    }
    console.log(`Already downloaded: ${reservation.path}`);
    await response.body.cancel();
    return false;
  }

  try {
    await pipeline(response.body, createWriteStream(reservation.path, { flags: "wx" }));
    recordDownloaded(fileUrl, response.url, reservation.path);
    console.log(`Saved: ${reservation.path}`);
    return true;
  } finally {
    reservedOutPaths.delete(reservation.path);
  }
}

async function downloadFile(fileUrl, referer) {
  const key = normalizeDownloadUrl(fileUrl);
  if (wasDownloaded(key)) {
    const filename = downloadedFiles.get(key);
    console.log(`Already downloaded: ${path.join(OUT_DIR, path.basename(filename))}`);
    return false;
  }

  const legacyPath = findLegacyDownloadedPath(key);
  if (legacyPath) {
    rememberDownloaded(key, null, legacyPath);
    console.log(`Already on disk; skipping duplicate: ${legacyPath}`);
    return false;
  }

  const existingDownload = inFlightDownloads.get(key);
  if (existingDownload) {
    try {
      await existingDownload;
    } catch {
      // The worker that started the request reports its failure.
    }
    return false;
  }

  const downloadPromise = (async () => {
    const response = await fetchWithRetry(fileUrl, { referer });
    return saveFileResponse(response, fileUrl);
  })();
  inFlightDownloads.set(key, downloadPromise);

  try {
    return await downloadPromise;
  } finally {
    inFlightDownloads.delete(key);
  }
}

async function downloadFiles(fileUrls) {
  const items = [...fileUrls];
  let nextIndex = 0;
  let nextStartAt = Date.now();
  let downloaded = 0;

  async function worker() {
    while (true) {
      const index = nextIndex++;
      if (index >= items.length) return;

      const [fileUrl, referer] = items[index];
      const startAt = Math.max(Date.now(), nextStartAt);
      nextStartAt = startAt + DOWNLOAD_DELAY_MS;
      await sleep(Math.max(0, startAt - Date.now()));

      console.log(`Downloading: ${fileUrl}`);
      try {
        if (await downloadFile(fileUrl, referer)) downloaded++;
      } catch (error) {
        console.warn(`Download failed: ${fileUrl} (${error.message})`);
      }
    }
  }

  const workerCount = Math.min(DOWNLOAD_CONCURRENCY, items.length);
  await Promise.all(Array.from({ length: workerCount }, () => worker()));
  return downloaded;
}

function readEntryItems(input) {
  if (!/^https?:\/\//i.test(input)) {
    if (!existsSync(input)) {
      throw new Error(`Entry list not found: ${input}. Run a crawler first or pass its output-list path.`);
    }
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
          referer: (sourceIndex >= 0 ? columns[sourceIndex] : "")?.trim() || null,
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
        referer: /^https?:\/\//i.test(possibleReferer || "") ? possibleReferer : null,
      };
    });
  }
  return [{ url: input, referer: null }];
}

async function main() {
  const entries = readEntryItems(ENTRY_INPUT);
  const fileUrls = new Map();

  for (const item of entries) {
    const url = parseEntryUrl(item.url);
    if (!fileUrls.has(url.href)) fileUrls.set(url.href, item.referer);
  }

  if (fileUrls.size === 0) throw new Error(`No download URLs found in ${ENTRY_INPUT}`);
  console.log(`Downloading ${fileUrls.size} URL(s) from ${ENTRY_INPUT}...`);
  const downloaded = await downloadFiles(fileUrls);
  console.log(`\nDone. Saved ${downloaded} new file(s) to ${OUT_DIR}.`);
}

main().catch((error) => {
  console.error(error.message);
  console.error("Usage: node node/download.js [crawler-output-list.txt|direct-file-url]");
  process.exitCode = 1;
});

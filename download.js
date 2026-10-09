import * as cheerio from "cheerio";
import { mkdirSync, createWriteStream, existsSync } from "node:fs";
import path from "node:path";
import { pipeline } from "node:stream/promises";
import crypto from "node:crypto";

const START_URLS = [
  // Burmese lists
  "https://slrd.gov.mm/laws-regulations/laws",
  "https://slrd.gov.mm/laws-regulations/regulations",
  "https://slrd.gov.mm/laws-regulations/orders",

  // English lists
  "https://slrd.gov.mm/en/laws-regulations/laws",
  "https://slrd.gov.mm/en/laws-regulations/regulations",

  // Your example node page
  "https://slrd.gov.mm/en/node/199",
];

const OUT_DIR = "slrd_pdfs";
const TIMEOUT_MS = 120_000;     // SLRD can be slow; 120s helps
const RETRIES = 3;
const DELAY_MS = 500;          // be polite
const MAX_NODE_PAGES = 200;    // safety cap (prevents crawling whole site)

mkdirSync(OUT_DIR, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function safeFilename(name) {
  return name
    .replace(/[<>:"/\\|?*\x00-\x1F]/g, "_")
    .replace(/\s+/g, " ")
    .trim();
}

function guessFilenameFromUrl(fileUrl) {
  const u = new URL(fileUrl);
  const base = u.pathname.split("/").pop() || "download.pdf";
  return safeFilename(decodeURIComponent(base));
}

function parseContentDispositionFilename(cd) {
  if (!cd) return null;

  // filename*=UTF-8''....
  const mStar = cd.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
  if (mStar) return safeFilename(decodeURIComponent(mStar[1]));

  // filename="..."
  const m = cd.match(/filename\s*=\s*"([^"]+)"/i) || cd.match(/filename\s*=\s*([^;]+)/i);
  if (m) return safeFilename(m[1].trim());

  return null;
}

async function fetchWithRetry(url, { binary = false } = {}) {
  let lastErr;
  for (let i = 1; i <= RETRIES; i++) {
    try {
      const res = await fetch(url, {
        headers: { "User-Agent": "Mozilla/5.0 (slrd-pdf-scraper)" },
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);

      if (binary) return res;
      return await res.text();
    } catch (e) {
      lastErr = e;
      if (i < RETRIES) await sleep(800 * i);
    }
  }
  throw lastErr;
}

function extractLinks(html, baseUrl) {
  const $ = cheerio.load(html);
  const pdfs = new Set();
  const nodes = new Set();

  $("a[href]").each((_, a) => {
    const href = $(a).attr("href");
    if (!href) return;

    const abs = new URL(href, baseUrl).toString();

    // PDF links
    if (/\.pdf(\?|$)/i.test(abs)) pdfs.add(abs);

    // Follow only node pages (keeps crawl small + relevant)
    const u = new URL(abs);
    if (u.hostname === "slrd.gov.mm" && /\/(en\/)?node\/\d+$/i.test(u.pathname)) {
      nodes.add(abs);
    }
  });

  return { pdfs, nodes };
}

async function downloadPdf(pdfUrl) {
  const res = await fetchWithRetry(pdfUrl, { binary: true });

  const cd = res.headers.get("content-disposition");
  const ct = res.headers.get("content-type") || "";
  const filename =
    parseContentDispositionFilename(cd) ||
    guessFilenameFromUrl(pdfUrl);

  if (!ct.toLowerCase().includes("pdf")) {
    console.warn(`Skip (not PDF content-type): ${pdfUrl} -> ${ct}`);
    return;
  }

  // avoid collisions
  let outPath = path.join(OUT_DIR, filename);
  if (existsSync(outPath)) {
    const hash = crypto.createHash("sha1").update(pdfUrl).digest("hex").slice(0, 8);
    const ext = path.extname(filename) || ".pdf";
    const base = filename.slice(0, filename.length - ext.length);
    outPath = path.join(OUT_DIR, `${base}__${hash}${ext}`);
  }

  await pipeline(res.body, createWriteStream(outPath));
  console.log(`Saved: ${outPath}`);
}

async function main() {
  const pdfUrls = new Set();
  const toVisitNodes = [];
  const visitedNodes = new Set();

  // 1) Seed pages: extract pdf links + node links
  for (const url of START_URLS) {
    console.log(`Fetching: ${url}`);
    const html = await fetchWithRetry(url);
    const { pdfs, nodes } = extractLinks(html, url);
    pdfs.forEach((p) => pdfUrls.add(p));
    nodes.forEach((n) => toVisitNodes.push(n));
    await sleep(DELAY_MS);
  }

  // 2) Visit node pages (limited) to discover attached PDFs
  while (toVisitNodes.length && visitedNodes.size < MAX_NODE_PAGES) {
    const nodeUrl = toVisitNodes.shift();
    if (visitedNodes.has(nodeUrl)) continue;
    visitedNodes.add(nodeUrl);

    console.log(`Node: ${nodeUrl}`);
    try {
      const html = await fetchWithRetry(nodeUrl);
      const { pdfs, nodes } = extractLinks(html, nodeUrl);
      pdfs.forEach((p) => pdfUrls.add(p));
      nodes.forEach((n) => {
        if (!visitedNodes.has(n)) toVisitNodes.push(n);
      });
    } catch (e) {
      console.warn(`Node fetch failed: ${nodeUrl} (${e.message})`);
    }

    await sleep(DELAY_MS);
  }

  console.log(`\nFound ${pdfUrls.size} unique PDF URLs. Downloading...\n`);

  // 3) Download PDFs
  for (const pdf of pdfUrls) {
    console.log(`Downloading: ${pdf}`);
    try {
      await downloadPdf(pdf);
    } catch (e) {
      console.warn(`Download failed: ${pdf} (${e.message})`);
    }
    await sleep(DELAY_MS);
  }

  console.log("\nDone.");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
// Pure helpers for parsing search-engine result pages.
//
// Nothing in this module reads the environment, touches the network, or writes
// files: it only turns HTML into result records. That keeps the behaviour
// testable with saved fixtures (see js/test/search_results.test.js).

import path from "node:path";

export const SEARCH_ENGINE_BASE_URLS = {
  mojeek: "https://www.mojeek.com/search",
  searx: "https://searx.be/search",
  brave: "https://search.brave.com/search",
  bing: "https://www.bing.com/search",
  duckduckgo: "https://html.duckduckgo.com/html/",
  google: "https://www.google.com/search",
};

// Only used for engines that take a page-size parameter. Brave pages by
// "offset=<page index>", so its page size is whatever the engine returns.
export const RESULTS_PER_PAGE_BY_ENGINE = {
  mojeek: 10,
  searx: 10,
  brave: 20,
  bing: 10,
  duckduckgo: 30,
  google: 10,
};

// Mojeek and SearXNG answer ordinary clients without CAPTCHAs at a polite
// request rate; the big engines throttle automated clients immediately.
export const BOT_FRIENDLY_ENGINES = ["mojeek", "searx"];

const NAMED_ENTITIES = {
  amp: "&",
  apos: "'",
  gt: ">",
  lt: "<",
  nbsp: " ",
  quot: '"',
};

// English names for the region plus Myanmar script, used to decide whether a
// PDF/DOCX result is likely to be a Burmese book.
export const LATIN_INTEREST_PATTERN = /myanmar|burmese|burma/i;
export const MYANMAR_SCRIPT_PATTERN = /[က-၁]/u;

// Bing serves (at least) two result markup shapes: the full page uses
// <li class="b_algo">, while the simplified page served to non-browser clients
// uses <div class="b_algo">. Both must be recognised or every result on the
// page is silently dropped.
const BING_BLOCK_START_PATTERN =
  /<(?:li|div|section|article|ul|ol)\b[^>]*class\s*=\s*(?:"[^"]*\bb_algo\b[^"]*"|'[^']*\bb_algo\b[^']*')[^>]*>/gi;

// DuckDuckGo's HTML endpoint marks titles with class="result__a"; the Google
// fallback looks for the title heading Google wraps around the result link.
const DUCKDUCKGO_TITLE_ANCHOR_PATTERN = /\bresult__a\b/i;
const GOOGLE_TITLE_PATTERN = /<h3\b[^>]*>[\s\S]*?<\/h3\s*>/gi;
const BRAVE_TITLE_PATTERN =
  /<div\b[^>]*class\s*=\s*(?:"[^"]*\btitle\b[^"]*"|'[^']*\btitle\b[^']*')[^>]*>([\s\S]*?)<\/div\s*>/i;

// Brave's web results are <div class="snippet …" data-type="web">; other
// snippet blocks (news, discussions, pagination) carry a different data-type.
const BRAVE_DIV_TAG_PATTERN = /<div\b[^>]*>/gi;

// Mojeek: <ul class="results"> (the class varies, so match any results list)<li>…<h2><a href="…">Title</a></h2>
const MOJEEK_CONTAINER_PATTERN =
  /<ul\b[^>]*class\s*=\s*(?:"[^"]*\bresults\b[^"]*"|'[^']*\bresults\b[^']*')[^>]*>([\s\S]*?)<\/ul\s*>/i;
const MOJEEK_ITEM_PATTERN = /<li\b[^>]*>/gi;

// SearXNG: <article class="result result-default category-general"> with the
// title in <h3><a>. (\bresult\b does not match the "results" wrapper class.)
const SEARX_RESULT_TAG_PATTERN = /<(?:article|div)\b[^>]*>/gi;

const BLOCKED_PAGE_PATTERNS = [
  // Cloudflare and similar interstitials (Brave sits behind one).
  /just a moment/i,
  /attention required/i,
  /checking your browser/i,
  /enable cookies and reload/i,
  // Google's cookie consent wall has no results, only a "continue" form.
  /before you continue to google/i,
  /\bcaptcha\b/i,
  /unusual traffic/i,
  /verify (?:that )?you(?:'re| are) (?:a )?human/i,
  /our systems have detected/i,
  /please enable javascript/i,
  /enable javascript to continue/i,
  /javascript is (?:disabled|turned off)/i,
  /sign in to confirm/i,
  /temporarily (?:unavailable|blocked)/i,
  /too many requests/i,
  /access denied/i,
  /rate limit/i,
];

const NO_RESULTS_PATTERNS = [
  /\bb_no\b/i,
  /no results (?:were )?found/i,
  /did not match any/i,
  /we did not find any results/i,
  /couldn't find any results/i,
  /did not match any documents/i,
  /your search did not/i,
  /no results containing all your search terms/i,
  /there are no results for/i,
];

export function decodeHtmlEntities(value) {
  return String(value ?? "").replace(
    /&(?:#x([\da-f]+);?|#(\d+);?|([a-z][a-z\d]+);)/gi,
    (match, hex, decimal, named) => {
      if (hex || decimal) {
        const codePoint = Number.parseInt(hex || decimal, hex ? 16 : 10);
        return Number.isFinite(codePoint) && codePoint <= 0x10ffff
          ? String.fromCodePoint(codePoint)
          : match;
      }
      const name = named.toLowerCase();
      return Object.prototype.hasOwnProperty.call(NAMED_ENTITIES, name)
        ? NAMED_ENTITIES[name]
        : match;
    },
  );
}

export function getAttribute(tag, name) {
  const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = tag.match(
    new RegExp(`(?:^|\\s)${escapedName}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s"'=<>\x60]+))`, "i"),
  );
  return match ? (match[1] ?? match[2] ?? match[3] ?? "") : null;
}

export function cleanLabel(value) {
  return decodeHtmlEntities(
    String(value ?? "")
      .replace(/<!--[\s\S]*?-->/g, " ")
      .replace(/<[^>]*>/g, " ")
      .replace(/\s+/g, " "),
  ).trim();
}

export function parseAnchorTags(markup) {
  const results = [];
  const anchorPattern = /<a\b[^>]*>[\s\S]*?<\/a\s*>/gi;
  for (const match of String(markup ?? "").matchAll(anchorPattern)) {
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

export function unwrapResultUrl(rawHref, pageUrl) {
  const raw = decodeHtmlEntities(String(rawHref ?? "").trim());
  // In-page anchors are not results.
  if (!raw || raw.startsWith("#")) return null;
  let url;
  try {
    url = new URL(raw, pageUrl);
  } catch {
    return null;
  }

  for (let depth = 0; depth < 3; depth++) {
    const host = url.hostname.toLowerCase();
    let target = null;
    if (host.includes("duckduckgo.com") || (url.pathname.endsWith("/l/") && url.searchParams.has("uddg"))) {
      target = url.searchParams.get("uddg");
    } else if (
      host === "google.com" ||
      host.endsWith(".google.com") ||
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

function stripComments(html) {
  return String(html ?? "").replace(/<!--[\s\S]*?-->/g, "");
}

function hostOf(value) {
  try {
    return new URL(value).hostname.toLowerCase().replace(/^www\./, "");
  } catch {
    return "";
  }
}

// "www.bing.com" -> "bing.com", so sub-domains of the engine (thumbnails,
// caches, redirects, pagination) are all recognised as the engine's own.
function baseDomain(hostname) {
  const labels = String(hostname || "").toLowerCase().split(".");
  return labels.length > 2 ? labels.slice(-2).join(".") : labels.join(".");
}

// True when the host belongs to the search engine itself (its own navigation,
// cache links, thumbnail hosts, ...) rather than being an organic result.
function isEngineHost(hostname, engineHost) {
  if (!hostname || !engineHost) return false;
  const host = baseDomain(hostname);
  const engine = baseDomain(engineHost);
  return Boolean(host) && host === engine;
}

export function getFilename(url) {
  let pathname = new URL(url).pathname;
  try {
    pathname = decodeURIComponent(pathname);
  } catch {
    // Keep the encoded path if it contains a malformed escape.
  }
  return path.posix.basename(pathname);
}

export function fallbackBookName(url) {
  return getFilename(url).replace(/\.(pdf|docx)$/i, "") || url;
}

export function looksBurmeseRelated(value) {
  const text = String(value ?? "");
  return LATIN_INTEREST_PATTERN.test(text) || MYANMAR_SCRIPT_PATTERN.test(text);
}

export function matchesBookFilename(url) {
  const filename = getFilename(url);
  const extension = path.posix.extname(filename).toLowerCase();
  if (extension !== ".pdf" && extension !== ".docx") return false;
  return looksBurmeseRelated(filename);
}

// `mode` is "filename" (only the file name may show the book is Burmese) or
// "loose" (the result title or URL path may show it too).
export function matchesBookCandidate({ url, title }, mode = "loose") {
  if (!matchesBookExtension(url)) return false;
  if (mode === "filename") return matchesBookFilename(url);

  let pathname = "";
  try {
    pathname = decodeURIComponent(new URL(url).pathname);
  } catch {
    pathname = new URL(url).pathname;
  }
  return (
    looksBurmeseRelated(getFilename(url)) ||
    looksBurmeseRelated(pathname) ||
    looksBurmeseRelated(title) ||
    looksBurmeseRelated(String(url))
  );
}

function matchesBookExtension(url) {
  return /\.(pdf|docx)$/i.test(getFilename(url));
}

export function isScribdDocumentUrl(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  const hostname = parsed.hostname.toLowerCase().replace(/^www\./, "");
  if (hostname !== "scribd.com" && !hostname.endsWith(".scribd.com")) return false;
  // Document pages always start with /doc/<id>/, /document/<id>/ or /book/<id>/.
  return /^\/(?:doc|document|book)\/\d+(?:\/|$)/i.test(parsed.pathname);
}

/**
 * Slice a page into blocks given the index of each block's opening tag.
 *
 * Blocks are delimited by the *start* of the next result rather than by a
 * matching closing tag: result blocks contain nested divs (Bing's
 * `<div class="b_algo">`, Brave's snippet), so a balanced-tag regex stops at
 * the first inner `</div>` and loses the title.
 */
function sliceBlocks(source, starts, fallbackEnd) {
  const blocks = [];
  for (let index = 0; index < starts.length; index++) {
    const start = starts[index];
    const nextStart = starts[index + 1] ?? fallbackEnd;
    const end = nextStart > start ? nextStart : source.length;
    blocks.push(source.slice(start, end));
  }
  return blocks;
}

/**
 * Split a Bing result page into result blocks.
 */
export function extractBingBlocks(html) {
  // Comments can contain documented example markup (or commented-out results),
  // which must not be mistaken for real results.
  const source = stripComments(html);
  const starts = [...source.matchAll(BING_BLOCK_START_PATTERN)].map((match) => match.index);
  if (!starts.length) return [];

  // Only look as far as the end of the results container, so the footer is not
  // swallowed into the final block.
  const containerEnd = source.search(/<\/(?:ol|ul)\s*>/i);
  return sliceBlocks(source, starts, containerEnd > 0 ? containerEnd : source.length);
}

/**
 * Split a Brave result page into web result blocks.
 */
export function extractBraveBlocks(html) {
  const source = stripComments(html);
  const starts = [];
  for (const match of source.matchAll(BRAVE_DIV_TAG_PATTERN)) {
    const tag = match[0];
    const classValue = getAttribute(tag, "class") || "";
    if (!/\bsnippet\b/i.test(classValue)) continue;
    if ((getAttribute(tag, "data-type") || "").toLowerCase() !== "web") continue;
    starts.push(match.index);
  }
  return sliceBlocks(source, starts, source.length);
}

/**
 * Split a Mojeek result page into result blocks.
 */
export function extractMojeekBlocks(html) {
  const source = stripComments(html);
  const container = source.match(MOJEEK_CONTAINER_PATTERN);
  if (!container) return [];
  const inner = container[1];
  const starts = [...inner.matchAll(MOJEEK_ITEM_PATTERN)].map((match) => match.index);
  return sliceBlocks(inner, starts, inner.length);
}

/**
 * Split a SearXNG result page into result blocks.
 */
export function extractSearxBlocks(html) {
  const source = stripComments(html);
  const starts = [];
  for (const match of source.matchAll(SEARX_RESULT_TAG_PATTERN)) {
    const classValue = getAttribute(match[0], "class") || "";
    if (/\bresult\b/i.test(classValue)) starts.push(match.index);
  }
  return sliceBlocks(source, starts, source.length);
}

function pickBlockAnchor(block) {
  const heading = block.match(/<h2\b[^>]*>[\s\S]*?<\/h2\s*>/i) || block.match(GOOGLE_TITLE_PATTERN);
  const scoped = parseAnchorTags(heading ? heading[0] : "");
  if (scoped.length) return scoped[0];
  return parseAnchorTags(block)[0] ?? null;
}

// Brave puts the page title in its own <div class="title …"> inside the result
// anchor; the anchor text also carries the site name.
function pickBraveAnchor(block) {
  const anchor = parseAnchorTags(block)[0];
  if (!anchor) return null;
  const titleMatch = block.match(BRAVE_TITLE_PATTERN);
  const title = titleMatch ? cleanLabel(titleMatch[1]) : "";
  return title ? { ...anchor, title } : anchor;
}

function anchorsToResults(anchors, pageUrl, engineHost) {
  const seen = new Set();
  const results = [];
  for (const anchor of anchors) {
    const url = unwrapResultUrl(anchor.href, pageUrl);
    if (!url || seen.has(url)) continue;
    // Never record the engine's own links (pagination, related searches, ...).
    if (isEngineHost(hostOf(url), engineHost)) continue;
    seen.add(url);
    results.push({ url, title: anchor.title });
  }
  return results;
}

/**
 * Extract organic results from a search result page.
 *
 * The structured parse (result blocks / title anchors) runs first. If it finds
 * nothing, the page falls back to every external link on the page so that a
 * markup change degrades into "some extra candidates" instead of "silently
 * zero results".
 */
export function extractSearchResults(html, pageUrl, engine = "bing") {
  const source = stripComments(html);
  const engineHost = hostOf(pageUrl || SEARCH_ENGINE_BASE_URLS[engine]);
  let anchors = [];
  let blocks = [];
  let usedFallback = false;

  if (engine === "duckduckgo") {
    anchors = parseAnchorTags(source).filter(({ tag }) =>
      DUCKDUCKGO_TITLE_ANCHOR_PATTERN.test(getAttribute(tag, "class") || ""),
    );
    if (!anchors.length) {
      usedFallback = true;
      anchors = parseAnchorTags(source).filter(({ markup }) => /<h2\b/i.test(markup));
    }
  } else if (engine === "brave") {
    blocks = extractBraveBlocks(source);
    anchors = blocks.map(pickBraveAnchor).filter(Boolean);
    if (!anchors.length) {
      usedFallback = true;
      anchors = parseAnchorTags(source).filter(({ markup }) =>
        /<div\b[^>]*\btitle\b/i.test(markup),
      );
    }
  } else if (engine === "mojeek") {
    blocks = extractMojeekBlocks(source);
    anchors = blocks.map(pickBlockAnchor).filter(Boolean);
    if (!anchors.length) {
      usedFallback = true;
      anchors = parseAnchorTags(source).filter(({ markup }) => /<h2\b/i.test(markup));
    }
  } else if (engine === "searx") {
    blocks = extractSearxBlocks(source);
    anchors = blocks.map(pickBlockAnchor).filter(Boolean);
    if (!anchors.length) {
      usedFallback = true;
      anchors = parseAnchorTags(source).filter(({ markup }) => /<h3\b/i.test(markup));
    }
  } else if (engine === "google") {
    // Current Google layout wraps the H3 title *inside* the result anchor;
    // older layouts put the anchor inside the H3. Support both.
    anchors = parseAnchorTags(source).filter(({ markup }) => /<h3\b/i.test(markup));
    if (!anchors.length) {
      const headings = [...source.matchAll(GOOGLE_TITLE_PATTERN)].map((match) => match[0]);
      anchors = headings.flatMap((heading) => parseAnchorTags(heading).slice(0, 1));
    }
    if (!anchors.length) {
      usedFallback = true;
      anchors = parseAnchorTags(source).filter(({ tag }) =>
        /^\/url\?/i.test(getAttribute(tag, "href") || ""),
      );
    }
  } else {
    blocks = extractBingBlocks(source);
    anchors = blocks.map(pickBlockAnchor).filter(Boolean);
    if (!anchors.length) {
      usedFallback = true;
      anchors = parseAnchorTags(source).filter(({ markup }) => /<h2\b/i.test(markup));
    }
  }

  // How many result containers the structured parse found, before any fallback.
  const blockCount = blocks.length || anchors.length;
  // Last resort: every external link on the page. Skipped when the page itself
  // says there is nothing to find, otherwise footer and navigation links get
  // reported as results.
  if (usedFallback && !anchors.length && !NO_RESULTS_PATTERNS.some((pattern) => pattern.test(source))) {
    anchors = parseAnchorTags(source);
  }

  return {
    results: anchorsToResults(anchors, pageUrl, engineHost),
    usedFallback,
    blockCount,
  };
}

/**
 * Classify a result page.
 *
 * - "results":  at least one organic result was parsed.
 * - "no-results": the engine itself says the query matched nothing (legit zero).
 * - "blocked":  a CAPTCHA / rate-limit / JavaScript-required interstitial.
 * - "empty":    HTML fetched, but no results and no explanation: the markup
 *               probably changed, so the page should be retried rather than
 *               recorded as done.
 */
export function classifySearchPage(html, resultCount, { consentRedirect = false } = {}) {
  if (resultCount > 0) return "results";
  if (consentRedirect) return "blocked";
  const source = String(html ?? "");
  if (BLOCKED_PAGE_PATTERNS.some((pattern) => pattern.test(source))) return "blocked";
  if (NO_RESULTS_PATTERNS.some((pattern) => pattern.test(source))) return "no-results";
  return "empty";
}

export function makeSearchPageUrl(query, pageNumber, { engine = "bing", baseUrl, resultsPerPage } = {}) {
  const url = new URL(baseUrl || SEARCH_ENGINE_BASE_URLS[engine]);
  const perPage = resultsPerPage || RESULTS_PER_PAGE_BY_ENGINE[engine] || 10;
  url.searchParams.set("q", query);
  if (engine === "brave") {
    // offset is a zero-based *page* index (page 2 is offset=1), so no page-size
    // parameter is needed and no results can be skipped.
    if (pageNumber > 0) url.searchParams.set("offset", String(pageNumber));
    // Keep the query verbatim: Brave would otherwise "correct" Burmese terms.
    url.searchParams.set("spellcheck", "0");
  } else if (engine === "mojeek") {
    // s is a result offset. Mojeek rate-limits requests that set s=0, so the
    // first page must omit it entirely.
    if (pageNumber > 0) url.searchParams.set("s", String(pageNumber * perPage));
  } else if (engine === "searx") {
    // SearXNG pages with a 1-based page number.
    url.searchParams.set("pageno", String(pageNumber + 1));
    url.searchParams.set("categories", "general");
    url.searchParams.set("safesearch", "0");
    url.searchParams.set("language", "all");
  } else if (engine === "bing") {
    url.searchParams.set("count", String(perPage));
    url.searchParams.set("first", String(pageNumber * perPage + 1));
  } else if (engine === "duckduckgo") {
    url.searchParams.set("s", String(pageNumber * perPage));
  } else {
    url.searchParams.set("num", String(perPage));
    url.searchParams.set("hl", "en");
    url.searchParams.set("start", String(pageNumber * perPage));
  }
  return url.href;
}

export function isCurrentEnginePage(value, { engine = "bing", baseUrl } = {}) {
  try {
    return hostOf(value) === hostOf(baseUrl || SEARCH_ENGINE_BASE_URLS[engine]);
  } catch {
    return false;
  }
}

// Google answers some clients with a redirect to its cookie consent form, which
// looks like a normal page but holds no results.
export function isConsentRedirect(finalUrl, engine = "google") {
  if (engine !== "google") return false;
  const host = hostOf(finalUrl);
  return host === "consent.google.com" || host === "consent.youtube.com";
}

export function buildQueries(terms, { includeScribdQuery = true } = {}) {
  const queries = [];
  for (const term of terms) {
    queries.push(term);
    queries.push(`${term} filetype:pdf`);
    queries.push(`${term} filetype:docx`);
    // site: only accepts a domain (with at most a shallow directory); the path
    // form "site:scribd.com/doc" made these queries return nothing.
    if (includeScribdQuery) queries.push(`site:scribd.com ${term}`);
  }
  return [...new Set(queries)];
}

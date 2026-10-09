// Run with: npm test   (node --test js/test/)
//
// These tests use saved result pages so the parser can be checked without
// hitting a search engine. Each fixture mirrors a markup shape seen in real
// responses; see fixtures/README.md.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  buildQueries,
  classifySearchPage,
  extractBingBlocks,
  extractBraveBlocks,
  extractMojeekBlocks,
  extractSearchResults,
  extractSearxBlocks,
  isConsentRedirect,
  isScribdDocumentUrl,
  makeSearchPageUrl,
  matchesBookCandidate,
  matchesBookFilename,
  unwrapResultUrl,
} from "../search_results.js";

const FIXTURES = path.join(import.meta.dirname, "fixtures");
const fixture = (name) => readFileSync(path.join(FIXTURES, name), "utf8");

const BING_PAGE = "https://www.bing.com/search?q=test&count=10&first=1";
const BRAVE_PAGE = "https://search.brave.com/search?q=test&spellcheck=0";
const MOJEEK_PAGE = "https://www.mojeek.com/search?q=test";
const SEARX_PAGE = "https://searx.be/search?q=test&pageno=1";
const DDG_PAGE = "https://html.duckduckgo.com/html/?q=test&s=0";
const GOOGLE_PAGE = "https://www.google.com/search?q=test&start=0";

test("parses Bing's <li class=\"b_algo\"> result markup", () => {
  const { results, usedFallback } = extractSearchResults(fixture("bing-li-algo.html"), BING_PAGE, "bing");
  assert.equal(usedFallback, false);
  assert.deepEqual(
    results.map((result) => result.url),
    [
      "https://www.scribd.com/document/451498539/Trigonometry-angle-value-table-pdf",
      "https://www.scribd.com/doc/1234567/Myanmar-Poems-File",
      "https://example.org/files/myanmar-grammar.pdf",
    ],
  );
  assert.equal(results[0].title, "Trigonometry angle value table - pdf");
});

// Regression test for the bug that made every query look empty: Bing also
// serves result blocks as <div class="b_algo">, which the old li-only parser
// dropped completely.
test("parses Bing's <div class=\"b_algo\"> result markup", () => {
  const html = fixture("bing-div-algo.html");
  const { results, usedFallback } = extractSearchResults(html, BING_PAGE, "bing");
  assert.equal(usedFallback, false);
  assert.equal(extractBingBlocks(html).length, 3);
  assert.deepEqual(
    results.map((result) => result.url),
    [
      "https://www.scribd.com/document/1045071803/%E1%80%9A%E1%80%B1%E1%80%AC%E1%80%80-%E1%80%81%E1%80%91%E1%80%AE%E1%80%B8%E1%80%94%E1%80%BE%E1%80%84-%E1%80%9B%E1%80%BE%E1%80%9A-%E1%80%81%E1%80%BB%E1%80%BD%E1%80%B1%E1%80%B8%E1%80%99-Dr-Mg-Nyo",
      "https://www.scribd.com/document/966837614/Oxford-Phonics-World-4-Workbook",
      "https://downloads.example.org/books/burmese-novel-2020.pdf",
    ],
  );
});

test("prefers the <h2> title anchor over a favicon/site anchor in the block", () => {
  const { results } = extractSearchResults(fixture("bing-algoheader-anchor.html"), BING_PAGE, "bing");
  assert.deepEqual(
    results.map((result) => result.url),
    [
      "https://www.scribd.com/document/451498539/Trigonometry-angle-value-table-pdf",
      "https://example.org/files/myanmar-poetry.docx",
    ],
  );
});

test("parses Brave results, skipping non-web snippets", () => {
  const html = fixture("brave-results.html");
  const { results, usedFallback } = extractSearchResults(html, BRAVE_PAGE, "brave");
  assert.equal(usedFallback, false);
  assert.equal(extractBraveBlocks(html).length, 2, "only data-type=web snippets count");
  assert.deepEqual(
    results.map((result) => result.url),
    [
      "https://www.scribd.com/document/451498539/Trigonometry-angle-value-table-pdf",
      "https://files.example.org/burma/myanmar-poetry.pdf",
    ],
  );
  // The title comes from the title DIV, not the whole anchor text.
  assert.equal(results[0].title, "Trigonometry angle value table - pdf");
  assert.equal(results[1].title, "မြန်မာကဗျာများ");
});

test("parses Mojeek results", () => {
  const html = fixture("mojeek-results.html");
  const { results, usedFallback } = extractSearchResults(html, MOJEEK_PAGE, "mojeek");
  assert.equal(usedFallback, false);
  assert.equal(extractMojeekBlocks(html).length, 3);
  assert.deepEqual(
    results.map((result) => result.url),
    [
      "https://www.scribd.com/document/451498539/Trigonometry-angle-value-table-pdf",
      "https://files.example.org/burma/myanmar-poetry.pdf",
      "https://example.org/not-interesting.pdf",
    ],
  );
  assert.equal(results[0].title, "Trigonometry angle value table - pdf");
});

test("parses SearXNG results, taking the title from the h3 anchor", () => {
  const html = fixture("searx-results.html");
  const { results, usedFallback } = extractSearchResults(html, SEARX_PAGE, "searx");
  assert.equal(usedFallback, false);
  assert.equal(extractSearxBlocks(html).length, 3);
  // The url_header anchor and the h3 anchor share a URL; it is deduped.
  assert.deepEqual(results.slice(0, 2).map((result) => result.url), [
    "https://www.scribd.com/document/451498539/Trigonometry-angle-value-table-pdf",
    "https://files.example.org/burma/myanmar-novel.pdf",
  ]);
  assert.equal(results[0].title, "Trigonometry angle value table - pdf");
});

// Mojeek rate-limits any request that sets s=0, so page one must omit it.
test("Mojeek page one omits the s parameter", () => {
  assert.equal(
    makeSearchPageUrl("myanmar pdf", 0, { engine: "mojeek" }),
    "https://www.mojeek.com/search?q=myanmar+pdf",
  );
  assert.equal(
    makeSearchPageUrl("myanmar pdf", 2, { engine: "mojeek" }),
    "https://www.mojeek.com/search?q=myanmar+pdf&s=20",
  );
});

test("SearXNG pages with a 1-based pageno", () => {
  assert.equal(
    makeSearchPageUrl("myanmar pdf", 0, { engine: "searx" }),
    "https://searx.be/search?q=myanmar+pdf&pageno=1&categories=general&safesearch=0&language=all",
  );
  assert.equal(
    makeSearchPageUrl("myanmar pdf", 1, { engine: "searx" }),
    "https://searx.be/search?q=myanmar+pdf&pageno=2&categories=general&safesearch=0&language=all",
  );
});

test("recognises Brave's empty and throttled pages", () => {
  assert.equal(classifySearchPage(fixture("brave-no-results.html"), 0), "no-results");
  assert.equal(classifySearchPage(fixture("brave-cloudflare.html"), 0), "blocked");
});

test("Google's consent wall counts as blocked, not as an empty query", () => {
  assert.equal(classifySearchPage(fixture("google-consent.html"), 0), "blocked");
  assert.equal(isConsentRedirect("https://consent.google.com/save", "google"), true);
  assert.equal(isConsentRedirect("https://www.google.com/search?q=a", "google"), false);
  assert.equal(isConsentRedirect("https://consent.google.com/save", "brave"), false);
});

test("parses DuckDuckGo results and unwraps the uddg redirect", () => {
  const { results } = extractSearchResults(fixture("duckduckgo-results.html"), DDG_PAGE, "duckduckgo");
  assert.deepEqual(
    results.map((result) => result.url),
    [
      "https://www.scribd.com/document/451498539/Trigonometry-angle-value-table-pdf",
      "https://files.example.org/burmese/%E1%80%99%E1%80%BC%E1%80%94%E1%80%BA%E1%80%99%E1%80%AC%E1%80%80%E1%80%97%E1%80%BB%E1%80%AC.pdf",
    ],
  );
});

test("parses Google results and unwraps the /url?q= redirect", () => {
  const { results } = extractSearchResults(fixture("google-results.html"), GOOGLE_PAGE, "google");
  assert.deepEqual(
    results.map((result) => result.url),
    [
      "https://www.scribd.com/document/451498539/Trigonometry-angle-value-table-pdf",
      "https://files.example.org/burma-history.pdf",
    ],
  );
});

test("classifies result pages so an unexplained zero can be retried", () => {
  const withResults = extractSearchResults(fixture("bing-li-algo.html"), BING_PAGE, "bing");
  assert.equal(classifySearchPage(fixture("bing-li-algo.html"), withResults.results.length), "results");

  assert.equal(classifySearchPage(fixture("bing-no-results.html"), 0), "no-results");
  assert.equal(classifySearchPage(fixture("bing-blocked.html"), 0), "blocked");
  assert.equal(classifySearchPage(fixture("bing-empty-unknown.html"), 0), "empty");
});

test("collects Scribd document links from a Bing page", () => {
  const { results } = extractSearchResults(fixture("bing-div-algo.html"), BING_PAGE, "bing");
  const scribd = results.filter((result) => isScribdDocumentUrl(result.url));
  assert.equal(scribd.length, 2);
});

test("recognises Scribd document URLs only", () => {
  for (const url of [
    "https://www.scribd.com/document/451498539/Trigonometry-angle-value-table-pdf",
    "https://scribd.com/document/123/Title",
    "https://www.scribd.com/doc/1234567/Myanmar-Poems",
    "https://www.scribd.com/book/987654321/Some-Book",
  ]) {
    assert.equal(isScribdDocumentUrl(url), true, url);
  }
  for (const url of [
    "https://www.scribd.com/",
    "https://www.scribd.com/search?query=myanmar",
    "https://www.scribd.com/document",
    "https://www.scribd.com/user/12345/name",
    "https://www.scribd.com/lists/12345",
    "https://example.com/document/12345/Title",
  ]) {
    assert.equal(isScribdDocumentUrl(url), false, url);
  }
});

test("matches Burmese PDF/DOCX candidates", () => {
  assert.equal(matchesBookFilename("https://example.org/a/myanmar-novel.pdf"), true);
  // Myanmar script in the file name (U+1000–U+1041).
  assert.equal(matchesBookFilename("https://example.org/a/%E1%80%99%E1%80%BC%E1%80%94%E1%80%BA%E1%80%99%E1%80%AC.pdf"), true);
  assert.equal(matchesBookFilename("https://example.org/a/random-book.pdf"), false);
  assert.equal(matchesBookFilename("https://example.org/a/myanmar-page.html"), false);

  // "loose" also accepts a match in the title or path, "filename" does not.
  const loose = { url: "https://files.example.org/12345.pdf", title: "မြန်မာကဗျာ PDF" };
  assert.equal(matchesBookCandidate(loose, "loose"), true);
  assert.equal(matchesBookCandidate(loose, "filename"), false);
});

test("site: queries use a bare domain, not a path", () => {
  const queries = buildQueries(["Myanmar PDF"]);
  assert.ok(queries.includes("site:scribd.com Myanmar PDF"));
  assert.ok(!queries.some((query) => query.includes("site:scribd.com/doc")));
});

test("result page URLs carry the engine's paging parameters", () => {
  // Brave pages with offset=<page index>, so no page-size parameter is needed
  // and switching page size cannot skip results.
  assert.equal(
    makeSearchPageUrl("myanmar pdf", 0, { engine: "brave" }),
    "https://search.brave.com/search?q=myanmar+pdf&spellcheck=0",
  );
  assert.equal(
    makeSearchPageUrl("myanmar pdf", 3, { engine: "brave" }),
    "https://search.brave.com/search?q=myanmar+pdf&offset=3&spellcheck=0",
  );
  assert.equal(
    makeSearchPageUrl("myanmar pdf", 2, { engine: "bing" }),
    "https://www.bing.com/search?q=myanmar+pdf&count=10&first=21",
  );
  assert.equal(
    makeSearchPageUrl("myanmar pdf", 1, { engine: "google" }),
    "https://www.google.com/search?q=myanmar+pdf&num=10&hl=en&start=10",
  );
  assert.equal(
    makeSearchPageUrl("myanmar pdf", 1, { engine: "duckduckgo" }),
    "https://html.duckduckgo.com/html/?q=myanmar+pdf&s=30",
  );
});

test("unwraps engine redirect links", () => {
  const bingRedirect =
    "https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly93d3cuc2NyaWJkLmNvbS9kb2N1bWVudC8xMjMvVGl0bGU&ntb=1";
  assert.equal(
    unwrapResultUrl(bingRedirect, BING_PAGE),
    "https://www.scribd.com/document/123/Title",
  );
  assert.equal(
    unwrapResultUrl("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa.pdf", DDG_PAGE),
    "https://example.org/a.pdf",
  );
  assert.equal(
    unwrapResultUrl("/url?q=https://example.org/a.pdf&sa=U", GOOGLE_PAGE),
    "https://example.org/a.pdf",
  );
  assert.equal(unwrapResultUrl("#anchor", BING_PAGE), null);
  assert.equal(unwrapResultUrl("mailto:someone@example.org", BING_PAGE), null);
  assert.equal(unwrapResultUrl("", BING_PAGE), null);
});

test("drops links that belong to the search engine itself", () => {
  const html = `<html><body>
    <ol id="b_results">
      <li class="b_algo"><h2><a href="https://www.scribd.com/document/123/Title">Title</a></h2></li>
      <li class="b_algo"><h2><a href="https://www.bing.com/search?q=related">Related search</a></h2></li>
      <li class="b_algo"><h2><a href="/search?q=other">Another engine link</a></h2></li>
    </ol></body></html>`;
  const { results } = extractSearchResults(html, BING_PAGE, "bing");
  assert.deepEqual(
    results.map((result) => result.url),
    ["https://www.scribd.com/document/123/Title"],
  );
});

import fs from "node:fs";
import path from "node:path";
import { readSecureJson } from "../model/secure.js";
import { MAP_DIR, MODEL_DIR } from "../core/paths.mjs";
import { MARKS as MM_MARKS } from "./mm-score.mjs";

/**
 * DATA: scan.json is generated from scan.py by
 * ../tools/gen-parser-data.mjs.
 *
 * The character predicates below are Python's own lambdas MATERIALISED over
 * the Myanmar block, not a JS restatement of them. The restatement is exactly
 * what drifted: it had dropped U+102B and U+102C -- the two aa vowels, among
 * the most common characters in Burmese -- plus five independent vowels,
 * while wrongly admitting U+1033-U+1035 and U+103F. The impossible-pair table
 * had lost a rule the same way.
 */
const DATA = JSON.parse(fs.readFileSync(path.join(MAP_DIR, "scan.json"), "utf8"));

const SPLIT = /[\s\u104a\u104b()\x5b\x5d"‘’“”,\x2e:;?!/=+_—-]+/u;
const DIGIT = /^[\u1040-\u1049]$/u;

const LETTER_OR_MARK_SET = new Set(DATA.letterOrMark);
const LETTER_OR_MARK = (ch) => LETTER_OR_MARK_SET.has(ch);
const BASES = new Set(DATA.base);
const EXTRA_BASES = new Set(DATA.extraBases);
const PLACEHOLDER_BASES = new Set(DATA.placeholderBases);

/**
 * Two different mark sets, as in scan.py. The orphan rule tests `is_mark`,
 * the sixteen dependent marks. The stray rule tests `is_mark(c) or c ==
 * U+1039`, so the virama counts there and only there -- a token of nothing
 * but marks and viramas is shadow-collapse debris.
 */
const MARKS = new Set(Array.from(MM_MARKS, (cp) => String.fromCodePoint(cp)));
const STRAY_MARKS = new Set(DATA.strayMarks);

/** [regex, title, why] -- patterns copied verbatim out of scan.py. */
const IMPOSSIBLE = DATA.impossible.map(([pattern, title, why]) => [new RegExp(pattern, "gu"), title, why]);

function isBase(ch) {
  return BASES.has(ch) || EXTRA_BASES.has(ch) || PLACEHOLDER_BASES.has(ch);
}

function words(text) {
  const result = [];
  let offset = 0;
  for (const token of text.split(SPLIT)) {
    if (!token) continue;
    const start = text.indexOf(token, offset);
    result.push([token, start < 0 ? offset : start]);
    offset = (start < 0 ? offset : start) + token.length;
  }
  return result;
}

function clusterAt(text, index, span = 9) {
  let start = index;
  while (start > 0 && !SPLIT.test(text[start - 1])) start -= 1;
  let end = index;
  while (end < text.length && !SPLIT.test(text[end])) end += 1;
  return text.slice(start, end).slice(0, span * 3) || text[index];
}

function lineNumber(text, index) {
  return text.slice(0, index).split("\n").length;
}

function findOrphans(text) {
  const hits = [];
  let attached = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (isBase(ch)) attached = true;
    else if (MARKS.has(ch)) { if (!attached) hits.push(i); }
    else attached = false;
  }
  return hits;
}

function findStray(text) {
  return words(text).filter(([token]) => [...token].every((ch) => STRAY_MARKS.has(ch))).map(([, i]) => i);
}

function findRepeat(text) {
  const hits = [];
  const re = /([\u1000-\u109f]{2,4})\1{2,}/gu;
  for (const match of text.matchAll(re)) hits.push(match.index);
  return hits;
}

function findDigits(text) {
  const hits = [];
  for (let i = 1; i < text.length - 1; i += 1) {
    if (DIGIT.test(text[i]) && LETTER_OR_MARK(text[i - 1]) && LETTER_OR_MARK(text[i + 1])) hits.push(i);
  }
  return hits;
}

function findVirama(text) {
  const hits = [];
  for (const match of text.matchAll(/္(?![က-ဧ])/gu)) hits.push(match.index);
  return hits;
}

const RULES = [
  ["orphan", "a dependent mark with no base in front of it", findOrphans],
  ["impossible", "a pair Burmese cannot write in one syllable", (text) => IMPOSSIBLE.flatMap(([re]) => [...text.matchAll(re)].map((m) => m.index))],
  ["stray", "a token made only of marks", findStray],
  ["repeat", "the same syllable printed 3+ times", findRepeat],
  ["digit", "a digit with Myanmar letters on both sides", findDigits],
  ["virama", "U+1039 with nothing stacked after it", findVirama],
];

function loadMap(filePath) {
  if (!filePath) return null;
  const absolute = path.resolve(filePath);
  if (absolute.endsWith(".enc")) return readSecureJson(absolute);
  if (absolute.endsWith(".db") || absolute.endsWith(".sqlite") || absolute.endsWith(".sqlite3")) {
    throw new Error("SQLite dictionary scanning is not available in the JavaScript scanner; export the master_json table to JSON or .enc first");
  }
  return JSON.parse(fs.readFileSync(absolute, "utf8"));
}

function dictionaryWords(data) {
  if (!data) return null;
  const result = new Set();
  const resolution = data.resolution || {};
  for (const section of Object.values(resolution)) {
    if (!section || typeof section !== "object") continue;
    for (const form of Object.keys(section)) {
      const value = form.replace(/\s+/gu, "");
      if (value) result.add(value);
    }
  }
  const walk = (value) => {
    if (typeof value === "string") {
      let word = value.replace(/\s+/gu, "");
      if (!word || word.startsWith("!")) return;
      word = word.replace(/\.[wcp]$/u, "");
      if (word) result.add(word);
    } else if (Array.isArray(value)) value.forEach(walk);
    else if (value && typeof value === "object") Object.values(value).forEach(walk);
  };
  walk(Object.fromEntries(Object.entries(data).filter(([key]) => key !== "mapData")));
  return result;
}

let scanDictionaryCache = undefined;

/**
 * The one sealed dictionary the scan verdict, the dictionary repairs and
 * the ZawCode guard all arbitrate over. Loading it in three places would
 * let the guard split a document on one word list and the mend repair it
 * on another. `PRASER_SCAN_DICTIONARY` keeps the worker's override.
 */
function loadScanDictionary() {
  if (scanDictionaryCache !== undefined) return scanDictionaryCache;
  const filePath = path.resolve(
    process.env.PRASER_SCAN_DICTIONARY || path.join(MODEL_DIR, "master.json.enc"),
  );
  try {
    scanDictionaryCache = loadMap(filePath) ?? {};
  } catch {
    scanDictionaryCache = {};
  }
  return scanDictionaryCache;
}

function segmentable(token, dictionary, memo, longest) {
  if (dictionary.has(token)) return true;
  if (memo.has(token)) return memo.get(token);
  memo.set(token, false);
  for (let end = Math.min(token.length, longest); end > 0; end -= 1) {
    if (dictionary.has(token.slice(0, end)) && segmentable(token.slice(end), dictionary, memo, longest)) {
      memo.set(token, true);
      return true;
    }
  }
  return false;
}

function unknownWords(text, dictionary, standalone = []) {
  const counts = new Map();
  const first = new Map();
  const positions = [];
  const memo = new Map();
  const longest = Math.max(0, ...[...dictionary].map((word) => word.length));
  let total = 0;
  for (const [token, index] of words(text)) {
    if (token.length < 2 || /^[\u1040-\u1049]+$/u.test(token)) continue;
    let core = token;
    for (const form of [...standalone].sort((a, b) => b.length - a.length)) {
      if (core.startsWith(form) && core.length > form.length) core = core.slice(form.length);
      if (core.endsWith(form) && core.length > form.length) core = core.slice(0, -form.length);
    }
    total += 1;
    if (core && !segmentable(core, dictionary, memo, longest)) {
      counts.set(token, (counts.get(token) || 0) + 1);
      if (!first.has(token)) first.set(token, index);
      positions.push(index);
    }
  }
  return { counts, first, total, positions };
}

function reportClusters(text, hits, limit) {
  const grouped = new Map();
  for (const index of hits) {
    const cluster = clusterAt(text, index);
    const item = grouped.get(cluster) || { count: 0, index };
    item.count += 1;
    grouped.set(cluster, item);
  }
  return [...grouped.entries()].sort((a, b) => b[1].count - a[1].count).slice(0, limit)
    .map(([cluster, item]) => {
      const lineStart = text.lastIndexOf("\n", item.index - 1) + 1;
      const nextBreak = text.indexOf("\n", item.index);
      const lineEnd = nextBreak < 0 ? text.length : nextBreak;
      const fullLine = text.slice(lineStart, lineEnd).trim();
      return {
        cluster,
        count: item.count,
        line: lineNumber(text, item.index),
        context: fullLine.length > 160 ? `${fullLine.slice(0, 157)}...` : fullLine,
      };
    });
}

export function scanText(text, { dictionary = null, standalone = [], perRule = 25 } = {}) {
  const report = {};
  for (const [key, title, finder] of RULES) {
    const hits = finder(text);
    report[key] = { total: hits.length, distinct: new Set(hits.map((i) => clusterAt(text, i))).size, title, clusters: reportClusters(text, hits, perRule) };
  }
  if (dictionary) {
    const unknown = unknownWords(text, dictionary, standalone);
    const hits = [...unknown.counts.entries()].flatMap(([word, count]) => Array.from({ length: count }, () => unknown.first.get(word)));
    report.unknown = { total: hits.length, distinct: unknown.counts.size, tokens: unknown.total, title: "not covered by dictionary", clusters: reportClusters(text, hits, perRule) };
  }
  return report;
}

/* clusterAt's boundary walk without the display cap: the full token span
   [start, end) around an index, bounded by the same SPLIT separators. */
function tokenSpanAt(text, index) {
  let start = index;
  while (start > 0 && !SPLIT.test(text[start - 1])) start -= 1;
  let end = index;
  while (end < text.length && !SPLIT.test(text[end])) end += 1;
  return [start, end];
}

/**
 * Coordinates for repair: every token span the named rules flag, as
 * [{ start, end, token, rules }] sorted by start, indices in UTF-16 code
 * units against the scanned string (the same indices the finders return).
 *
 * scanText's report is a verdict — totals and a capped sample of clusters
 * for display. A repair that must touch ONLY what the scan found needs
 * positions, all of them, so it can splice those spans and leave every
 * other byte alone. Hits are expanded to their whole token (the unit a
 * repair rewrites) and deduplicated across rules; a token flagged by two
 * rules appears once with both names.
 */
export function scanBrokenSpans(text, {
  dictionary = null,
  standalone = [],
  rules = ["orphan", "impossible", "stray", "virama", "unknown"],
} = {}) {
  const wanted = new Set(rules);
  const hits = [];
  for (const [key, , finder] of RULES) {
    if (!wanted.has(key)) continue;
    for (const index of finder(text)) hits.push([key, index]);
  }
  if (dictionary && wanted.has("unknown")) {
    for (const index of unknownWords(text, dictionary, standalone).positions) {
      hits.push(["unknown", index]);
    }
  }
  const bySpan = new Map();
  for (const [rule, index] of hits) {
    const [start, end] = tokenSpanAt(text, index);
    const key = `${start}:${end}`;
    const item = bySpan.get(key) || { start, end, token: text.slice(start, end), rules: new Set() };
    item.rules.add(rule);
    bySpan.set(key, item);
  }
  return [...bySpan.values()]
    .map(({ rules: rs, ...rest }) => ({ ...rest, rules: [...rs] }))
    .sort((a, b) => a.start - b.start);
}

export function formatScanReport(report, { source = "", totalChars = 0 } = {}) {
  const out = ["=".repeat(78), `praser scan${source ? `  ${source}` : ""}`, ...(totalChars ? [`${totalChars.toLocaleString()} characters`] : []), "=".repeat(78), "", "rule              hits  distinct   what it means"];
  for (const [key, value] of Object.entries(report)) out.push(`${key.padEnd(16)}${String(value.total).padStart(6)}${String(value.distinct).padStart(10)}   ${value.title}`);
  for (const [key, value] of Object.entries(report)) {
    if (!value.clusters.length) continue;
    out.push("", `[${key}]`);
    for (const item of value.clusters) {
      out.push(`  cluster: ${item.cluster}  (${item.count} hit(s), line ${item.line})`);
      if (item.context) out.push(`    context: ${item.context}`);
    }
  }
  return `${out.join("\n")}\n`;
}

/**
 * Mirror of scan.py's `clause_model`: which forms stand alone as their own
 * word. U+104D U+104C U+104F U+1024 U+102A and the like are words, not
 * affixes -- gluing them onto a neighbour is the tokeniser's own error, and
 * it surfaces as a long unknown compound rather than as damage in the text.
 *
 * Returns [standalone, stick].
 */
export function clauseModel(filePath) {
  const mapData = (loadMap(filePath) || {}).mapData || {};
  const newClause = mapData.newClause || {};
  return [newClause.standalone || [], newClause.stick || []];
}

export { loadMap, loadScanDictionary, dictionaryWords, unknownWords, RULES };

/**
 * FILE: Parsed/parser/text/repair.mjs
 *
 * Dictionary-arbitrated repair of homoglyph confusions.
 *
 * Several Myanmar code points share a single glyph in legacy faces -- ဝ/၀,
 * ရ/၇, ဉ/ဥ. A legacy code page has to resolve each shared glyph to one side,
 * and when it picks the wrong one the text is left with a character that
 * looks right and reads wrong. `scan.mjs`'s `digit` rule reports one case of
 * this (a digit flanked by letters) but it is one-directional: a LETTER
 * standing where a digit belongs is invisible to it, and ဉ/ဥ is outside it
 * entirely.
 *
 * Repairing by rule is unsafe -- ၀ is often a genuine digit and ဝ a genuine
 * letter -- so the dictionary arbitrates instead:
 *
 *   1. the token must NOT already be a word
 *   2. exactly ONE homoglyph-swapped variant must BE a word
 *   3. if two or more variants resolve, change nothing
 *
 * Under those conditions a repair cannot invent a word that was not already
 * in the dictionary, and cannot choose between competing readings.
 *
 * Measured over mm-grammar.pdf (445 pages, 3952 unknown tokens): 67 repaired,
 * 0 ambiguous. The dictionary never returned two candidates.
 */
import fs from "node:fs";
import path from "node:path";
import { MAP_DIR } from "../core/paths.mjs";
import { scanText, scanBrokenSpans } from "./scan.mjs";
import { orderText, unmixZawCodeMarks } from "./cleanup.mjs";
import { scoreText } from "./mm-score.mjs";

const DATA = JSON.parse(fs.readFileSync(path.join(MAP_DIR, "homoglyphs.json"), "utf8"));

/** [a, b] pairs, usable in either direction. */
export const HOMOGLYPH_PAIRS = DATA.pairs.map(([a, b]) => [a, b]);

/** Every single-character swap, both directions, as [from, to]. */
const SWAPS = HOMOGLYPH_PAIRS.flatMap(([a, b]) => [[a, b], [b, a]]);

/** Tokens are Myanmar letters, marks and digits -- the same run scan.mjs uses. */
const MM_RUN = /[\u1000-\u1049\u105a-\u109f]+/gu;

/**
 * Every distinct token reachable by homoglyph swaps: all occurrences of a
 * character replaced together, and each occurrence replaced on its own.
 */
export function homoglyphVariants(token) {
  const out = new Set();
  for (const [from, to] of SWAPS) {
    if (!token.includes(from)) continue;
    out.add(token.split(from).join(to));
    let i = -1;
    while ((i = token.indexOf(from, i + 1)) >= 0) {
      out.add(token.slice(0, i) + to + token.slice(i + 1));
    }
  }
  out.delete(token);
  return [...out];
}

/**
 * The single dictionary-resolving variant of `token`, or null when the token
 * is already a word, nothing resolves, or more than one does.
 */
export function repairToken(token, words) {
  if (!words || !words.size || words.has(token)) return null;
  const hits = homoglyphVariants(token).filter((v) => words.has(v));
  return hits.length === 1 ? hits[0] : null;
}

/**
 * Repair homoglyph confusions across a whole text.
 * Returns { text, repairs } where repairs is [{ from, to, count }].
 */
export function repairHomoglyphs(text, words) {
  if (!text || !words || !words.size) return { text, repairs: [] };
  const tally = new Map();
  const out = String(text).replace(MM_RUN, (token) => {
    const fixed = repairToken(token, words);
    if (!fixed) return token;
    const key = `${token}\u0000${fixed}`;
    tally.set(key, (tally.get(key) || 0) + 1);
    return fixed;
  });
  const repairs = [...tally].map(([k, count]) => {
    const [from, to] = k.split("\u0000");
    return { from, to, count };
  }).sort((a, b) => b.count - a.count);
  return { text: out, repairs };
}

/**
 * Split a number that extraction glued onto a word.
 *
 * Table-of-contents and index lines lose their dot leaders during
 * extraction, so the page number joins the heading and the whole token
 * misses the dictionary -- သဒ္ဒါ၁, ဝဂ်၃၈, မျိုး၁၀၅, and in numbered lists the
 * marker leads instead: ၁။သဒ္ဒါ.
 *
 * The dictionary arbitrates exactly as it does for homoglyphs: a split is
 * applied only when the token is not already a word and stripping the digit
 * run from ONE end leaves a word. If both ends would work the token is left
 * alone. Over mm-grammar.pdf that is 108 trailing and 330 leading splits
 * with zero ambiguous cases.
 *
 * Returns [word, digits] in reading order, or null.
 */
export function splitGluedNumber(token, words) {
  if (!words || !words.size || words.has(token)) return null;
  const tail = /^(.*?)([\u1040-\u1049]+)$/u.exec(token);
  const head = /^([\u1040-\u1049]+)(.*)$/u.exec(token);
  const tailOk = Boolean(tail && tail[1] && words.has(tail[1]));
  const headOk = Boolean(head && head[2] && words.has(head[2]));
  if (tailOk && headOk) return null;        // ambiguous, change nothing
  if (tailOk) return [tail[1], tail[2]];
  if (headOk) return [head[1], head[2]];
  return null;
}

/**
 * Apply both repairs across a text: homoglyph swaps first, then glued
 * numbers. Returns { text, repairs, splits }.
 */
export function repairText(text, words) {
  const first = repairHomoglyphs(text, words);
  const tally = new Map();
  const out = first.text.replace(MM_RUN, (token) => {
    const parts = splitGluedNumber(token, words);
    if (!parts) return token;
    tally.set(token, (tally.get(token) || 0) + 1);
    return parts.join(" ");
  });
  const splits = [...tally].map(([from, count]) => ({ from, count }))
    .sort((a, b) => b.count - a.count);
  return { text: out, repairs: first.repairs, splits };
}

/*
 * ZawCode stored-order mend, applied ONLY where the scanner points.
 *
 * cleanup.mjs's unmixZawCodeMarks is the pure rule and it is destructive
 * on finished Unicode — မြေတော် would lose its ြ to တ. What separates the
 * damaged နြေပီ from the healthy မြေတော် is the scan verdict itself:
 * နြေပီ flags (နြေ covers no dictionary segment), မြေတော် / ကြေနပ် /
 * ကြေးဇူး come back clean. So the rule never goes looking for work:
 * scanBrokenSpans hands over the exact token coordinates the scan
 * flagged, the pure rule rewrites just those spans, and the splice runs
 * back-to-front so earlier coordinates stay valid. Text the scan did not
 * flag is byte-identical going out.
 *
 * This is the "repair driven by what the scan found" the slot below asks
 * for, kept as its own named rule and NOT folded into the generic
 * repairText: the worker reaches it only on the separated `zawcode`
 * path, behind that path's scan verdict.
 *
 * Returns { text, repairs } where repairs is [{ from, to, count }], the
 * same shape as repairHomoglyphs.
 */
const MEND_GATE_RULES = ["orphan", "impossible", "stray", "virama", "unknown"];

function gateHits(token, words, standalone) {
  const rep = scanText(token, { dictionary: words, standalone, perRule: 1 });
  return MEND_GATE_RULES.reduce((n, k) => n + (rep[k]?.total ?? 0), 0);
}

/**
 * The readable-result guard that splits ZawCode-stored Unicode out of the
 * Unicode/cmap reading. It judges text, never a font name: the same raw
 * cmap sample is read two ways — as finished Unicode (`orderText`) and
 * through the ZawCode stored-order rule first
 * (`orderText(unmixZawCodeMarks(...))`). The structural score alone cannot
 * separate them on the real ယဲ့နင် source (both readings 0.999 over
 * the probe pages); the dictionary scan gate can (pipeline guard sample
 * 116 -> 110; on the finished page text 70 -> 60), while applying the
 * same crossed reading to the finished mm-grammar / bg-mlc-1-1 texts
 * moves their gates the other way (219 -> 272, 532 -> 558).
 * Zawgyi-cmap readings never reach the split: sample/test score
 * 0.878/0.881 as ordered Unicode, far under the 0.99 floor.
 */
export function zawCodeGuard(sample, words, standalone = []) {
  const empty = {
    zawcode: false, pureScore: 0, crossedScore: 0, marks: 0,
    pureGate: 0, crossedGate: 0, gain: 0,
  };
  if (!sample || !words || !words.size) return empty;
  const pureText = orderText(String(sample));
  const crossedText = orderText(unmixZawCodeMarks(String(sample)));
  const pure = scoreText(pureText);
  const crossed = scoreText(crossedText);
  const pureGate = gateHits(pureText, words, standalone);
  const crossedGate = gateHits(crossedText, words, standalone);
  const gain = pureGate - crossedGate;
  const zawcode = pure.marks >= 50
    && pure.score >= 0.99
    && crossed.score >= pure.score - 0.001
    && crossedText !== pureText
    && pureGate >= 10
    && gain >= 2
    && gain * 20 >= pureGate;
  return {
    zawcode,
    pureScore: pure.score,
    crossedScore: crossed.score,
    marks: pure.marks,
    pureGate,
    crossedGate,
    gain,
  };
}

export function mendZawCodeText(text, words, standalone = []) {
  if (!text || !words || !words.size) return { text, repairs: [] };
  const spans = scanBrokenSpans(String(text), { dictionary: words, standalone });
  const tally = new Map();
  let out = String(text);
  for (let s = spans.length - 1; s >= 0; s -= 1) {
    const { start, end, token } = spans[s];
    const fixed = unmixZawCodeMarks(token);
    if (fixed === token) continue;
    // A flagged token can hold healthy clusters beside the damaged one
    // (ဖြေးညှင်းစွာ: ဖြေး is finished, only the covering fails). The pure
    // rule cannot tell them apart, the scanner can: the rewrite is kept
    // only when it re-scans strictly cleaner than what it replaces.
    if (gateHits(fixed, words, standalone) >= gateHits(token, words, standalone)) continue;
    const key = `${token}\u0000${fixed}`;
    tally.set(key, (tally.get(key) || 0) + 1);
    out = out.slice(0, start) + fixed + out.slice(end);
  }
  const repairs = [...tally].map(([k, count]) => {
    const [from, to] = k.split("\u0000");
    return { from, to, count };
  }).sort((a, b) => b.count - a.count);
  return { text: out, repairs };
}

/**
 * The slot behind the scan verdict.
 *
 * Order in the worker (worker/runtime.mjs): dictionary repairs -> scanText
 * verdict on the wininnwa / ayar / vector / zawcode paths -> THIS rule ->
 * write. A repair driven by what the scan found belongs here and nowhere
 * else, so the scan never grows a second, hidden repair hand inside
 * itself.
 *
 * One rule fills the slot today: on the separated `zawcode` path, the
 * scan-coordinate mend above. Pure Unicode needs no such rule — a correct
 * reading scans clean — and the other verdict paths keep the pass-through:
 * their repairs are the code-page/shape traceback upstream, not a
 * behind-scan rewrite. Page text and layout lines are mended with the
 * same accepted token replacements, so the TXT and DOCX writers cannot
 * disagree about what was repaired.
 */
export function repairFromScanVerdict(pages, report, words, { path = null, standalone = [] } = {}) {
  if (path !== "zawcode" || !report || !words || !words.size) return pages;
  return pages.map((page) => {
    const text = page?.text || "";
    if (!text) return page;
    const mended = mendZawCodeText(text, words, standalone);
    if (mended.text === text) return page;
    const byToken = new Map(mended.repairs.map(({ from, to }) => [from, to]));
    let lines = Array.isArray(page.lines)
      ? page.lines.map(([x, y, size, lineText]) => [
        x, y, size,
        String(lineText ?? "").replace(MM_RUN, (token) => byToken.get(token) ?? token),
      ])
      : page.lines;
    // Page text and layout lines are assembled separately (and the
    // earlier dictionary repairs touch text only), so a repaired page
    // token can fail to appear verbatim in any line. On those pages the
    // lines get their own scan-coordinate mend rather than silently
    // keeping the damage for the layout writer.
    if (Array.isArray(lines)
      && lines.every((line, i) => line[3] === page.lines[i][3])) {
      lines = page.lines.map(([x, y, size, lineText]) => [
        x, y, size,
        mendZawCodeText(String(lineText ?? ""), words, standalone).text,
      ]);
    }
    return { ...page, text: mended.text, ...(lines ? { lines } : {}) };
  });
}

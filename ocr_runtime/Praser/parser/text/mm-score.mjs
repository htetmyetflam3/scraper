


import fs from "node:fs";
import path from "node:path";
import { MAP_DIR } from "../core/paths.mjs";

/**
 * DATA: mm-score.json is generated from mm_score.py by
 * ../tools/gen-parser-data.mjs. Never hand-edit it, and never retype a
 * Python table here -- these two tables previously existed twice, hardcoded
 * below AND inside normalize.json, with nothing keeping the copies in step.
 */
const DATA = JSON.parse(fs.readFileSync(path.join(MAP_DIR, "mm-score.json"), "utf8"));

export const MARKS = new Set(DATA.marks.map((c) => c.codePointAt(0)));

/**
 * The single copy of the mark priority table. cleanup.mjs imports it from
 * here, exactly as cleanup.py does `from mm_score import MARK_ORDER`.
 */
export const MARK_ORDER = new Map(Object.entries(DATA.priority));

export function isIndex(ch) {
  if (!ch) return false;
  const cp = ch.codePointAt(0);
  return (cp >= 0x1000 && cp <= 0x1021) || cp === 0x1025 || cp === 0x1027;
}

export function isMark(ch) {
  return !!ch && MARKS.has(ch.codePointAt(0));
}

export function isMyanmar(ch) {
  if (!ch) return false;
  const cp = ch.codePointAt(0);
  return cp >= 0x1000 && cp <= 0x109f;
}

export function myanmarRatio(text) {
  if (!text) return 0.0;
  const chars = Array.from(text);
  let n = 0;
  for (const ch of chars) if (isMyanmar(ch)) n += 1;
  return n / chars.length;
}


function* walk(text) {
  let attached = false;
  let run = [];
  for (const ch of text) {
    const mark = isMark(ch);
    yield { ch, attached, run };
    if (isIndex(ch)) {
      attached = true;
      run = [];
    } else if (mark) {
      if (attached) run.push(ch);
    } else {
      attached = false;
      run = [];
    }
  }
}

export function orphanMarkRate(text) {
  let total = 0;
  let bad = 0;
  for (const { ch, attached } of walk(text)) {
    if (isMark(ch)) {
      total += 1;
      if (!attached) bad += 1;
    }
  }
  return total ? bad / total : 0.0;
}

export function duplicateMarkRate(text) {
  let total = 0;
  let dup = 0;
  for (const { ch, attached, run } of walk(text)) {
    if (isMark(ch) && attached) {
      total += 1;
      if (run.includes(ch)) dup += 1;
    }
  }
  return total ? dup / total : 0.0;
}

export function orderViolationRate(text) {
  let pairs = 0;
  let bad = 0;
  let run = null;
  for (const ch of text) {
    if (isIndex(ch)) {
      run = [];
      continue;
    }
    if (MARKS.has(ch.codePointAt(0))) {
      if (run !== null) {
        run.push(ch);
        if (run.length > 1) {
          pairs += 1;
          if (MARK_ORDER.get(run[run.length - 2]) > MARK_ORDER.get(run[run.length - 1])) {
            bad += 1;
          }
        }
      }
      continue;
    }
    run = null;
  }
  return pairs ? bad / pairs : 0.0;
}


export function scoreText(text) {
  const chars = Array.from(text);
  let marks = 0;
  for (const ch of chars) if (isMark(ch)) marks += 1;
  if (!marks) {
    return { score: 0.0, orphan: 0.0, disorder: 0.0, duplicate: 0.0, marks: 0, myanmarRatio: myanmarRatio(text) };
  }
  const orphan = orphanMarkRate(text);
  const disorder = orderViolationRate(text);
  return {
    score: 1.0 - Math.max(orphan, disorder),
    orphan,
    disorder,
    duplicate: duplicateMarkRate(text),
    marks,
    myanmarRatio: myanmarRatio(text),
  };
}


/**
 * The ONE-SIDED English detector, sibling of the Burmese check above.
 *
 * A legacy Myanmar layout pushed through its vague Latin /ToUnicode decodes
 * to ASCII salad ("abdegajdl"), while real Latin faces in the same book carry
 * real English. In a Burmese book English appears as words and short labels
 * ("Update:", "Myanmar Grammar: MLC") -- never as the sentence structure the
 * Burmese scorer relies on -- so this check judges WORDS, not sentences:
 *
 *   - SPACE is the confidence signal: salad from a ToUnicode without a space
 *     code arrives as one unbroken run; real English arrives as many short
 *     separated words.
 *   - WORD LENGTH: ordinary English words stay under ~15 letters; runs longer
 *     than 16 are rare, longer than 20 are effectively absent from books, and
 *     the dictionary freak maximum is 45 (pneumonoultramicroscopicsilicano-
 *     volcanoconiosis). A longer unbroken run is salad.
 *   - IMPOSSIBLE SEQUENCES, the English counterpart of ိ+ံ: a Q not followed
 *     by U, the doubles qq/yy, four identical letters in a row, vowel-less
 *     words of 3+ letters, and consonant pairs English never writes (jq, vq,
 *     xz, bz, ...).
 *   - COMMON WORDS: the/of/and/... cover a large share of any real snippet.
 *
 * The verdict is one-sided on purpose: "english" excludes a font from the
 * Myanmar face vote; "not-english" keeps it as a legacy candidate; null means
 * too little signal to judge -- the pixels decide.
 */
const COMMON_ENGLISH = new Set(("the of and a to in is you that it he was for on are as with his they i at be this "
  + "have from or one had by word but not what all were we when your can said there use an each which she do how "
  + "their if will up other about out many then them these so some her would make like him into time has look two "
  + "more write go see number no way could people my than first water been call who its now find long down day did "
  + "get come made may part update volume volumes three commission ministry education government language union "
  + "myanmar grammar mlc jan feb mar apr jun jul aug sep oct nov dec am pm").split(" "));

const VOWELLESS_OK = new Set(["shh", "hmm", "grr", "brr", "psst", "tsk", "cwm", "nth"]);
const IMPOSSIBLE_PAIRS = new Set([
  "bq", "cq", "gq", "hq", "jq", "kq", "lq", "mq", "pq", "vq", "wq", "xq", "yq", "zq",
  "qg", "qh", "qj", "qk", "ql", "qm", "qo", "qr", "qs", "qv", "qw", "qx", "qy", "qz",
  "jz", "jx", "vx", "xz", "bz", "pz", "vz", "wz", "kx", "cx", "bx", "fx", "gx", "hx",
  "mx", "px", "dx", "qq", "yy",
]);
const isAsciiLetter = (ch) => (ch >= "a" && ch <= "z") || (ch >= "A" && ch <= "Z");

export function englishLikeness(text) {
  const sample = String(text || "");
  // Letter runs: maximal unbroken ASCII-letter sequences (everything else,
  // including Burmese code points and Latin-1 symbols, acts as a separator).
  const runs = [];
  let cur = "";
  for (const ch of sample) {
    if (isAsciiLetter(ch)) cur += ch;
    else if (cur) { runs.push(cur); cur = ""; }
  }
  if (cur) runs.push(cur);
  const letters = runs.reduce((n, w) => n + w.length, 0);
  if (!runs.length) return { verdict: null, letters: 0, runs: 0, commonRatio: 0, violationRate: 0, maxRun: 0, hasSpaces: false };

  const maxRun = Math.max(...runs.map((w) => w.length));
  const hasSpaces = runs.length > 1 && /[ ]/.test(sample);
  const wordRuns = runs.filter((w) => w.length >= 2).map((w) => w.toLowerCase());
  const common = wordRuns.filter((w) => COMMON_ENGLISH.has(w)).length;
  const commonRatio = wordRuns.length ? common / wordRuns.length : 0;

  let violations = 0;
  for (const w of runs) {
    const lower = w.toLowerCase();
    const acronym = w.length <= 6 && w === w.toUpperCase(); // MLC, TIL, AM -- real English too
    if (!acronym && lower.length >= 3 && !VOWELLESS_OK.has(lower) && !/[aeiouy]/.test(lower)) violations += 1; // vowel-less word
    if (/(.)\1\1\1/.test(lower)) violations += 2;                                               // aaaa
    for (let i = 0; i + 1 < lower.length; i += 1) {
      if (IMPOSSIBLE_PAIRS.has(lower.slice(i, i + 2))) violations += 1;
      if (lower[i] === "q" && lower[i + 1] !== "u") violations += 1;                            // q without u
    }
    if (lower.length > 16) violations += 1;                                                     // rare-long word
    if (lower.length > 20) violations += 1;                                                     // not a book word
    if (lower.length > 45) violations += 2;                                                     // past the dictionary freak
  }
  const violationRate = letters ? violations / letters : 1;

  let verdict = null;
  if (letters >= 12) {
    if (commonRatio >= 0.15 && violationRate <= 0.02 && maxRun <= 20) verdict = "english";
    else if (commonRatio >= 0.08 && hasSpaces && violationRate <= 0.01 && maxRun <= 16) verdict = "english";
    else if (violationRate > 0.03 && commonRatio < 0.05) verdict = "not-english";
    else if (!hasSpaces && maxRun > 20 && commonRatio < 0.05) verdict = "not-english";
    else if (commonRatio > 0 && commonRatio < 0.05 && violationRate > 0.015) verdict = "not-english";
    else if (commonRatio === 0 && letters >= 24 && runs.length >= 4) verdict = "not-english";
  }
  return { verdict, letters, runs: runs.length, commonRatio, violationRate, maxRun, hasSpaces };
}

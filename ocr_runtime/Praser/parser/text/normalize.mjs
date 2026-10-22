/**
 * FILE: Parsed/parser/text/normalize.mjs
 *
 * JS-only pipeline orchestration. There is deliberately no normalize.py --
 * this module has NO Python counterpart, so it must not define any data or
 * behaviour that a Python module owns.
 *
 * The five functions cleanup.py owns live in their mirror, cleanup.mjs, and
 * are re-exported here so existing callers keep working unchanged.
 * `normalizeGlue` is cleanup.py's `normalize_text`, aliased on the way
 * through because this module exports an unrelated `normalizeText` of its own
 * further down -- the one name that genuinely means two different things on
 * the two sides of the port.
 *
 * MARKS belongs to mm_score.py and is re-exported from its mirror for the
 * same reason. It used to be read from normalize.json while mm-score.mjs kept
 * a second hand-written copy; there is now one source.
 */
import fs from "node:fs";
import path from "node:path";
import { MAP_DIR } from "../core/paths.mjs";
import { MARKS } from "./mm-score.mjs";
import {
  cleanImposters,
  reorderMarks,
  unmixZawCodeMarks,
  movePrebaseMarks,
  dropImpossibleMarks,
  normalizeText as normalizeGlue,
  orderText,
  cleanupText,
} from "./cleanup.mjs";

export {
  MARKS, cleanImposters, reorderMarks, unmixZawCodeMarks, movePrebaseMarks,
  dropImpossibleMarks, normalizeGlue, orderText, cleanupText,
};

let external = null;


export async function initNormalize() {
  const mod = process.env.PRASER_NORMALIZE_MODULE;
  if (!mod) return { external: false };
  const imported = await import(mod);
  const fn = imported.cleanupText ?? imported.normalize ?? imported.default?.cleanupText ?? imported.default?.normalize;
  if (typeof fn !== "function") throw new TypeError(`${mod} exports no cleanupText/normalize function`);
  external = fn;
  return { external: true };
}

export const isExternalNormalize = () => external !== null;









const MAPDATA = JSON.parse(fs.readFileSync(path.join(MAP_DIR, "normalize-map.json"), "utf8"));

const PRIORITY2 = MAPDATA.priority;                                    
const GLUE = new Set(MAPDATA.glue);
const PUNCTUATION = new Set(MAPDATA.punctuation);
const ENGLISH_DIGITS = new Set(["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]);
const BURMESE_DIGITS = new Set(["\u1040", "\u1041", "\u1042", "\u1043", "\u1044",
  "\u1045", "\u1046", "\u1047", "\u1048", "\u1049"]);




const IMPOSTER_REPS = [];
for (const entry of MAPDATA.imposter) {
  for (const imp of entry.imposters) IMPOSTER_REPS.push({ from: imp, to: entry.correct });
}
IMPOSTER_REPS.sort((a, b) => b.from.length - a.from.length);


export function engineCleanImposters(t) {
  let tmp = t;
  for (const r of IMPOSTER_REPS) tmp = tmp.split(r.from).join(r.to);
  return tmp;
}


const isClusterBase = (ch) => {
  const cp = ch.codePointAt(0);
  return (cp >= 0x1000 && cp <= 0x1021) || cp === 0x1025 || cp === 0x1027;
};


export function engineReorderString(t) {
  if (!t || typeof t !== "string") return "";
  const chars = Array.from(t);
  let out = "";
  let i = 0;
  while (i < chars.length && !isClusterBase(chars[i])) { out += chars[i]; i += 1; }
  while (i < chars.length) {
    const ch = chars[i];
    if (isClusterBase(ch)) {
      out += ch;
      i += 1;
      const marks = [];
      const seen = new Set();
      while (i < chars.length) {
        const next = chars[i];
        if (isClusterBase(next)) break;
        const p = PRIORITY2[next];
        if (p === undefined) break;
        if (!seen.has(p)) { seen.add(p); marks.push(next); }
        i += 1;
      }
      if (marks.length) {
        marks.sort((a, b) => PRIORITY2[a] - PRIORITY2[b]);
        out += marks.join("");
      }
    } else {
      out += ch;
      i += 1;
    }
  }
  return out;
}

const isLowerOrUpperAZ = (ch) => {
  if (typeof ch !== "string" || ch.length !== 1) return false;
  const code = ch.charCodeAt(0);
  return (code >= 97 && code <= 122) || (code >= 65 && code <= 90);   
};

const BLOCK_PUNCT = new Set([",", ".", ":", ";", "/", "(", ")", "-", "!", "?", " "]);

function blockCharType(ch) {
  if (isLowerOrUpperAZ(ch)) return "E";
  if (ENGLISH_DIGITS.has(ch)) return "L";
  if (BURMESE_DIGITS.has(ch)) return "B";
  if (BLOCK_PUNCT.has(ch)) return "P";
  return null;
}

function englishBlock(str, i) {
  const startType = blockCharType(str[i]);
  if (startType !== "E" && startType !== "L" && startType !== "B") return null;
  let j = i;
  while (j < str.length) {
    const t = blockCharType(str[j]);
    if (t === null) break;
    if (t === "P" || t === startType) { j += 1; continue; }
    break;
  }
  const block = str.slice(i, j).replace(/ +/g, " ");
  const trimmed = block.replace(/ +$/, "");
  return { end: j, trimmed, tail: block.slice(trimmed.length) };
}


/**
 * Bridge-token wrap — MAP BUILDER ONLY.
 *
 * `{...}` is not punctuation, it is the delimiter the MonoSyllabism map builder
 * puts around an English/digit block so the grapheme traverser sees one token
 * (upstream: MonoSyllabism/mapbuilder/base/normalize.js, `reorderAndWrap`,
 * reached through `mapperNormalize` — never through plain `normalize`).
 *
 * It must not run on text a human reads: it is what turned
 * "God of Slaughter" into "{God of Slaughter}" and "(၁)" into "({၁)}" in
 * parser output. Text output goes through `normalizeText` below, which is the
 * site-facing equivalent of the upstream `normalize()`.
 */
export function wrapEnglishBlocks(str) {
  let out = "";
  let i = 0;
  while (i < str.length) {
    const blk = englishBlock(str, i);
    if (blk) { out += `{${blk.trimmed}}${blk.tail}`; i = blk.end; continue; }
    out += str[i];
    i += 1;
  }
  return out;
}


export function deleteEnglishBlocks(str) {
  let out = "";
  let i = 0;
  while (i < str.length) {
    const blk = englishBlock(str, i);
    if (blk) { out += blk.tail; i = blk.end; continue; }
    out += str[i];
    i += 1;
  }
  return out;
}


/**
 * The text pipeline: NFC, drop glue + bridge delimiters, reorder marks, fix
 * imposters. Mirrors `normalize()` in MonoSyllabism/mapbuilder/base/normalize.js
 * and stops exactly where that function stops.
 *
 * It deliberately does NOT call `wrapEnglishBlocks`. `mapperNormalize` below is
 * the pipeline that adds bridge tokens, and only the map builder wants those.
 */
export function normalizeText(text, opts = {}) {
  if (!text || typeof text !== "string") return "";
  const { stripPunctuation = false, cleanMode = "gentle" } = opts;
  let result = text.normalize("NFC");
  const stripPunct = stripPunctuation || cleanMode === "aggressive";
  let cleaned = "";
  for (const ch of result) {
    if (GLUE.has(ch)) continue;
    
    
    if (ch === "$" || ch === "#" || ch === "^" || ch === "{" || ch === "}" || ch === "\u221A") continue;
    if (stripPunct && PUNCTUATION.has(ch)) continue;
    cleaned += ch;
  }
  result = engineReorderString(cleaned);
  result = engineCleanImposters(result);
  return result;
}


/**
 * normalizeText + bridge-token wrap, i.e. upstream `mapperNormalize` minus
 * `mapperStr`. Keep it exported and reachable (normalize mode "mapper") so the
 * map-building side still has the format it consumes — but no text output
 * route selects it.
 */
export function mapperNormalize(text, opts = {}) {
  if (!text || typeof text !== "string") return "";
  return wrapEnglishBlocks(normalizeText(text, opts));
}


export function myNormalize(str) {
  if (!str || typeof str !== "string") return "";
  let result = str.normalize("NFC");
  result = deleteEnglishBlocks(result);
  result = engineCleanImposters(result);
  result = engineReorderString(result);
  return result;
}



export const normalize = normalizeText;
export const mynormalize = myNormalize;

export const NORMALIZE_MODES = ["normalize", "mynormalize", "mapper", "none"];


export function pickNormalize(mode = "normalize") {
  if (mode === null || mode === undefined || mode === "" || mode === "none" || mode === false) return null;
  const m = String(mode).toLowerCase().replace(/[\s_-]/g, "");
  if (m === "normalize") return normalizeText;
  if (m === "mynormalize") return myNormalize;
  
  
  if (m === "mapper" || m === "mappernormalize") return mapperNormalize;
  throw new Error(`Unknown normalize mode: ${JSON.stringify(mode)} (use ${NORMALIZE_MODES.map((x) => `"${x}"`).join(" | ")})`);
}

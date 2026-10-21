/* formatter.js — the FORMAT event. One home for every output shape, and for
   the two text contracts the pipeline hands between its stages:

     √   the segmentor's suffix  ("data piece done") — was assembled inline in
         lexer.js at every emit branch; now written here once.
     <eos>  the tagger's suffix (untagged sentence boundary behind "။")

   Nothing in this file reads or writes files; a formatter turns values into
   display strings. That is the point of the split: the day the delimiter
   changes, only this file knows.
*/

import { toBurmeseStr } from "../../MonoSyllabism/map/loader.js";

/** Output delimiter between printed tokens. */
export const DELIM = "  ";

/* ── the √ handoff contract (segmentor → holder/tagger/formatters) ── */
export const SYLLABLE_SUFFIX = "√";
/** Explicit space the segmentor emits as its own barrier token. */
export const SPACE_TOKEN = " √";
/** Internal ASCII text → one emitted syllable token (Burmese surface + √). */
export function toSyllableToken(text) {
	return toBurmeseStr(text) + SYLLABLE_SUFFIX;
}
/** Reader side of the handoff: holder line → its tokens (empties dropped). */
export function splitSyllables(line) {
	return line.split(SYLLABLE_SUFFIX).filter((t) => t);
}
/** √-suffixed raw line → printed syllable line (writer's syllable file). */
export function toSyllableLine(lineStr) {
	return lineStr
		.split(SYLLABLE_SUFFIX)
		.filter((t) => t.trim())
		.join(DELIM);
}

/* ── the <eos> boundary contract (tagger → line formatters) ── */
export const EOS_MARK = "<eos>";
/** Tag a surface as the untagged sentence boundary. */
export function asEos(surface) {
	return surface + EOS_MARK;
}
export function isEosToken(surface) {
	return surface.endsWith(EOS_MARK);
}

/* ── token pieces ── */
export function tokenBurmese(t) {
	return (t.syllables ?? []).join("");
}

/** Tree-merged word id: the ids joined (endofmain's joinedIds field). */
export function mergedIds(ids) {
	return (ids ?? []).join("");
}

export function tokenId(t) {
	return mergedIds(t.ids) || tokenBurmese(t);
}

/** True when the token is the tagger-injected sentence boundary. */
export function tokenIsBoundary(t) {
	return isEosToken(tokenBurmese(t));
}

/* ── line formatters: one lineResult → one printed line ── */

/** Burmese only, flat syllables: "ကောင်း  ကင်" */
export function toBurmeseLine(lineResult) {
	const parts = [];
	for (const token of lineResult.tokens ?? []) {
		parts.push(...(token.syllables ?? []));
	}
	return parts.join(DELIM);
}

/** Burmese + pos tag, merged word: "ကောင်းကင်{n}"; a boundary prints bare. */
export function toBurmeseLineWithPos(lineResult, posField = "finalPos") {
	return (lineResult.tokens ?? [])
		.map((t) => (tokenIsBoundary(t) ? tokenBurmese(t) : `${tokenBurmese(t)}{${t[posField] ?? "?"}}`))
		.join(DELIM);
}

/* ── positional map (the syllable jobs API in Ginit) ── */

/** Base consonant test — the same rule the encoder uses. */
export function isBase(ch) {
	const cp = ch.codePointAt(0);
	return (cp >= 0x1000 && cp <= 0x1021) || cp === 0x1025 || cp === 0x1027;
}

/** One cleaned line → { index, syllable }; null when the line has no Burmese.
   Number/English tokens can carry a trailing space (classifyNumber /
   wrapEnglishBlocks), which turns the delimiter into 3 spaces — trim so the
   syllable after one is not mistaken for a non-Burmese token. */
export function toPositions(line) {
	const tokens = line
		.split(DELIM)
		.map((t) => t.trim())
		.filter((t) => t.length > 0);
	const burmese = tokens.filter((t) => isBase(Array.from(t)[0] ?? ""));
	if (burmese.length === 0) return null;
	return {
		index: Array.from(burmese[0])[0],
		syllable: burmese.map((t) => String(Array.from(t).length - 1)),
	};
}

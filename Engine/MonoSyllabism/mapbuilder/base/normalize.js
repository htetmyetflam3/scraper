import { debugLogInit } from "../../../Bridge/logen.js";
import { isLowerOrUpperAZ } from "./function.js";
import {
	Priority,
	Glue,
	IMPOSTER,
	ENGLISH_DIGITS,
	BURMESE_DIGITS,
	BLOCK_PUNCT,
} from "./sourceMap.js";
import { mapperStr } from "./sourceMapper.js";

debugLogInit();

// flat char tables come from sourceMap.js (data: sourceMap.json) — re-exported
// here so map-loader.js's surface is unchanged.


// ─── Character classifiers ───────────────────────────────────────────────
const index = (ch) => {
	const cp = ch.codePointAt(0);
	return (
		(cp >= 0x1000 && cp <= 0x1021) || cp === 0x1025 || cp === 0x1027
	);
};

function classifyEnglish(ch) {
	if (isLowerOrUpperAZ(ch)) return "E";
	if (BLOCK_PUNCT.has(ch)) return "P";
	return null;
}

function classifyDigit(ch) {
	if (ENGLISH_DIGITS.has(ch)) return "L";
	if (BURMESE_DIGITS.has(ch)) return "B";
	return null;
}

function classifyBlock(ch) {
	return classifyEnglish(ch) || classifyDigit(ch);
}

// ─── Imposter trie (built once at module load) ────────────────────────────

const _imposterTrie = (() => {
	const trie = {};
	for (const entry of Object.values(IMPOSTER)) {
		for (const imp of entry.imposters) {
			let node = trie;
			for (const ch of imp) {
				if (!node[ch]) node[ch] = {};
				node = node[ch];
			}
			node._replacement = entry.correct;
		}
	}
	return trie;
})();

export function cleanImposters(text) {
	const chars = Array.from(text);
	let out = "";
	let i = 0;
	while (i < chars.length) {
		let node = _imposterTrie;
		let j = i;
		let lastMatch = -1;
		let replacement = null;
		while (j < chars.length && node[chars[j]]) {
			node = node[chars[j]];
			j++;
			if (node._replacement !== undefined) {
				lastMatch = j;
				replacement = node._replacement;
			}
		}
		if (replacement !== null) {
			out += replacement;
			i = lastMatch;
		} else {
			out += chars[i];
			i++;
		}
	}
	return out;
}

// ─── Reorder + Wrap (single pass) ────────────────────────────────────────

export function reorderAndWrap(text) {
	if (!text || typeof text !== "string") return "";

	const chars = Array.from(text);
	let out = "";
	let i = 0;

	// Phase A: skip only leading spaces/tabs. (This used to pass through ALL
	// leading non-index chars — which let a block at the very start, like
	// "hello ကာ", escape unwrapped. Non-whitespace leaders need no skip: the
	// loop below passes them through or wraps them, per type.)
	while (i < chars.length && (chars[i] === " " || chars[i] === "\t")) {
		out += chars[i];
		i++;
	}

	// Phase B: fused loop
	while (i < chars.length) {
		const ch = chars[i];

		// reorderString logic: index char + dependent marks
		if (index(ch)) {
			out += ch;
			i++;
			const marks = [];
			const seen = new Set();
			while (i < chars.length) {
				const next = chars[i];
				if (index(next)) break;
				const p = Priority[next];
				if (p === undefined) break;
				if (!seen.has(p)) {
					seen.add(p);
					marks.push(next);
				}
				i++;
			}
			if (marks.length) {
				marks.sort((a, b) => Priority[a] - Priority[b]);
				out += marks.join("");
			}
			continue;
		}

		// wrapEnglishBlocks logic: English/digit block detection
		const startType = classifyBlock(ch);
		if (startType === "E" || startType === "L" || startType === "B") {
			let j = i;
			while (j < chars.length) {
				const t = classifyBlock(chars[j]);
				if (t === null) break;
				if (t === "P") { j++; continue; }
				if (t === startType) { j++; continue; }
				break;
			}
			/* Trailing spaces belong BETWEEN tokens, not inside one.
			   Leaving them in produced "{hello }" → a token "hello "
			   whose trailing space became a 3-space delimiter and a
			   leading-space syllable downstream. */
			const block = chars.slice(i, j).join("").replace(/ +/g, " ");
			const trimmed = block.replace(/ +$/, "");
			out += "{" + trimmed + "}" + block.slice(trimmed.length);
			i = j;
			continue;
		}

		// default: passthrough
		out += ch;
		i++;
	}

	return out;
}

// ─── English block scan (the finder deleteEnglishBlocks consumes) ─────────
// Same scan as the wrap branch of reorderAndWrap, as a locator: one run of a
// single block type (E/L/B) that block punctuation may interrupt, internal
// spaces collapsed, trailing spaces split off BETWEEN tokens.

function EnglishBlock(text, i) {
	const startType = classifyBlock(text[i]);
	if (startType !== "E" && startType !== "L" && startType !== "B") return null;
	let j = i;
	while (j < text.length) {
		const t = classifyBlock(text[j]);
		if (t === null) break;
		if (t === "P" || t === startType) { j++; continue; }
		break;
	}
	const block = text.slice(i, j).replace(/ +/g, " ");
	const trimmed = block.replace(/ +$/, "");
	return { end: j, trimmed, tail: block.slice(trimmed.length) };
}

// ─── Delete English blocks ───────────────────────────────────────────────

export function deleteEnglishBlocks(text) {
	let out = "";
	let i = 0;
	while (i < text.length) {
		const blk = EnglishBlock(text, i);
		if (blk) { out += blk.tail; i = blk.end; continue; }
		out += text[i];
		i += 1;
	}
	return out;
}

// ─── Normalize ───────────────────────────────────────────────────────────

export function normalize(text) {
	if (!text || typeof text !== "string") return "";

	let result = text.normalize("NFC");
	let cleaned = "";
	for (const ch of result) {
		/* Glue includes the bridge token delimiters (#, ^, {, }, √) —
		   stripped on the way in so input text can never collide with them. */
		if (Glue.has(ch)) continue;
		cleaned += ch;
	}
	result = cleaned;
	result = cleanImposters(result);
	return result;
}

export { normalize as default };

// ─── Public pipelines ────────────────────────────────────────────────────

export function mapperNormalize(text) {
	if (!text || typeof text !== "string") return "";
	const cleaned = normalize(text);
	const reordered = reorderAndWrap(cleaned);
	return mapperStr(reordered);
}

export function myNormalize(text) {
	if (!text || typeof text !== "string") return "";
	const cleaned = normalize(text);
	const stripped = deleteEnglishBlocks(cleaned);
	return mapperStr(stripped);
}

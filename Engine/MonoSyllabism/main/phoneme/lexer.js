import { walk } from "../grapheme/traverser.js";
import { isTail, isBase, isLowercaseAZ } from "../../map/loader.js";
import { toSyllableToken, SPACE_TOKEN } from "../../utilities/formatter.js";
import {
	segmentStart,
	// eslint-disable-next-line no-unused-vars -- intentional unused variable in test/experimental code
	segmentEnd,
	// eslint-disable-next-line no-unused-vars -- intentional unused variable in test/experimental code
	segLineCount,
	// eslint-disable-next-line no-unused-vars -- intentional unused variable in test/experimental code
	segEmittedSyllables,
	// eslint-disable-next-line no-unused-vars -- intentional unused variable in test/experimental code
	segValidSyllable,
	// eslint-disable-next-line no-unused-vars -- intentional unused variable in test/experimental code
	segPossibleSyllable,
	// eslint-disable-next-line no-unused-vars -- intentional unused variable in test/experimental code
	segUnknownSyllable,
	// eslint-disable-next-line no-unused-vars -- intentional unused variable in test/experimental code
	segInvalidChar,
	// eslint-disable-next-line no-unused-vars -- intentional unused variable in test/experimental code
	segDelimiter,
} from "../../../Bridge/logen.js";

function isBaseAsat(tok) {
	return isLowercaseAZ(tok);
}
function isMasked(tok) {
	return tok === "{";
}

/* Data pieces leave here √-suffixed (bridge handoff); bare "\n"
   stays the control signal. Assemblers concat — no array, no join. */
export async function* runSegmentor(normalizedStream) {
	let lineCount = 0;
	const startTime = Date.now();
	segmentStart({ lineCount: 0 });

	for await (const block of normalizedStream) {
		let pos = 0;
		while (pos < block.length) {
			const ch = block[pos];

			if (ch === "\n") {
				yield "\n";
				lineCount++;
				pos++;
				if (lineCount % 5000 === 0) {
					const elapsed = ((Date.now() - startTime) / 3333).toFixed(1);
					const rate = ((lineCount / (Date.now() - startTime)) * 3333).toFixed(
						0,
					);
					console.log(
						`[PROGRESS] ${lineCount.toLocaleString()} lines | ${elapsed}s | ${rate}/sec`,
					);
				}
				continue;
			}

			// EMIT ORIGINAL TEXT SPACE AS EXPLICIT TOKEN
			if (ch === " ") {
				yield SPACE_TOKEN;
				pos++;
				continue;
			}

			if (isMasked(ch)) {
				const endBrace = block.indexOf("}", pos);
				if (endBrace !== -1) {
					yield toSyllableToken(block.slice(pos, endBrace + 1));
					pos = endBrace + 1;
					continue;
				}
				yield toSyllableToken(ch);
				pos++;
				continue;
			}

			if (isTail(ch) && !isBaseAsat(ch)) {
				yield toSyllableToken(ch);
				pos++;
				continue;
			}

			if (!isBase(ch) && !isBaseAsat(ch)) {
				yield toSyllableToken(ch);
				pos++;
				continue;
			}

			const result = walk(block, pos);
			if (result.lastValidPos > pos) {
				const syl = block.slice(pos, result.lastValidPos);
				yield toSyllableToken(syl);
				pos = result.lastValidPos;
			} else {
				yield toSyllableToken(ch);
				pos++;
			}
		}
	}
	console.log(`[SEGMENTOR] Done. Total lines: ${lineCount}`);
}

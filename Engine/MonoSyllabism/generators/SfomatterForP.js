/* \n-assembler for the syllable-only path. Pieces arrive √-suffixed
   from the segmentor — pure concat, no array, no join. Bare "\n" is
   the control signal: emit the line. */
export async function* toLineStrings(seg) {
	let line = "";
	for await (const item of seg) {
		if (item === "\n") {
			yield line;
			line = "";
			continue;
		}
		if (typeof item !== "string") continue;
		line += item;
	}
	if (line.length) yield line;
}
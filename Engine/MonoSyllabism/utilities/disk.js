/* disk.js — the DISK event: common fs read/write to disk.

   One home for everything that actually touches the filesystem: the session
   hash that names this run's output files, the stream writer the bridge uses,
   the tree writers the graph build uses, and the small common fs helpers.
   (Module-store.js — renamed from _writer.js — stream + tree writers.)

   Reads that need a stream (collector) or sizing (buffer tiers) are NOT here:
   stream.js owns the streaming event, buffer.js owns buffer and size.
   Enc-aware JSON parsing lives where the trees load — S.loaded.js.
*/

import fs from "fs";
import { writerStart, writerEnd, writerWrote, getDateTimeHash, readerStart, readerEnd, readerFail } from "../../Bridge/logen.js";
import { OutputFile, TreeFile, joinPath } from "../../Bridge/path.js";
import { toSyllableLine } from "./formatter.js";
import { toLineStrings } from "../generators/SfomatterForP.js";

// ── session hash (names this run's segmented_<hash>_*.txt) ──
let _sessionHash = null;

function getHash() {
	if (!_sessionHash) _sessionHash = getDateTimeHash();
	return _sessionHash;
}

export function resetHash() {
	_sessionHash = null;
}

export function setHash(hash) {
	_sessionHash = hash;
}

// ── the stream writer (the bridge's output channel) ──
export function createStreamWriter(outputDir = OutputFile(), suffix = "", flushLines = 1000) {
	const ts = getHash();
	const suffixPart = suffix ? `_${suffix}` : "";
	const outputPath = joinPath(outputDir, `segmented_${ts}${suffixPart}.txt`);
	let lineCount = 0;
	let buffer = "";
	fs.writeFileSync(outputPath, "", "utf8");
	writerStart({ outputDir, lineCount: 0 });

	function flush() {
		if (buffer.length === 0) return;
		fs.appendFileSync(outputPath, buffer, "utf8");
		buffer = "";
	}

	function writeLine(syllables) {
		buffer += syllables.join("  ") + "\n";
		lineCount++;
		if (lineCount % flushLines === 0) flush();
	}

	/* Syllable-only path: the writer consumes the segmentor generator
	   directly — assemble each \n line (generator.js) and write it. */
	async function consumeSyllable(seg) {
		let n = 0;
		for await (const lineStr of toLineStrings(seg)) {
			writeLine([toSyllableLine(lineStr)]);
			n++;
		}
		return n;
	}

	return {
		outputPath,
		writeLine,
		consumeSyllable,
		flush,
		close() {
			flush();
			writerWrote({ path: outputPath, lines: lineCount });
			writerEnd({ path: outputPath });
			return outputPath;
		},
	};
}

// ── tree writers (the graph build's output channel) ──
export function writeTreeToDir(treeData, treeDir = TreeFile()) {
	const filePath = joinPath(treeDir, "syllable.tree.json");
	fs.writeFileSync(filePath, JSON.stringify(treeData, null, 2), "utf8");
	return filePath;
}

export function writeMappedToDir(lines, treeDir = TreeFile()) {
	const filePath = joinPath(treeDir, "syllable.mapped.txt");
	fs.writeFileSync(filePath, lines.join("\n") + "\n", "utf8");
	return filePath;
}

export function writeRtlToDir(rtlData, treeDir = TreeFile()) {
	const filePath = joinPath(treeDir, "rtl-tree.json");
	fs.writeFileSync(filePath, JSON.stringify(rtlData, null, 2), "utf8");
	return filePath;
}

export function writeRefToDir(refData, treeDir = TreeFile()) {
	const filePath = joinPath(treeDir, "ref-syllables.json");
	fs.writeFileSync(filePath, JSON.stringify(refData, null, 2), "utf8");
	return filePath;
}

// ── common fs helpers ──
export function readFile(filePath, encoding = "utf8") {
	if (!fs.existsSync(filePath)) return null;
	return fs.readFileSync(filePath, encoding);
}

export function fileExists(filePath) {
	return fs.existsSync(filePath);
}

export function writeFile(filePath, content, encoding = "utf8") {
	fs.writeFileSync(filePath, content, encoding);
	return filePath;
}

export function writeJson(filePath, data, space = 2) {
	fs.writeFileSync(filePath, JSON.stringify(data, null, space), "utf8");
	return filePath;
}

export function appendFile(filePath, content, encoding = "utf8") {
	fs.appendFileSync(filePath, content, encoding);
	return filePath;
}

export function readLines(filePath) {
	const raw = readFile(filePath);
	if (!raw || typeof raw !== "string") return [];
	return raw
		.split("\n")
		.map((l) => l.trim())
		.filter(Boolean);
}

export function readInput(filePath) {
	readerStart({ filePath });
	const text = readFile(filePath);
	if (!text || typeof text !== "string") {
		readerFail({ filePath, reason: "not found or empty" });
		return null;
	}
	readerEnd({ filePath, length: text.length });
	return text;
}

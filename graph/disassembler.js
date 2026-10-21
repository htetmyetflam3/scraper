import { MyNormalize } from "../map/loader.js";
import { ensureNode, ensureStep, addPos } from "./shape.js";

const STANDALONES = [
	{ original: "၍", id: "π000" },
	{ original: "၌", id: "π001" },
	{ original: "၏", id: "π002" },
	{ original: "ဤ", id: "π003" },
	{ original: "ဪ", id: "π004" },
];

const POS_KEY= 'x';
const POS_MAP = {
	ADJECTIVE: "aj",
	ADVERB: "av",
	CONJUNCTION: "cn",
	INTERJECTION: "in",
	NOUN: "n",
	PRONOUN: "pn",
	VERB: "v",
	PARTICLE: "p",
	WIBAT: "w",
 RBNV: 'rbvn'
};

/* Extracted from Bb() below: was inline, now the single canonical copy.
   Active logic kept verbatim (true key reorder via fresh sorted object).
   shape.js's unused twin is deleted. */
export function finalizeRootSteps(rootNode) {
	const steps = Object.keys(rootNode).filter(
		(k) => k.startsWith("St-") && k !== "St-F",
	);
	if (steps.length === 0) return rootNode;
	const maxStep = steps
		.sort(
			(a, b) =>
				parseInt(a.replace("St-", "")) - parseInt(b.replace("St-", "")),
		)
		.pop();
	if (maxStep) {
		rootNode["St-F"] = rootNode[maxStep];
		delete rootNode[maxStep];
	}
	const sorted = {};
	Object.keys(rootNode)
		.sort((a, b) => {
			if (a === "St-F") return 1;
			if (b === "St-F") return -1;
			return parseInt(a.replace("St-", "")) - parseInt(b.replace("St-", ""));
		})
		.forEach((k) => (sorted[k] = rootNode[k]));
	return sorted;
}

export function Bb(lookupData, { getStep }) {
	const data = { Root: {} };
	const changes = [];
	const roots = Object.entries(lookupData);

	for (let rIdx = 0; rIdx < roots.length; rIdx++) {
		const [root, sequences] = roots[rIdx];
		if (!data.Root[root]) data.Root[root] = {};
		const seqs = Object.entries(sequences);
		for (let sIdx = 0; sIdx < seqs.length; sIdx++) {
			const [id, seqStr] = seqs[sIdx];
			const tokens = seqStr.split("");
			for (let i = 0; i < tokens.length; i++) {
				const tok = tokens[i];
				const stepNum = i + 1;
				const arr = getStep(data.Root[root], stepNum);
				const isFinal = i === tokens.length - 1;
				let entry = arr.find((e) => tok in e);
				if (!entry) {
					entry = { [tok]: [] };
					arr.push(entry);
				}
				if (isFinal) {
					entry[tok].push(id);
					changes.push({
						token: tok,
						path: root,
						step: stepNum,
						ids: [...entry[tok]],
						note: `root[${rIdx}] seq[${sIdx}]`,
					});
				}
			}
		}
	}

	for (const root of Object.keys(data.Root)) {
		data.Root[root] = finalizeRootSteps(data.Root[root]);
	}

	return { data, changes };
}

function buildRtlLookup(grouped) {
	const out = {};
	for (const [outerKey, innerObj] of Object.entries(grouped)) {
		for (const [innerKey, value] of Object.entries(innerObj)) {
			const reversed = [...value].reverse().join("");
			const stepKey = `St-${reversed.length}`;
			if (!out[stepKey]) out[stepKey] = {};
			if (!out[stepKey][reversed]) out[stepKey][reversed] = {};
			if (!out[stepKey][reversed][innerKey])
				out[stepKey][reversed][innerKey] = [];
			out[stepKey][reversed][innerKey].push(outerKey);
		}
	}
	const sorted = {};
	const stepKeys = Object.keys(out).sort((a, b) => {
		return parseInt(a.replace("St-", "")) - parseInt(b.replace("St-", ""));
	});
	for (const key of stepKeys) sorted[key] = out[key];
	return sorted;
}

export function buildSyllableLookup(syllables) {
	const entries = [];
	const bodyToSuffix = {};
	let nextId = 1;

	for (const syllable of syllables) {
		const ascii = MyNormalize(syllable) || "";
		const base = ascii[0] || "A";
		const body = ascii.slice(1);

		let id;
		if (bodyToSuffix[body] !== undefined) {
			id = bodyToSuffix[body];
		} else {
			id = String(nextId).padStart(3, "0");
			bodyToSuffix[body] = id;
			nextId++;
		}
		entries.push({ original: syllable, mapped: ascii, base, body, id });
	}

	const byBase = {};
	for (const e of entries) {
		if (!byBase[e.base]) byBase[e.base] = [];
		byBase[e.base].push(e);
	}

	let injectedCount = 0;
	for (const base of Object.keys(byBase)) {
		const group = byBase[base];
		const hasBareBase = group.some((e) => e.body === "");
		if (hasBareBase) continue;
		const firstEntry = group[0];
		const bareMyanmar = firstEntry.original[0];
		entries.unshift({
			original: bareMyanmar,
			mapped: base,
			base: base,
			body: "",
			id: "000",
			injected: true,
		});
		injectedCount++;
	}

	let standaloneCount = 0;
	for (const s of STANDALONES) {
		const alreadyExists = entries.some((e) => e.original === s.original);
		if (alreadyExists) continue;
		const mapped = MyNormalize(s.original) || s.original;
		entries.push({
			original: s.original,
			mapped: mapped,
			base: mapped,
			body: "",
			id: s.id,
			standalone: true,
		});
		standaloneCount++;
	}

	console.log(
		`[sid-build] Syllables: ${syllables.length}, Injected: ${injectedCount}, Standalones: ${standaloneCount}, Total: ${entries.length}`,
	);

	const mappedLines = entries.map((e) => {
		const fullId = e.standalone ? e.id : e.base + e.id;
		return `"${e.original}" : "${e.mapped}" : "${fullId}"`;
	});

	const refArray = entries.map((e) => {
		const fullId = e.standalone ? e.id : e.base + e.id;
		return { original: e.original, mapped: e.mapped, id: fullId };
	});

	const grouped = {};
	for (const e of entries) {
		if (!grouped[e.base]) grouped[e.base] = {};
		grouped[e.base][e.id] = e.id === "000" ? "" : e.body;
	}

	const sortedGrouped = {};
	for (const base of Object.keys(grouped).sort()) {
		sortedGrouped[base] = {};
		const suffixes = Object.keys(grouped[base]).sort((a, b) => {
			if (a === "000") return -1;
			if (b === "000") return 1;
			return parseInt(a, 10) - parseInt(b, 10);
		});
		for (const suffix of suffixes) {
			sortedGrouped[base][suffix] = grouped[base][suffix];
		}
	}

	const rtlGrouped = buildRtlLookup(sortedGrouped);

	return {
		lookupData: sortedGrouped,
		mappedLines,
		refArray,
		rtlGrouped,
		entries,
	};
}

// Add this near your POS_MAP
const POS_SOURCE_KEY = "dictionary";

/* Live dictionary unwrap — single home, moved here from build.js runBuildPos.
   (The old inner `posData["dictionary"]` fallback never fired and is deleted.)
   Returns BOTH refs: `source` (cleanup mutates + rewrites it to master.json)
   and `posData` (array-only, fed to buildPosTree). Same object refs as before. */
export function extractPosData(masterData) {
	const source = masterData[POS_SOURCE_KEY] || masterData;
	const posData = {};
	for (const [key, value] of Object.entries(source)) {
		if (key === 'syllables' || key === 'SPLIT') continue;
		if (Array.isArray(value)) posData[key] = value;
	}
	return { source, posData };
}

export function buildPosTree(sylToId, posData) {
	const tree = {};
	let added = 0,
		skipped = 0,
		failed = 0;
	const failedEntries = [];

	// Loop over posData (unwrapped upstream by extractPosData)
	for (const [jsonKey, lines] of Object.entries(posData)) {
		if (jsonKey === "syllables" || jsonKey === "SPLIT") continue;

		const currentPos = POS_MAP[jsonKey] ?? jsonKey;
		console.log(`[${currentPos}] ${jsonKey}: ${lines.length} entries`);

		for (const line of lines) {
			if (!line || typeof line !== "string") continue;

			const syllables = line
				.split("  ")
				.map((s) => s.trim())
				.filter((s) => s);

			if (syllables.length === 0) continue;

			const ids = [];
			const missing = [];

			for (const syl of syllables) {
				if (sylToId.has(syl)) {
					ids.push(sylToId.get(syl));
				} else {
					ids.push(`[${syl}]`);
					missing.push(syl);
				}
			}

			if (missing.length > 0) {
				failed++;
				failedEntries.push({ jsonKey, pos: currentPos, line, missing });
				continue;
			}

			const root = ids[0];
			const node = ensureNode(tree, root, POS_KEY);

			if (ids.length === 1) {
				if (addPos(node[POS_KEY], currentPos)) added++;
				else skipped++;
			} else {
				let currentNode = node;

				for (let i = 1; i < ids.length; i++) {
					const step = ensureStep(currentNode, i);
					const id = ids[i];

					if (!step[id]) {
						step[id] = {};
					}

					currentNode = step[id];
				}

				if (!Array.isArray(currentNode[POS_KEY])) {
					currentNode[POS_KEY] = [];
				}

				if (addPos(currentNode[POS_KEY], currentPos)) added++;
				else skipped++;
			}
		}
	}

	console.log(`[POS] Added: ${added}, Skipped: ${skipped}, Failed: ${failed}`);
	return { tree, failedEntries };
}

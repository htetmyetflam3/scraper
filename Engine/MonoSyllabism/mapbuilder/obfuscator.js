#!/usr/bin/env node
/**
 * obfuscator.js
 * Category: build
 *
 * Bundles the map payload sources and ships them as one compiled, encrypted
 * binary plus its decrypting loader. NO command-line paths: the layout comes
 * from Bridge/path.js (the path map), so a run is one word:
 *
 *   node MonoSyllabism/mapbuilder/obfuscator.js
 *
 *   SRC  = MonoSyllabism/mapbuilder/base/   (the payload sources, bundled whole)
 *   OUT  = MonoSyllabism/map/               (the fixed output dir)
 *          ├── payload.jsc   encrypted V8 bytecode      (the binary)
 *          ├── loader.js     this run's decrypting loader
 *          └── base/         READABLE runtime modules that CONSUME the
 *                            loader (BurmeseToAscii.js readAndPrepare,
 *                            idmapper.js burmeseToId) — never bundled.
 *
 * Pipeline:
 *   1. bundle      every *.js in base/ (sorted) into one flattened file: ESM
 *                  import/export stripped, JSON imports inlined as consts,
 *                  each module's exports registered on globalThis.__modules
 *                  (deleted again once harvested), the node builtins the
 *                  sources imported handed over as shims
 *   2. obfuscate   the flattened payload
 *   3. build       the binary — the OBFUSCATED JS compiled to V8 bytecode with
 *                  bytenode (compileCode → Buffer, nothing written yet)
 *   4. encrypt     that bytecode (AES-256-GCM, fresh random key per build) and
 *                  write the blob to map/payload.jsc — the shipped artifact is
 *                  encrypted bytecode, not bytecode
 *   5. generate    map/loader.js: it holds the key, decrypts the artifact in
 *                  memory, hands the bytecode to bytenode.runBytecode(), and
 *                  re-exports every binding by name. The key exists only in
 *                  the loader — the binary is useless without it, and the
 *                  build writes no key file anywhere.
 *
 * Intermediates (the flattened bundle) live in <src>/.build and are removed
 * when the build succeeds. The artifact is ENCRYPTED bytecode: load it through
 * the generated loader, not with require() — the .jsc handler cannot open it
 * by design. The paths are fixed on purpose: the runtime imports
 * MonoSyllabism/map/loader.js by name, so the build has exactly one valid
 * output location and one valid name.
 *
 * Loader export contract (the same surface the staging map-loader.js keeps):
 * every payload export re-exported by name, MyNormalize kept as a name for
 * myNormalize, and loader's default = mapperNormalize (the loader's contract
 * wins over normalize.js's own default — loaded.js imports the default as
 * mapperNormalize).
 *
 * The repo root is derived from THIS FILE's location (never the caller's cwd),
 * and every path-map constant is joined against it. Dependencies
 * (javascript-obfuscator, bytenode) are likewise required from this file's
 * own directory — install them there:
 *   npm install --prefix MonoSyllabism/mapbuilder javascript-obfuscator bytenode
 * ⚠️ V8 bytecode is tied to the exact Node/V8 version that compiled it —
 *    regenerate it on the runtime you deploy on.
 */

import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import vm from "node:vm";
import crypto from "node:crypto";
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { MapSourceDir, MapLoaderFile, MapBinaryFile } from "../../Bridge/path.js";

// Everything this script resolves on its own starts here: its own directory.
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const SELF = path.resolve(fileURLToPath(import.meta.url));
// The repo root from THIS FILE's location — never the caller's cwd: the path
// map holds root-relative constants, and this script may be run from anywhere.
const REPO_ROOT = path.resolve(SCRIPT_DIR, "..", "..");
// path.join, not resolve: the constants are root-relative; join glues them
// under the root while resolve would let an absolute-looking one slip off it.
const fromRoot = (...segments) => path.join(REPO_ROOT, ...segments);
// Dependencies are required from this file's location (the node_modules lookup
// walks up from SCRIPT_DIR), never from a path someone else declared.
const localRequire = createRequire(import.meta.url);

/** Path as typed by the caller, for logs: relative to cwd when it is below it. */
const shown = (p) => {
	const rel = path.relative(process.cwd(), p);
	return rel && !rel.startsWith("..") ? rel : p;
};

// ── paths — fixed, from Bridge/path.js (no CLI paths anywhere) ──
const USAGE = [
	"Usage: node MonoSyllabism/mapbuilder/obfuscator.js",
	"",
	"  No arguments. The layout is fixed in Bridge/path.js:",
	"    src    " + MapSourceDir() + "     (payload sources, bundled whole)",
	"    binary " + MapBinaryFile(),
	"    loader " + MapLoaderFile(),
	"",
	"  map/base/ beside the output holds the READABLE runtime modules that",
	"  consume the loader — they are never part of the payload.",
].join("\n");

// A build error is a message, not a stack trace.
process.on("uncaughtException", (err) => {
	console.error(process.env.STACK ? err.stack : `error: ${err.message}`);
	process.exit(1);
});

const fail = (msg) => {
	console.error(msg);
	process.exit(1);
};

const argv = process.argv.slice(2);
if (argv.some((a) => a === "-h" || a === "--help")) {
	console.log(USAGE);
	process.exit(0);
}
if (argv.length > 0) {
	fail(`This script takes no arguments — every path is fixed in Bridge/path.js (got: ${argv.join(" ")}).\n\n${USAGE}`);
}

const SRC_DIR = fromRoot(MapSourceDir());
const LOADER_FILE = fromRoot(MapLoaderFile());
const BINARY_FILE = fromRoot(MapBinaryFile());
const OUT_DIR = path.dirname(LOADER_FILE);
const WORK_DIR = path.join(SRC_DIR, ".build");
const BINARY_NAME = path.basename(BINARY_FILE);
const LOADER_NAME = path.basename(LOADER_FILE);

if (!fs.existsSync(SRC_DIR)) fail(`payload source dir not found: ${SRC_DIR}`);
if (!fs.statSync(SRC_DIR).isDirectory()) fail(`payload source path is not a directory: ${SRC_DIR}`);
if (!BINARY_NAME.endsWith(".jsc")) fail(`the binary must end in .jsc (bytenode's handler): ${BINARY_NAME}`);
if (!/\.(mjs|js)$/.test(LOADER_NAME)) fail(`the loader must end in .mjs or .js: ${LOADER_NAME}`);
if (path.dirname(BINARY_FILE) !== OUT_DIR) {
	fail(`binary and loader must land in the same dir (${OUT_DIR}):\n  binary: ${BINARY_FILE}`);
}

// ═══════════════════════════ 1. bundle the dir ════════════════════════════
const BUNDLE_FILE = path.join(WORK_DIR, "bundle.js");

/**
 * Exports of one module: [exported name, local binding] pairs — what the loader
 * re-exports and what the registry maps. `export { BASE as base }` is exported
 * "base" over local "BASE"; the rest are name === name.
 */
function parseExports(src) {
	const pairs = [];
	for (const match of src.matchAll(/^export\s*\{([^}]+)\}\s*;?/gm)) {
		for (const item of match[1].split(",")) {
			const parts = item.trim().split(/\s+as\s+/);
			const exported = (parts[1] || parts[0]).trim();
			const local = (parts[0] || "").trim();
			if (exported && local) pairs.push([exported, local]);
		}
	}
	for (const match of src.matchAll(/^export\s+(?:async\s+)?(?:function\*?|class|const|let|var)\s+([a-zA-Z0-9_$]+)/gm)) {
		pairs.push([match[1], match[1]]);
	}
	// Deduplicate by exported name — the first declaration wins.
	const seenNames = new Set();
	return pairs.filter(([exported]) => (seenNames.has(exported) ? false : (seenNames.add(exported), true)));
}

/** Remove ESM import declarations. */
function stripImports(src) {
	return src.replace(/^\s*import[\s\S]*?from\s*['"][^'"]+['"]\s*;?/gm, "").replace(/^\s*import\s*['"][^'"]+['"]\s*;?$/gm, "");
}

/** Remove ESM export keywords so declarations stay in the flat scope. */
function stripExports(src) {
	return src
		.replace(/^export\s*\{[^}]*\}\s*;?$/gm, "")
		.replace(/\bexport\s+default\b/g, "")
		// generator forms included: `export async function*` / `export function*`
		// (this branch failed bundling here — BurmeseToAscii's readAndPrepare —
		// when only the plain `function` form was stripped and `export` survived).
		.replace(/^export\s+(async\s+function\*?)\s*/gm, "$1 ")
		.replace(/^export\s+function(\*?)\s+/gm, "function$1 ")
		.replace(/^export\s+class\s+/gm, "class ")
		.replace(/^export\s+const\s+/gm, "const ")
		.replace(/^export\s+let\s+/gm, "let ")
		.replace(/^export\s+var\s+/gm, "var ");
}

/** Node builtins the sources imported — the runtime hands them over as shims. */
const BUILTINS = ["fs", "path", "crypto", "os", "url", "util", "child_process", "events", "stream", "zlib", "assert", "buffer"];
const BUILTIN_SET = new Set(BUILTINS);
const IDENTIFIER = /^[A-Za-z_$][\w$]*$/;
// Names that cannot be a `const` binding in the generated loader.
const RESERVED = new Set([
	"break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete", "do", "else", "enum", "export",
	"extends", "false", "finally", "for", "function", "if", "import", "in", "instanceof", "let", "new", "null", "return",
	"super", "switch", "this", "throw", "true", "try", "typeof", "var", "void", "while", "with", "yield", "await",
]);

/**
 * Import bindings of one module: [binding, expression] pairs, the expression
 * reading the builtin off the runtime's `globalThis.__node`. Anything imported
 * from outside the dir is reported instead — flattening cannot resolve it.
 */
/**
 * Import bindings of one module: [binding, expression] pairs. `isInDir(spec)`
 * tells a same-dir relative import (flattening covers it — not foreign) from
 * a genuinely external one (reported: the binder must exist in the runtime).
 */
function scanImports(src, foreign, isInDir = () => false) {
	const pairs = [];
	for (const match of src.matchAll(/^\s*import\s+([\s\S]*?)\s*from\s*['"]([^'"]+)['"]/gm)) {
		const clause = match[1].trim();
		const spec = match[2];
		const bare = spec.replace(/^node:/, "");
		const isBuiltin = BUILTIN_SET.has(bare);
		/** One imported name: `{ a as b }`, `* as ns`, or a default binding. */
		const take = (local, member) => {
			if (!isBuiltin) {
				if (!isInDir(spec)) foreign.add(`${spec} (${local})`);
				return;
			}
			const base = `globalThis.__node.${bare}`;
			pairs.push([local, member ? `${base}.${member}` : base]);
		};

		let clauseRest = clause;
		const named = /\{([\s\S]*)\}/.exec(clauseRest);
		if (named) {
			for (const item of named[1].split(",")) {
				const [imported, local] = item.trim().split(/\s+as\s+/).map((s) => (s || "").trim());
				if (imported) take(local || imported, imported);
			}
			clauseRest = clauseRest.slice(0, named.index).trim();
		}
		const namespace = /\*\s+as\s+([A-Za-z_$][\w$]*)/.exec(clauseRest);
		if (namespace) take(namespace[1], null);
		else {
			const def = clauseRest.replace(/,$/, "").trim();
			if (IDENTIFIER.test(def)) take(def, null);
		}
	}
	for (const match of src.matchAll(/^\s*import\s*['"]([^'"]+)['"]/gm)) if (!isInDir(match[1])) foreign.add(`${match[1]} (side effect)`);
	return pairs;
}

/**
 * Read ONE top-level section out of a JSON file with the repo's own line
 * cursor (createReadStream + readline — the same shape as the knowledge
 * readers). master.json is ~1.4 MB of which the payload needs exactly the
 * tables sitting under ONE key — a couple of KB at the far end — so the file
 * is never fs.readFileSync'd whole and never JSON.parse'd whole: the stream
 * walks to `"<key>": {` and only that slice gets parsed. Brace depth is
 * counted string-aware, because the data itself lists "{" / "}" as characters.
 */
async function readJsonSection(jsonFile, key) {
	const rl = readline.createInterface({ input: fs.createReadStream(jsonFile), crlfDelay: Infinity });
	const opener = new RegExp(`^\\s*"${key}"\\s*:\\s*([\\{\\[])`);
	let slice = null;
	let depth = 0;
	for await (const line of rl) {
		let part = line;
		if (slice === null) {
			const m = opener.exec(line);
			if (!m) continue;
			part = line.slice(line.indexOf(m[1]));
			slice = "";
		}
		slice += (slice ? "\n" : "") + part;
		let inString = false;
		let esc = false;
		for (const ch of part) {
			if (inString) {
				if (esc) esc = false;
				else if (ch === "\\") esc = true;
				else if (ch === '"') inString = false;
			} else if (ch === '"') inString = true;
			else if (ch === "{" || ch === "[") depth++;
			else if (ch === "}" || ch === "]") depth--;
		}
		if (depth === 0) break;
	}
	rl.close();
	if (slice === null) fail(`"${key}": no such top-level section in ${shown(jsonFile)}`);
	try {
		return JSON.parse(slice.replace(/,\s*$/, ""));
	} catch (err) {
		fail(`"${key}" section in ${shown(jsonFile)} does not parse as JSON (${err.message})`);
	}
}

/**
 * JSON imports are inlined as consts before any other transform:
 * `import mapData from "../../knowledge/isTreasure/master.json" with { ... };`
 * becomes `const mapData = { ... };`, carrying ONLY the top-level section THE
 * BINDING NAMES (import of `mapData` pulls the `mapData` key — the 2 KB of
 * tables, not the 1.4 MB file around them). The payload ships as encrypted
 * bytecode with no readable JSON beside it, so the build bakes the tables in.
 * (Stripping the import alone would leave the dangling `with { ... }` clause —
 * the second bundling break on this branch — and an unbound `mapData`.)
 */
async function inlineJsonImports(src, fileDir, inlined) {
	const jobs = [];
	for (const match of src.matchAll(/^\s*import\s+([A-Za-z_$][\w$]*)\s+from\s*['"]([^'"]+\.json)['"]\s*(?:with\s*\{[^}]*\}\s*)?;?/gm)) {
		const [whole, binding, spec] = match;
		const jsonFile = path.resolve(fileDir, spec);
		if (!fs.existsSync(jsonFile)) fail(`${spec}: JSON not found for inlining (imported from ${fileDir})`);
		jobs.push(readJsonSection(jsonFile, binding).then((section) => {
			inlined.push(`${binding} <- ${shown(jsonFile)} ("${binding}" section)`);
			src = src.replace(whole, `const ${binding} = ${JSON.stringify(section)};`);
		}));
	}
	await Promise.all(jobs);
	return src;
}

const files = fs
	.readdirSync(SRC_DIR)
	.filter((f) => f.endsWith(".js"))
	.filter((f) => f !== path.basename(SELF) && f !== LOADER_NAME && f !== BINARY_NAME && f !== "bundle.js")
	.sort();

if (files.length === 0) {
	console.error(`No *.js sources to bundle in ${SRC_DIR}`);
	process.exit(1);
}

fs.mkdirSync(WORK_DIR, { recursive: true });
fs.mkdirSync(OUT_DIR, { recursive: true });

// A relative import resolving to a bundled file is flattened with it — not a
// foreign import. Used both by the ordering below and by the shim scanner.
const FILE_SET = new Set(files.map((f) => path.join(SRC_DIR, f)));
const isInDirImport = (spec) => {
	if (!spec.startsWith(".")) return false;
	const abs = path.resolve(SRC_DIR, spec);
	return FILE_SET.has(abs) || FILE_SET.has(abs + ".js");
};

// Module order is the IMPORT GRAPH, not the alphabet: flattening is one
// sequential scope, so a module whose top-level runs against another module's
// const (normalize builds its imposter trie from sourceMap's IMPOSTER at
// module scope) must come AFTER the module that declares it. Real ESM linking
// does this for you; the flat text has to reproduce it. Kahn's algorithm,
// alphabetical as the tie-break so a no-dependency layout keeps the old order.
{
	const depsOf = (f) =>
		[...fs.readFileSync(path.join(SRC_DIR, f), "utf8").matchAll(/from\s*['"]([^'"]+)['"]/g)]
			.map((m) => path.resolve(SRC_DIR, m[1]))
			.filter((p) => FILE_SET.has(p) || FILE_SET.has(p + ".js"))
			.map((p) => path.relative(SRC_DIR, FILE_SET.has(p) ? p : p + ".js"));
	const pending = new Map(files.map((f) => [f, depsOf(f).filter((d) => d !== f)]));
	const ordered = [];
	while (pending.size > 0) {
		const ready = [...pending.keys()].filter((f) => pending.get(f).every((d) => ordered.includes(d))).sort();
		if (ready.length === 0) {
			console.warn(`  ! import cycle among: ${[...pending.keys()].join(", ")} — falling back to name order for those`);
			ordered.push(...[...pending.keys()].sort());
			break;
		}
		for (const f of ready) pending.delete(f);
		ordered.push(...ready);
	}
	files.length = 0;
	files.push(...ordered);
}

const modules = [];
const shims = new Map(); // binding -> expression the runtime hands over
const foreignImports = new Set();
const inlinedJson = [];

for (const file of files) {
	const src = await inlineJsonImports(fs.readFileSync(path.join(SRC_DIR, file), "utf8"), SRC_DIR, inlinedJson);
	for (const [binding, expr] of scanImports(src, foreignImports, isInDirImport)) shims.set(binding, expr);
	// Column-0 declarations: enough to attribute a flat-scope clash to files.
	const declared = [...src.matchAll(/^(?:export\s+)?(?:async\s+function|function|class|const|let|var)\s+([A-Za-z_$][\w$]*)/gm)].map((m) => m[1]);
	modules.push({ name: path.basename(file, ".js"), file, exports: parseExports(src), declared, src: stripExports(stripImports(src)) });
}

if (foreignImports.size > 0) {
	console.warn(
		`  ! imports outside the dir cannot be flattened: ${[...foreignImports].join(", ")}\n` +
			"    (the bundle assumes those bindings exist in the runtime scope — keep such deps inside <src-dir>)",
	);
}

// Collisions: the flattened bundle is one scope, so duplicate names must be named.
const seen = new Map();
for (const mod of modules) {
	for (const [exported] of mod.exports) {
		if (seen.has(exported)) {
			console.warn(`  ! duplicate export "${exported}" (${seen.get(exported)} and ${mod.name}) — both share one flat binding`);
		} else {
			seen.set(exported, mod.name);
		}
	}
}

// ── logen DEBUG hooks: the bundler's own neutralisation list (kept verbatim),
//    emitted only when the dir does not define them itself. ──
const LOGEN_HOOKS = [
	"normalizeStart",
	"normalizeEnd",
	"normFixImposter",
	"normComposeNormal",
	"normComposeNgaasat",
	"debugLogInit",
	"debugLogPass",
];

/** Names declared at the flat top level by any module in the dir. */
const declaredTop = new Set(modules.flatMap((m) => m.declared));

/**
 * Calls with no definition anywhere in the dir — the other half of the hook
 * story: a caller can survive flattening while its definition (an import from
 * outside the dir) does not, and the crash only shows up at run time. Anything
 * that is only ever called, is not declared, and is not a JS global is such a
 * stray; it is reported and stubbed rather than left to throw later.
 */
const JS_GLOBALS = new Set([
	"Object", "Array", "String", "Number", "Boolean", "Symbol", "BigInt", "Math", "JSON", "Date", "RegExp", "Error",
	"TypeError", "RangeError", "SyntaxError", "ReferenceError", "EvalError", "URIError", "AggregateError", "Map", "Set",
	"WeakMap", "WeakSet", "WeakRef", "FinalizationRegistry", "Promise", "Proxy", "Reflect", "Intl", "globalThis",
	"console", "process", "require", "module", "exports", "setTimeout", "setInterval", "clearTimeout", "clearInterval",
	"setImmediate", "clearImmediate", "queueMicrotask", "structuredClone", "fetch", "URL", "URLSearchParams", "TextEncoder",
	"TextDecoder", "Buffer", "atob", "btoa", "isNaN", "isFinite", "parseFloat", "parseInt", "decodeURI", "decodeURIComponent",
	"encodeURI", "encodeURIComponent", "eval", "Function", "ArrayBuffer", "SharedArrayBuffer", "DataView", "Atomics",
	"WebAssembly", "Int8Array", "Uint8Array", "Uint8ClampedArray", "Int16Array", "Uint16Array", "Int32Array", "Uint32Array",
	"Float32Array", "Float64Array", "BigInt64Array", "BigUint64Array", "Iterator", "AsyncIterator", "DisposableStack",
]);

function findStrayCalls(sources) {
	const flat = sources.join("\n");
	const calls = new Map();
	for (const [, name] of flat.matchAll(/(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(/g)) {
		calls.set(name, (calls.get(name) || 0) + 1);
	}
	const stray = [];
	for (const [name, callCount] of calls) {
		if (JS_GLOBALS.has(name) || RESERVED.has(name) || declaredTop.has(name) || shims.has(name)) continue;
		if (LOGEN_HOOKS.includes(name)) continue;
		// Every occurrence must be a call: a declaration or a value use would add
		// more, and a method shorthand (`run() {`) is a definition, not a call.
		const all = flat.match(new RegExp(`(?<![.\\w$])${name}\\b`, "g")) || [];
		if (all.length !== callCount) continue;
		if (new RegExp(`(?<![.\\w$])${name}\\s*\\([^)]*\\)\\s*\\{`).test(flat)) continue;
		stray.push(name);
	}
	return stray;
}

const strayCalls = findStrayCalls(modules.map((m) => m.src));

const hookShims = LOGEN_HOOKS.filter((n) => !declaredTop.has(n) && !shims.has(n));
const strayShims = strayCalls.filter((n) => !hookShims.includes(n));

if (hookShims.length > 0) {
	console.log(`  neutralised ${hookShims.length} logen hook(s) the dir calls but does not define: ${hookShims.join(", ")}`);
}
if (strayShims.length > 0) {
	console.warn(
		`  ! ${strayShims.length} call(s) with no definition in the dir — stubbed as no-ops:\n` +
			`    ${strayShims.join(", ")}\n` +
			"    (a caller can survive flattening while its definition does not — check these are hooks, not typos)",
	);
}

// The fs shim keeps the original bundler's convention working (bare `fs` with
// the runtime handing it over) and accepts the __node map the runtimes set.
const fsShim = declaredTop.has("fs") || shims.has("fs") ? null : "var fs = globalThis.__fs || globalThis.__node.fs;";

const shimLines = [
	...(fsShim ? [fsShim] : []),
	...[...shims].map(([binding, expr]) => `var ${binding} = ${expr};`),
	...hookShims.length
		? ["", "// ── logen DEBUG hooks neutralised: the sensitive normalize path never logs ──", ...hookShims.map((n) => `var ${n} = function(){};`)]
		: [],
	...strayShims.length ? ["", "// ── stubs for calls whose definition did not survive flattening ──", ...strayShims.map((n) => `var ${n} = function(){};`)] : [],
];

const shimBlock = shimLines.length === 0 ? "" : `// ── handed over by the runtime / neutralised before the modules ──\n${shimLines.join("\n")}\n`;

const shortcut = (m) =>
	m.exports
		.map(([exported, local]) => {
			if (exported === local && IDENTIFIER.test(exported)) return exported;
			return `${IDENTIFIER.test(exported) ? exported : JSON.stringify(exported)}: ${local}`;
		})
		.join(", ");

const registry =
	`\n// ── REGISTRY: every module's public exports (values captured in-scope, so the\n//    runtime can reach const/let bindings the eval would otherwise drop) ──\n` +
	`globalThis.__modules = Object.create(null);\n` +
	modules.map((m) => `globalThis.__modules[${JSON.stringify(m.name)}] = { ${shortcut(m)} };`).join("\n") +
	"\n";

const header = `/* bundle.js — FLATTENED payload (generated by obfuscator.js)
   ${modules.length} module(s) from ${SRC_DIR} — do not edit by hand.
   This is what the runtime obfuscates + encrypts into the binary. */
`;

const bundle =
	header +
	(shimBlock ? `\n${shimBlock}` : "") +

	modules.map((m) => `\n// ── FILE: ${m.file} ──\n${m.src}\n`).join("") +
	registry;

fs.writeFileSync(BUNDLE_FILE, bundle, "utf8");
console.log(`Bundled ${modules.length} file(s) from ${shown(SRC_DIR)}:`);
for (const m of modules) console.log(`  ${m.file}${m.exports.length ? `  → ${m.exports.map(([e]) => e).join(", ")}` : ""}`);
for (const line of inlinedJson) console.log(`  inlined JSON: ${line}`);
console.log(`  bundle.js: ${(Buffer.byteLength(bundle) / 1024).toFixed(2)} KB`);

// The bundle is ONE flat scope: two modules declaring the same const/let/class
// are a syntax error, and the obfuscator would only bury it in its own dump.
try {
	new vm.Script(bundle, { filename: BUNDLE_FILE });
} catch (err) {
	const clash = /Identifier '([^']+)' has already been declared/.exec(err.message);
	const hint = clash ? modules.filter((m) => m.declared.includes(clash[1])).map((m) => m.file) : [];
	console.error(
		[
			`The flattened bundle does not parse: ${err.message}`,
			...(hint.length ? [`  "${clash[1]}" is declared at top level in: ${hint.join(", ")}`] : []),
			"",
			"  Flattening puts every module in one scope — rename one side, or keep",
			"  the colliding module out of the bundled dir.",
		].join("\n"),
	);
	process.exit(1);
}

// ═══════════════════════ 2. obfuscate the payload ═════════════════════════
let payload = bundle;
{
	let obfuscator;
	try {
		obfuscator = localRequire("javascript-obfuscator");
	} catch {
		console.error(
			`javascript-obfuscator is not installed next to this script — install it there:\n` +
				`  npm install --prefix ${SCRIPT_DIR} javascript-obfuscator`,
		);
		process.exit(1);
	}
	const mod = obfuscator.default || obfuscator;
	try {
		payload = mod
			.obfuscate(bundle, {
				compact: true,
				controlFlowFlattening: true,
				controlFlowFlatteningThreshold: 0.9,
				deadCodeInjection: true,
				deadCodeInjectionThreshold: 0.4,
				stringArray: true,
				stringArrayEncoding: ["rc4"],
				stringArrayThreshold: 1.0,
				splitStrings: true,
				splitStringsChunkLength: 10,
				renameGlobals: true,
				identifierNamesGenerator: "hexadecimal",
				simplify: true,
				target: "node",
			})
			.getObfuscatedCode();
	} catch (err) {
		console.error(`Obfuscation failed: ${err.message}`);
		process.exit(1);
	}
	console.log(`Obfuscated: ${(Buffer.byteLength(payload) / 1024).toFixed(2)} KB`);
}

// Named exports: one flat name per exported binding (first writer wins).
const exportedNames = [];
const skipped = [];
for (const mod of modules) {
	for (const [name] of mod.exports) {
		if (exportedNames.includes(name)) continue;
		if (!IDENTIFIER.test(name) || RESERVED.has(name)) {
			skipped.push(`${name} (${mod.file})`);
			continue;
		}
		exportedNames.push(name);
	}
}
if (skipped.length > 0) {
	console.warn(
		`  ! ${skipped.length} export name(s) cannot be a named export (reserved for the loader form) — ` +
			`re-run with INSPECT=1 to list them`,
	);
	if (process.env.INSPECT === "1") console.warn(`    ${skipped.join(", ")}`);
}

// ═══════════════ 3. build the binary — obfuscated JS → V8 bytecode ═════════
// What gets compiled is the OBFUSCATED bundle; the readable sources never reach
// this step. compileCode() returns the bytecode as a Buffer in memory — nothing
// is written until it has been encrypted in the next step.
let bytenode;
try {
	bytenode = localRequire("bytenode");
} catch {
	fail(
		`bytenode is not installed next to this script — install it there:\n  npm install --prefix ${SCRIPT_DIR} bytenode`,
	);
}

let bytecode;
try {
	bytecode = bytenode.compileCode(payload);
} catch (err) {
	fail(`Bytecode compilation failed: ${err.message}`);
}
console.log(`Binary: ${(bytecode.length / 1024).toFixed(2)} KB of V8 bytecode (${exportedNames.length} named export(s))`);

// ═══════════════════════ 4. encrypt the binary ════════════════════════════
// AES-256-GCM over the bytecode. The artifact is a binary blob:
//   iv (12 bytes) | ciphertext | auth tag (16 bytes)
// The key is generated here and baked into the loader (next step). The build
// writes no key file anywhere — between two builds only the artifact changes.
const key = crypto.randomBytes(32);
const iv = crypto.randomBytes(12);
const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
const ciphertext = Buffer.concat([cipher.update(bytecode), cipher.final()]);
const k = key.toString("hex");
fs.writeFileSync(BINARY_FILE, Buffer.concat([iv, ciphertext, cipher.getAuthTag()]));
console.log(
	`Encrypted: ${(fs.statSync(BINARY_FILE).size / 1024).toFixed(2)} KB → ${shown(BINARY_FILE)} (AES-256-GCM; the key lives in the loader)`,
);

// ═══════════════════ 5. generate the decrypting loader ════════════════════

// The loader must be able to require bytenode at run time. Its own directory is
// tried first (a payload dir inside a project that has the dep needs nothing
// more). As a fallback, bake the RELATIVE hop from the loader to the very
// bytenode this build used — derived here, never a fixed path in this script.
let bytenodeHop = null;
try {
	const entry = localRequire.resolve("bytenode");
	const rel = path.relative(OUT_DIR, entry).split(path.sep).join("/");
	if (rel && !path.isAbsolute(rel)) bytenodeHop = rel.startsWith(".") ? rel : `./${rel}`;
} catch {
	/* no hop to bake */
}

const loader = `// FILE: ${LOADER_NAME}  —  GENERATED by obfuscator.js
// Loader for ${BINARY_NAME} — AES-256-GCM encrypted V8 bytecode.
//
// Build order: bundle → obfuscate → compile → ENCRYPT. So this file is the only
// place the key exists. It decrypts ${BINARY_NAME} in memory, hands the bytecode
// to bytenode.runBytecode(), and what runs is the obfuscated module code as
// bytecode — the readable sources are not in the artifact in any form.
//
// Load this file, do NOT require() ${BINARY_NAME}: the artifact is encrypted, so
// bytenode's .jsc handler cannot open it (that is the point).
//
// V8 bytecode is tied to the Node/V8 version that compiled it — rebuild on the
// runtime you deploy on.

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import os from "node:os";
import url from "node:url";
import util from "node:util";
import child_process from "node:child_process";
import events from "node:events";
import stream from "node:stream";
import zlib from "node:zlib";
import assert from "node:assert";
import buffer from "node:buffer";
import { createRequire } from "node:module";

const BINARY = path.join(import.meta.dirname, ${JSON.stringify(BINARY_NAME)});

// The key, in order of precedence:
//   1. PAYLOAD_KEY from the deployment (.env) — wins when set, so the key can be
//      rotated/kept outside the artifact instead of travelling inside it.
//   2. the key baked in at build time (always present, so the artifact works
//      without any environment setup).
const BAKED_KEY = "${k}";
const ENV_KEY = (process.env.PAYLOAD_KEY || "").trim();
if (ENV_KEY && !/^[0-9a-f]{64}$/i.test(ENV_KEY)) {
	throw new Error(
		"PAYLOAD_KEY is set but is not a 32-byte hex key (64 hex characters) — got " + ENV_KEY.length + " character(s).",
	);
}
const KEY = Buffer.from(ENV_KEY || BAKED_KEY, "hex");

// bytenode, in order: this loader's own dir upward, then the bytenode this build
// ran with (relative hop baked below), then the process cwd.
const CANDIDATES = [${bytenodeHop ? JSON.stringify(bytenodeHop) : "null"}];
const attempt = (spec, base) => {
	try {
		return createRequire(base)(spec);
	} catch {
		return null;
	}
};
let bytenode = attempt("bytenode", import.meta.url);
for (const candidate of CANDIDATES) {
	if (bytenode) break;
	if (candidate) bytenode = attempt(candidate, import.meta.url);
}
if (!bytenode) bytenode = attempt("bytenode", path.join(process.cwd(), "package.json"));
if (!bytenode) {
	throw new Error(
		"bytenode is not resolvable from " + import.meta.dirname + " nor from " + process.cwd() +
			" — install it where the payload lives (npm install bytenode) or next to the build script.",
	);
}

const blob = fs.readFileSync(BINARY);
const decipher = crypto.createDecipheriv("aes-256-gcm", KEY, blob.subarray(0, 12));
decipher.setAuthTag(blob.subarray(-16));
let bytecode;
try {
	bytecode = Buffer.concat([decipher.update(blob.subarray(12, -16)), decipher.final()]);
} catch {
	throw new Error(
		BINARY + " could not be decrypted — the file is corrupt, was built with a different key, or" +
			" PAYLOAD_KEY does not match this build.",
	);
}

// The bundle expects the node builtins on globalThis (its own convention).
globalThis.__node = { fs, path, crypto, os, url, util, child_process, events, stream, zlib, assert, buffer };
globalThis.__fs = fs;

bytenode.runBytecode(bytecode);

// Harvested and gone: the registry is how the payload's bindings are collected,
// not a public handle. Nothing outside this module can enumerate them.
const api = Object.assign({}, ...Object.values(globalThis.__modules));
delete globalThis.__modules;

// Loader contract (the surface the staging map-loader keeps): every payload
// export by name; MyNormalize kept as a name for the owner's myNormalize;
// default = mapperNormalize — the loader's contract wins over normalize.js's
// own default (loaded.js imports the default AS mapperNormalize).
export const MyNormalize = api.myNormalize;
export default api.mapperNormalize ?? api.default;

${exportedNames.map((name) => `export const ${name} = api.${name};`).join("\n")}
`;

fs.writeFileSync(LOADER_FILE, loader, "utf8");
console.log(`Loader: ${shown(LOADER_FILE)} (holds the key, ${exportedNames.length} re-exported name(s))`);

// The key is per-build and lives in the loader — publish it next to the script
// (gitignored) and print the .env line, so a deployment can set PAYLOAD_KEY.
{
	const ENV_FILE = path.join(SCRIPT_DIR, ".secrets", "keys.txt");
const ENV_HEADER = `# Keys — plaintext, for copying into .env
# This file is NOT committed (.secrets/ is in .gitignore).
# Each line follows the most recent run of the script that owns it:
#   MASTER_JSON_KEY  <- encrypt.json.js  (master.json.enc; sealed into the reader)
#   PAYLOAD_KEY      <- obfuscator.js    (payload.jsc; baked into the loader)
`;
	const line = `PAYLOAD_KEY=${k}`;
	let body;
	if (fs.existsSync(ENV_FILE)) {
		const current = fs.readFileSync(ENV_FILE, "utf8");
		body = /^PAYLOAD_KEY=/m.test(current)
			? current.replace(/^PAYLOAD_KEY=.*$/m, () => line) // () => : keep any $ in the key literal
			: current.replace(/\n*$/, `\n${line}\n`);
	} else {
		body = ENV_HEADER + `${line}\n`;
	}
	fs.mkdirSync(path.dirname(ENV_FILE), { recursive: true });
	fs.writeFileSync(ENV_FILE, body, "utf8");
	console.log(`Key sidecar -> ${shown(ENV_FILE)}`);
	console.log(`\n  ${line}\n`);
}

// ═══════════════════════ 6. verify by loading it back ═════════════════════
{
	const probe = `import * as m from ${JSON.stringify(LOADER_FILE)}; const names = Object.keys(m); console.log(names.length + "|" + names.join(",")); `;
	const res = spawnSync(process.execPath, ["--input-type=module", "-e", probe], { cwd: process.cwd(), encoding: "utf8" });
	if (res.status !== 0) {
		console.error(`\nLoader verification FAILED:\n${res.stderr.trim()}`);
		process.exit(1);
	}
	const [count, names] = res.stdout.trim().split("|");
	console.log(`\nVerified: loader imported, ${count} binding(s) live.`);
	// The binding names are part of the payload's surface — only print them when
	// the operator asks for them (INSPECT=1), never in a normal build log.
	if (names && process.env.INSPECT === "1") {
		console.log(`  ${names.length > 400 ? names.slice(0, 400) + "…" : names}`);
	}
}

// ═══════════════════════ 7. clean up ═════════════════════════════════════
fs.rmSync(BUNDLE_FILE, { force: true });
console.log(`Intermediates removed: ${shown(WORK_DIR)}`);

console.log(`Done. Payload: ${shown(OUT_DIR)}`);

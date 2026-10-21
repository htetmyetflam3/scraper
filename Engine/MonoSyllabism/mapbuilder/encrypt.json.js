#!/usr/bin/env node
/**
 * encrypt.json.js  (the secure-json step)
 * Category: build
 *
 * Encrypts the knowledge JSON and writes the reader module that decrypts it.
 * NO command-line paths: the layout comes from Bridge/path.js (the path map),
 * so the whole CLI is three fixed forms:
 *
 *   node MonoSyllabism/mapbuilder/encrypt.json.js           seal master.json
 *   node MonoSyllabism/mapbuilder/encrypt.json.js fresh     ... with a new key
 *   node MonoSyllabism/mapbuilder/encrypt.json.js decrypt   back to plaintext
 *
 *   IN   knowledge/isTreasure/master.json   (JSON_SOURCE_FILE — the readable
 *                                           source of truth, never shipped)
 *   OUT  MonoSyllabism/map/master.json.enc  (MAP_MASTER_FILE's .enc twin —
 *                                           readSecureText picks it up under
 *                                           the readable name)
 *        MonoSyllabism/map/secure.js        (MAP_SECURE_FILE — the obfuscated
 *                                           reader module, key sealed in)
 * `decrypt` writes back to the IN path (one fixed round-trip), regenerating
 * the readable source from the shipped container.
 *
 * The key is reused from `.secrets/json.key` next to this script when it exists,
 * and minted there when it does not; `fresh` mints a new one and overwrites the
 * sidecar. Re-running the encrypt form regenerates the `.enc` and the reader
 * together, so a rotated key is just `fresh`.
 *
 * After a run the key is ALSO written out plainly, twice: the `.env` line is
 * printed so it can be copied straight into a deployment env, and the
 * `MASTER_JSON_KEY=` line of `.secrets/keys.txt` (next to this script) is
 * updated — that file is the plaintext copy of both keys and is gitignored.
 *
 * Container layout (the generated reader depends on it):
 *   [4-byte magic "MGR1"][12-byte IV][16-byte auth tag][AES-256-GCM ciphertext]
 *
 * The repo root is derived from THIS FILE's location (never the caller's cwd).
 * The obfuscator dependency is required from this file's own directory:
 *   npm install --prefix MonoSyllabism/mapbuilder javascript-obfuscator
 */

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

import { JsonSourceFile, MapMasterFile, MapSecureFile } from "../../Bridge/path.js";

// Everything this script resolves on its own starts here: its own directory.
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
// The repo root from THIS FILE's location — never the caller's cwd: the path
// map holds root-relative constants, and this script may be run from anywhere.
const REPO_ROOT = path.resolve(SCRIPT_DIR, "..", "..");
// path.join, not resolve: the constants are root-relative; join glues them
// under the root while resolve would let an absolute-looking one slip off it.
const fromRoot = (...segments) => path.join(REPO_ROOT, ...segments);
// Dependencies are required from this file's location (the node_modules lookup
// walks up from SCRIPT_DIR), never from a path someone else declared.
const localRequire = createRequire(import.meta.url);
const KEY_FILE = path.join(SCRIPT_DIR, ".secrets", "json.key");

// The fixed round-trip (Bridge/path.js): readable source -> sealed artifact
// under the map's readable name + ".enc" + the reader module beside it.
const JSON_PATH = fromRoot(JsonSourceFile());
const ENC_PATH = fromRoot(MapMasterFile()) + ".enc";
const READER_PATH = fromRoot(MapSecureFile());

const MAGIC = "MGR1";
const ENV_HEADER = `# Keys — plaintext, for copying into .env
# This file is NOT committed (.secrets/ is in .gitignore).
# Each line follows the most recent run of the script that owns it:
#   MASTER_JSON_KEY  <- encrypt.json.js  (master.json.enc; sealed into the reader)
#   PAYLOAD_KEY      <- obfuscator.js    (payload.jsc; baked into the loader)
`;
const KEY_BYTES = 32;
const IV_BYTES = 12;
const TAG_BYTES = 16;

/** Path as typed by the caller, for logs: relative to cwd when it is below it. */
const shown = (p) => {
	const rel = path.relative(process.cwd(), p);
	return rel && !rel.startsWith("..") ? rel : p;
};

const USAGE = [
	"Usage (no paths — every file is fixed in Bridge/path.js):",
	"  node MonoSyllabism/mapbuilder/encrypt.json.js          seal " + JsonSourceFile(),
	"  node MonoSyllabism/mapbuilder/encrypt.json.js fresh    ... with a new key",
	"  node MonoSyllabism/mapbuilder/encrypt.json.js decrypt  " + MapMasterFile() + ".enc -> " + JsonSourceFile(),
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
if (argv.length > 1 || (argv.length === 1 && argv[0] !== "fresh" && argv[0] !== "decrypt")) {
	fail(`Unknown form: "${argv.join(" ")}" — there are no path arguments, the layout is fixed.\n\n${USAGE}`);
}

// ── key: the sidecar next to this script, minted when missing ──
function loadKey(wantFresh) {
	if (!wantFresh && fs.existsSync(KEY_FILE)) {
		const hex = fs.readFileSync(KEY_FILE, "utf8").trim();
		const k = Buffer.from(hex, "hex");
		if (k.length !== KEY_BYTES) {
			console.error(`Key must be ${KEY_BYTES} bytes (hex) — got ${k.length} in ${shown(KEY_FILE)}`);
			process.exit(1);
		}
		return { key: k, hex, reused: true };
	}
	const key = crypto.randomBytes(KEY_BYTES);
	const hex = key.toString("hex");
	fs.mkdirSync(path.dirname(KEY_FILE), { recursive: true });
	fs.writeFileSync(KEY_FILE, hex + "\n", "utf8");
	return { key, hex, reused: false };
}

// The plaintext key sidecar both keys live in (gitignored), kept in `.env` form.
const ENV_FILE = path.join(SCRIPT_DIR, ".secrets", "keys.txt");

/**
 * Keep `.secrets/keys.txt` in step with the key this run used, and print the
 * `.env` line. The file also carries PAYLOAD_KEY (the payload binary's own key,
 * regenerated by obfuscator.js) — only the MASTER_JSON_KEY line is touched.
 */
function writeEnvSidecar(keyHex) {
	const line = `MASTER_JSON_KEY=${keyHex}`;
	let body;
	if (fs.existsSync(ENV_FILE)) {
		const current = fs.readFileSync(ENV_FILE, "utf8");
		body = /^MASTER_JSON_KEY=/m.test(current)
			? current.replace(/^MASTER_JSON_KEY=.*$/m, () => line) // () => : keep any $ in the key literal
			: current.replace(/\n*$/, `\n${line}\n`);
	} else {
		body = ENV_HEADER + `${line}\n`;
	}
	fs.mkdirSync(path.dirname(ENV_FILE), { recursive: true });
	fs.writeFileSync(ENV_FILE, body, "utf8");
	console.log(`Key sidecar -> ${shown(ENV_FILE)}`);
	console.log(`\n  ${line}\n`);
}

/** AES-256-GCM encrypt a buffer into the "MGR1" container. */
function seal(key, plain) {
	const iv = crypto.randomBytes(IV_BYTES);
	const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
	const ct = Buffer.concat([cipher.update(plain), cipher.final()]);
	return Buffer.concat([Buffer.from(MAGIC, "ascii"), iv, cipher.getAuthTag(), ct]);
}

/** Inverse: "MGR1" container -> plaintext buffer. */
function open(key, container) {
	if (container.length < 4 + IV_BYTES + TAG_BYTES || container.subarray(0, 4).toString("ascii") !== MAGIC) {
		throw new Error("invalid .enc container (expected the MGR1 layout)");
	}
	const iv = container.subarray(4, 4 + IV_BYTES);
	const tag = container.subarray(4 + IV_BYTES, 4 + IV_BYTES + TAG_BYTES);
	const ct = container.subarray(4 + IV_BYTES + TAG_BYTES);
	const decipher = crypto.createDecipheriv("aes-256-gcm", key, iv);
	decipher.setAuthTag(tag);
	return Buffer.concat([decipher.update(ct), decipher.final()]);
}

// ── the reader module this script owns (generated whole, never patched) ──
const READER_TEMPLATE = `// FILE: secure.js  —  GENERATED by encrypt.json.js
// Do not edit by hand: every run writes this file again, with the key it used
// (the sidecar key when one is reused, a fresh one otherwise).
//
// Reads the AES-256-GCM encrypted JSON written by the same script.
// Container: [4-byte magic "MGR1"][12-byte IV][16-byte auth tag][ciphertext]
//
//   readSecureText(file)  → the text of \`file\`, or of \`file + ".enc"\` when that
//                           encrypted twin exists (the callers pass the plain
//                           path and get the sealed one transparently)
//   readSecureJson(file)  → the same, JSON.parse'd; a relative path resolves
//                           against the process cwd (start the process at the
//                           directory the paths are written for)

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const KEY = Buffer.from("__SECURE_KEY__", "hex");
const MAGIC = "MGR1";
const IV_BYTES = 12;
const TAG_BYTES = 16;

function decrypt(file) {
	const container = fs.readFileSync(file);
	if (container.length < 4 + IV_BYTES + TAG_BYTES || container.subarray(0, 4).toString("ascii") !== MAGIC) {
		throw new Error(\`Invalid .enc container: \${file}\`);
	}
	const iv = container.subarray(4, 4 + IV_BYTES);
	const tag = container.subarray(4 + IV_BYTES, 4 + IV_BYTES + TAG_BYTES);
	const ciphertext = container.subarray(4 + IV_BYTES + TAG_BYTES);
	const decipher = crypto.createDecipheriv("aes-256-gcm", KEY, iv);
	decipher.setAuthTag(tag);
	return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString("utf8");
}

export function readSecureText(file) {
	const enc = file.endsWith(".enc") ? file : file + ".enc";
	if (fs.existsSync(enc)) return decrypt(enc);
	return fs.readFileSync(file, "utf8");
}

export function readSecureJson(file) {
	const resolved = path.isAbsolute(file) ? file : path.resolve(process.cwd(), file);
	if (!fs.existsSync(resolved) && !fs.existsSync(resolved + ".enc")) {
		throw new Error(\`No such file: \${resolved}\`);
	}
	const text = readSecureText(resolved);
	try {
		return JSON.parse(text);
	} catch (err) {
		throw new Error(\`Invalid JSON in \${resolved}: \${err.message}\`, { cause: err });
	}
}
`;

/** Write the reader module (key sealed in), obfuscated. */
function writeReaderModule(keyHex, readerPath) {
	let src = READER_TEMPLATE.replace(/__SECURE_KEY__/g, keyHex);
	let JavaScriptObfuscator;
	try {
		JavaScriptObfuscator = localRequire("javascript-obfuscator");
	} catch {
		console.error(
			"javascript-obfuscator is not installed next to this script — install it there:\n" +
				`  npm install --prefix ${SCRIPT_DIR} javascript-obfuscator`,
		);
		process.exit(1);
	}
	src = JavaScriptObfuscator.obfuscate(src, {
		compact: true,
		controlFlowFlattening: true,
		deadCodeInjection: false,
		stringArray: true,
		stringArrayEncoding: ["base64"],
		stringArrayThreshold: 1.0,
		renameGlobals: false,
		identifierNamesGenerator: "hexadecimal",
		simplify: true,
		target: "node",
	}).getObfuscatedCode();

	fs.mkdirSync(path.dirname(readerPath), { recursive: true });
	fs.writeFileSync(readerPath, src, "utf8");
	console.log(`Reader -> ${shown(readerPath)} (${(Buffer.byteLength(src) / 1024).toFixed(2)} KB, obfuscated)`);
}

// ── decrypt: node encrypt.json.js decrypt — the fixed round-trip back ──
if (argv[0] === "decrypt") {
	if (!fs.existsSync(ENC_PATH)) fail(`encrypted file not found: ${ENC_PATH} (encrypt first)`);
	if (!fs.existsSync(KEY_FILE)) {
		console.error(`No key at ${shown(KEY_FILE)} — it is written by an encrypt run beside this script.`);
		process.exit(1);
	}
	const keyHex = fs.readFileSync(KEY_FILE, "utf8").trim();
	const key = Buffer.from(keyHex, "hex");
	if (key.length !== KEY_BYTES) {
		console.error(`Key must be ${KEY_BYTES} bytes (hex) — got ${key.length}`);
		process.exit(1);
	}
	let plain;
	try {
		plain = open(key, fs.readFileSync(ENC_PATH));
	} catch (err) {
		console.error(`Cannot decrypt ${shown(ENC_PATH)}: ${err.message}`);
		process.exit(1);
	}
	fs.mkdirSync(path.dirname(JSON_PATH), { recursive: true });
	fs.writeFileSync(JSON_PATH, plain);
	console.log(`Decrypted ${shown(ENC_PATH)} -> ${shown(JSON_PATH)} (${plain.length} bytes)`);
	process.exit(0);
}

// ── encrypt: node encrypt.json.js [fresh] — the fixed layout, key per policy ──
if (!fs.existsSync(JSON_PATH)) fail(`readable JSON not found: ${JSON_PATH} (Bridge/path.js JSON_SOURCE_FILE)`);
if (fs.statSync(JSON_PATH).isDirectory()) fail(`readable JSON is a directory, not a file: ${JSON_PATH}`);

const data = fs.readFileSync(JSON_PATH);
try {
	JSON.parse(data.toString("utf8"));
} catch (err) {
	// Not fatal — readSecureText can still read it — but the name says JSON.
	console.warn(`  ! ${shown(JSON_PATH)} does not parse as JSON (${err.message}) — readSecureText can read it, readSecureJson cannot`);
}

const { key, hex: keyHex, reused } = loadKey(argv[0] === "fresh");
console.log(`Key: ${reused ? `reused ${shown(KEY_FILE)}` : `new -> ${shown(KEY_FILE)}`} (${keyHex.slice(0, 8)}…${keyHex.slice(-4)})`);

const container = seal(key, data);
fs.mkdirSync(path.dirname(ENC_PATH), { recursive: true });
fs.writeFileSync(ENC_PATH, container);
console.log(
	`Encrypted ${shown(JSON_PATH)} -> ${shown(ENC_PATH)} (${(data.length / 1024).toFixed(2)} KB -> ${(container.length / 1024).toFixed(2)} KB)`,
);

writeReaderModule(keyHex, READER_PATH);
// Both halves of the rotation: the new `.enc` + reader are on disk, so publish
// the key where a deployment (and a .env) can pick it up.
writeEnvSidecar(keyHex);
console.log("Done.");
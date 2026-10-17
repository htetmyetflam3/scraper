// FILE: Engine/Bridge/restore-master.js — scraper-side build shim.
//
// Regenerates the READABLE knowledge JSON (knowledge/isTreasure/master.json)
// that the payload build inlines, from the shipped encrypted container
// (MonoSyllabism/map/master.json.enc) via the shipped reader
// (MonoSyllabism/map/secure.js — the key is sealed inside it, so no
// .secrets/ sidecar is needed). The readable file is a LOCAL build input:
// gitignored, never shipped, exactly the Engine repo's own convention.
//
// Used by `npm run rebuild` before mapbuilder/obfuscator.js, because the
// payload binary (map/payload.jsc) is V8 bytecode tied to the exact Node
// version that compiled it — it must be rebuilt on the machine that runs it.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { readSecureText } from "../MonoSyllabism/map/secure.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, ".."); // <repo>/Engine
const TARGET = path.join(ROOT, "knowledge/isTreasure/master.json");
const SEALED = path.join(ROOT, "MonoSyllabism/map/master.json");

if (fs.existsSync(TARGET)) {
	console.log(`[restore-master] already present: ${TARGET}`);
	process.exit(0);
}
const text = readSecureText(SEALED); // reads the .enc twin, decrypts in memory
JSON.parse(text); // sanity: the container must hold valid JSON
fs.mkdirSync(path.dirname(TARGET), { recursive: true });
fs.writeFileSync(TARGET, text, "utf8");
console.log(`[restore-master] ${TARGET} restored from master.json.enc (${text.length} chars)`);

// step 3: payload = encrypt( pack( obfuscate( base/ entry logic ) + knowledge ) )
// knowledge (tables) is inlined before module obfuscation; AES wraps the pack.
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import JavaScriptObfuscator from "javascript-obfuscator";
import { assembleKey, decryptToLoad } from "./loader.mjs";

function obfuscatePayloadModule(src) {
  return JavaScriptObfuscator.obfuscate(src, {
    compact: true,
    target: "browser",
    identifierNamesGenerator: "hexadecimal",
    renameGlobals: false,
    stringArray: true,
    // QuickJS has no atob; leave strings encoded by the obfuscator's JS decoder.
    stringArrayEncoding: [],
    stringArrayThreshold: 0.8,
    splitStrings: false,
    controlFlowFlattening: false,
    deadCodeInjection: false,
    selfDefending: false,
    debugProtection: false,
  }).getObfuscatedCode();
}

const BASE = path.resolve(import.meta.dirname, "../../Engine/MonoSyllabism/mapbuilder/base");
const KNOWLEDGE = path.resolve(import.meta.dirname, "../../Engine/knowledge/isTreasure/master.json");

export function buildPack() {
  const pack = {};
  let inlined = 0;
  const inlineSection = (_, name) => {
    const section = JSON.parse(fs.readFileSync(KNOWLEDGE, "utf8"))[name];
    if (section === undefined) throw new Error(`missing "${name}" in ${KNOWLEDGE}`);
    inlined++;
    return `const ${name} = ${JSON.stringify(section)};`;
  };
  for (const [disk, key] of [["function.js", "function.js"], ["normalize.js", "normalize.js"], ["sourceMap.js", "sourceMap.js"], ["sourceMapper.js", "sourceMapper.js"]]) {
    let src = fs.readFileSync(path.join(BASE, disk), "utf8");
    // inline only the imported key (mapData) from the knowledge JSON, compact
    src = src.replace(
      /import\s+(\w+)\s+from\s+["'][^"']*master\.json["']\s*with\s*\{[^}]*\}\s*;/g,
      inlineSection
    );
    // base/ files are obfuscator-bundle sources, not valid standalone ESM:
    // some names are exported twice (export const X + export { X }). Keep one.
    const constNames = new Set([...src.matchAll(/export\s+const\s+(\w+)/g)].map((m) => m[1]));
    src = src.replace(/export\s*\{([^}]*)\}\s*;/g, (whole, list) => {
      const keep = list.split(",").map((s) => s.trim()).filter(Boolean)
        .filter((n) => !constNames.has(n.split(/\s+as\s+/)[0].trim()));
      return keep.length ? `export { ${keep.join(", ")} };` : "";
    });
    pack[key] = obfuscatePayloadModule(src);
  }
  if (!inlined) throw new Error(`no master.json import found in ${BASE}`);
  return pack;
}

export function encryptPack(pack) {
  const iv = crypto.randomBytes(12);
  const c = crypto.createCipheriv("aes-256-gcm", assembleKey(), iv);
  const ct = Buffer.concat([c.update(JSON.stringify(pack), "utf8"), c.final()]);
  return Uint8Array.from(Buffer.concat([iv, ct, c.getAuthTag()]));
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const payload = encryptPack(buildPack());
  fs.writeFileSync("payload.bin", payload);
  console.log("payload.bin:", payload.length, "bytes — pack round trip:",
    JSON.stringify(JSON.parse(decryptToLoad(payload))).length, "chars json");
}

// FILE: wasm/box/tools/pack-box.mjs — step 4: ONE box.wasm.
// Shape A ("QuickJS-box"), per the owner's design:
//   box.wasm = [compiled QuickJS engine] + [data] loader.js + [data] payload
// where payload = encrypt( compile( obfuscate( base/ bundle ) )) and the key
// lives split under decoy names inside the loader ("decrypt to load, not to
// decrypt to convert readable"). The loader rides in the CLEAR but obfuscated
// (bootstrap runs raw) — obfuscation is honest, non-cryptographic.
//
// Packaging = WASM custom sections (id 0) appended to the engine module:
//   "linga.loader"  — the loader.js bytes, SELF-CONTAINED (aes.mjs inlined;
//                     the shipped loader is one bootstrap, obfuscated later)
//   "linga.payload" — obfuscated module source, AES-256-GCM encrypted by build-payload.mjs
// Custom sections cannot break the engine: every wasm consumer must skip
// unknown ones. Hosts (python/wasmtime) read them back with
// Module.custom_sections(name) and feed the engine in-box.
import fs from "node:fs";
import path from "node:path";
import { obfuscateLoader } from "./obfuscate-loader.mjs";

const ROOT = path.resolve(import.meta.dirname, "..");
const ENGINE = path.join(ROOT, "engine.wasm");
const OUT = path.join(ROOT, "box.wasm");

function bundleLoader(loaderPath) {
  // Inline every relative *.mjs import of the loader (the section must stand
  // alone: a data: URL / in-engine module cannot resolve './aes.mjs').
  let src = fs.readFileSync(loaderPath, "utf8");
  src = src.replace(
    /import\s*\{([^}]*)\}\s*from\s*["']\.\/([^"']+\.mjs)["'];?/g,
    (_, names, file) => {
      const body = fs.readFileSync(path.join(path.dirname(loaderPath), file), "utf8");
      return `// -- inlined ${file} (${names.trim()}) --\n${body}`;
    }
  );
  return Buffer.from(src, "utf8");
}

function uleb128(n) {
  const bytes = [];
  do {
    let b = n & 0x7f;
    n >>>= 7;
    if (n) b |= 0x80;
    bytes.push(b);
  } while (n);
  return Buffer.from(bytes);
}

function customSection(name, data) {
  const nameBytes = Buffer.from(name, "utf8");
  const payload = Buffer.concat([
    uleb128(nameBytes.length),
    nameBytes,
    data,
  ]);
  return Buffer.concat([Buffer.from([0x00]), uleb128(payload.length), payload]);
}

export function packBox({ enginePath = ENGINE, loaderPath, payloadPath, outPath = OUT } = {}) {
  const engine = fs.readFileSync(enginePath);
  // The section carries the obfuscated loader (owner contract: obfuscated,
  // NOT encrypted — bootstrap runs raw). LINGA_BOX_RAW=1 packs the source.
  let loader = bundleLoader(loaderPath);
  if (!process.env.LINGA_BOX_RAW) loader = Buffer.from(obfuscateLoader(loader.toString("utf8")), "utf8");
  const payload = fs.readFileSync(payloadPath);
  const box = Buffer.concat([
    engine,
    customSection("linga.loader", loader),
    customSection("linga.payload", payload),
  ]);
  fs.writeFileSync(outPath, box);
  return {
    out: outPath,
    engineBytes: engine.length,
    loaderBytes: loader.length,
    payloadBytes: payload.length,
    boxBytes: box.length,
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const loaderPath = path.join(ROOT, "loader.mjs");
  const payloadPath = path.join(ROOT, "payload.bin");
  for (const p of [loaderPath, payloadPath]) {
    if (!fs.existsSync(p)) {
      console.error(`missing ${p} - run build-payload.mjs first`);
      process.exit(1);
    }
  }
  console.log(packBox({ loaderPath, payloadPath }));
}

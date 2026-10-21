// FILE: wasm/box/tools/re-box.mjs — open the box: key, payload, module sources.
//
//   node wasm/box/tools/re-box.mjs                 report only (sizes, hashes)
//   node wasm/box/tools/re-box.mjs --dump DIR      also write the recovered
//                                                  module sources to DIR
//   node wasm/box/tools/re-box.mjs --run           boot the recovered
//                                                  normalizer and compare it
//                                                  with the Engine loader
//
// What it does, in order, using box.wasm ONLY:
//   1. parse the wasm custom sections `linga.loader` and `linga.payload`;
//   2. import the loader section as a module and call its `assembleKey()` —
//      that is the AES-256-GCM key, recovered from the binary;
//   3. decrypt the payload by hand (12-byte IV | ciphertext | 16-byte tag,
//      AES-256-GCM) — no loader code involved in this step;
//   4. the plaintext is a JSON object of module sources; `--dump` writes them,
//      `--run` rewrites their relative imports and executes normalize.js.
//
// Result: the box protects nothing cryptographically. The decryptor and the
// key material ship next to it (loader.mjs, aes.mjs), so anyone holding
// box.wasm can recover the module sources. See wasm/box/REVERSE-ENGINEERING.md.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const HERE = path.dirname(new URL(import.meta.url).pathname);
const BOX = path.resolve(HERE, "..", "box.wasm");
const ENGINE = path.resolve(HERE, "..", "..", "..", "Engine");

const argv = process.argv.slice(2);
const dumpDir = argv.includes("--dump") ? argv[argv.indexOf("--dump") + 1] : null;
const run = argv.includes("--run");

function uleb(buf, i) {
  let n = 0;
  let shift = 0;
  for (;;) {
    const byte = buf[i++];
    n |= (byte & 0x7f) << shift;
    if (!(byte & 0x80)) return [n, i];
    shift += 7;
  }
}

function sections(buf) {
  if (buf.subarray(0, 4).toString() !== "\0asm") throw new Error("not a wasm module");
  const out = {};
  let i = 8;
  while (i < buf.length) {
    const id = buf[i++];
    const [size, start] = uleb(buf, i);
    const body = buf.subarray(start, start + size);
    i = start + size;
    if (id !== 0) continue;
    const [nameLen, at] = uleb(body, 0);
    out[body.subarray(at, at + nameLen).toString()] = body.subarray(at + nameLen);
  }
  return out;
}

const raw = fs.readFileSync(BOX);
const sec = sections(raw);
if (!sec["linga.loader"] || !sec["linga.payload"]) {
  throw new Error("box.wasm has no linga.loader / linga.payload section");
}
const sha = (b) => crypto.createHash("sha256").update(b).digest("hex");
console.log(`box.wasm        ${raw.length} B  sha256 ${sha(raw).slice(0, 16)}`);
console.log(`linga.loader    ${sec["linga.loader"].length} B  sha256 ${sha(sec["linga.loader"]).slice(0, 16)}`);
console.log(`linga.payload   ${sec["linga.payload"].length} B  sha256 ${sha(sec["linga.payload"]).slice(0, 16)}`);

const loaderUrl =
  "data:text/javascript;base64," + sec["linga.loader"].toString("base64");
const loader = await import(loaderUrl);
console.log(`loader exports  ${Object.keys(loader).join(", ")}`);

const key = Buffer.from(loader.assembleKey());
console.log(`assembleKey()   ${key.toString("hex")}  (${key.length} bytes)`);

const payload = sec["linga.payload"];
const iv = payload.subarray(0, 12);
const tag = payload.subarray(payload.length - 16);
const ct = payload.subarray(12, payload.length - 16);
const decipher = crypto.createDecipheriv("aes-256-gcm", key, iv);
decipher.setAuthTag(tag);
const plain = Buffer.concat([decipher.update(ct), decipher.final()]).toString("utf8");
const pack = JSON.parse(plain);
console.log(`plaintext       ${plain.length} chars, ${Object.keys(pack).length} modules`);
for (const [name, source] of Object.entries(pack)) {
  console.log(`  ${name.padEnd(18)} ${String(source.length).padStart(7)} chars`);
}

if (dumpDir) {
  fs.mkdirSync(dumpDir, { recursive: true });
  for (const [name, source] of Object.entries(pack)) {
    fs.writeFileSync(path.join(dumpDir, name), source);
  }
  fs.writeFileSync(
    path.join(dumpDir, "logen.js"),
    "export const debugLogInit=()=>{};\nexport default {};\n",
  );
  console.log(`dumped          ${Object.keys(pack).length} sources -> ${dumpDir}`);
}

if (run) {
  const dir = fs.mkdtempSync("/tmp/re-box-");
  for (const [name, source] of Object.entries(pack)) {
    fs.writeFileSync(
      path.join(dir, name),
      source.replace(/(["'][^"']*Bridge\/logen\.js["'])/g, '"./logen.js"'),
    );
  }
  fs.writeFileSync(
    path.join(dir, "logen.js"),
    "export const debugLogInit=()=>{};\nexport default {};\n",
  );
  const recovered = await import(path.join(dir, "normalize.js"));
  const sample = "မြန်မာစာ";
  const got = recovered.mapperNormalize(sample);
  const engine = await import(path.join(ENGINE, "MonoSyllabism", "map", "loader.js"));
  const want = engine.default(sample);
  console.log(`recovered run   mapperNormalize(${JSON.stringify(sample)}) = ${JSON.stringify(got)}`);
  console.log(`Engine loader   ${JSON.stringify(want)}`);
  console.log(`same output     ${got === want}`);
}

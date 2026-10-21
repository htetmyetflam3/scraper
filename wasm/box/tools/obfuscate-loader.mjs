// FILE: wasm/box/tools/obfuscate-loader.mjs — the loader's obfuscation pass.
// Owner's contract: the loader rides obfuscated but NOT encrypted — the
// bootstrap must run raw once extracted. Obfuscation is honestly disclosed
// as non-cryptographic: it raises the reading cost, it does not protect the
// key beyond the split-under-decoy-names trick. Export names are kept
// (bootModules / decryptToLoad / assembleKey / run) so the host contract
// stays stable; everything internal gets hex names and packed strings.
import JavaScriptObfuscator from "javascript-obfuscator";

export function obfuscateLoader(src) {
  return JavaScriptObfuscator.obfuscate(src, {
    compact: true,
    target: "browser",
    identifierNamesGenerator: "hexadecimal",
    renameGlobals: false,
    stringArray: true,
    stringArrayEncoding: ["base64"],
    stringArrayThreshold: 0.95,
    splitStrings: false,
    controlFlowFlattening: false,
    deadCodeInjection: false,
    selfDefending: false,
    debugProtection: false,
  }).getObfuscatedCode();
}

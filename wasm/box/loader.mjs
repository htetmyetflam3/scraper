// FILE: loader.mjs — the in-box loader (owner contract, 2026-10-17):
//   * key ASSEMBLED at runtime from split, XOR-masked pieces
//   * key variables carry near-miss decoy names (mapperrnormalize ~ mapperNormalize)
//   * loader is obfuscated later, NEVER encrypted (bootstrap must run raw)
//   * the key decrypts TO LOAD (run in engine), never to produce readable source
import { aesGcmDecrypt } from "./aes.mjs";

// --- split key pieces (masked hex). Decoy names on purpose. ---
const PIECE_A = "7f401aa2215179ed"; // piece 0
const PIECE_B = "b119b210e4851fd1"; // piece 1
const PIECE_C = "f9b82a33204bc19f"; // piece 2
const PIECE_D = "0b84a9616dc340b2"; // piece 3
const mapperrnormalize = PIECE_A;
const MapperrNormalize = PIECE_B;
const mapperNormalise = PIECE_C;
const mapperNormaliz = PIECE_D;
const _MASKS = [0xa7, 0x5c, 0x3e, 0x91];

export function assembleKey() {
  const out = new Uint8Array(32);
  const src = [mapperrnormalize, MapperrNormalize, mapperNormalise, mapperNormaliz];
  for (let i = 0; i < 4; i++) {
    const hex = src[i];
    for (let j = 0; j < 8; j++) out[i * 8 + j] = parseInt(hex.substr(j * 2, 2), 16) ^ _MASKS[i];
  }
  return out;
}

export function decryptToLoad(payload) {
  const key = assembleKey();
  const iv = payload.subarray(0, 12);
  const ctTag = payload.subarray(12);
  const pt = aesGcmDecrypt(key, iv, ctTag);
  return new TextDecoder().decode(pt);
}

export function run(payload) {
  return (0, eval)(decryptToLoad(payload)); // decrypt-TO-LOAD: runs, never returns source
}

// --- boot: decrypt-TO-LOAD a module pack {filename: source} ---
export function bootModules(payload) {
  return JSON.parse(decryptToLoad(payload));
}

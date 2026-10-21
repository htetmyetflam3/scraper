

export function pyTruthy(x) {
  if (x === null || x === undefined) return false;
  if (typeof x === "boolean") return x;
  if (typeof x === "number") return x !== 0;
  if (typeof x === "string") return x.length > 0;
  if (Array.isArray(x)) return x.length > 0;
  if (Buffer.isBuffer(x)) return x.length > 0;
  if (x instanceof Map || x instanceof Set) return x.size > 0;
  return true;
}


export function pyRound(x) {
  if (!Number.isFinite(x)) {
    throw new RangeError(`cannot round ${x}`); 
  }
  const fl = Math.floor(x);
  const diff = x - fl;
  if (diff > 0.5) return fl + 1;
  if (diff < 0.5) return fl;
  return fl % 2 === 0 ? fl : fl + 1;
}


const PY_WS = new Set([0x20, 0x09, 0x0a, 0x0d, 0x0b, 0x0c]);
export function pyStripBytes(buf) {
  let i = 0;
  let j = buf.length;
  while (i < j && PY_WS.has(buf[i])) i += 1;
  while (j > i && PY_WS.has(buf[j - 1])) j -= 1;
  return buf.subarray(i, j);
}


export function fromHexStrict(hex) {
  if (hex.length % 2 !== 0) {
    throw new TypeError(`non-hexadecimal number found in fromhex() arg at position ${hex.length - 1}`);
  }
  if (!/^[0-9A-Fa-f]*$/.test(hex)) {
    const bad = hex.search(/[^0-9A-Fa-f]/);
    throw new TypeError(`non-hexadecimal number found in fromhex() arg at position ${bad}`);
  }
  return Buffer.from(hex, "hex");
}


export function pyInt16(s) {
  let t = String(s).trim().replace(/_/g, "");
  let sign = 1n;
  if (t.startsWith("+")) t = t.slice(1);
  else if (t.startsWith("-")) { sign = -1n; t = t.slice(1); }
  if (/^0[xX]/.test(t)) t = t.slice(2);
  if (!/^[0-9A-Fa-f]+$/.test(t)) throw new ValueError(`invalid literal for int() with base 16: '${s}'`);
  const v = BigInt(`0x${t}`) * sign;
  if (v >= BigInt(Number.MIN_SAFE_INTEGER) && v <= BigInt(Number.MAX_SAFE_INTEGER)) return Number(v);
  return v; 
}


export function pyInt(s) {
  const t = String(s).trim().replace(/_/g, "");
  if (!/^[+-]?\d+$/.test(t)) throw new ValueError(`invalid literal for int() with base 10: '${s}'`);
  const v = BigInt(t);
  if (v >= BigInt(Number.MIN_SAFE_INTEGER) && v <= BigInt(Number.MAX_SAFE_INTEGER)) return Number(v);
  return v;
}

export class ValueError extends Error {
  constructor(msg) { super(msg); this.name = "ValueError"; }
}
export class RuntimeError extends Error {
  constructor(msg) { super(msg); this.name = "RuntimeError"; }
}


export function pyChr(i) {
  if (typeof i === "bigint") i = Number(i);
  if (i < 0 || i > 0x10ffff) {
    throw new ValueError(`chr() arg not in range(0x110000)`);
  }
  return String.fromCodePoint(i);
}


export function pyStrCompare(a, b) {
  const ka = Array.from(a);
  const kb = Array.from(b);
  const n = Math.min(ka.length, kb.length);
  for (let i = 0; i < n; i += 1) {
    const ca = ka[i].codePointAt(0);
    const cb = kb[i].codePointAt(0);
    if (ca !== cb) return ca < cb ? -1 : 1;
  }
  if (ka.length === kb.length) return 0;
  return ka.length < kb.length ? -1 : 1;
}


export function pyLen(str) {
  let n = 0;
  for (const _ch of str) n += 1;
  return n;
}


export function splitExt(p) {
  const base = p.split(/[\\/]/).pop();
  const idx = base.lastIndexOf(".");
  const lastSep = Math.max(base.lastIndexOf("/"), base.lastIndexOf("\\"));
  if (idx <= 0 || idx < lastSep) return [p, ""];
  return [p.slice(0, p.length - (base.length - idx)), base.slice(idx).toLowerCase()];
}


export function baseName(p) {
  const t = String(p).replace(/[\\/]+$/, "");
  const i = Math.max(t.lastIndexOf("/"), t.lastIndexOf("\\"));
  return i === -1 ? t : t.slice(i + 1);
}


export function pyFloat(s) {
  const t = String(s).trim().replace(/_/g, "");
  if (t === "nan" || t === "inf" || t === "-inf" || t === "+inf") return Number(t);
  if (!/^(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/.test(t)) {
    throw new ValueError(`could not convert string to float: '${s}'`);
  }
  return Number(t);
}

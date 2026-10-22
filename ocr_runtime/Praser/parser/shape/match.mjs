/**
 * FILE: Parsed/parser/shape/match.mjs
 *
 * COMPARE side of glyph shape matching: given ink, which labelled glyph is it?
 *
 * Pure geometry. Masks, distance fields and nearest-neighbour search over the
 * labelled SVG specimen sheets in map/vector/. No PDF, no fonts, no decoding --
 * the only thing it reads from disk is a specimen sheet, through shape/svg.mjs.
 *
 * The three files in shape/ are the three sides of one question:
 *   svg.mjs     REFERENCE  labelled specimen sheet  -> contours   (vector)
 *   raster.mjs  PAGE       scanned/rendered pixels  -> contours   (bitmap)
 *   match.mjs   COMPARE    contours or mask         -> a label
 */

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import v8 from "node:v8";
import { fileURLToPath } from "node:url";

import { VECTOR_MAP_DIR } from "../core/paths.mjs";

/* ── per-glyph accept bars ────────────────────────────────────────────
   Loaded once, read with a Map lookup. Everything expensive about this
   happened offline in .bin/gen-shape-thresholds.mjs; nothing here
   computes, scans or allocates per crop.

   The old gate was one flat number for every shape, and it could not
   work: measured against the real sheet, maxDistance 0.12 never fired at
   all (the largest gap between any two glyphs is 0.062) while minMargin
   0.012 was larger than the room 2,572 of 2,598 glyphs have, so a
   PERFECT, noise-free crop of a known glyph was rejected 94.8% of the
   time -- and never once mislabelled. The ranking was sound; the bar was
   on the wrong scale by roughly 40x. */
let _bars = null;
function bars() {
  if (_bars) return _bars;
  const file = path.join(VECTOR_MAP_DIR, "shape-thresholds.json");
  try {
    const raw = JSON.parse(fs.readFileSync(file, "utf8"));
    _bars = {
      byLabel: new Map(Object.entries(raw.byLabel || {})),
      // "X" and "X + a mark" are not rivals: no font draws the bare letter
      // to look like the marked one, so extra ink IS the mark and the
      // ranking has already answered it. Asking for a margin between them
      // throws away a decision that was already correct.
      composition: new Set(raw.composition || []),
      fallback: 0.0016,
    };
  } catch {
    _bars = { byLabel: new Map(), composition: new Set(), fallback: 0.012 };
  }
  return _bars;
}

const isComposition = (a, b) => {
  const t = bars().composition;
  return t.has(`${a}\u0000${b}`) || t.has(`${b}\u0000${a}`);
};

/* The cross-font alias list, generated offline from the witness crowd:
   map/vector/shape-alias.json. A pair on it is a pair of letters that faces in
   the corpus draw alike -- one font's ထ sits nearer the reference's က than its
   own does -- so no margin measured against ONE reference face can separate
   them for a document drawn in such a face. Absent file, absent veto: the
   matcher behaves exactly as it did. */
let _alias = null;
function aliasTable() {
  if (_alias !== null) return _alias;
  try {
    const file = path.join(VECTOR_MAP_DIR, "shape-alias.json");
    const raw = JSON.parse(fs.readFileSync(file, "utf8"));
    _alias = new Set((raw.pairs || []).map(([a, b]) => `${a}\u0000${b}`));
  } catch {
    _alias = new Set();
  }
  return _alias;
}

/** Two letters the crowd itself cannot place. Order-insensitive. */
export function isCrossFontAlias(a, b) {
  const t = aliasTable();
  return t.has(`${a}\u0000${b}`) || t.has(`${b}\u0000${a}`);
}
import { loadSheet } from "./svg.mjs";

const DEFAULT_SHEET_PREFIX = "PYIDAUNGSU-2.5.2_REGULAR-sheet-";
const DEFAULT_GRID = 48;

let defaultIndex = null;

function allPoints(contours) {
  return contours.flatMap((contour) => contour);
}

function boundsOfPoints(points) {
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (const [x, y] of points) {
    if (x < x0) x0 = x;
    if (x > x1) x1 = x;
    if (y < y0) y0 = y;
    if (y > y1) y1 = y;
  }
  return { x0, y0, x1, y1, w: x1 - x0, h: y1 - y0 };
}

/** Fill flattened SVG contours into a small, aspect-preserving binary mask. */
export function maskFromContours(contours, size = DEFAULT_GRID, pad = 2) {
  const points = allPoints(contours);
  if (!points.length) return null;
  const box = boundsOfPoints(points);
  if (!(box.w > 0) || !(box.h > 0)) return null;
  const room = Math.max(1, size - pad * 2);
  const scale = Math.min(room / box.w, room / box.h);
  const ox = (size - box.w * scale) / 2 - box.x0 * scale;
  const oy = (size - box.h * scale) / 2 - box.y0 * scale;
  const polys = contours.map((contour) => contour.map(([x, y]) => [x * scale + ox, y * scale + oy]));
  const mask = new Uint8Array(size * size);

  // The specimen sheet declares fill-rule=evenodd. Intersect one horizontal
  // scanline with every contour at once, then fill alternating pairs. This
  // preserves counters and disconnected marks without rasterising the SVG in
  // a browser.
  for (let y = 0; y < size; y += 1) {
    const yc = y + 0.5;
    const xs = [];
    for (const poly of polys) {
      for (let i = 0; i < poly.length; i += 1) {
        const [ax, ay] = poly[i];
        const [bx, by] = poly[(i + 1) % poly.length];
        if ((yc >= ay && yc < by) || (yc >= by && yc < ay)) {
          xs.push(ax + ((yc - ay) / (by - ay)) * (bx - ax));
        }
      }
    }
    xs.sort((a, b) => a - b);
    for (let i = 0; i + 1 < xs.length; i += 2) {
      const from = Math.max(0, Math.ceil(xs[i] - 0.5));
      const to = Math.min(size - 1, Math.floor(xs[i + 1] - 0.5));
      for (let x = from; x <= to; x += 1) mask[y * size + x] = 1;
    }
  }
  return { mask, size, aspect: box.w / box.h };
}

function inkBounds(mask, width, height) {
  let x0 = width;
  let y0 = height;
  let x1 = -1;
  let y1 = -1;
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      if (!mask[y * width + x]) continue;
      if (x < x0) x0 = x;
      if (x > x1) x1 = x;
      if (y < y0) y0 = y;
      if (y > y1) y1 = y;
    }
  }
  return x1 >= x0 && y1 >= y0 ? { x0, y0, x1, y1, w: x1 - x0 + 1, h: y1 - y0 + 1 } : null;
}

/** Trim and resize a raster ink mask onto the same square used by the SVG. */
export function normalizeRasterMask(mask, width, height, size = DEFAULT_GRID, pad = 2) {
  const box = inkBounds(mask, width, height);
  if (!box) return null;
  const room = Math.max(1, size - pad * 2);
  const scale = Math.min(room / box.w, room / box.h);
  const drawnW = box.w * scale;
  const drawnH = box.h * scale;
  const ox = (size - drawnW) / 2;
  const oy = (size - drawnH) / 2;
  const out = new Uint8Array(size * size);
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const sx = Math.floor(box.x0 + (x - ox) / scale);
      const sy = Math.floor(box.y0 + (y - oy) / scale);
      if (sx >= box.x0 && sx <= box.x1 && sy >= box.y0 && sy <= box.y1) {
        out[y * size + x] = mask[sy * width + sx];
      }
    }
  }
  return { mask: out, size, aspect: box.w / box.h };
}

/**
 * Chamfer distance field of a binary mask, shared by the runtime matcher and
 * by offline witness-crowd tools that build indices of their own.
 */
export function distanceField(mask, size) {
  const diagonal = Math.SQRT2;
  const field = new Float32Array(size * size);
  for (let i = 0; i < field.length; i += 1) field[i] = mask[i] ? 0 : size * 2;
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const i = y * size + x;
      if (x) field[i] = Math.min(field[i], field[i - 1] + 1);
      if (y) field[i] = Math.min(field[i], field[i - size] + 1);
      if (x && y) field[i] = Math.min(field[i], field[i - size - 1] + diagonal);
      if (x + 1 < size && y) field[i] = Math.min(field[i], field[i - size + 1] + diagonal);
    }
  }
  for (let y = size - 1; y >= 0; y -= 1) {
    for (let x = size - 1; x >= 0; x -= 1) {
      const i = y * size + x;
      if (x + 1 < size) field[i] = Math.min(field[i], field[i + 1] + 1);
      if (y + 1 < size) field[i] = Math.min(field[i], field[i + size] + 1);
      if (x + 1 < size && y + 1 < size) field[i] = Math.min(field[i], field[i + size + 1] + diagonal);
      if (x && y + 1 < size) field[i] = Math.min(field[i], field[i + size - 1] + diagonal);
    }
  }
  return field;
}

function inkDistance(mask, field) {
  let sum = 0;
  let count = 0;
  for (let i = 0; i < mask.length; i += 1) {
    if (!mask[i]) continue;
    sum += field[i];
    count += 1;
  }
  return count ? sum / count : Infinity;
}

/** Symmetric chamfer distance with aspect penalty, in grid units. */
export function shapeDistance(a, b) {
  if (a.size !== b.size) throw new Error("vector masks use different grids");
  const forward = inkDistance(a.mask, b.field);
  const reverse = inkDistance(b.mask, a.field);
  const aspectPenalty = Math.abs(Math.log(Math.max(0.01, a.aspect) / Math.max(0.01, b.aspect))) * 2;
  return ((forward + reverse) / 2 + aspectPenalty) / a.size;
}

function withField(item) {
  return { ...item, field: distanceField(item.mask, item.size) };
}

export function vectorSheetFiles(dir = VECTOR_MAP_DIR) {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir)
    .filter((name) => name.startsWith(DEFAULT_SHEET_PREFIX) && name.endsWith(".svg"))
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    .map((name) => path.join(dir, name));
}

/** Load the labelled SVG map once. No TTF/font library is opened here. */
export function loadVectorIndex(files = vectorSheetFiles(), { size = DEFAULT_GRID } = {}) {
  const byText = new Map();
  for (const file of files) {
    const sheet = loadSheet(file, 5);
    for (const entry of sheet.entries) {
      if (byText.has(entry.text)) continue;
      const raster = maskFromContours(entry.contours, size);
      if (!raster) continue;
      byText.set(entry.text, withField({
        ...raster,
        text: entry.text,
        codepoints: entry.cp,
        kind: entry.kind,
        sheet: file,
      }));
    }
  }
  return { byText, entries: [...byText.values()], files: [...files], size };
}

export function getDefaultVectorIndex() {
  if (!defaultIndex) {
    const dir = process.env.PRASER_ASSET_CACHE_DIR;
    if (!dir) defaultIndex = loadVectorIndex();
    else {
      // Cache the COMPLETE reference index, never queries or matched words.
      // Content fingerprint includes preparation code, assets, and V8 version.
      const here = path.dirname(fileURLToPath(import.meta.url));
      const files = [...vectorSheetFiles(), ...fs.readdirSync(VECTOR_MAP_DIR)
        .filter((name) => name.endsWith('.json')).sort().map((name) => path.join(VECTOR_MAP_DIR, name)),
        ...['match.mjs', 'svg.mjs', 'raster.mjs', 'contour.mjs'].map((name) => path.join(here, name))];
      const hash = crypto.createHash('sha256').update(`vector-index-v1:${process.versions.v8}:${DEFAULT_GRID}`);
      for (const file of files) hash.update(path.basename(file)).update(fs.readFileSync(file));
      const key = hash.digest('hex');
      fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
      const target = path.join(dir, 'vector-index.v8');
      try {
        const saved = v8.deserialize(fs.readFileSync(target));
        if (saved.key === key && saved.index?.byText instanceof Map && saved.index.entries?.length > 0
          && saved.index.size === DEFAULT_GRID) defaultIndex = saved.index;
      } catch { /* Missing/corrupt/incompatible cache is rebuilt atomically. */ }
      if (!defaultIndex) {
        defaultIndex = loadVectorIndex();
        const tmp = `${target}.${process.pid}.tmp`;
        try {
          const fd = fs.openSync(tmp, 'w', 0o600);
          try { fs.writeFileSync(fd, v8.serialize({ key, index: defaultIndex })); fs.fsyncSync(fd); }
          finally { fs.closeSync(fd); }
          fs.renameSync(tmp, target);
        } finally { if (fs.existsSync(tmp)) fs.unlinkSync(tmp); }
      }
    }
    bars(); aliasTable();
  }
  return defaultIndex;
}

/**
 * Match a binarised crop against labelled entries from the SVG vector map.
 * The function abstains unless the best shape is both close enough and clear
 * of the runner-up. Callers may pass the candidate strings supplied by
 * disagreeing layouts; omitting them searches the complete sheet.
 */
export function matchRasterMask(mask, width, height, {
  index = getDefaultVectorIndex(),
  candidates = null,
  maxDistance = 0.12,
  // null = use the per-glyph bar. A number overrides it, for probes.
  minMargin = null,
} = {}) {
  const normalized = normalizeRasterMask(mask, width, height, index.size);
  if (!normalized) return { accepted: false, reason: "empty crop", matches: [] };
  const query = withField(normalized);
  const pool = candidates
    ? [...new Set(candidates)].map((text) => index.byText.get(text)).filter(Boolean)
    : index.entries.filter((entry) => {
      const ratio = normalized.aspect / entry.aspect;
      return ratio >= 0.45 && ratio <= 2.2;
    });
  if (!pool.length) return { accepted: false, reason: "no SVG candidate", matches: [] };
  const matches = pool.map((entry) => ({
    text: entry.text,
    codepoints: entry.codepoints,
    kind: entry.kind,
    distance: shapeDistance(query, entry),
  })).sort((a, b) => a.distance - b.distance);
  const best = matches[0];
  const second = matches[1] ?? null;
  const margin = second ? second.distance - best.distance : Infinity;
  /* One lookup for the bar, one set probe for composition. The bar is a
     fraction of the room THIS glyph actually has, so a crowded shape
     (တ against က, 0.0039 apart) must clear a high proportion of a small
     gap, while an isolated one is accepted readily -- which is what a
     flat number could never express. */
  const table = bars();
  const runnerUp = matches[1]?.text ?? null;
  const freeOfMargin = runnerUp !== null && isComposition(best.text, runnerUp);
  /* Majority is not evidence. Three faces claiming က and one claiming ထ does
     not settle it on က: the one is a font that draws ထ like က, which is the
     whole reason a single reference face cannot be trusted here. Refuse, and
     let the disagreement stay visible instead of writing a letter nobody can
     defend. Composition pairs keep their exemption: extra ink IS the mark. */
  const aliased = runnerUp !== null && !freeOfMargin && isCrossFontAlias(best.text, runnerUp);
  const need = minMargin === null
    ? (table.byLabel.get(best.text) ?? table.fallback)
    : minMargin;
  const accepted = !aliased && best.distance <= maxDistance && (freeOfMargin || margin >= need);
  return {
    accepted,
    text: accepted ? best.text : null,
    codepoints: accepted ? best.codepoints : null,
    distance: best.distance,
    margin,
    matches: matches.slice(0, 8),
    need,
    reason: accepted
      ? null
      : aliased
        ? `cross-font alias: the witness crowd draws ${best.text} and ${runnerUp} alike, so shape cannot decide between them`
        : best.distance > maxDistance
          ? "shape too far from SVG"
          : `margin ${margin.toFixed(4)} below ${need.toFixed(4)} needed for ${best.text}`,
  };
}

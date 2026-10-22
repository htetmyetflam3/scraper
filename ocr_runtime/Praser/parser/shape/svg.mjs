/**
 * FILE: Parsed/parser/shape/svg.mjs
 *
 * Minimal SVG path reader for externally traced pages, plus line
 * segmentation. No dependency, and deliberately no rasterisation -- the
 * whole value of an external trace is that it keeps sub-glyph topology, so
 * everything downstream works on closed contours, never pixels.
 */
import fs from "node:fs";

/** Flatten one `d` attribute into closed contours of [x, y] points. */
export function parsePathData(d, steps = 8) {
  const toks = d.match(/[MCLZmclzHhVvSsQqTtAa]|-?\d*\.?\d+(?:e-?\d+)?/g) || [];
  const subs = [];
  let cur = null; let x = 0; let y = 0; let sx = 0; let sy = 0; let i = 0;
  const cubic = (x0, y0, x1, y1, x2, y2, x3, y3) => {
    for (let s = 1; s <= steps; s += 1) {
      const t = s / steps; const u = 1 - t;
      cur.push([
        u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
        u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3,
      ]);
    }
  };
  const num = () => +toks[i++];
  while (i < toks.length) {
    const c = toks[i];
    if (c === "M" || c === "m") {
      i += 1; const nx = num(); const ny = num();
      x = c === "M" ? nx : x + nx; y = c === "M" ? ny : y + ny;
      sx = x; sy = y; cur = [[x, y]]; subs.push(cur);
    } else if (c === "C" || c === "c") {
      i += 1;
      const a = num(); const b = num(); const cc = num(); const dd = num(); const e = num(); const f = num();
      const rel = c === "c";
      cubic(x, y, rel ? x + a : a, rel ? y + b : b, rel ? x + cc : cc, rel ? y + dd : dd, rel ? x + e : e, rel ? y + f : f);
      x = rel ? x + e : e; y = rel ? y + f : f;
    } else if (c === "L" || c === "l") {
      i += 1; const a = num(); const b = num();
      x = c === "L" ? a : x + a; y = c === "L" ? b : y + b;
      if (cur) cur.push([x, y]);
    } else if (c === "Q" || c === "q") {
      i += 1;
      const a = num(); const b = num(); const e = num(); const f = num();
      const rel = c === "q";
      const qx = rel ? x + a : a; const qy = rel ? y + b : b;
      const ex = rel ? x + e : e; const ey = rel ? y + f : f;
      for (let s2 = 1; s2 <= steps; s2 += 1) {
        const t = s2 / steps; const u = 1 - t;
        cur.push([u * u * x + 2 * u * t * qx + t * t * ex, u * u * y + 2 * u * t * qy + t * t * ey]);
      }
      x = ex; y = ey;
    } else if (c === "H" || c === "h") { i += 1; const a = num(); x = c === "H" ? a : x + a; if (cur) cur.push([x, y]); }
    else if (c === "V" || c === "v") { i += 1; const a = num(); y = c === "V" ? a : y + a; if (cur) cur.push([x, y]); }
    else if (c === "Z" || c === "z") { i += 1; if (cur) cur.push([sx, sy]); x = sx; y = sy; }
    else if (!Number.isNaN(+c)) {
      // Implicit repeat of the previous command; Grafika emits runs of C.
      const a = num(); const b = num(); const cc = num(); const dd = num(); const e = num(); const f = num();
      if ([a, b, cc, dd, e, f].some(Number.isNaN)) break;
      cubic(x, y, a, b, cc, dd, e, f); x = e; y = f;
    } else i += 1;
  }
  return subs.filter((s) => s.length > 2);
}

export function loadSvgContours(file, steps = 8) {
  const svg = fs.readFileSync(file, "utf8");
  const ds = [...svg.matchAll(/<path\b[^>]*\bd="([^"]+)"/g)].map((m) => m[1]);
  return ds.flatMap((d) => parsePathData(d, steps));
}

export function bboxOf(contour) {
  let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
  for (const [x, y] of contour) {
    if (x < x0) x0 = x; if (x > x1) x1 = x;
    if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  return { x0, y0, x1, y1, w: x1 - x0, h: y1 - y0, cx: (x0 + x1) / 2, cy: (y0 + y1) / 2 };
}

/** Shoelace area; sign ignored so holes count as positive. */
export function areaOf(contour) {
  let a = 0;
  for (let i = 0; i < contour.length; i += 1) {
    const [x1, y1] = contour[i];
    const [x2, y2] = contour[(i + 1) % contour.length];
    a += x1 * y2 - x2 * y1;
  }
  return Math.abs(a) / 2;
}

/**
 * Split contours into text lines.
 *
 * Burmese ascenders and descenders overlap between lines, so a plain ink
 * histogram merges them. Instead cluster on the CENTRE of each contour, which
 * stays inside its own line, then grow the line's extent from its members.
 */
export function segmentLines(shapes, { gapFactor = 0.6 } = {}) {
  if (!shapes.length) return [];
  const med = (xs) => { const s = [...xs].sort((a, b) => a - b); return s[Math.floor(s.length / 2)] || 0; };
  // Typical glyph height drives the clustering gap.
  const typical = med(shapes.filter((s) => s.area > 20).map((s) => s.box.h)) || med(shapes.map((s) => s.box.h));
  const gap = Math.max(1, typical * gapFactor);

  const sorted = [...shapes].sort((a, b) => a.box.cy - b.box.cy);
  const groups = [];
  let cur = [sorted[0]];
  for (let i = 1; i < sorted.length; i += 1) {
    if (sorted[i].box.cy - sorted[i - 1].box.cy > gap) { groups.push(cur); cur = []; }
    cur.push(sorted[i]);
  }
  groups.push(cur);

  return groups.map((members) => {
    const bodies = members.filter((m) => m.area >= 0.25 * med(members.map((x) => x.area)) && m.box.h >= 0.4 * typical);
    const ref = bodies.length >= 3 ? bodies : members;
    const baseline = med(ref.map((m) => m.box.y1));
    const bodyTop = med(ref.map((m) => m.box.y0));
    return {
      members: members.sort((a, b) => a.box.x0 - b.box.x0),
      baseline,
      bodyTop,
      xHeight: baseline - bodyTop,
      y0: Math.min(...members.map((m) => m.box.y0)),
      y1: Math.max(...members.map((m) => m.box.y1)),
      x0: Math.min(...members.map((m) => m.box.x0)),
      x1: Math.max(...members.map((m) => m.box.x1)),
    };
  }).filter((l) => l.members.length > 1);
}

export function loadShapes(file, steps = 8) {
  return loadSvgContours(file, steps).map((c) => ({ contour: c, box: bboxOf(c), area: areaOf(c) }));
}

/**
 * Ink contours only: drops counters (holes) by nesting depth, and drops page
 * furniture -- rules, borders, edge marks -- by absolute height. On page 3
 * that is 893 contours -> 587 ink, of which 2 are furniture at the right
 * margin (h 473 and h 225 against a median glyph height of 14).
 */
export function inkContours(shapes, { furnitureFactor = 6 } = {}) {
  const inside = (pt, poly) => {
    let c = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i += 1) {
      const [xi, yi] = poly[i]; const [xj, yj] = poly[j];
      if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < ((xj - xi) * (pt[1] - yi)) / (yj - yi) + xi) c = !c;
    }
    return c;
  };
  const ink = shapes.filter((a) => !shapes.some((b) => b !== a && b.area > a.area
    && a.box.cx > b.box.x0 && a.box.cx < b.box.x1 && a.box.cy > b.box.y0 && a.box.cy < b.box.y1
    && inside([a.box.cx, a.box.cy], b.contour)));
  const hs = ink.map((s2) => s2.box.h).sort((a, b) => a - b);
  const medH = hs[Math.floor(hs.length / 2)] || 1;
  return ink.filter((s2) => s2.box.h <= medH * furnitureFactor);
}

/** Least-squares y = a*x + b. */
function lsq(pairs) {
  const n = pairs.length;
  const sx = pairs.reduce((s, [x]) => s + x, 0);
  const sy = pairs.reduce((s, [, y]) => s + y, 0);
  const sxy = pairs.reduce((s, [x, y]) => s + x * y, 0);
  const sxx = pairs.reduce((s, [x]) => s + x * x, 0);
  const a = (n * sxy - sx * sy) / (n * sxx - sx * sx);
  return [a, (sy - a * sx) / n];
}

/**
 * Fit SVG coordinates onto the OCR raster, by pairing traced lines with OCR
 * lines in reading order and least-squaring their edges.
 *
 * The scale is NOT uniform. The owner traced onto an A4 canvas and zoomed the
 * text to fill it, but the novel page is not A4-proportioned, so the fit
 * stretches one axis: page 3 comes out at x 1.5661, y 1.7637, an ANISOTROPY
 * of 0.8879. Glyphs in the trace are therefore ~11% out of aspect, and any
 * comparison against font outlines must undo that first or every distance is
 * wrong by construction.
 *
 * Verified on page 3: the ကြုံ cluster maps to raster x 468-537 y 1566-1626
 * against OCR line 9 at x 469-1462 y 1563-1626 -- 1 px on the left edge.
 */
export function fitTraceToRaster(traceLines, ocrLines) {
  const n = Math.min(traceLines.length, ocrLines.length);
  const ys = []; const xs = [];
  for (let i = 0; i < n; i += 1) {
    const t = traceLines[i]; const o = ocrLines[i].bbox;
    if (!o) continue;
    ys.push([t.y0, o.y0], [t.y1, o.y1]);
    xs.push([t.x0, o.x0], [t.x1, o.x1]);
  }
  const [ax, bx] = lsq(xs);
  const [ay, by] = lsq(ys);
  let worst = 0;
  for (let i = 0; i < n; i += 1) {
    const t = traceLines[i]; const o = ocrLines[i].bbox;
    if (!o) continue;
    worst = Math.max(worst, Math.abs(ay * t.y0 + by - o.y0), Math.abs(ay * t.y1 + by - o.y1));
  }
  return {
    ax, bx, ay, by,
    anisotropy: ax / ay,
    worstResidual: worst,
    toRaster: ([x, y]) => [ax * x + bx, ay * y + by],
    /** Undo the anisotropic stretch so a traced glyph is comparable to a font outline. */
    deskew: ([x, y]) => [x * (ax / ay), y],
  };
}

/**
 * Read a generated specimen sheet back, resolving <defs>/<use>.
 *
 * The sheet stores each distinct glyph once in <defs> and places it with
 * <use x= y=>, which is what keeps a 2541-entry A4 sheet at ~730 KB instead
 * of 7 MB. Returns one record per inventory entry, with its contours already
 * positioned on the page.
 */
export function loadSheet(file, steps = 8) {
  const svg = fs.readFileSync(file, "utf8");
  const defs = new Map();
  const defsBlock = svg.slice(svg.indexOf("<defs>"), svg.indexOf("</defs>") + 7);
  for (const m of defsBlock.matchAll(/<path\s+id="([^"]+)"\s+d="([^"]+)"\s*\/>/g)) {
    defs.set(m[1], parsePathData(m[2], steps));
  }
  const entries = [];
  for (const g of svg.matchAll(/<g\s+id="([^"]*)"\s+data-kind="([^"]*)"\s+data-cp="([^"]*)"\s+data-row="([^"]*)"\s+data-x="([^"]*)"\s+data-baseline="([^"]*)"\s*>([\s\S]*?)<\/g>/g)) {
    const [, text, kind, cp, row, x, baseline, inner] = g;
    const contours = [];
    for (const u of inner.matchAll(/<use\s+href="#([^"]+)"\s+x="([^"]+)"\s+y="([^"]+)"\s*\/>/g)) {
      const base = defs.get(u[1]);
      if (!base) continue;
      const dx = Number(u[2]); const dy = Number(u[3]);
      for (const c of base) contours.push(c.map(([px, py]) => [px + dx, py + dy]));
    }
    if (contours.length) {
      entries.push({
        text, kind, cp, row: Number(row), x: Number(x), baseline: Number(baseline),
        contours, shapes: contours.map((c) => ({ contour: c, box: bboxOf(c), area: areaOf(c) })),
      });
    }
  }
  const meta = {};
  const head = svg.slice(0, svg.indexOf(">", svg.indexOf("<svg")));
  for (const m of head.matchAll(/\b(data-[\w-]+|width|height)="([^"]*)"/g)) meta[m[1]] = m[2];
  return { meta, entries, glyphCount: defs.size };
}

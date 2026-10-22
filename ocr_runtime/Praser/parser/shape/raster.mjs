/**
 * FILE: Parsed/parser/shape/raster.mjs
 *
 * Raster -> vector, so the shape vote can be fed from scanned ink rather
 * than from a font's own outline.
 *
 * Two halves:
 *   rasteriseContours()  outline -> bitmap. Only needed to SIMULATE a scan
 *                        for measurement: render a glyph at scan resolution,
 *                        trace it back, and the gap between that and the
 *                        clean outline is the cost of tracing.
 *   traceBitmap()        bitmap -> closed contours, by boundary following.
 *                        This is the real path a cropped error region takes.
 *
 * Contours come back as pixel coordinates; `cloudFromContours` converts to
 * em units given a baseline row and a px-per-em scale, which the fiducial
 * ladder recovers. Nothing here bbox-normalises -- vertical position relative
 * to the baseline carries
 * real information and must survive.
 */

/** Flatten a quadratic bezier run into line points. */
function flattenContour(contour, steps = 4) {
  const pts = [];
  const n = contour.length;
  if (!n) return pts;
  // Build an on-curve start.
  let startIdx = contour.findIndex((p) => p.onCurve);
  if (startIdx < 0) startIdx = 0;
  const at = (i) => contour[(i % n + n) % n];
  let cur = contour[startIdx].onCurve
    ? { x: at(startIdx).x, y: at(startIdx).y }
    : { x: (at(startIdx).x + at(startIdx + 1).x) / 2, y: (at(startIdx).y + at(startIdx + 1).y) / 2 };
  pts.push([cur.x, cur.y]);
  let i = startIdx + 1;
  const end = startIdx + n;
  while (i <= end) {
    const p = at(i);
    if (p.onCurve) { pts.push([p.x, p.y]); cur = p; i += 1; continue; }
    const nxt = at(i + 1);
    const to = nxt.onCurve ? nxt : { x: (p.x + nxt.x) / 2, y: (p.y + nxt.y) / 2 };
    for (let s = 1; s <= steps; s += 1) {
      const t = s / steps; const u = 1 - t;
      pts.push([
        u * u * cur.x + 2 * u * t * p.x + t * t * to.x,
        u * u * cur.y + 2 * u * t * p.y + t * t * to.y,
      ]);
    }
    cur = to;
    i += nxt.onCurve ? 2 : 1;
  }
  return pts;
}

/**
 * Scan-convert font-unit contours to a bitmap at `pxPerEm`, non-zero-ish
 * even-odd fill. Returns { bits, w, h, baselineRow, originX }.
 */
export function rasteriseContours(contours, unitsPerEm, pxPerEm, pad = 2) {
  const polys = contours.map((c) => flattenContour(c));
  let minX = Infinity; let maxX = -Infinity; let minY = Infinity; let maxY = -Infinity;
  for (const poly of polys) {
    for (const [x, y] of poly) {
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
    }
  }
  if (!Number.isFinite(minX)) return null;
  const k = pxPerEm / unitsPerEm;
  const w = Math.max(1, Math.ceil((maxX - minX) * k) + pad * 2);
  const h = Math.max(1, Math.ceil((maxY - minY) * k) + pad * 2);
  const bits = new Uint8Array(w * h);
  // y is flipped: font y grows up, raster rows grow down.
  const toPx = ([x, y]) => [(x - minX) * k + pad, (maxY - y) * k + pad];
  const edges = [];
  for (const poly of polys) {
    for (let i = 0; i < poly.length; i += 1) {
      const a = toPx(poly[i]); const b = toPx(poly[(i + 1) % poly.length]);
      if (a[1] !== b[1]) edges.push([a, b]);
    }
  }
  for (let row = 0; row < h; row += 1) {
    const yc = row + 0.5;
    const xs = [];
    for (const [a, b] of edges) {
      const [ax, ay] = a; const [bx, by] = b;
      if ((yc >= ay && yc < by) || (yc >= by && yc < ay)) {
        xs.push(ax + ((yc - ay) / (by - ay)) * (bx - ax));
      }
    }
    xs.sort((p, q) => p - q);
    for (let i = 0; i + 1 < xs.length; i += 2) {
      const from = Math.max(0, Math.ceil(xs[i] - 0.5));
      const to = Math.min(w - 1, Math.floor(xs[i + 1] - 0.5));
      for (let x = from; x <= to; x += 1) bits[row * w + x] = 1;
    }
  }
  // Baseline (font y=0) in raster rows.
  const baselineRow = (maxY - 0) * k + pad;
  return { bits, w, h, baselineRow, originX: pad - minX * k };
}

/** Otsu threshold on an 8-bit grayscale buffer. Returns a 0/1 bitmap (1=ink). */
export function binarise(gray, w, h) {
  const hist = new Uint32Array(256);
  for (let i = 0; i < w * h; i += 1) hist[gray[i]] += 1;
  const total = w * h;
  let sum = 0;
  for (let i = 0; i < 256; i += 1) sum += i * hist[i];
  let sumB = 0; let wB = 0; let best = 0; let threshold = 127;
  for (let t = 0; t < 256; t += 1) {
    wB += hist[t];
    if (!wB) continue;
    const wF = total - wB;
    if (!wF) break;
    sumB += t * hist[t];
    const mB = sumB / wB;
    const mF = (sum - sumB) / wF;
    const between = wB * wF * (mB - mF) * (mB - mF);
    if (between > best) { best = between; threshold = t; }
  }
  const bits = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i += 1) bits[i] = gray[i] <= threshold ? 1 : 0; // dark = ink
  return bits;
}

/**
 * Closed boundaries of every ink blob, by Moore-neighbour tracing.
 * Returns an array of contours, each an array of [x, y] pixel coordinates.
 */
export function traceBitmap(bits, w, h, { minLength = 8 } = {}) {
  const at = (x, y) => (x < 0 || y < 0 || x >= w || y >= h ? 0 : bits[y * w + x]);
  const seen = new Uint8Array(w * h);
  const contours = [];
  // 8-neighbourhood, clockwise from west.
  const NB = [[-1, 0], [-1, -1], [0, -1], [1, -1], [1, 0], [1, 1], [0, 1], [-1, 1]];
  for (let y = 0; y < h; y += 1) {
    for (let x = 0; x < w; x += 1) {
      if (!at(x, y) || seen[y * w + x]) continue;
      if (at(x, y - 1)) continue;                   // not a top edge: inside a blob
      const contour = [];
      let cx = x; let cy = y; let dir = 0;
      const startX = x; const startY = y;
      let guard = 0;
      do {
        contour.push([cx, cy]);
        seen[cy * w + cx] = 1;
        let found = false;
        for (let k = 0; k < 8; k += 1) {
          const d = (dir + 6 + k) % 8;              // back up, then sweep
          const nx = cx + NB[d][0]; const ny = cy + NB[d][1];
          if (at(nx, ny)) { cx = nx; cy = ny; dir = d; found = true; break; }
        }
        if (!found) break;
        guard += 1;
      } while ((cx !== startX || cy !== startY) && guard < w * h * 4);
      if (contour.length >= minLength) contours.push(contour);
    }
  }
  return contours;
}

/**
 * Pixel contours -> point cloud in em units, baseline at y=0.
 * `baselineRow` is the raster row the baseline falls on; `pxPerEm` the scale
 * the fiducial ladder recovered.
 */
export function cloudFromContours(contours, { baselineRow, pxPerEm, originX = 0 }) {
  const pts = [];
  for (const c of contours) {
    for (const [x, y] of c) {
      pts.push([(x - originX) / pxPerEm, (baselineRow - y) / pxPerEm]);
    }
  }
  return pts;
}

/**
 * FILE: Parsed/parser/shape/contour.mjs
 *
 * CONTOUR-NATIVE MATCHING. No raster, no grid, no threshold, at any point.
 *
 * WHY THIS EXISTS. The matcher it replaced painted both sides into a
 * 48x48 binary mask, which throws away the two things an outline knows
 * exactly:
 *
 *   how many closed contours there are
 *   how big each one is relative to the others
 *
 * Measured on the sheet, those two facts separate six of the nine pairs
 * that were being treated as confusable, outright and for free:
 *
 *   တ 2 contours  vs  က 1         ဂ 1  vs  ဝ 2
 *   တ 2           vs  ထ 3         ဓ 3  vs  မ 2
 *   ပ 1           vs  ဖ 2         ဝ areas 66/34  vs  ၀ areas 51/49
 *
 * A mask cannot express any of that -- it is 2,304 bits of ink coverage.
 * So the matcher was being asked to resolve, by ink overlap, differences
 * that were exactly known before it rasterised them.
 *
 * SCALE INVARIANCE. The specimen sheet is drawn to A4. A traced page is
 * whatever size the scanner produced. Everything here normalises to the
 * glyph's own bounding box first, so the comparison is of shape only and
 * the two sides never need to agree on units.
 *
 * COST. Topology is an integer and a few floats: it filters the candidate
 * list down to a handful before any geometry runs. Only the survivors pay
 * for point matching. That ordering is what keeps this out of the hot
 * loop's way -- the expensive comparison runs on candidates that already
 * agree about their structure.
 */

const SAMPLES = 64;

/* ── geometry helpers ────────────────────────────────────────────────── */

function polygonArea(points) {
  let sum = 0;
  for (let i = 0, n = points.length; i < n; i += 1) {
    const [x1, y1] = points[i];
    const [x2, y2] = points[(i + 1) % n];
    sum += x1 * y2 - x2 * y1;
  }
  return Math.abs(sum) / 2;
}

function bounds(points) {
  let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
  for (const [x, y] of points) {
    if (x < x0) x0 = x;
    if (x > x1) x1 = x;
    if (y < y0) y0 = y;
    if (y > y1) y1 = y;
  }
  return { x0, y0, x1, y1, w: x1 - x0, h: y1 - y0 };
}

/**
 * Resample a closed contour to a fixed number of points, evenly spaced by
 * arc length. This is what makes the comparison independent of how the
 * tracer chose to place its vertices: a scanner trace and a font outline
 * describe the same curve with completely different vertex counts.
 */
function resample(points, count) {
  const n = points.length;
  if (n < 2) return new Array(count).fill(points[0] ?? [0, 0]);
  const seg = [];
  let total = 0;
  for (let i = 0; i < n; i += 1) {
    const [ax, ay] = points[i];
    const [bx, by] = points[(i + 1) % n];
    const d = Math.hypot(bx - ax, by - ay);
    seg.push(d);
    total += d;
  }
  if (total === 0) return new Array(count).fill(points[0]);

  const out = [];
  const step = total / count;
  let i = 0;
  let carried = 0;
  for (let k = 0; k < count; k += 1) {
    let want = k * step;
    while (i < n - 1 && carried + seg[i] < want) { carried += seg[i]; i += 1; }
    const t = seg[i] > 0 ? (want - carried) / seg[i] : 0;
    const [ax, ay] = points[i];
    const [bx, by] = points[(i + 1) % n];
    out.push([ax + (bx - ax) * t, ay + (by - ay) * t]);
  }
  return out;
}

/* ── signature ───────────────────────────────────────────────────────── */

/**
 * Reduce a glyph's contours to what identifies it, scale-free.
 *
 * `count` and `areas` are the exact facts a mask would have destroyed;
 * `points` is the geometry, normalised into a unit box so an A4 specimen
 * and a scanned page compare directly.
 */
export function glyphSignature(contours, { samples = SAMPLES } = {}) {
  const usable = (contours || []).filter((c) => Array.isArray(c) && c.length >= 3);
  if (!usable.length) return null;

  const box = bounds(usable.flat());
  const scale = Math.max(box.w, box.h) || 1;
  const areas = usable.map(polygonArea);
  const total = areas.reduce((a, b) => a + b, 0) || 1;

  /* Largest contour first, so two drawings of the same glyph line their
     parts up even when the tracer emitted them in a different order. */
  const order = usable
    .map((c, i) => ({ c, area: areas[i] }))
    .sort((a, b) => b.area - a.area);

  return {
    count: usable.length,
    aspect: box.h > 0 ? box.w / box.h : 1,
    areas: order.map((o) => o.area / total),
    points: order.map((o) => resample(o.c, samples).map(
      ([x, y]) => [(x - box.x0) / scale, (y - box.y0) / scale],
    )),
  };
}

/* ── distance ────────────────────────────────────────────────────────── */

/** Symmetric mean nearest-neighbour distance between two point sets. */
function cloudDistance(a, b, stride = 1) {
  let sum = 0;
  let n = 0;
  for (let i = 0; i < a.length; i += stride) {
    const [ax, ay] = a[i];
    let best = Infinity;
    for (let j = 0; j < b.length; j += stride) {
      const d = (ax - b[j][0]) ** 2 + (ay - b[j][1]) ** 2;
      if (d < best) best = d;
    }
    sum += Math.sqrt(best); n += 1;
  }
  for (let j = 0; j < b.length; j += stride) {
    const [bx, by] = b[j];
    let best = Infinity;
    for (let i = 0; i < a.length; i += stride) {
      const d = (a[i][0] - bx) ** 2 + (a[i][1] - by) ** 2;
      if (d < best) best = d;
    }
    sum += Math.sqrt(best); n += 1;
  }
  return n ? sum / n : Infinity;
}

/**
 * Do these two signatures agree about their STRUCTURE? Cheap, exact, and
 * decided before any geometry runs. A different number of closed contours
 * is a different letter -- not a near miss.
 */
export function topologyMatches(a, b, { areaTolerance = 0.08, aspectTolerance = 0.22 } = {}) {
  if (!a || !b) return false;
  // Cheapest first: an integer, then one float, then the area profile.
  if (a.count !== b.count) return false;
  if (Math.abs(a.aspect - b.aspect) > aspectTolerance * Math.max(a.aspect, b.aspect)) return false;
  for (let i = 0; i < a.areas.length; i += 1) {
    if (Math.abs(a.areas[i] - b.areas[i]) > areaTolerance) return false;
  }
  return true;
}

/**
 * Shape distance between two glyphs, on contours.
 *
 * Returns Infinity when the topology disagrees: that is a refusal, not a
 * large number, and it is the whole point -- တ has two contours and က has
 * one, so no amount of ink overlap should ever make them compete.
 */
/**
 * Shape distance between two glyphs, on contours.
 *
 * Topology is a VETO by default: တ has two closed contours and က has one,
 * so within a typeface they should never compete. Across typefaces it must
 * be a penalty instead -- a decorative face can draw the same letter with a
 * different number of loops, and refusing those outright scores every
 * candidate at zero. Pass `topologyPenalty` for the cross-font case.
 */
export function contourDistance(a, b, options = {}) {
  const penalty = options.topologyPenalty ?? null;
  if (!topologyMatches(a, b, options)) {
    if (penalty === null) return Infinity;
    // Compare the largest contour of each: the letter's body, whatever
    // decoration the face hangs off it.
    const d = cloudDistance(a.points[0], b.points[0], options.stride ?? 1);
    return d + penalty * Math.abs(a.count - b.count);
  }
  const stride = options.stride ?? 1;
  let sum = 0;
  for (let i = 0; i < a.points.length; i += 1) {
    sum += cloudDistance(a.points[i], b.points[i], stride);
  }
  return sum / a.points.length;
}

/**
 * Best match for a glyph among labelled signatures.
 *
 * `index` is [{ text, signature }]. Topology filters first, so the point
 * matching only runs against candidates that already share the structure.
 */
export function matchContours(signature, index, { maxDistance = 0.08, minMargin = 0.004 } = {}) {
  if (!signature) return { accepted: false, reason: 'empty glyph', matches: [] };

  const viable = [];
  for (const entry of index) {
    if (!topologyMatches(signature, entry.signature)) continue;
    viable.push(entry);
  }
  if (!viable.length) {
    return { accepted: false, reason: `no candidate with ${signature.count} contour(s)`, matches: [], considered: 0 };
  }

  /* Coarse pass on every sixteenth point, then the full comparison on the
     best few. Same answer, a fraction of the work: the point matching is
     quadratic, so shrinking the candidate list matters far more than
     shrinking the constant. */
  const coarse = viable
    .map((entry) => ({ entry, d: contourDistance(signature, entry.signature, { stride: 4 }) }))
    .sort((x, y) => x.d - y.d)
    .slice(0, 12);

  const scored = coarse
    .map(({ entry }) => ({ text: entry.text, distance: contourDistance(signature, entry.signature) }))
    .sort((x, y) => x.distance - y.distance);

  const best = scored[0];
  const margin = scored.length > 1 ? scored[1].distance - best.distance : Infinity;
  const accepted = best.distance <= maxDistance && margin >= minMargin;
  return {
    accepted,
    text: accepted ? best.text : null,
    distance: best.distance,
    margin,
    considered: viable.length,
    matches: scored.slice(0, 5),
    reason: accepted ? null
      : best.distance > maxDistance ? 'shape too far' : 'ambiguous among same-topology candidates',
  };
}

/** Build the labelled index once, from sheet entries. */
export function buildContourIndex(entries, options = {}) {
  const index = [];
  for (const entry of entries) {
    const signature = glyphSignature(entry.contours, options);
    if (signature) index.push({ text: entry.text, signature });
  }
  return index;
}

/* ── runtime font swap ───────────────────────────────────────────────
   ONE SYLLABLE INDEX, ANY FONT.

   The specimen sheet is drawn from a single typeface. There are 400+
   Myanmar legacy fonts, and a sheet per font is 400 files to generate,
   store and keep in step -- which is why the sheet was designed to be
   swappable rather than multiplied.

   It has to be per-font, not one sheet for all, because TOPOLOGY ITSELF
   CHANGES BETWEEN TYPEFACES: တ is two closed contours in Pyidaungsu and
   three in Ayar. A structure signature is only meaningful against the
   font that drew it.

   So the index is built from the font's own outlines at load time. No SVG
   is involved and nothing is rasterised: TrueType already stores closed
   contours, which is exactly what this module compares.

   Pass `glyphContours` and `loadFont` in rather than importing them --
   they live in tools/, which is beside this layer, not below it. */
export function buildFontIndex(fontFile, texts, { loadFont, glyphContours, samples = SAMPLES, codePage = null } = {}) {
  if (typeof loadFont !== 'function' || typeof glyphContours !== 'function') {
    throw new TypeError('buildFontIndex needs loadFont and glyphContours');
  }
  const font = loadFont(fontFile);
  const upm = font.unitsPerEm || 1000;
  const index = [];
  const missing = [];

  /* A LEGACY font does not map Unicode at all -- it maps the byte values of
     its own code page, which is the entire reason LEGACY_CODE_PAGES exists.
     Asking MAHURA.TTF for U+1000 returns nothing; asking it for byte 0x6B
     returns the glyph, and the code page says that byte means က. So when a
     code page is supplied, the glyph is looked up by BYTE and labelled with
     the text that byte decodes to. */
  if (codePage) {
    for (const [code, text] of codePage) {
      const gid = font.glyphId(code);
      if (!gid) { missing.push(text); continue; }
      const raw = glyphContours(font, gid);
      if (!raw || !raw.length) { missing.push(text); continue; }
      const signature = glyphSignature(
        raw.map((c) => c.map((pt) => [pt.x / upm, pt.y / upm])), { samples },
      );
      if (signature) index.push({ text, code, signature });
      else missing.push(text);
    }
    return { index, missing, font: fontFile, via: 'code-page' };
  }

  for (const text of texts) {
    // Single code points only: a cluster needs shaping, which belongs to
    // the sheet generator, not here.
    const cps = [...text];
    if (cps.length !== 1) continue;
    const gid = font.glyphId(cps[0].codePointAt(0));
    if (!gid) { missing.push(text); continue; }
    const raw = glyphContours(font, gid);
    if (!raw || !raw.length) { missing.push(text); continue; }
    const signature = glyphSignature(raw.map((c) => c.map((pt) => [pt.x / upm, pt.y / upm])), { samples });
    if (signature) index.push({ text, signature });
    else missing.push(text);
  }
  return { index, missing, font: fontFile, via: 'unicode' };
}

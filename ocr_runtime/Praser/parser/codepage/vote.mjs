/**
 * FILE: Parsed/parser/codepage/vote.mjs
 *
 * RASTER-TO-SVG REGION REPAIR -- the OCR lane's measuring tools.
 *
 * Everything here compares pixels against the labelled SVG specimen
 * sheets (shape/): a rendered PDF region, or an image a document or a
 * scanner already carries, is measured and either named or refused.
 * This is the repair stage of OCR, where the input genuinely is ink
 * with no text layer behind it.
 *
 * It is NOT a text path and must not be wired in as one: a digital PDF
 * carries its glyphs as outlines, and deciding a code page from
 * rendered pixels throws that away to recover it approximately. The
 * document-scale auto-build that used to live here (pixel crops voting
 * a candidate layout into resolve.mjs) was replaced by the outline
 * build in vector-repair.mjs (`buildVectorOutlineLayout`, text path
 * 5), which reads the embedded font programs directly. What remains
 * in this file is region-scale work only.
 *
 * Split out of the former module/vector-repair.mjs, which held both
 * this and the pure shape maths now in shape/match.mjs.
 */

import {
  cropCanvasRgba,
  decodeImageRgba,
  openPdf,
  renderPageCanvas,
} from "../read/pdf-render.mjs";
import { binarise } from "../shape/raster.mjs";
import { matchRasterMask } from "../shape/match.mjs";

function grayFromRgba({ data, width, height }) {
  const gray = new Uint8Array(width * height);
  for (let i = 0; i < width * height; i += 1) {
    const at = i * 4;
    // Composite transparent pixels onto white before thresholding.
    const alpha = data[at + 3] / 255;
    const r = data[at] * alpha + 255 * (1 - alpha);
    const g = data[at + 1] * alpha + 255 * (1 - alpha);
    const b = data[at + 2] * alpha + 255 * (1 - alpha);
    gray[i] = Math.round(r * 0.299 + g * 0.587 + b * 0.114);
  }
  return { gray, width, height };
}

/** Map RGBA page pixels to SVG labels: composite, threshold, measure. Synchronous. */
export function matchPixelsToVector({ data, width, height }, options = {}) {
  const { gray } = grayFromRgba({ data, width, height });
  return matchRasterMask(binarise(gray, width, height), width, height, options);
}

/**
 * Same measurement for an image a document or a scanner already carries: those
 * bytes arrive encoded (PNG, JPEG, ...) and only get decoded to be measured.
 * Nothing here ever ENCODES -- a caller that renders a page keeps the pixels.
 */
export async function matchImageToVector(imageBytes, options = {}) {
  return matchPixelsToVector(await decodeImageRgba(imageBytes), options);
}

/**
 * Render exactly one suspect PDF region and hand back its RGBA pixels. `bbox`
 * uses PDF.js viewport coordinates (page points, origin at the top left) and
 * may come from PDF.js text geometry, Tesseract, or extraction-line layout.
 */
export async function renderPdfRegionRgba(source, pageNumber, bbox, {
  scale = 4,
  pad = 4,
} = {}) {
  const doc = await openPdf(Buffer.isBuffer(source) ? source : Buffer.from(source));
  try {
    if (pageNumber < 1 || pageNumber > doc.numPages) throw new RangeError(`Invalid PDF page ${pageNumber}`);
    const page = await doc.getPage(pageNumber);
    try {
      const rendered = await renderPageCanvas(page, scale);
      return cropCanvasRgba(rendered.canvas, {
        x0: Math.min(bbox.x0, bbox.x1),
        y0: Math.min(bbox.y0, bbox.y1),
        x1: Math.max(bbox.x0, bbox.x1),
        y1: Math.max(bbox.y0, bbox.y1),
      }, { scale, pad });
    } finally {
      page.cleanup();
    }
  } finally {
    await doc.destroy();
  }
}

export async function matchPdfRegionToVector(source, pageNumber, bbox, options = {}) {
  const crop = await renderPdfRegionRgba(source, pageNumber, bbox, options);
  // The pixel buffer stays out of the result: a caller asked for the label.
  return { ...matchPixelsToVector(crop, options), crop: { width: crop.width, height: crop.height } };
}

/**
 * Replace one scanner/OCR-located text range only when the pixels of that
 * region have an unambiguous labelled-SVG match. A refusal leaves the original
 * text intact. `image` is the region as bytes the source already carries.
 */
export async function repairTextRegionWithVector(text, {
  start,
  end,
  image,
  candidates,
}, options = {}) {
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end < start || end > text.length) {
    throw new RangeError("invalid vector-repair text range");
  }
  const match = await matchImageToVector(image, { ...options, candidates });
  if (!match.accepted) return { text, changed: false, match };
  return {
    text: `${text.slice(0, start)}${match.text}${text.slice(end)}`,
    changed: true,
    match,
  };
}

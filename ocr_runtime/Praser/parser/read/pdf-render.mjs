import { fileURLToPath } from "node:url";
import { createCanvas, loadImage } from "@napi-rs/canvas";

let pdfjsPromise = null;
let pdfjsAssetRoot = null;

async function pdfjs() {
  if (!pdfjsPromise) {
    const moduleUrl = import.meta.resolve("pdfjs-dist/legacy/build/pdf.mjs");
    pdfjsAssetRoot = fileURLToPath(new URL("../..", moduleUrl));
    pdfjsPromise = import(moduleUrl).catch((cause) => {
      pdfjsPromise = null;
      throw new Error(`PDF.js renderer is unavailable: ${cause?.message || cause}`, { cause });
    });
  }
  return pdfjsPromise;
}

function asBytes(source) {
  if (source instanceof Uint8Array) return new Uint8Array(source);
  if (Buffer.isBuffer(source)) return new Uint8Array(source);
  throw new TypeError("PDF source must be a Buffer or Uint8Array");
}

/** Open a PDF with Mozilla PDF.js (Apache-2.0). The caller must call destroy(). */
export async function openPdf(source, options = {}) {
  const api = await pdfjs();
  const task = api.getDocument({
    data: asBytes(source),
    isEvalSupported: false,
    useSystemFonts: true,
    stopEventLoop: true,
    wasmUrl: `${pdfjsAssetRoot}wasm/`,
    cMapUrl: `${pdfjsAssetRoot}cmaps/`,
    cMapPacked: true,
    standardFontDataUrl: `${pdfjsAssetRoot}standard_fonts/`,
    ...options,
  });
  const doc = await task.promise;
  // PDF.js 6 exposes destruction on the loading task rather than the proxy.
  if (typeof doc.destroy !== "function") {
    Object.defineProperty(doc, "destroy", { value: () => task.destroy() });
  }
  return doc;
}

export async function renderPageCanvas(page, scale = 3, { maxPixels = Number.MAX_SAFE_INTEGER } = {}) {
  if (!Number.isFinite(scale) || scale <= 0 || scale > 8) {
    throw new RangeError("PDF render scale must be greater than zero and at most 8");
  }
  const viewport = page.getViewport({ scale });
  const width = Math.max(1, Math.ceil(viewport.width));
  const height = Math.max(1, Math.ceil(viewport.height));
  const pixels = width * height;
  if (!Number.isSafeInteger(pixels) || pixels > maxPixels) {
    throw new Error(`PDF render exceeds the ${maxPixels} pixel cap`);
  }
  const canvas = createCanvas(width, height);
  const context = canvas.getContext("2d");
  await page.render({ canvasContext: context, viewport, canvas }).promise;
  return { canvas, viewport, width, height, scale };
}

/**
 * Render one page to pixels. The only encoding produced here is PNG, for tools
 * that are handed an image FILE rather than a canvas -- tesseract reads bytes,
 * so the OCR lane gets lossless PNG. Nothing is ever decoded back: a caller
 * that stays in this process takes `canvas` or a crop of it instead.
 */
export async function renderPdfPagePng(source, pageNumber, {
  scale = 3,
  maxPixels = Number.MAX_SAFE_INTEGER,
} = {}) {
  const doc = await openPdf(source);
  try {
    if (pageNumber < 1 || pageNumber > doc.numPages) throw new RangeError(`Invalid PDF page ${pageNumber}`);
    const page = await doc.getPage(pageNumber);
    try {
      const rendered = await renderPageCanvas(page, scale, { maxPixels });
      return {
        png: rendered.canvas.toBuffer("image/png"),
        canvas: rendered.canvas,
        width: rendered.width,
        height: rendered.height,
        scale,
      };
    } finally {
      page.cleanup();
    }
  } finally {
    await doc.destroy();
  }
}

/**
 * Crop a top-left-origin PDF.js page bbox out of an already rendered canvas and
 * return its RGBA pixels. Reading the region straight off the page canvas is the
 * whole point: the ink goes to the matcher without an image file format, a
 * lossy encode, or a decode in between.
 */
export function cropCanvasRgba(canvas, bbox, { scale = 1, pad = 0 } = {}) {
  const x0 = Math.max(0, Math.floor((bbox.x0 - pad) * scale));
  const y0 = Math.max(0, Math.floor((bbox.y0 - pad) * scale));
  const x1 = Math.min(canvas.width, Math.ceil((bbox.x1 + pad) * scale));
  const y1 = Math.min(canvas.height, Math.ceil((bbox.y1 + pad) * scale));
  const width = Math.max(1, x1 - x0);
  const height = Math.max(1, y1 - y0);
  return { width, height, data: canvas.getContext("2d").getImageData(x0, y0, width, height).data };
}

/** Decode an image a document or a scanner already carries (PNG, JPEG, ...) to RGBA. */
export async function decodeImageRgba(bytes) {
  const image = await loadImage(Buffer.isBuffer(bytes) ? bytes : Buffer.from(bytes));
  const canvas = createCanvas(image.width, image.height);
  const context = canvas.getContext("2d");
  context.drawImage(image, 0, 0);
  const data = context.getImageData(0, 0, image.width, image.height).data;
  return { width: image.width, height: image.height, data: new Uint8ClampedArray(data) };
}

export async function pdfPageCount(source) {
  const doc = await openPdf(source);
  try {
    return doc.numPages;
  } finally {
    await doc.destroy();
  }
}

export async function pageHasRasterImage(source, pageNumber) {
  const api = await pdfjs();
  const doc = await openPdf(source);
  try {
    if (pageNumber < 1 || pageNumber > doc.numPages) return false;
    const page = await doc.getPage(pageNumber);
    try {
      const operators = await page.getOperatorList();
      const imageOps = new Set([
        api.OPS.paintImageMaskXObject,
        api.OPS.paintImageXObject,
        api.OPS.paintInlineImageXObject,
        api.OPS.paintImageXObjectRepeat,
        api.OPS.paintImageMaskXObjectRepeat,
        api.OPS.paintSolidColorImageMask,
      ]);
      return operators.fnArray.some((op) => imageOps.has(op));
    } finally {
      page.cleanup();
    }
  } finally {
    await doc.destroy();
  }
}

/**
 * Return approximate character boxes in PDF.js viewport coordinates at scale 1.
 * PDF text items occasionally group several source characters; their advance is
 * divided over code points so narrow marks still get a local crop with padding.
 */
export async function pageTextCharacters(page) {
  const content = await page.getTextContent({ disableNormalization: true, includeMarkedContent: false });
  const viewport = page.getViewport({ scale: 1 });
  const styles = content.styles || {};
  const chars = [];
  for (const item of content.items) {
    if (!item || typeof item.str !== "string" || !item.str) continue;
    const text = [...item.str];
    const tx = item.transform || [1, 0, 0, 1, 0, 0];
    const fontHeight = Math.max(1, Math.hypot(tx[2], tx[3]));
    const totalWidth = Math.max(0.01, Number(item.width) || Math.hypot(tx[0], tx[1]) * text.length);
    const perChar = totalWidth / text.length;
    // Font metrics from PDF.js let a caller correlate this PDF.js font with
    // the PDF's own /FontDescriptor (ascent/descent match), i.e. recover the
    // /BaseFont name of the face that drew the ink.
    const style = styles[item.fontName] || null;
    for (let index = 0; index < text.length; index += 1) {
      const pdfX0 = tx[4] + perChar * index;
      const pdfX1 = tx[4] + perChar * (index + 1);
      const first = viewport.convertToViewportPoint(pdfX0, tx[5]);
      const second = viewport.convertToViewportPoint(pdfX1, tx[5] + fontHeight);
      chars.push({
        code: text[index].codePointAt(0),
        text: text[index],
        bbox: {
          x0: Math.min(first[0], second[0]),
          y0: Math.min(first[1], second[1]),
          x1: Math.max(first[0], second[0]),
          y1: Math.max(first[1], second[1]),
        },
        fontName: item.fontName || "",
        fontAscent: style ? style.ascent : null,
        fontDescent: style ? style.descent : null,
      });
    }
  }
  return chars;
}

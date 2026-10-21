import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import { OCR_MODEL_DIR } from "../core/paths.mjs";
import { MAX_OCR_PIXELS } from "../core/limits.mjs";
import {
  pageHasRasterImage,
  pdfPageCount as countPdfPages,
  renderPdfPagePng,
} from "./pdf-render.mjs";

const OCR_CACHE_DIR = path.join(os.tmpdir(), "linga-ocr");
const BUNDLED_OCR_MODEL_DIR = OCR_MODEL_DIR;
const DEFAULT_LANGS = (process.env.PRASER_OCR_LANG || "mya")
  .split(/[+,]/).map((s) => s.trim()).filter(Boolean);

let workerPromise = null;
let workerKey = null;





function findInDir(dir, langs) {
  if (!dir || !fs.existsSync(dir)) return null;
  for (const lang of langs) {
    if (fs.existsSync(path.join(dir, `${lang}.traineddata.gz`))) return { lang, gzip: true };
    if (fs.existsSync(path.join(dir, `${lang}.traineddata`))) return { lang, gzip: false };
  }
  return null;
}


function findLangPath(langs) {
  const customDir = process.env.PRASER_OCR_LANG_PATH;
  let hit = findInDir(customDir, langs);
  if (hit) return { path: path.resolve(customDir), gzip: hit.gzip };

  hit = findInDir(BUNDLED_OCR_MODEL_DIR, langs);
  if (hit) return { path: BUNDLED_OCR_MODEL_DIR, gzip: hit.gzip };

  const base = path.resolve("node_modules/@tesseract.js-data");
  if (fs.existsSync(base)) {
    for (const lang of langs) {
      const dir = path.join(base, lang);
      if (!fs.existsSync(dir)) continue;
      const versions = fs.readdirSync(dir)
        .filter((v) => fs.existsSync(path.join(dir, v, `${lang}.traineddata.gz`)))
        .sort()
        .reverse();
      if (versions.length) return { path: path.join(dir, versions[0]), gzip: true };
    }
  }
  return null;
}


export async function ocrAvailable() {
  try {
    await import("tesseract.js");
    await import("./pdf-render.mjs");
    return true;
  } catch {
    return false;
  }
}


export async function initOcr({ langs = DEFAULT_LANGS, log = () => {} } = {}) {
  const key = langs.join("+");
  if (!workerPromise || workerKey !== key) {
    workerKey = key;
    workerPromise = (async () => {
      const { createWorker } = await import("tesseract.js");
      const found = findLangPath(langs);
      const options = {
        cacheMethod: "none",
        logger: () => {},
        
        
        workerPath: path.join(path.dirname(fileURLToPath(import.meta.url)), "ocr-no-relaxedsimd.cjs"),
      };
      if (found) {
        options.langPath = found.path;
        options.gzip = found.gzip; 
      } else {
        fs.mkdirSync(OCR_CACHE_DIR, { recursive: true });
        options.cachePath = OCR_CACHE_DIR;
      }
      log(`[+] OCR: tesseract.js ${key}${found ? ` (traineddata: ${path.relative(process.cwd(), found.path)})` : " (downloads traineddata once)"}`);
      return createWorker(langs, 1, options);
    })();
  }
  return workerPromise;
}

function flattenOcrRegions(blocks) {
  const regions = [];
  for (const block of blocks || []) {
    for (const paragraph of block.paragraphs || []) {
      for (const line of paragraph.lines || []) {
        for (const word of line.words || []) {
          regions.push({
            text: word.text || "",
            confidence: Number(word.confidence) || 0,
            bbox: word.bbox ? { ...word.bbox } : null,
            symbols: (word.symbols || []).map((symbol) => ({
              text: symbol.text || "",
              confidence: Number(symbol.confidence) || 0,
              bbox: symbol.bbox ? { ...symbol.bbox } : null,
            })),
          });
        }
      }
    }
  }
  return regions;
}

/** OCR text plus pixel geometry for localizing scanner/unknown regions. */
export async function ocrImageDetailed(source, { log = () => {} } = {}) {
  const bytes = Buffer.isBuffer(source) ? source : Buffer.from(source);
  const worker = await initOcr({ log });
  const { data } = await worker.recognize(bytes, {}, { text: true, blocks: true });
  return {
    text: data.text || "",
    confidence: Number(data.confidence) || 0,
    regions: flattenOcrRegions(data.blocks),
  };
}

export async function ocrImage(source, options = {}) {
  return (await ocrImageDetailed(source, options)).text;
}


export async function pageHasImage(source, pageNumber) {
  return pageHasRasterImage(source, pageNumber);
}


export async function pdfPageCount(source) {
  return countPdfPages(source);
}


export async function ocrPdfPage(source, pageNumber, { scale = 3, log = () => {} } = {}) {
  if (!Number.isFinite(scale) || scale <= 0 || scale > 4) {
    throw new Error("OCR scale must be greater than zero and at most 4");
  }
  // Lossless in and out: tesseract thresholds the page itself, so a lossy
  // encode here only adds ringing around the marks it is asked to read.
  const rendered = await renderPdfPagePng(source, pageNumber, { scale, maxPixels: MAX_OCR_PIXELS });
  const pixels = rendered.width * rendered.height;
  if (!Number.isSafeInteger(pixels) || pixels > MAX_OCR_PIXELS) {
    throw new Error(`OCR page exceeds the ${MAX_OCR_PIXELS} pixel cap`);
  }
  return ocrImage(rendered.png, { log });
}


export async function closeOcr() {
  if (!workerPromise) return;
  try {
    const worker = await workerPromise;
    await worker.terminate();
  } catch { /* worker never started */ }
  workerPromise = null;
  workerKey = null;
}

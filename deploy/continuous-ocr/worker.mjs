// Persistent OCR/model/reference assets. No overall/book/page timeout.
import fs from 'node:fs';
import readline from 'node:readline';
import { openPdf, renderPageCanvas, cropCanvasRgba } from '../../ocr_runtime/Praser/parser/read/pdf-render.mjs';
import { initOcr, ocrImageDetailed, closeOcr } from '../../ocr_runtime/Praser/parser/read/ocr.mjs';
import { getDefaultVectorIndex } from '../../ocr_runtime/Praser/parser/shape/match.mjs';
import { matchPixelsToVector } from '../../ocr_runtime/Praser/parser/codepage/vote.mjs';
import { loadScanDictionary, dictionaryWords, scanBrokenSpans } from '../../ocr_runtime/Praser/parser/text/scan.mjs';
import { repairText, HOMOGLYPH_PAIRS } from '../../ocr_runtime/Praser/parser/text/repair.mjs';
import { normalizeText } from '../../ocr_runtime/Praser/parser/text/normalize.mjs';
import { MAX_OCR_PIXELS } from '../../ocr_runtime/Praser/parser/core/limits.mjs';

const reply = (v) => process.stdout.write('OCR_RESULT ' + JSON.stringify(v) + '\n');
const data = loadScanDictionary();
const words = dictionaryWords(data);
if (!words?.size) throw new Error('OCR scan dictionary is empty; refusing unguarded repair');
const standalone = data?.mapData?.newClause?.standalone || [];
const index = getDefaultVectorIndex();
await initOcr(); // The trained model stays in one worker across all books.
reply({ ready: true, vectorEntries: index.entries.length, dictionaryWords: words.size });

function isHomoglyphOnly(a, b) {
  const x = [...a], y = [...b];
  return x.length === y.length && x.every((c, i) => c === y[i] || HOMOGLYPH_PAIRS.some(([p,q]) =>
    (c === p && y[i] === q) || (c === q && y[i] === p)));
}

async function run(request) {
  const doc = await openPdf(fs.readFileSync(request.input));
  const fd = fs.openSync(request.output, 'w', 0o600);
  let chars = 0, vectorRepairs = 0;
  try {
    for (let n = 1; n <= doc.numPages; n++) {
      const page = await doc.getPage(n);
      try {
        const rendered = await renderPageCanvas(page, 3, { maxPixels: MAX_OCR_PIXELS });
        const ocr = await ocrImageDetailed(rendered.canvas.toBuffer('image/png'));
        let text = ocr.text;
        const spans = scanBrokenSpans(text, { dictionary: words, standalone });
        // Work from original Tesseract geometry/text. Whole index is reused.
        const edits = [];
        let cursor = 0;
        for (const region of ocr.regions) {
          const at = text.indexOf(region.text, cursor);
          if (at < 0 || !region.text) continue;
          cursor = at + region.text.length;
          if (!region.bbox || !spans.some((s) => s.start < cursor && s.end > at)) continue;
          // Numeric/letter homoglyphs are dictionary-only, never shape guesses.
          if (/^[\u1040-\u1049]+$/u.test(region.text)) continue;
          const crop = cropCanvasRgba(rendered.canvas, region.bbox, { scale: 1, pad: 2 });
          const match = matchPixelsToVector(crop, { index });
          if (!match.accepted || !match.text || isHomoglyphOnly(region.text, match.text)) continue;
          const candidate = normalizeText(match.text);
          if (!words.has(candidate) || words.has(normalizeText(region.text))) continue;
          edits.push({ start: at, end: cursor, text: candidate });
        }
        for (const edit of edits.reverse()) text = text.slice(0, edit.start) + edit.text + text.slice(edit.end);
        vectorRepairs += edits.length;
        text = repairText(normalizeText(text), words).text;
        chars += text.trim().length;
        fs.writeSync(fd, JSON.stringify({ page: n, text }) + '\n');
        process.stderr.write(`[ocr-page] ${n}/${doc.numPages}\n`);
      } finally { page.cleanup(); }
    }
    if (!chars) throw new Error('OCR returned no text; PDF retained');
    fs.fsyncSync(fd);
    return { ok: true, pages: doc.numPages, chars, vectorRepairs };
  } finally { fs.closeSync(fd); await doc.destroy(); }
}
const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
try {
  for await (const line of lines) {
    if (!line.trim()) continue;
    const request = JSON.parse(line);
    if (request.close) break;
    try { reply(await run(request)); }
    catch (error) { reply({ ok: false, error: `${error.name || 'Error'}: ${error.message || String(error)}\n${error.stack || ''}` }); }
  }
} finally { await closeOcr(); }

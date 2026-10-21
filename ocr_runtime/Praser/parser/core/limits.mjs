import { ValueError } from './pylib.mjs';

export function positiveLimit(name, fallback, maximum) {
  const raw = process.env[name] ?? String(fallback);
  if (!/^\d+$/.test(String(raw))) {
    throw new ValueError(`${name} must be a positive integer`);
  }
  const value = Number.parseInt(raw, 10);
  if (!Number.isSafeInteger(value) || value < 1 || value > maximum) {
    throw new ValueError(`${name} must be between 1 and ${maximum}`);
  }
  return value;
}

export const MAX_INPUT_MB = positiveLimit('UPLOAD_MAX_MB', 64, 2048);
export const MAX_INPUT_BYTES = MAX_INPUT_MB * 1024 * 1024;
export const MAX_PAGES = positiveLimit('PRASER_MAX_PAGES', 10_000, 500_000);
export const MAX_TEXT_CHARS = positiveLimit('PRASER_MAX_TEXT_CHARS', 20_000_000, 1_000_000_000);
// Both `default` and `manual` methods run in the Praser extraction lane and
// share this budget. OCR has a separate base plus a per-page allowance.
export const WORKER_TIMEOUT_MS = positiveLimit('PRASER_TIMEOUT_MS', 120_000, 3_600_000);
export const OCR_BASE_TIMEOUT_MS = positiveLimit(
  'PRASER_OCR_TIMEOUT_MS',
  120_000,
  1_800_000,
);
export const WORKER_HEAP_MB = positiveLimit('PRASER_WORKER_HEAP_MB', 384, 1024);
export const MAX_OCR_PIXELS = positiveLimit('PRASER_MAX_OCR_PIXELS', 40_000_000, 100_000_000);

export const MAX_OCR_PAGES = positiveLimit('PRASER_MAX_OCR_PAGES', 50, 500);
export const OCR_PAGE_TIMEOUT_MS = positiveLimit('PRASER_OCR_PAGE_MS', 20_000, 120_000);

export function ocrTimeoutMs(pages = 0) {
  const n = Number.isSafeInteger(pages) && pages > 0 ? Math.min(pages, MAX_OCR_PAGES) : 1;
  return OCR_BASE_TIMEOUT_MS + n * OCR_PAGE_TIMEOUT_MS;
}

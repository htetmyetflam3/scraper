/**
 * Myanmar character mapping data.
 *
 * All mapping objects live in sourceMap.json so the data can be audited,
 * regenerated, and reused without editing executable code. This module only
 * turns the JSON arrays into Sets and exposes the JSON tables to consumers.
 */
// The readable tables: knowledge/isTreasure/master.json is the single source
// of truth on this branch. The obfuscator INLINES this JSON into the flattened
// bundle at build time (the shipped payload never reads a readable JSON); the
// encrypted twin for runtime reads is MonoSyllabism/map/master.json.enc.
import mapData from "../../../knowledge/isTreasure/master.json" with { type: "json" };

// Set-based tables used by normalize.js and the segmenter.
export const PUNCTUATION = new Set(mapData.PUNCTUATION);
export const Glue = new Set(mapData.Glue);
export const ENGLISH_DIGITS = new Set(mapData.ENGLISH_DIGITS);
export const BURMESE_DIGITS = new Set(mapData.BURMESE_DIGITS);
export const ALL_DIGITS = new Set([...ENGLISH_DIGITS, ...BURMESE_DIGITS]);

export const BLOCK_PUNCT = new Set(mapData.BLOCK_PUNCT);

// Structured mapping data is JSON data, not executable object literals.
const MMap = mapData.MMap;
const NewMap = mapData.NewMap;
const newClause = mapData.newClause;
const Priority = mapData.Priority;
const IMPOSTER = mapData.IMPOSTER;

export { MMap, NewMap, newClause, IMPOSTER, Priority,BLOCK_PUNCT,ALL_DIGITS, PUNCTUATION,Glue,BURMESE_DIGITS,ENGLISH_DIGITS};

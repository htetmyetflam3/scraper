/* --- graph/build.js --- */
import fs from 'fs';
import { buildStart, buildEnd, buildFail, treeBuilt } from '../../Bridge/logen.js';
import { Bb, buildSyllableLookup, buildPosTree, extractPosData } from './disassembler.js';
import { TreeFile, joinPath, DataFile } from '../../Bridge/path.js';
import { getStep } from './shape.js';
import { loadSyllablesData, loadMappedRows } from '../main/grapheme/Tree.loaded.js';
import {
  writeTreeToDir,
  writeMappedToDir,
  writeRtlToDir,
  writeRefToDir,
} from '../utilities/disk.js';
import { parseJson } from '../main/grapheme/Tree.loaded.js';

export async function runBuild(sourceData, outputDir = TreeFile()) {
  if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir, { recursive: true });
  }
  buildStart({ outputDir });

  try {
    let lookupData = sourceData;
    let refArray = null;

    if (!lookupData) {
      const syllables = await loadSyllablesData();
      const result = buildSyllableLookup(syllables);
      lookupData = result.lookupData;
      refArray = result.refArray;

      writeMappedToDir(result.mappedLines, outputDir);
      writeRtlToDir(result.rtlGrouped, outputDir);
      writeRefToDir(result.refArray, outputDir);
    }

    const built = Bb(lookupData, { getStep });
    writeTreeToDir(built.data, outputDir);

    treeBuilt({ roots: Object.keys(built.data.Root).length, outputFile: outputDir });
    buildEnd({ roots: Object.keys(built.data.Root).length, outputFile: outputDir });

    return { data: built.data, refArray };
  } catch (err) {
    buildFail({ error: err.message });
    throw err;
  }
}

export async function runBuildPos(outputDir = TreeFile(), sylEntries = null) {
  console.log(`[POS-BUILD] start: ${outputDir}`);

  try {
    // Memory first (threaded from runBuild via runBuildPhase); file fallback
    // loads through loaded.js — the single home for between-stage files.
    const sylToId = new Map();
    const feed = (syl, id) => {
      if (!sylToId.has(syl)) sylToId.set(syl, id);
    };
    if (sylEntries) {
      for (const e of sylEntries) feed(e.original, e.id);
    } else {
      const mappedPath = joinPath(outputDir, 'syllable.mapped.txt');
      const rows = loadMappedRows(outputDir, {
        skipComments: false, // exact legacy parse: even #-lines hit the warning below
        onBadLine: (n, line) => console.error(`  Warning: Could not parse line ${n}: ${line}`),
      });
      if (!rows) {
        throw new Error(`Mapped file not found: ${mappedPath}. Run syllable build first.`);
      }
      for (const r of rows) feed(r.burmese, r.id);
    }

    console.log(`[POS-BUILD] Loaded ${sylToId.size} mappings`);

    const masterPath = joinPath(DataFile(), 'master.json');
    const masterData = parseJson(masterPath);

    const { source, posData } = extractPosData(masterData);

    const { tree, failedEntries } = buildPosTree(sylToId, posData);

    if (failedEntries.length > 0) {
      const failPath = joinPath(outputDir, 'pos-fail.txt');
      const failLines = failedEntries.map(
        (e) => `[${e.pos}] "${e.line}" | missing: ${e.missing.join(', ')}`
      );
      fs.writeFileSync(failPath, failLines.join('\n') + '\n', 'utf8');
      console.log(`[POS-BUILD] Wrote ${failedEntries.length} failures to: ${failPath}`);
    }

    let cleaned = 0;
    const byKey = {};
    for (const e of failedEntries) {
      if (!byKey[e.jsonKey]) byKey[e.jsonKey] = new Set();
      byKey[e.jsonKey].add(e.line);
    }

    for (const [jsonKey, failedSet] of Object.entries(byKey)) {
      const originalArray = source[jsonKey];
      if (!Array.isArray(originalArray)) continue;

      const before = originalArray.length;
      const cleanedArray = originalArray.filter((line) => !failedSet.has(line));
      cleaned += (before - cleanedArray.length);
      source[jsonKey] = cleanedArray;
    }

    if (cleaned > 0) {
      fs.writeFileSync(masterPath, JSON.stringify(masterData, null, 2), 'utf8');
      console.log(`[POS-BUILD] Removed ${cleaned} failed entries from ${masterPath}`);
    }

    const posPath = joinPath(outputDir, 'pos.tree.json');
    fs.writeFileSync(posPath, JSON.stringify(tree, null, 2), 'utf8');

    console.log(`[POS-BUILD] Wrote: ${posPath}`);
    return tree;
  } catch (err) {
    console.error(`[POS-BUILD] fail: ${err.message}`);
    throw err;
  }
}

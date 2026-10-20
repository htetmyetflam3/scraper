// Syllable-segmentor bridge: stdin (documents separated by U+001D) ->
// OUTPUT FILE argv[2] (segmented documents separated by U+001D).
//
// The result goes to a file, not stdout, because the Engine's logger prints
// its badges to stdout and must stay untouched; stdout/stderr are purely
// diagnostic here.
//
// Run by text_collect.py exactly once per download run: the whole collected
// text of the run is flushed through a single process in one stream. Each
// document is normalised by the Engine's own normalizer and walked by
// runSegmentor; the token stream is joined back into one line-per-input-line
// string, so the output keeps the per-document split the Parquet writer
// needs while the Engine itself stays byte-for-byte untouched.
//
// WHICH normalizer: the loader's DEFAULT export = mapperNormalize — the same
// one the Engine's own pipeline uses (loaded.js imports the default as
// mapperNormalize). It is the variant where mark REORDERING is part of
// normalize (normalize -> reorderAndWrap -> mapperStr); English/digit blocks
// survive wrapped ({...}) instead of being deleted. MyNormalize was the
// earlier, wrong pick here: no reordering, and it deletes English blocks.
//
// Runtime requirement: Node.js 22+ (the systemd preflight enforces this too).
// Requirements (one-time, per machine):
//   * Engine/Bridge/{path.js,logen.js} — committed shims;
//   * Engine/MonoSyllabism/mapbuilder: `npm install` (bytenode for the
//     encrypted map loader);
//   * Engine/.cache-tree/ — the PRECOMPUTED syllable tree, committed to the
//     repo (syllable.tree.json, rtl-tree.json, syllable.mapped.txt,
//     ref-syllables.json). The tree builder (graph/build.js) is not shipped;
//     these files are the build output and are read as-is.
import fs from "node:fs";
import path from "node:path";
import url from "node:url";

const NODE_MAJOR = Number(process.versions.node.split(".", 1)[0]);
if (NODE_MAJOR < 22) {
  console.error(`[segment] Node.js 22+ is required; found ${process.version} at ${process.execPath}.`);
  process.exit(1);
}

const HERE = path.dirname(url.fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const ENGINE = path.join(REPO, "Engine");
const SEP = "\u001d"; // GROUP SEPARATOR between documents, both directions

async function ensureTree() {
  const { TreeFile } = await import(
    url.pathToFileURL(path.join(ENGINE, "Bridge", "path.js")).href
  );
  const tree = TreeFile();
  if (fs.existsSync(tree) && fs.readdirSync(tree).length > 0) return;
  console.error(
    `[segment] missing the precomputed syllable tree: ${tree}\n` +
      "[segment] Engine/.cache-tree/ ships with the repo — restore it " +
      "(git checkout -- Engine/.cache-tree) instead of rebuilding.",
  );
  process.exit(1);
}

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on("data", (c) => chunks.push(c));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    process.stdin.on("error", reject);
  });
}

async function segmentDocument(text, engineNormalize, runSegmentor, toSyllableLine) {
  // The segmentor consumes a line stream and emits one token per syllable;
  // "\n" tokens come back as themselves, so the stream preserves lines.
  const normalized = engineNormalize(text);
  async function* source() {
    yield normalized.endsWith("\n") ? normalized : normalized + "\n";
  }
  const parts = [];
  for await (const token of runSegmentor(source())) parts.push(token);
  // The √-suffixed stream is the segmentor's internal handoff contract
  // (utilities/formatter.js owns it). What leaves the Engine is the
  // FORMATTER's result: each line through toSyllableLine — syllables
  // joined by the formatter's own delimiter, the √ marks consumed here
  // and nowhere downstream.
  return parts
    .join("")
    .split("\n")
    .map((line) => toSyllableLine(line))
    .join("\n");
}

const input = await readStdin();
await ensureTree();
const { default: engineNormalize } = await import(
  url.pathToFileURL(path.join(ENGINE, "MonoSyllabism", "map", "loader.js")).href
);
const { runSegmentor } = await import(
  url.pathToFileURL(
    path.join(ENGINE, "MonoSyllabism", "main", "phoneme", "lexer.js"),
  ).href
);
const { toSyllableLine } = await import(
  url.pathToFileURL(
    path.join(ENGINE, "MonoSyllabism", "utilities", "formatter.js"),
  ).href
);

const outFile = process.argv[2];
if (!outFile) {
  console.error("usage: node segment_bridge.mjs OUTPUT_FILE < input");
  process.exit(2);
}
const docs = input.split(SEP);
const out = [];
for (const doc of docs) {
  out.push(doc.trim() ? await segmentDocument(doc, engineNormalize, runSegmentor, toSyllableLine) : "");
}
fs.writeFileSync(outFile, out.join(SEP), "utf8");

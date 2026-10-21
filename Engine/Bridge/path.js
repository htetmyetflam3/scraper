// FILE: Engine/Bridge/path.js — scraper-side path shim for the Engine.
//
// The MonoSyllabism modules import "../../Bridge/path.js" (and the deeper
// ones "../../../Bridge/path.js"), both of which resolve to this directory
// when the Engine lives at <repo>/Engine/MonoSyllabism. On the Engine's own
// repository these constants point at a full workspace layout; here they are
// reduced to the two things the scraper actually uses — the syllable
// segmentor and the tree it walks — with every path anchored to the Engine
// directory itself so the caller's working directory never matters.
//
// The tree cache (.cache-tree) and log directory are build artifacts, kept
// out of git.
import path from "path";
import url from "url";

const HERE = path.dirname(url.fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, ".."); // <repo>/Engine
const TREE = path.join(ROOT, ".cache-tree");

// RUNTIME constants (segmentor, tree builder, logger): consumed directly as
// paths, so they are ABSOLUTE — anchored to the Engine directory.
export function TreeFile() { return TREE; }
export function DataFile() { return path.join(ROOT, "knowledge/isTreasure"); }
export function MasterFile() { return path.join(ROOT, "MonoSyllabism/map/master.json"); }
export function resolvePath(p) { return path.isAbsolute(p) ? p : path.join(ROOT, p); }
export function joinPath(...xs) { return path.join(...xs); }
export function LogFile() { return path.join(ROOT, ".cache-tree/log"); }
export function GetMasterFile() { return MasterFile(); }
export function OutputFile() { return path.join(ROOT, ".cache-tree/out"); }
export function InputFile() { return path.join(ROOT, ".cache-tree/in"); }

// BUILD constants (mapbuilder/obfuscator.js, mapbuilder/encrypt.json.js):
// those scripts document "the constants are root-relative; join glues them"
// and wrap every one in path.join(<their own root>, constant) — so these
// return RELATIVE paths. Absolute values here double the prefix
// (<root>/home/user/... was the bug npm run rebuild exposed).
export function JsonSourceFile() { return "knowledge/isTreasure/master.json"; }
export function MapMasterFile() { return "MonoSyllabism/map/master.json"; }
export function MapBinaryFile() { return "MonoSyllabism/map/payload.jsc"; }
export function MapLoaderFile() { return "MonoSyllabism/map/loader.js"; }
export function MapSecureFile() { return "MonoSyllabism/map/secure.js"; }
export function MapSourceDir() { return "MonoSyllabism/mapbuilder/base"; }

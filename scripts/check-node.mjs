#!/usr/bin/env node
// Build entry-point guard: never install, switch, or downgrade Node.
const MIN_MAJOR = 22;
const match = process.version.match(/^v?(\d+)/);
const major = match ? Number(match[1]) : NaN;
if (!Number.isInteger(major)) {
  console.error(`[node] cannot parse version ${JSON.stringify(process.version)} (${process.execPath})`);
  process.exit(1);
}
if (major < MIN_MAJOR) {
  console.error(`[node] Node.js ${MIN_MAJOR}+ required; found ${process.version} at ${process.execPath}.`);
  console.error("[node] Select an existing Node 22+ binary in PATH; this project will not install or replace Node.");
  process.exit(1);
}
console.log(`[node] using ${process.version} at ${process.execPath}`);

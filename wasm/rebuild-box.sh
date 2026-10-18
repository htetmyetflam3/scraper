#!/bin/sh
# Rebuild wasm/box/box.wasm in one command: payload first, then packaging.
#   npm run rebuild:wasm        (alias)
#   sh wasm/rebuild-box.sh
#
# The JS steps run as separate `node` child processes -- this script is meant
# to be run by itself (or spawned from Python), never imported into the
# Python pipeline. main.py does not call it.
#
# Needs, in order:
#   node + npm                the two JS build steps
#   Engine/knowledge/isTreasure/master.json
#                             restored by `npm run rebuild` (repo root); the
#                             payload inlines that knowledge file
#   wasm/box/node_modules     installed here on first run (package-lock.json)
#   wasm/box/engine.wasm      committed our quickjs-ng build; rebuild it
#                             separately with tools/build-engine.sh (needs the
#                             quickjs-ng sources + pypi ziglang)
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
BOX="$DIR/box"
KNOWLEDGE="$ROOT/Engine/knowledge/isTreasure/master.json"

command -v node >/dev/null 2>&1 || {
  echo "[wasm] Node.js 22+ is required; this script will not install or replace Node." >&2
  exit 1
}
node "$ROOT/scripts/check-node.mjs"

if [ ! -f "$KNOWLEDGE" ]; then
  echo "[wasm] restoring Engine/knowledge/isTreasure/master.json from master.json.enc"
  node "$ROOT/Engine/Bridge/restore-master.js"
fi

if [ ! -f "$BOX/engine.wasm" ]; then
  echo "[wasm] missing wasm/box/engine.wasm" >&2
  echo "[wasm] rebuild it with wasm/box/tools/build-engine.sh (quickjs-ng sources + ziglang)" >&2
  exit 1
fi

if [ ! -d "$BOX/node_modules" ]; then
  echo "[wasm] installing wasm/box dependencies (first run)"
  npm install --prefix "$BOX" --no-audit --no-fund
fi

echo "[wasm] 1/2 payload -> wasm/box/payload.bin"
(cd "$BOX" && node build-payload.mjs)

echo "[wasm] 2/2 package -> wasm/box/box.wasm"
(cd "$BOX" && node tools/pack-box.mjs)

echo "[wasm] done: $BOX/box.wasm"

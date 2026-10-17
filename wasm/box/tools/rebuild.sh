#!/bin/sh
# One-shot wasm box rebuild: engine -> payload -> pack -> tests.
#   sh wasm/box/tools/rebuild.sh
# Needs pypi ziglang + quickjs-ng sources (see tools/build-engine.sh).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
BOX="$(dirname "$DIR")"
sh "$DIR/build-engine.sh"
cd "$BOX"
node build-payload.mjs
node "$DIR/pack-box.mjs"
node test-loader.mjs
node test-payload.mjs
node test-box.mjs
python3 test-box-py.py
echo "wasm/box rebuild complete"

#!/bin/sh
# Build the box engine: quickjs-ng (v0.17.0) + c/shim.c, plain box ABI.
#   exports: alloc(len)->ptr  outPtr()->ptr  boot(lptr,llen,pptr,plen)->status
#            run(mode,ptr,len)->outLen   mode 0=normalize 1=mapperNormalize 2=myNormalize
# Proven toolchain (2026-10-17): pypi ziglang 0.17.0, quickjs-ng cloned via git
# (codeload tarballs 404 in this sandbox):
#   git clone --depth 1 --branch v0.17.0 https://github.com/quickjs-ng/quickjs /tmp/qjs-ng
# The four engine sources (quickjs/libregexp/libunicode/dtoa) + shim link clean
# to wasm32-wasi. Then: node tools/pack-box.mjs (embeds engine + loader + payload).
set -e
QJS_SRC="${QJS_SRC:-/tmp/qjs-ng}"
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$DIR/../engine.wasm}"
python3 -m ziglang cc -target wasm32-wasi -O2 \
  -I "$QJS_SRC" -DCONFIG_VERSION='"ng-0.17.0"' \
  -o "$OUT" \
  "$QJS_SRC/quickjs.c" "$QJS_SRC/libregexp.c" "$QJS_SRC/libunicode.c" "$QJS_SRC/dtoa.c" \
  "$DIR/../c/shim.c" -lm
echo "built $OUT"

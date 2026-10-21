# wasm/box — the black box

One `box.wasm` that evaluates the syllable normalizer with **no JS glue and no
host strings**: a QuickJS engine, the obfuscated loader, and the encrypted
payload, welded into a single module. A Python/wasmtime host is the proof case.

```
box.wasm = engine.wasm            (quickjs-ng v0.17.0 + c/shim.c, wasm32-wasi)
         + custom section linga.loader   (loader.mjs with aes.mjs inlined,
                                          javascript-obfuscator, NOT encrypted)
         + custom section linga.payload  (AES-256-GCM pack: normalize.js,
                                          sourceMap.js, _function.js,
                                          sourceMapper.js — knowledge baked in)
```

## Box ABI (the exports the host sees)

| export | meaning |
| --- | --- |
| `alloc(len) -> ptr` | reserve `len` bytes of linear memory (host writes into it) |
| `outPtr() -> ptr` | result buffer of the last `run` |
| `boot(lptr, llen, pptr, plen) -> status` | feed loader + payload bytes; decrypt-to-load in-engine (key assembled inside from the decoy-named pieces); keeps the three normalize fns. `0` = ok |
| `run(mode, ptr, len) -> outLen` | UTF-8 in/out; `mode` 0 = `normalize`, 1 = `mapperNormalize`, 2 = `myNormalize` |

On `boot`/`run` failure the status codes are `1`–`4`, `9`, `10` (boot) and `-2`
(run), and `outPtr()` holds the JS exception text + stack.

## Build

```sh
# engine (needs pypi ziglang + quickjs-ng v0.17.0 sources)
git clone --depth 1 --branch v0.17.0 https://github.com/quickjs-ng/quickjs /tmp/qjs-ng
sh wasm/box/tools/build-engine.sh          # -> wasm/box/engine.wasm

# pack (loader obfuscated by default; LINGA_BOX_RAW=1 to skip)
node wasm/box/tools/pack-box.mjs           # -> wasm/box/box.wasm
```

## Tests

```sh
node wasm/box/test-loader.mjs     # 5/5 — loader vs node:crypto, tamper
node wasm/box/test-payload.mjs    # 4/4 — pack vs Engine loader parity
node wasm/box/test-box.mjs        # 6/6 — sections -> decrypt-to-load -> parity
python3 wasm/box/test-box-py.py   #     — wasmtime host: boot + mapperNormalize
```

`test-box-py.py` pins `mapperNormalize('မြန်မာစာ') == 'U¢pU¼F¼'`
(codepoints `55 a2 70 55 bc 46 bc`) against the Engine loader.

## Contract & traps (learned by testing, all fixed)

- **Loader is obfuscated, NOT encrypted** — it is the bootstrap and must run
  raw. The payload is decrypt-TO-LOAD: the key never appears in the box; it is
  assembled at run time from the decoy-named load-time constants.
- **QuickJS has no web APIs.** The shim's boot entry polyfills
  `TextDecoder`/`TextEncoder` before the loader runs (the Node spike ran the
  loader on the host, which hides this).
- **`aes.mjs` must be inlined** at pack time so the loader section is
  self-contained (`bundleLoader` in `tools/pack-box.mjs`).
- **quickjs-ng v0.17 module loader pattern**: after
  `JS_Eval(..., MODULE|COMPILE_ONLY)`, take `JS_VALUE_GET_PTR` and
  `JS_FreeValue` the value (the module keeps its own reference) — as
  quickjs-ng's own `quickjs-libc.c` does.
- Watch string lengths in C: a one-byte source length overread turns into a
  baffling `TypeError: not a function` (the eval fails, the call lands on an
  exception value).
- `box.wasm`, `payload.bin` and the sandbox-rebuilt `payload.jsc`/`loader.js`
  are gitignored or never committed (V8 bytecode is Node-version-bound);
  `engine.wasm` is committed so `pack-box` works without the C toolchain.

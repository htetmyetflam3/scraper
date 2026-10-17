# wasm/box — the black box

One `box.wasm` containing the QuickJS engine and two custom sections:

```text
box.wasm = engine.wasm
         + linga.loader   (loader.mjs + inlined AES helper; obfuscated)
         + linga.payload  (AES-256-GCM encrypted pack of four obfuscated JS modules)
```

`build-payload.mjs` reads `function.js`, `normalize.js`, `sourceMap.js`, and `sourceMapper.js`, inlines the required `mapData`, obfuscates each module with `javascript-obfuscator`, then encrypts the pack. The payload is still JavaScript source, not QuickJS bytecode. Obfuscation raises the effort needed to understand the code, but it is not cryptographic source protection; the loader and decryption key material ship in the same box.

## Box ABI

| export | meaning |
| --- | --- |
| `alloc(len) -> ptr` | reserve linear memory for host-provided bytes |
| `outPtr() -> ptr` | pointer to the result or error buffer |
| `boot(loaderPtr, loaderLen, payloadPtr, payloadLen) -> status` | initialize QuickJS, decrypt the payload in memory, and load the three normalizer functions |
| `run(mode, ptr, len) -> outLen` | invoke a normalizer with UTF-8 input; mode 0 = `normalize`, 1 = `mapperNormalize`, 2 = `myNormalize` |

On successful boot, `boot` returns 0. On `run`, the host reads `outLen` bytes at `outPtr()`. Error codes and messages are described in `c/shim.c` and `loader.py`.

## Build

```sh
npm run rebuild:wasm
```

This runs the payload obfuscation/encryption and packages `engine.wasm`, the loader, and payload. Rebuilding the QuickJS engine itself requires quickjs-ng and Zig; see `tools/build-engine.sh`.

## Security boundary

Anyone who obtains `box.wasm` can extract its custom sections and has the loader required to decrypt the payload. The payload modules are obfuscated JavaScript, so their original names and structure are obscured, but their behavior can be analyzed and the logic may be deobfuscated. Do not treat this box as a place to keep secrets from its users.

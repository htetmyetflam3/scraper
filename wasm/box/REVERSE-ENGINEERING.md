# The box, opened — what `box.wasm` actually protects

Date: 2026-10-17. Method: `node wasm/box/tools/re-box.mjs --run` against the
shipped `wasm/box/box.wasm` (5,128,492 B). Nothing below is quoted from the
build scripts; every value was read back out of the binary.

## Container layout

`box.wasm` is a normal wasm module (quickjs-ng + `c/shim.c`) with two custom
sections appended:

| section | bytes | sha256 (16) | what it is |
|---|---|---|---|
| `linga.loader` | 11,068 | `2336bb0ba3bc0a27` | the obfuscated bootstrap module (`loader.mjs` + inlined `aes.mjs`) |
| `linga.payload` | 19,025 | `a99f2171d40006e3` | `[12-byte IV][ciphertext][16-byte GCM tag]` |

## Step 1 — the loader exports its own key

The loader section is ESM. Importing it is enough to get the key function:

```
loader exports  aesGcmDecrypt, aesGcmEncrypt, assembleKey, bootModules, decryptToLoad, run
assembleKey()   d8e7bd0586f6de4aed45ee4cb8d9438dc786140d1e75ffa19a1538f0fc52d123  (32 bytes)
```

The same 32 bytes come out of `wasm/box/loader.mjs` in the repo. The
obfuscation (119 distinct `_0x…` identifiers, string-array indirection) hides
the spelling, not the value — and the deobfuscator is not needed, because the
module is imported and asked.

## Step 2 — the payload decrypts by hand

Layout per `build-payload.mjs`: IV(12) ‖ ciphertext ‖ tag(16), AES-256-GCM,
JSON in, JSON out.

```
plaintext       17837 chars, 4 modules
  _function.js          4832 chars
  normalize.js          6070 chars
  sourceMap.js          3099 chars
  sourceMapper.js       2079 chars
```

`_function.js`, `normalize.js` and `sourceMapper.js` are **byte-identical** to
`Engine/MonoSyllabism/mapbuilder/base/{function,normalize,sourceMapper}.js` —
the thing the box was built to hide is the repo's own readable source.
`sourceMap.js` differs only where the build inlined the knowledge: it ends
`const IMPOSTER = mapData.IMPOSTER;` instead of a JSON import.

## Step 3 — the recovered sources run

`--run` writes the four modules to a temp dir, rewrites the one `Bridge/logen.js`
import to a stub, and executes `normalize.js`:

```
recovered run   mapperNormalize("မြန်မာစာ") = "U¢pU¼F¼"
Engine loader   "U¢pU¼F¼"
same output     true
```

## Design vs reality — the payload is source, not bytecode

`wasm/box/PLAN.md` states the contract as: *"The key is decrypt-TO-LOAD …
**Not** decrypt-to-readable: even a holder of the key gets bytecode, never
source."* What actually ships does not meet that:

```
first 40 chars of the recovered normalize.js: "import { debugLogInit } from \"../../../B"
looks like SOURCE (not V8 bytecode): true
contains raw function bodies:            true
```

`build-payload.mjs` packs the `mapbuilder/base/*.js` **text**, encrypts the
JSON, and the QuickJS engine evaluates it at boot. There is no compile step
and no V8 bytecode anywhere in `box.wasm` (unlike the Engine's
`payload.jsc`, which really is V8 bytecode). So the box currently protects
source text with a key that the box itself hands out.

## Conclusion

The box is **obfuscation, not protection**:

1. the key is inside the artifact and a public function returns it;
2. the ciphertext is a documented AES-GCM container;
3. the plaintext is the original module sources, unchanged;
4. the decryptor (`loader.mjs`, `aes.mjs`) is committed in the same repo.

Anyone who can read `box.wasm` can recover `normalize.js` in under a second.
The honest claim the box supports is **"not readable by opening it in an
editor"**, not "the logic is secret".

If real secrecy is ever wanted, the key must not ship with the artifact —
otherwise step 1 above is a one-liner. That is an owner decision; this note
only records what the shipped binary permits today.

## Reproduce

```sh
node wasm/box/tools/re-box.mjs            # sizes, hashes, key
node wasm/box/tools/re-box.mjs --dump /tmp/box-out
node wasm/box/tools/re-box.mjs --run      # recover + execute + compare
```

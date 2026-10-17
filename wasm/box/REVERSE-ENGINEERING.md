# wasm/box — current payload inspection notes

The box bundles a QuickJS engine plus two wasm custom sections:

- `linga.loader`: the loader with the AES helper inlined, obfuscated but executable.
- `linga.payload`: an AES-256-GCM encrypted JSON module pack.

The payload build now obfuscates every module before encryption. It preserves the four module filenames and their public exports so the QuickJS module loader can connect them. The current pack contains `function.js`, `normalize.js`, `sourceMap.js`, and `sourceMapper.js`; `mapData` is inlined into the source map module.

## What obfuscation changes

`build-payload.mjs` runs each module through `javascript-obfuscator` with compact output, hexadecimal local identifiers, and a JavaScript string-array decoder. The string-array encoding deliberately does not use Base64/`atob`, which is unavailable in the embedded QuickJS environment.

This makes the decrypted payload obfuscated JavaScript rather than readable original source. It is still JavaScript source, not QuickJS bytecode. Obfuscation is not cryptographic source protection: someone with the box can obtain the loader and payload, execute the loader to decrypt the pack, then analyze or deobfuscate the resulting modules. The AES layer does not keep the contents secret from a box holder because the loader and key material are distributed with the payload.

## Verify the build

```sh
npm run rebuild:wasm
node wasm/box/tools/re-box.mjs --dump /tmp/box-obfuscated
node wasm/box/tools/re-box.mjs --run
```

The dump should contain compact, mangled module code rather than the original readable module text. `--run` checks that the recovered pack still produces the same normalization output as the Engine loader. The archived tests in `test.zip` include payload/box round trips and a Python/wasmtime host test.

## Remaining limitation

If the requirement is to make the original implementation substantially harder to recover, obfuscation is only a deterrent. The design document's stronger goal—compile the obfuscated modules to QuickJS bytecode before encryption—is not implemented by this source-obfuscation step.

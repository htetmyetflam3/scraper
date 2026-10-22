# wasm/box — the blackbox package (owner design, approved 2026-10-17)

One distributable: `box.wasm` =

- [compiled] QuickJS engine
- [data] obfuscated `loader.js` — key assembled at runtime from split/encoded strings; loader is obfuscated but **NOT encrypted** (bootstrap must run raw)
- [data] `payload = encrypt( compile( obfuscate( base/ bundle ) ) )` — engine bytecode

## Contract (owner's words)

- The key is **decrypt-TO-LOAD** (decrypt in memory → hand to engine → run).
  **Not** decrypt-to-readable: even a holder of the key gets bytecode, never source.
- Knowledge/tables = commodity (baked wherever convenient, never a secret).
- Walker/segmentor/`Parser` cheap parts ship as readable code.
- The ONLY secret = the `mapbuilder/base/` entry logic (normalize / mapperNormalize /
  myNormalize + helpers) — the part that answers both before-stage (text collect)
  and after-stage (lexer/phonology/disassembler/formatter) questions.
- Near-miss decoy names (`mapperrnormalize` ≈ `mapperNormalize`) for key variables
  in the loader — static-reading camouflage.
- Consumers: `Praser/` and `Engine/` ship individually with this box; the python
  scraper hosts the same box.

## Build order

1. spike: engine boots, in-engine decode-to-load + eval loop (Node host)  ← now
2. in-box loader: pure-JS AES-256-GCM, key split across encoded strings
3. payload: obfuscate(base/) → QuickJS bytecode → encrypt
4. package single `box.wasm` (engine + loader + payload in one binary)
5. parity: box == `Engine/MonoSyllabism/map/loader.js` on the test vector corpus

## Toolchain probe (for step 4)

(filled in by build — see git history of this file)

## Supersedes

The AssemblyScript `wasm/normalize/` port (its `tools/gen-data.mjs` stays as a
fallback baking path).

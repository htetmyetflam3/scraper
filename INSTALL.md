# Install & verify — Burmese PDF scraper pipeline

What this system does, in one line per stage:

```
download  ->  validate / unlock / repair / linearize   (python, pikepdf)
          ->  classify the final readable bytes        (crawler/bin/pdf_classify.py)
                convertable/  Unicode or Zawgyi-One text books
                scanned/      image-only books (NO OCR)
                old/          pre-standard fonts / undecodable text (NO recovery)
          ->  convertable text journaled to scan/collected.jsonl during the run
run end   ->  ONE node subprocess: Engine MonoSyllabism segmentor + formatter
          ->  ONE cumulative Parquet, pdfs/parquet/books.parquet (zstd level 19;
              book-collection schema: author | book | text | segmented | sha256)
          ->  staged PDFs released .convert/ -> convertable/
upload    ->  category folders + parquet, verify sha256, then delete local
```

## Requirements

| what | why |
| --- | --- |
| Python 3.11+ | the whole pipeline |
| [uv](https://docs.astral.sh/uv/) (or plain `pip`) | dependency install, `uv run main.py …` |
| Node.js 22+ | the run-end segmentor bridge (`crawler/bin/segment_bridge.mjs`) and the continuous-OCR worker; `scripts/check-node.mjs` enforces 22 and will not install or switch Node |
| npm | one-time `npm install` in `Engine/MonoSyllabism/mapbuilder` (bytenode, for the Engine's encrypted syllable-map loader) |

Python packages (installed by the commands below, from `pyproject.toml`):
httpx, pikepdf, pyarrow, pypdf, cryptography, beautifulsoup4, tenacity,
selenium, requests, trio, trio-websocket, websocket-client.

## Install

From the repository root, either run the installer:

```sh
./install.sh
```

or do the same two steps by hand:

```sh
uv sync      # no uv? the project is not a package (tool.uv package=false) —
             # install the dependency list instead: python3 -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" | xargs pip3 install
cd Engine/MonoSyllabism/mapbuilder && npm install && cd -
```

Nothing else: the syllable tree ships PRECOMPUTED in `Engine/.cache-tree/`
(committed build output — the builder that produced it is not shipped), so
the first flush only reads it; `scan/` and the `pdfs/` category folders are
created on demand.

## Rebuild the Engine binary — run this SEPARATELY, before the uv commands

The segmentor's map binary (`Engine/MonoSyllabism/map/payload.jsc`) is V8
bytecode — **tied to the exact Node version that compiled it**. On a new
machine (or after a Node upgrade) rebuild it first:

```sh
npm run rebuild
```

One command, three steps (all inside this repo, nothing downloaded but the
two build deps): installs mapbuilder's deps (`--no-package-lock`, the repo
convention), restores the readable knowledge JSON from the shipped
encrypted container (`Engine/Bridge/restore-master.js` — the shipped
`map/secure.js` reader carries its own key, no secrets file needed), then
runs the Engine's own builder (`mapbuilder/obfuscator.js`): bundle →
obfuscate → compile to V8 bytecode with THIS machine's Node → encrypt →
write `map/payload.jsc` + its matching `map/loader.js`. The build verifies
itself ("loader imported, 35 binding(s) live").

## Rebuild the wasm box — also SEPARATELY

`wasm/box/box.wasm` is the distributable blackbox (engine + obfuscated
loader + encrypted payload). One command rebuilds its payload and packaging:

```sh
npm run rebuild:wasm        # = sh wasm/rebuild-box.sh
```

It runs the two JS build steps as separate `node` child processes —
`wasm/box/build-payload.mjs` then `wasm/box/tools/pack-box.mjs` — and writes
`wasm/box/payload.bin` + `wasm/box/box.wasm` (both gitignored). Run it
yourself; `main.py` never calls it. It needs `Engine/knowledge/isTreasure/
master.json` (created by `npm run rebuild` above — since 2026-10-17 that is
the `mapData`-only subset, regenerated from the sealed `master.json.enc`;
no readable knowledge JSON is tracked), the committed
`wasm/box/engine.wasm`, and `wasm/box/node_modules` (installed on first run
from `package-lock.json`). Rebuilding the engine itself is separate:
`wasm/box/tools/build-engine.sh` (quickjs-ng sources + pypi ziglang).

Prove the result from Python: `python3 wasm/box/test-box-py.py` (needs
`wasmtime`) — it boots the box and compares `mapperNormalize` with the
Engine loader.

## Verify — the fake-download test

```sh
uv run python/tests/fake_download.py      # or: python3 python/tests/fake_download.py
```

It fakes the downloader's save call with the repo's own books and asserts
every one lands in its defined directory:

| book | what it really is | must land in |
| --- | --- | --- |
| `sample.pdf` | Zawgyi-One text book | `.convert/` → `convertable/` after flush |
| `sample-scan.pdf` | **"scan" in name only** — real Unicode text layer | `.convert/` → `convertable/` after flush |
| `scan-book.pdf` | **built by the test**: true image-only pages, zero text operators | `scanned/` |
| `bg-mlc-1-1.pdf` | Win Innwa old-font book | `old/` |

then runs the real run-end flush (node segmentor → Parquet) and checks the
batch — the book-collection schema `author | book | text | segmented | sha256`
(author left empty on purpose: PDF metadata authorship is untrusted, the
owner extracts authors from book content later; book = filename without
.pdf; segmented = formatter output, no √ marks) — and that staged PDFs are
released only after the commit. 15 checks; exit code 0 means all passed.

The three real books are recovered from this repository's git history
(`origin/working`, `origin/site` — the test fetches them itself). In a
checkout without that history, put the three PDFs into
`python/tests/fixtures/` by hand and the test runs the same.

Everything the test writes stays under `python/tests/fixtures/`,
`python/tests/.out/` (both gitignored) and the `scan/` journal it retires
itself; it refuses to run if a real journal with pending documents exists.

## Run

```sh
uv run main.py --process            # download + upload from scan/entry.txt
uv run main.py --process <site-url> # same, one site, no entry.txt
uv run main.py --upload             # send what is saved, verify, delete, exit
```

## Run unattended — survives SSH disconnect (systemd installer)

The repo ships its own systemd installer (`deploy/install_service.py`,
details in `deploy/README.md`). On the VPS, as root, from this checkout:

```sh
uv run deploy/install_service.py -- --process -l 2 -i 60
systemctl start scraper.service        # now you can close SSH
journalctl -u scraper.service -f       # watch the live log any time
systemctl stop scraper.service         # clean stop (SIGINT, same as Ctrl+C)
```

Everything after `--` is your normal `main.py` arguments. A stop or crash
mid-run loses nothing: the collected-text journal and the staged
`.convert/` PDFs wait, and the next run (or `--upload`) flushes them.

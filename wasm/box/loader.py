#!/usr/bin/env python3
"""Host loader for wasm/box/box.wasm (Python + wasmtime).

Extracts the `linga.loader` and `linga.payload` custom sections from `box.wasm`,
boots the QuickJS engine in-memory (`alloc` / `boot` / `run` / `outPtr`), and
exposes the three normalizer modes:
  - mode 0: normalize(text)
  - mode 1: mapper_normalize(text)
  - mode 2: my_normalize(text)
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path
from wasmtime import Engine, Linker, Module, Store, WasiConfig

BOX_PATH = Path(__file__).resolve().parent / "box.wasm"


def _uleb128(buf: bytes, index: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, index
        shift += 7


def extract_sections(wasm_bytes: bytes) -> dict[str, bytes]:
    if wasm_bytes[:4] != b"\x00asm":
        raise ValueError("not a valid wasm module")
    index = 8
    out: dict[str, bytes] = {}
    while index < len(wasm_bytes):
        section_id = wasm_bytes[index]
        index += 1
        size, index = _uleb128(wasm_bytes, index)
        body = wasm_bytes[index : index + size]
        index += size
        if section_id == 0:
            name_len, offset = _uleb128(body, 0)
            name = body[offset : offset + name_len].decode("utf-8")
            out[name] = body[offset + name_len :]
    return out


class LingaBox:
    """In-memory QuickJS wasm box instance."""

    def __init__(self, box_path: Path = BOX_PATH) -> None:
        if not box_path.is_file():
            raise FileNotFoundError(f"missing {box_path} (run `npm run rebuild:wasm`)")
        raw = box_path.read_bytes()
        sections = extract_sections(raw)
        if "linga.loader" not in sections or "linga.payload" not in sections:
            raise ValueError(f"{box_path} is missing linga.loader or linga.payload custom section")

        engine = Engine()
        module = Module(engine, raw)
        linker = Linker(engine)
        linker.define_wasi()
        store = Store(engine)
        store.set_wasi(WasiConfig())
        instance = linker.instantiate(store, module)
        exports = instance.exports(store)

        self._store = store
        self._mem = exports["memory"]
        self._alloc = exports["alloc"]
        self._boot = exports["boot"]
        self._run = exports["run"]
        self._out_ptr = exports["outPtr"]

        loader_bytes = sections["linga.loader"]
        payload_bytes = sections["linga.payload"]
        lptr = self._write(loader_bytes)
        pptr = self._write(payload_bytes)
        status = self._boot(store, lptr, len(loader_bytes), pptr, len(payload_bytes))
        if status != 0:
            raise RuntimeError(f"box boot failed (status {status}): {self._read_error()}")

    def _write(self, data: bytes) -> int:
        ptr = self._alloc(self._store, len(data))
        if not ptr:
            raise MemoryError(f"box alloc({len(data)}) returned NULL")
        self._mem.write(self._store, data, ptr)
        return ptr

    def _read_error(self) -> str:
        op = self._out_ptr(self._store)
        if not op:
            return "no exception detail"
        raw = bytes(self._mem.read(self._store, op, op + 1200)).split(b"\x00", 1)[0]
        return raw.decode("utf-8", errors="replace")

    def call(self, mode: int, text: str) -> str:
        if mode not in (0, 1, 2):
            raise ValueError(f"invalid mode {mode}; expected 0, 1, or 2")
        if not text:
            return ""
        nfc = unicodedata.normalize("NFC", text).encode("utf-8")
        ptr = self._write(nfc)
        out_len = self._run(self._store, mode, ptr, len(nfc))
        if out_len < 0:
            raise RuntimeError(f"box run(mode={mode}) failed ({out_len}): {self._read_error()}")
        op = self._out_ptr(self._store)
        return bytes(self._mem.read(self._store, op, op + out_len)).decode("utf-8")

    def normalize(self, text: str) -> str:
        return self.call(0, text)

    def mapper_normalize(self, text: str) -> str:
        return self.call(1, text)

    def my_normalize(self, text: str) -> str:
        return self.call(2, text)


_DEFAULT_BOX: LingaBox | None = None


def _box() -> LingaBox:
    global _DEFAULT_BOX
    if _DEFAULT_BOX is None:
        _DEFAULT_BOX = LingaBox()
    return _DEFAULT_BOX


def normalize(text: str) -> str:
    return _box().normalize(text)


def mapper_normalize(text: str) -> str:
    return _box().mapper_normalize(text)


def my_normalize(text: str) -> str:
    return _box().my_normalize(text)


if __name__ == "__main__":
    sample = sys.argv[1] if len(sys.argv) > 1 else "မြန်မာစာ"
    result = mapper_normalize(sample)
    if len(sys.argv) == 1:
        expected = "U\u00a2pU\u00bcF\u00bc"
        if result != expected:
            raise SystemExit(f"mismatch: {result!r} != {expected!r}")
    print(result)

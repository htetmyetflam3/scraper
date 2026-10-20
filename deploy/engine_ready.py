#!/usr/bin/env python3
"""Pre-start check: the run-end JS engine must run on THIS machine's Node.

`scraper.service` runs this as ExecStartPre, so one `systemctl start` is the
whole automated process -- nobody has to run `npm run rebuild` by hand on
the VPS. It also works standalone:

    uv run deploy/engine_ready.py          # check only
    uv run deploy/engine_ready.py --fix    # rebuild the Engine bytecode if needed

What it checks, in order:
  1. `node` is on PATH and is version 22 or newer (the bridge and Engine build require it).
  2. `Engine/MonoSyllabism/map/payload.jsc` + `loader.js` exist.
  3. `Engine/.cache-tree/` holds the precomputed syllable tree.
  4. The real bridge boots end to end with empty input -- this loads the
     encrypted payload through the loader, the tree and the formatter, so a
     V8-bytecode/Node mismatch (the usual failure on a new VPS) is caught
     before main.py starts.

`--fix` runs the repo's `npm run rebuild` once when step 2/4 fails and retries;
`npm run rebuild` is what recompiles payload.jsc for this Node (V8 bytecode is
tied to the exact Node that compiled it).

Exit codes: 0 ready, 1 not ready.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "Engine"
PAYLOAD = ENGINE / "MonoSyllabism" / "map" / "payload.jsc"
LOADER = ENGINE / "MonoSyllabism" / "map" / "loader.js"
TREE = ENGINE / ".cache-tree"
BOX_WASM = ROOT / "wasm" / "box" / "box.wasm"
BRIDGE = ROOT / "crawler" / "bin" / "segment_bridge.mjs"
MIN_NODE_MAJOR = 22
BOOT_TIMEOUT = 300
REBUILD_TIMEOUT = 1800


def say(message: str) -> None:
    print(f"[engine] {message}", flush=True)


def find_node() -> str | None:
    candidates: list[Path] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            candidates.append(Path(entry) / "node")
    for root in (Path.home(), Path("/root")):
        candidates.extend(sorted(root.glob(".nvm/versions/node/v*/bin/node"), reverse=True))
        candidates.extend(sorted(root.glob(".local/share/fnm/node-versions/v*/installation/bin/node"), reverse=True))
        candidates.append(root / ".volta/bin/node")
    candidates.extend((Path("/usr/local/bin/node"), Path("/usr/bin/node")))

    seen: set[str] = set()
    older: tuple[str, str] | None = None
    for cand in candidates:
        try:
            resolved = str(cand.resolve())
        except OSError:
            continue
        if resolved in seen or not cand.is_file() or not os.access(cand, os.X_OK):
            continue
        seen.add(resolved)
        result = subprocess.run([str(cand), "--version"], capture_output=True, text=True)
        raw = (result.stdout or result.stderr or "").strip().lstrip("v")
        try:
            major = int(raw.split(".")[0])
        except ValueError:
            continue
        if major >= MIN_NODE_MAJOR:
            node_dir = str(cand.resolve().parent)
            os.environ["PATH"] = f"{node_dir}{os.pathsep}{os.environ.get('PATH', '')}"
            say(f"node {raw} at {cand}")
            return str(cand)
        if older is None:
            older = (raw, str(cand))

    if older is not None:
        say(f"node {older[0]} at {older[1]} is too old; {MIN_NODE_MAJOR}+ is required")
        return None
    say("node is not on PATH; the run-end segmentor cannot start")
    return None


def boot_bridge(node: str) -> tuple[bool, str]:
    """Run the real bridge with empty input: the full loader+tree+formatter path."""
    with tempfile.TemporaryDirectory(prefix="engine-ready-") as tmp:
        out = Path(tmp) / "out.txt"
        try:
            result = subprocess.run(
                [node, str(BRIDGE), str(out)],
                input=b"",
                capture_output=True,
                timeout=BOOT_TIMEOUT,
                cwd=ROOT,
            )
        except subprocess.TimeoutExpired:
            return False, f"bridge did not finish within {BOOT_TIMEOUT}s"
        except OSError as error:
            return False, f"cannot run {BRIDGE}: {error}"
        if result.returncode != 0:
            lines = [
                line
                for line in (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
                if line.strip() and not line.startswith("Node.js v")
            ]
            return False, " | ".join(lines[-3:]) if lines else f"bridge exited {result.returncode}"
        return True, ""


def rebuild(script: str = "rebuild") -> bool:
    npm = shutil.which("npm")
    if npm is None:
        say(f"npm is not on PATH; cannot run npm run {script}")
        return False
    say(f"running npm run {script}")
    try:
        result = subprocess.run(
            [npm, "run", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=REBUILD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        say(f"npm run {script} did not finish within {REBUILD_TIMEOUT}s")
        return False
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        say(f"npm run {script} failed: {tail[-1] if tail else result.returncode}")
        return False
    return True


def main(argv: list[str]) -> int:
    fix = "--fix" in argv
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0

    node = find_node()
    if node is None:
        return 1

    missing = [str(path) for path in (PAYLOAD, LOADER) if not path.is_file()]
    if missing:
        say(f"missing Engine artifact(s): {', '.join(missing)}")
        if not (fix and rebuild("rebuild")):
            return 1
        say("Engine artifacts rebuilt")

    if not BOX_WASM.is_file():
        say(f"missing wasm box artifact: {BOX_WASM}")
        if not (fix and rebuild("rebuild:wasm")):
            return 1
        say("wasm box artifact rebuilt")

    if not TREE.is_dir() or not any(TREE.iterdir()):
        say(
            f"missing the precomputed syllable tree: {TREE}\n"
            "[engine] it ships with the repo -- restore it with "
            "`git checkout -- Engine/.cache-tree`"
        )
        return 1
    say(f"tree {TREE.name}/ present ({len(list(TREE.iterdir()))} entries)")

    ok, detail = boot_bridge(node)
    if not ok:
        say(f"bridge boot failed: {detail}")
        if fix:
            if not rebuild("rebuild"):
                return 1
            ok, detail = boot_bridge(node)
            if not ok:
                say(f"bridge still fails after the rebuild: {detail}")
                return 1
        else:
            say("run `npm run rebuild` (or re-run with --fix), then try again")
            return 1
    say("JS engine boots: loader + payload + tree + formatter OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

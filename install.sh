#!/usr/bin/env bash
# System installer for the Burmese PDF scraper pipeline (see INSTALL.md).
# Installs the Python dependencies and the Engine's one npm requirement,
# then prints how to verify. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

echo "== node preflight =="
if ! command -v node >/dev/null 2>&1; then
    echo "ERROR: Node.js 22+ is required; this installer will not install or replace Node." >&2
    exit 1
fi
node scripts/check-node.mjs

echo "== python dependencies =="
if command -v uv >/dev/null 2>&1; then
    uv sync
else
    echo "(uv not found; using pip3 — install uv for the normal 'uv run main.py' workflow)"
    # this project is not a package (tool.uv package = false): install the
    # dependency list from pyproject.toml, exactly what 'uv sync' installs
    deps_list() {
        python3 -c "import tomllib; print('\n'.join(
            tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))"
    }
    deps_list | xargs pip3 install || {
        echo "(externally-managed python: retrying with --break-system-packages)"
        deps_list | xargs pip3 install --break-system-packages
    }
fi

echo
echo "== node (segmentor bridge) =="
node --version

echo
echo "== Engine syllable-map bytecode & wasm box =="
npm run rebuild
python3 deploy/engine_ready.py

echo
echo "Unattended run that survives SSH disconnect (VPS, as root):"
echo "  python3 deploy/install_service.py -- --process -l 2 -i 60"
echo "  systemctl start scraper.service   (see deploy/README.md)"

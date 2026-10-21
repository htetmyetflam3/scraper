#!/usr/bin/env bash
# Upload Parquet to Hugging Face with the HG_TOKEN access token.
#
#   on the VPS         ./hf-upload.sh                  (the hardcoded file below)
#   on the local box   ./hf-upload.sh parquet          (cwd-relative file or folder)
#                      ./hf-upload.sh parquet/books.parquet
#
# The repo is created on the first run if it does not exist, private by
# default, and its name defaults to the Parquet name:
#   pdfs/parquet/books.parquet  ->  datasets/htetmyetflam3/books
#
# Downloading Parquet from the VPS is the other case, the other script:
#   ./vps-pull.sh
#
# The Hub needs a token here, not an SSH key: it authenticates the upload and
# creates the repo when it is missing. ssh/hf is not read by this script.
# Large Parquet needs no git-lfs on this box - `hf` streams it and resumes an
# interrupted upload when you run the same command again.
#
# Needs:  huggingface_hub on PATH (the `hf` command), else `uv run hf` is used.
#         Install with:  pip3 install -U huggingface_hub   (or: uv sync)
# Needs:  HG_TOKEN - your Hugging Face access token, the one named "hg_full"
#         (huggingface.co -> Settings -> Access Tokens, write scope).
#         Export it, or put HG_TOKEN=... in the repo-root .env.
#
# Nothing is deleted: not the Parquet here, not the copy on the VPS.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"   # the repository root

# ── Defaults: the VPS case. Edit these lines. ──
DEFAULT_FILE="pdfs/parquet/books.parquet"     # what text_collect.py appends rows to
HF_USER="htetmyetflam3"                       # your Hugging Face username (same as git;
                                              # account email htetmyet986@gmail.com)
DEFAULT_TYPE="dataset"                        # dataset or model
PRIVATE="--private"                           # "" for a public repo

# ── What to upload: argument (cwd-relative) or the hardcoded default ──
if [ "$#" -ge 1 ]; then
    TARGET="$1"
else
    TARGET="$SCRIPT_DIR/$DEFAULT_FILE"
    echo "[INFO] no path given: using the hardcoded $DEFAULT_FILE"
fi

# The repo name defaults to the Parquet name: books.parquet -> htetmyetflam3/books
REPO="${HF_REPO:-$HF_USER/$(basename "${TARGET%.parquet}")}"
TYPE="${HF_REPO_TYPE:-$DEFAULT_TYPE}"

# ── HG_TOKEN: environment wins over the repo-root .env ──
if [ -z "${HG_TOKEN:-}" ] && [ -f "$SCRIPT_DIR/.env" ]; then
    HG_TOKEN=$(grep -E '^[[:space:]]*(export[[:space:]]+)?HG_TOKEN=' "$SCRIPT_DIR/.env" |
        tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' | tr -d "\"'" |
        sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
fi

if [ -z "${HG_TOKEN:-}" ]; then
    echo "[ERROR] HG_TOKEN is not set."
    echo "        Export it, or add HG_TOKEN=hf_xxx to $SCRIPT_DIR/.env"
    echo "        It is the access token named \"hg_full\" on huggingface.co"
    echo "        (Settings -> Access Tokens, write scope)."
    exit 1
fi

# ── Find the Hub CLI ──
HF=""
if command -v hf >/dev/null 2>&1; then
    HF="hf"
elif command -v huggingface-cli >/dev/null 2>&1; then
    HF="huggingface-cli"
elif command -v uv >/dev/null 2>&1; then
    HF="uv run hf"
fi

if [ -z "$HF" ]; then
    echo "[ERROR] no Hugging Face CLI found (tried 'hf', 'huggingface-cli', 'uv run hf')."
    echo "        Install it:  pip3 install -U huggingface_hub"
    exit 1
fi

if [ ! -e "$TARGET" ]; then
    echo "[ERROR] nothing to upload: $TARGET does not exist"
    exit 1
fi

echo "[INFO] uploading  : $TARGET"
echo "[INFO] to repo    : ${TYPE}s/$REPO $([ -n "$PRIVATE" ] && echo '(private)' || echo '(public)')"
echo "[INFO] via        : $HF upload"

# HF_TOKEN, not --token: keeps the secret out of `ps` output.
if ! HF_TOKEN="$HG_TOKEN" $HF upload "$REPO" "$TARGET" \
    --repo-type "$TYPE" $PRIVATE \
    --commit-message "Update parquet from $(hostname)"; then
    echo ""
    echo "[ERROR] the upload failed - see the Hub's message above."
    echo "        Check that HG_TOKEN has the write scope, and that"
    echo "        $REPO is your repo (edit HF_USER in $0,"
    echo "        or export HF_REPO=username/name)."
    exit 1
fi

echo "[OK] uploaded: https://huggingface.co/${TYPE}s/$REPO/tree/main"
echo "[OK] nothing was deleted - the Parquet is still on this box."

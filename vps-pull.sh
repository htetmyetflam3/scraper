#!/usr/bin/env bash
# Download the Parquet collection from the VPS onto this box (local container).
#
#   ./vps-pull.sh                        # books.parquet      -> ./parquet/
#   ./vps-pull.sh --all                  # every remote *.parquet
#   ./vps-pull.sh --dest downloads       # somewhere else (cwd-relative)
#   ./vps-pull.sh batch-2026-09-01.parquet
#
# This is the download half only - it never talks to Hugging Face. Pushing what
# lands here to the Hub is the other script:
#   ./hf-upload.sh parquet
#
# Plain ssh in BatchMode: it never prompts, so a wrong key fails fast instead of
# hanging. Nothing extra is needed on the VPS (no rsync) - only ls, cat, tail,
# stat and sha256sum.
#
# The ssh Host alias carries HostName, Port and User; the private key is
# hardcoded below and pinned with IdentitiesOnly, so this script only ever
# offers the VPS key - never a general key used for other hosts.
#
# Verified: the local sha256 must equal the VPS sha256 before the dot-temp is
# renamed into place. Nothing is deleted - not the VPS copy, not a local file
# already on disk. The only thing removed is this script's own failed dot-temp.
#
# NOTE: VPS_* names on purpose. The repo-root .env SSH_* settings point at the
# hostinger shared host (`hs`), which is a different box from the VPS.
set -euo pipefail

# ── Defaults: the VPS case. Edit these lines. ──
DEFAULT_HOST="hostingervps"                  # ssh alias in this box's ~/.ssh/config
DEFAULT_KEY="${HOME}/.ssh/mylinux"           # the VPS private key, hardcoded
DEFAULT_REMOTE_DIR="scraper/pdfs/parquet"    # relative to the VPS account home
DEFAULT_REMOTE_FILE="books.parquet"          # what text_collect.py appends rows to
DEFAULT_DEST="parquet"                       # cwd-relative

HOST="${VPS_SSH_HOST:-$DEFAULT_HOST}"
REMOTE_DIR="${VPS_REMOTE_DIR:-$DEFAULT_REMOTE_DIR}"
REMOTE_FILE="${VPS_REMOTE_FILE:-$DEFAULT_REMOTE_FILE}"
DEST="${VPS_LOCAL_DIR:-$DEFAULT_DEST}"
ALL=0
NAMES=()

while [ "$#" -gt 0 ]; do
    case "$1" in
        --all) ALL=1 ;;
        --dest) DEST="${2:?--dest needs a directory}"; shift ;;
        --host) HOST="${2:?--host needs an ssh alias}"; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) NAMES+=("$1") ;;
    esac
    shift
done

# ── Can we see the VPS and its Parquet dir? ──
if ! command -v ssh >/dev/null 2>&1; then
    echo "[ERROR] no ssh client on PATH; install openssh-client"
    exit 1
fi

if [ ! -f "$DEFAULT_KEY" ]; then
    echo "[ERROR] SSH private key not found: $DEFAULT_KEY"
    echo "        Copy your VPS key there, or edit DEFAULT_KEY in $0."
    exit 1
fi

# IdentitiesOnly: offer this one key and nothing else, so a general key from
# the agent or ~/.ssh/id_* is never sent to the VPS.
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30
    -i "$DEFAULT_KEY" -o IdentitiesOnly=yes)

if ! "${SSH[@]}" "$HOST" "test -d '$REMOTE_DIR'"; then
    echo "[ERROR] cannot see '$REMOTE_DIR' on '$HOST'."
    echo "        Check the alias in ~/.ssh/config (HostName, Port, User),"
    echo "        the hardcoded key at $DEFAULT_KEY,"
    echo "        or the dir: $HOST:$REMOTE_DIR"
    exit 1
fi

mkdir -p "$DEST"

# ── What to pull ──
if [ "$ALL" -eq 1 ]; then
    mapfile -t NAMES < <("${SSH[@]}" "$HOST" "ls -1 -- '$REMOTE_DIR'" |
        grep -E '\.parquet$' | grep -v '^\.' || true)
elif [ "${#NAMES[@]}" -eq 0 ]; then
    NAMES=("$REMOTE_FILE")
fi

if [ "${#NAMES[@]}" -eq 0 ]; then
    echo "[INFO] nothing to do: no .parquet in $HOST:$REMOTE_DIR"
    exit 0
fi

echo "[INFO] from : $HOST:$REMOTE_DIR"
echo "[INFO] to   : $DEST/"
echo "[INFO] files: ${NAMES[*]}"

FAILED=0
for name in "${NAMES[@]}"; do
    remote="$REMOTE_DIR/$name"
    final="$DEST/$name"
    tmp="$DEST/.$name.tmp"

    # size + sha256 of the remote file, in one ssh round trip
    meta=$("${SSH[@]}" "$HOST" "stat -c %s -- '$remote' && sha256sum -- '$remote' | cut -d' ' -f1") || {
        echo "[ERROR] cannot read $remote on $HOST"; FAILED=1; continue; }
    rsize=$(printf '%s\n' "$meta" | sed -n 1p)
    rsha=$(printf '%s\n' "$meta" | sed -n 2p)

    # already here and identical? then there is nothing to do
    if [ -f "$final" ] && [ "$(wc -c < "$final" | tr -d ' ')" = "$rsize" ]; then
        if [ "$(sha256sum "$final" | cut -d' ' -f1)" = "$rsha" ]; then
            echo "[OK] unchanged: $name ($rsize byte(s))"
            continue
        fi
        echo "[INFO] $name changed on the VPS: re-pulling"
    fi

    # resume a half-finished download instead of starting over
    offset=0
    [ -f "$tmp" ] && offset=$(wc -c < "$tmp" | tr -d ' ')
    if [ "$offset" -gt "$rsize" ]; then rm -f "$tmp"; offset=0; fi

    echo "[INFO] pulling $name ($rsize byte(s))"
    if [ "$offset" -gt 0 ]; then
        echo "[INFO] resuming from byte $offset"
        ok=0; "${SSH[@]}" "$HOST" "tail -c +$((offset + 1)) -- '$remote'" >> "$tmp" && ok=1
    else
        ok=0; "${SSH[@]}" "$HOST" "cat -- '$remote'" > "$tmp" && ok=1
    fi

    lsha=$(sha256sum "$tmp" | cut -d' ' -f1)
    if [ "$ok" -eq 0 ] || [ "$lsha" != "$rsha" ]; then
        rm -f "$tmp"   # our own unverified temp is the only file removed
        echo "[ERROR] $name failed verification - nothing was overwritten."
        echo "        VPS sha256  : $rsha"
        echo "        local sha256: $lsha"
        echo "        The VPS file may have been rewritten mid-pull (the crawler"
        echo "        appends rows to books.parquet). Rerun to try again."
        FAILED=1
        continue
    fi

    mv -f "$tmp" "$final"   # commit: only a verified file lands here
    echo "[OK] verified $name -> $final ($rsize byte(s))"
done

if [ "$FAILED" -ne 0 ]; then
    echo "[INFO] the VPS copy was not touched; nothing was deleted"
    echo "[ERROR] at least one file failed - see above"
    exit 1
fi
echo "[OK] the VPS copy was not touched; nothing was deleted"
echo "[INFO] next: ./hf-upload.sh $DEST"

#!/bin/bash
# Description: Reads package.json in the git repo root, bumps the patch version,
# stages all changes recursively, commits with a custom or default message,
# and records commit history details into .commit.
# Usage: ./git.sh [commit message]

set -e

# --- Capture commit message ---
CUSTOM_MSG="$*"

# Resolve paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# Check if we're in a git repo
if [ ! -e "$REPO_ROOT/.git" ]; then
    echo "[ERR] Not a git repository"
    exit 1
fi

cd "$REPO_ROOT"

# Stage all changes (including nested dirs)
git add -A

# --- Bump version in package.json ---
PKG_FILE="$REPO_ROOT/package.json"

if [ -f "$PKG_FILE" ]; then
    # Extract current version (handles "version": "x.y.z")
    CURRENT_VERSION=$(grep -oP '"version"\s*:\s*"\K[0-9]+\.[0-9]+\.[0-9]+' "$PKG_FILE" | head -1)

    if [ -n "$CURRENT_VERSION" ]; then
        # Split into major.minor.patch
        MAJOR=$(echo "$CURRENT_VERSION" | cut -d. -f1)
        MINOR=$(echo "$CURRENT_VERSION" | cut -d. -f2)
        PATCH=$(echo "$CURRENT_VERSION" | cut -d. -f3)

        # Bump patch
        PATCH=$((PATCH + 1))
        if [ "$PATCH" -gt 9 ]; then
            PATCH=0
            MINOR=$((MINOR + 1))
        fi
        if [ "$MINOR" -gt 9 ]; then
            MINOR=0
            MAJOR=$((MAJOR + 1))
        fi

        NEW_VERSION="$MAJOR.$MINOR.$PATCH"

        # Update package.json
        sed -i "s/\"version\": \"[0-9]*\.[0-9]*\.[0-9]*\"/\"version\": \"$NEW_VERSION\"/" "$PKG_FILE"

        git add "$PKG_FILE"
        echo "[OK] Bumped version: $CURRENT_VERSION -> $NEW_VERSION"
    else
        echo "[WARN] Could not parse version from package.json"
        NEW_VERSION="unknown"
    fi
else
    echo "[WARN] No package.json found"
    NEW_VERSION="unknown"
fi

# Check if there's anything to commit
if git diff --cached --quiet; then
    echo "[SKIP] No changes to commit"
    exit 0
fi

# --- Dynamic day name for default message ---
DAY_NAME=$(date +"%a")

# Use custom message if provided, otherwise fallback to default daily commit message
MSG="${CUSTOM_MSG:-daily commit{ $DAY_NAME }}"

git commit -m "$MSG"

echo "[OK] Committed: $MSG"
if [ "$NEW_VERSION" != "unknown" ]; then
    echo "[OK] Version: $NEW_VERSION"
fi

# --- Record commit metadata to .commit file ---
BRANCH=$(git rev-parse --abbrev-ref HEAD)
HASH=$(git rev-parse --short HEAD)
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
COMMIT_LOG="$REPO_ROOT/.commit"

# Initialize header if file does not exist
if [ ! -f "$COMMIT_LOG" ]; then
    printf "timestamp\tbranch\thash\tmessage\n" > "$COMMIT_LOG"
fi

# Append tab-separated entry for programmatic extraction (cut/awk/python)
printf "%s\t%s\t%s\t%s\n" "$TIMESTAMP" "$BRANCH" "$HASH" "$MSG" >> "$COMMIT_LOG"

echo "[OK] Recorded in .commit ($HASH on $BRANCH)"

git log --oneline -1

 #!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(pwd)"

# ── Defaults ──
DEFAULT_REMOTE="git@github.com:htetmyetflam3/scraper.git"
DEFAULT_USER="htetmyetflam3"
DEFAULT_EMAIL="149076482+htetmyetflam3@users.noreply.github.com"
DEFAULT_BRANCH="main"
DEFAULT_SSH_KEY="${HOME}/.ssh/default"


# ── Helpers ──
prompt() {
    local var_name="$1"
    local default="$2"
    local is_secret="$3"
    local message="$4"
    local current_val="${!var_name-}"

    if [ -n "$current_val" ] && [ "$current_val" != "$default" ]; then
        echo "$current_val"
        return
    fi

    if [ "$is_secret" = "secret" ]; then
        read -rsp "$message${default:+ [$default]}: " input
        echo >&2
    else
        read -rp "$message${default:+ [$default]}: " input
    fi

    echo "${input:-$default}"
}

# ── Create / enter project ──
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

# ── Initialize Git ──
if [ ! -d ".git" ]; then
    git init
    git branch -M "$DEFAULT_BRANCH" 2>/dev/null || true
    echo "[OK] Git initialized on branch '$DEFAULT_BRANCH'"
else
    git branch -M "$DEFAULT_BRANCH" 2>/dev/null || true
    echo "[OK] Git repo exists"
fi

# ── SSH key setup ──
echo ""
echo "=== SSH Key Setup ==="

read -rp "SSH private key [$DEFAULT_SSH_KEY]: " ssh_key_input
SSH_KEY="${ssh_key_input:-$DEFAULT_SSH_KEY}"

# Expand ~/ if entered manually
if [[ "$SSH_KEY" == "~/"* ]]; then
    SSH_KEY="$HOME/${SSH_KEY#~/}"
fi

if [ ! -f "$SSH_KEY" ]; then
    echo "[ERROR] SSH private key not found: $SSH_KEY"
    echo "Create or copy your key to that location, then run this script again."
    exit 1
fi

chmod 600 "$SSH_KEY"

# Configure this repository to always use authkey
printf -v SSH_COMMAND 'ssh -i %q -o IdentitiesOnly=yes' "$SSH_KEY"
git config --local core.sshCommand "$SSH_COMMAND"

echo "[OK] SSH key configured: $SSH_KEY"

# ── Interactive identity setup ──
echo ""
echo "=== Git Identity (leave blank to use default) ==="

# user.name
current_name=$(git config user.name 2>/dev/null || echo "")

if [ -n "$current_name" ]; then
    echo "[INFO] user.name already set: $current_name"
    read -rp "  Change it? [y/N]: " change_name

    if [[ "$change_name" =~ ^[Yy]$ ]]; then
        GIT_USER=$(prompt "GIT_USER" "$DEFAULT_USER" "" "  GitHub username")
        git config user.name "$GIT_USER"
        echo "[OK] user.name → $GIT_USER"
    fi
else
    GIT_USER=$(prompt "GIT_USER" "$DEFAULT_USER" "" "  GitHub username")
    git config user.name "$GIT_USER"
    echo "[OK] user.name → $GIT_USER"
fi

# user.email
current_email=$(git config user.email 2>/dev/null || echo "")

if [ -n "$current_email" ]; then
    echo "[INFO] user.email already set: $current_email"
    read -rp "  Change it? [y/N]: " change_email

    if [[ "$change_email" =~ ^[Yy]$ ]]; then
        GIT_EMAIL=$(prompt "GIT_EMAIL" "$DEFAULT_EMAIL" "" "  Email")
        git config user.email "$GIT_EMAIL"
        echo "[OK] user.email → $GIT_EMAIL"
    fi
else
    GIT_EMAIL=$(prompt "GIT_EMAIL" "$DEFAULT_EMAIL" "" "  Email")
    git config user.email "$GIT_EMAIL"
    echo "[OK] user.email → $GIT_EMAIL"
fi

# ── SSH Remote Setup ──
echo ""
echo "=== Remote Setup ==="

current_remote=$(git remote get-url origin 2>/dev/null || echo "")

if [ -n "$current_remote" ]; then
    echo "[INFO] Current remote: $current_remote"

    if [[ "$current_remote" =~ ^https?:// ]]; then
        echo "[INFO] The current remote uses HTTPS and may ask for username/password."
        read -rp "  Convert origin to SSH remote? [Y/n]: " update_remote

        if [[ "$update_remote" =~ ^[Nn]$ ]]; then
            REMOTE_URL="$current_remote"
            echo "[WARN] HTTPS remote kept. Git may still ask for credentials."
        else
            read -rp "  SSH remote URL [$DEFAULT_REMOTE]: " new_remote
            REMOTE_URL="${new_remote:-$DEFAULT_REMOTE}"
        fi
    else
        read -rp "  Update remote URL? [y/N]: " update_remote

        if [[ "$update_remote" =~ ^[Yy]$ ]]; then
            read -rp "  SSH remote URL [$DEFAULT_REMOTE]: " new_remote
            REMOTE_URL="${new_remote:-$DEFAULT_REMOTE}"
        else
            REMOTE_URL="$current_remote"
        fi
    fi
else
    read -rp "  SSH remote URL [$DEFAULT_REMOTE]: " new_remote
    REMOTE_URL="${new_remote:-$DEFAULT_REMOTE}"
fi

if [[ "$REMOTE_URL" =~ ^https?:// ]]; then
    echo "[ERROR] HTTPS remote detected:"
    echo "        $REMOTE_URL"
    echo "Use an SSH remote such as:"
    echo "        git@github.com:htetmyetflam3/pdf.git"
    exit 1
fi

if git remote | grep -q "^origin$"; then
    git remote set-url origin "$REMOTE_URL"
else
    git remote add origin "$REMOTE_URL"
fi

echo "[OK] Remote 'origin' → $REMOTE_URL"
echo "[OK] Git push will use SSH key authentication"

# ── .gitignore ──
if [ ! -f ".gitignore" ]; then cat > .gitignore << 'EOF'
# Untracked directories

*.venv*
*.log*
*frontend/
*__pycache__*
/__pycache__/
*package.lock.json*
# Runtime build artifacts
/node_modules/
*node_modules*
coverage/
.DS_Store
Thumbs.db
*.tmp
*.swp

EOF

    echo "[OK] Created .gitignore"
else
    echo "[OK] .gitignore already exists, not overwriting"
fi

# ── Untrack playground if previously staged ──
if git ls-files | grep -q "^playground/frontend"; then
    git rm -r --cached playground/ >/dev/null 2>&1 || true
    echo "[OK] Removed playground/ from index; kept on disk"
fi

# ── Untrack logs if previously staged ──
if git ls-files | grep -qE '^logs/|\.log(\.txt)?$|log\.txt$'; then
    git rm -r --cached logs/ 2>/dev/null || true
    git ls-files | grep -E '\.log(\.txt)?$|log\.txt$' |
        xargs -r git rm --cached -- 2>/dev/null || true
    echo "[OK] Removed logs and log files from index; kept on disk"
fi

TARGET_PATHS=(
   "python/"
    "node/"
)


echo ""
echo "=== Staging ==="

for path in "${TARGET_PATHS[@]}"; do
    if [ -e "$path" ]; then
        git add -f "$path"
        echo "[ADD] $path"
    else
        echo "[WARN] $path not found, skipping"
    fi
done

# ── Commit ──
if git diff --cached --quiet; then
    echo ""
    echo "[SKIP] Nothing staged to commit"
else
    echo ""
    read -rp "Commit message [0.5: refactor for sequential execution]: " msg
    COMMIT_MSG="${msg:-0.5: refactor for sequential execution}"

    git commit -m "$COMMIT_MSG"

    echo ""
    echo "[OK] Committed to $DEFAULT_BRANCH"
fi

# ── Summary ──
echo ""
echo "=== Repo Status ==="

git log --oneline -3 2>/dev/null || echo "(no commits yet)"

echo ""
echo "Identity: $(git config user.name) <$(git config user.email)>"
echo "Branch:   $DEFAULT_BRANCH"
echo "Remote:   $(git remote get-url origin 2>/dev/null || echo 'none')"
echo "SSH key:  $SSH_KEY"
echo ""
echo "To push:"
echo "  git push -u origin $DEFAULT_BRANCH"
echo ""
echo "GitHub username/password will not be requested."
echo "If authkey has a passphrase, Git may ask for the key passphrase."

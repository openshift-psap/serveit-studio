#!/bin/bash
# Inftune Studio server entrypoint
# Clones code to PVC on first boot, pulls latest on restart.
# Server runs from /mnt/storage/app/ (writable PVC).

set -e

APP_DIR="/mnt/storage/app"
REPO="git@github.com:bbenshab/inftune-studio.git"
BRANCH="${GIT_BRANCH:-main}"

echo "--- Starting Inftune Studio ---"

# Fix HOME for non-root users (OpenShift runs as random UID)
export HOME="${HOME:-/mnt/storage}"

# Setup SSH key for git (copy from accessible location)
mkdir -p "$HOME/.ssh" 2>/dev/null || true
if [ -f /etc/inftune-ssh/id_ed25519 ]; then
    cp /etc/inftune-ssh/id_ed25519 "$HOME/.ssh/id_ed25519" 2>/dev/null || true
    chmod 600 "$HOME/.ssh/id_ed25519" 2>/dev/null || true
    cp /etc/inftune-ssh/known_hosts "$HOME/.ssh/known_hosts" 2>/dev/null || true
fi

# Fix git safe directory (needed when running as non-root)
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

# Clone or pull
if [ -d "$APP_DIR/.git" ]; then
    echo "Code found on PVC — pulling latest..."
    cd "$APP_DIR"
    git fetch origin 2>/dev/null || true
    git reset --hard "origin/$BRANCH" 2>/dev/null || echo "Pull failed — using existing code"
else
    echo "Cloning repo to PVC..."
    rm -rf "$APP_DIR" 2>/dev/null || true
    git clone --depth 1 -b "$BRANCH" "$REPO" "$APP_DIR" 2>&1 || {
        echo "Git clone failed — check deploy key and network"
        if [ -d "/app/web" ]; then
            echo "Falling back to image-bundled code at /app"
            APP_DIR="/app"
        else
            echo "No code available — exiting"
            exit 1
        fi
    }
fi

echo "Running from: $APP_DIR"
echo "Commit: $(cd "$APP_DIR" && git log --oneline -1 2>/dev/null || echo 'unknown')"

# Server auto-restart loop
while true; do
    cd "$APP_DIR/web"
    find "$APP_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    python3.11 server.py || true
    echo "Server exited, restarting in 3s..."
    sleep 3
done

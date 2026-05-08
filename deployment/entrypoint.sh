#!/bin/bash
# InfeRecipe server entrypoint — used by both launcher and instance pods.
# Pulls latest code from git, then runs server with auto-restart loop.

echo "--- Starting InfeRecipe ---"
cd /app && git pull --ff-only 2>/dev/null || true

while true; do
    cd /app/web
    find /app -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    python3.11 server.py || true
    echo "Server exited, restarting in 3s..."
    sleep 3
done

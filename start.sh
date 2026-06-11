#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Pulling latest data..."
if [ -d "$DIR/data/.git" ]; then
    git -C "$DIR/data" pull --ff-only && echo "Data up to date." || true
else
    echo "Warning: data/ is not a git repo — skipping pull."
fi

echo "Installing dependencies..."
python3 -m pip install -q --break-system-packages -r "$DIR/requirements.txt"

echo "Starting bot..."
bash "$DIR/scripts/bot.sh" start
echo "Done."

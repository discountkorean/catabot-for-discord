#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing dependencies..."
python3 -m pip install -q --break-system-packages -r "$DIR/requirements.txt"

echo "Starting bot..."
bash "$DIR/scripts/bot.sh" start
echo "Done."

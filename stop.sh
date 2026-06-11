#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Stopping bot..."
bash "$DIR/scripts/bot.sh" stop
echo "Done."

#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Restarting bot..."
bash "$DIR/scripts/bot.sh" restart
echo "Done."

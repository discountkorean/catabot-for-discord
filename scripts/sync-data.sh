#!/usr/bin/env bash
# Data repo sync removed. Only syncs the main bot repo.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Syncing bot code to main repo..."
cd "$DIR"
git add .
if git diff --cached --quiet; then
    echo "No changes to sync."
else
    git commit -m "bot sync $(date '+%Y-%m-%d %H:%M:%S')"
    git push
fi

echo "Done."

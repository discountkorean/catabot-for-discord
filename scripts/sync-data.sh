#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Syncing bot data to private repo..."
cd "$DIR/data"
git add .
if git diff --cached --quiet; then
    echo "No changes to sync."
else
    git commit -m "data sync $(date '+%Y-%m-%d %H:%M:%S')"
    git push
fi

echo ""
echo "Syncing bot code to main repo..."
cd "$DIR"
git add .
if git diff --cached --quiet; then
    echo "No changes to sync."
else
    git commit -m "bot sync $(date '+%Y-%m-%d %H:%M:%S')"
    git push
fi

echo ""
echo "Done."

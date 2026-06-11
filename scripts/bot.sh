#!/usr/bin/env bash
set -euo pipefail
ACTION="${1:-start}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOT_FILE="$DIR/bot.py"
PID_FILE="$DIR/data/bot.pid"

find_python() {
    for cmd in python3.14 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            echo "$cmd"
            return
        fi
    done
    echo "Error: could not find Python executable." >&2
    exit 1
}

stop_bot() {
    if [ -f "$PID_FILE" ]; then
        BOT_PID=$(cat "$PID_FILE" 2>/dev/null || true)
        if [ -n "$BOT_PID" ] && kill -0 "$BOT_PID" 2>/dev/null; then
            kill "$BOT_PID"
            echo "Sent stop signal to PID $BOT_PID."
        else
            echo "No running bot found for PID $BOT_PID."
        fi
        rm -f "$PID_FILE"
    else
        echo "No PID file found — bot may not be running."
    fi
}

start_bot() {
    PYTHON=$(find_python)
    mkdir -p "$DIR/data" "$DIR/logs"
    nohup "$PYTHON" "$BOT_FILE" >> "$DIR/logs/monitor.log" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Bot started (PID $(cat "$PID_FILE"))."
}

case "$ACTION" in
    start)
        echo "Starting bot..."
        start_bot
        ;;
    stop)
        echo "Stopping bot..."
        stop_bot
        ;;
    restart)
        echo "Stopping bot..."
        stop_bot
        sleep 2
        echo "Starting bot..."
        start_bot
        ;;
    *)
        echo "Usage: $0 {start|stop|restart}" >&2
        exit 1
        ;;
esac

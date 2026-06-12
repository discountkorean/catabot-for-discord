#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOT_FILE="$DIR/bot.py"
PID_FILE="$DIR/data/bot.pid"
STOP_FILE="$DIR/data/bot.stop"

# Scheduled restarts are owned by the bot itself (catabot.app.scheduled_restart,
# at 00/08/16 AEST) so it can save state cleanly before exiting. The watchdog
# only supervises and crash-restarts. Leave empty to disable watchdog-side flushes.
RESTART_HOURS=()

CRASH_DELAY=10       # seconds to wait after an unexpected crash
SOCKET_BACKOFF=60    # extra wait when bot dies in <30s (likely socket exhaustion)
LAST_RESTART_HOUR=-1

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

PYTHON=$(find_python)
mkdir -p "$DIR/data" "$DIR/logs"

# Clear any stale stop file
rm -f "$STOP_FILE"

echo "=== Catabot Watchdog ==="
echo "Python          : $PYTHON"
echo "Bot             : $BOT_FILE"
echo "Scheduled flush : ${RESTART_HOURS[*]:-none (bot self-restarts at 00/08/16 AEST)}"
echo "Run 'touch $STOP_FILE' or bash stop.sh to shut down."
echo ""

while true; do
    if [ -f "$STOP_FILE" ]; then
        echo "[$(date '+%H:%M:%S')] Stop requested — watchdog exiting."
        rm -f "$STOP_FILE"
        break
    fi

    echo "[$(date '+%H:%M:%S')] Starting bot..."
    START_TIME=$(date +%s)

    # console.log, NOT monitor.log — the bot's own file handler already writes
    # monitor.log; redirecting stdout there too duplicates every line.
    CATABOT_SUPERVISED=1 "$PYTHON" "$BOT_FILE" >> "$DIR/logs/console.log" 2>&1 &
    BOT_PID=$!
    echo $BOT_PID > "$PID_FILE"
    echo "[$(date '+%H:%M:%S')] Bot running (PID $BOT_PID)"

    SCHEDULED_RESTART=false

    # Monitor loop — checks every 30s
    while kill -0 "$BOT_PID" 2>/dev/null; do
        sleep 30

        if [ -f "$STOP_FILE" ]; then
            echo "[$(date '+%H:%M:%S')] Stop requested — shutting down bot."
            kill "$BOT_PID" 2>/dev/null || true
            break
        fi

        CURRENT_HOUR=$(date '+%H' | sed 's/^0//')
        CURRENT_MIN=$(date '+%M' | sed 's/^0//')
        for H in "${RESTART_HOURS[@]}"; do
            if [ "$CURRENT_HOUR" -eq "$H" ] && [ "$CURRENT_MIN" -lt 5 ] && [ "$LAST_RESTART_HOUR" -ne "$H" ]; then
                LAST_RESTART_HOUR=$H
                SCHEDULED_RESTART=true
                echo "[$(date '+%H:%M:%S')] Scheduled restart at $(date '+%H:%M') — flushing bot..."
                kill "$BOT_PID" 2>/dev/null || true
                break
            fi
        done

        $SCHEDULED_RESTART && break
    done

    wait "$BOT_PID" 2>/dev/null || true
    EXIT_CODE=$?

    if [ -f "$STOP_FILE" ]; then
        echo "[$(date '+%H:%M:%S')] Stop confirmed — not restarting."
        rm -f "$STOP_FILE" "$PID_FILE"
        break
    fi

    if $SCHEDULED_RESTART; then
        echo "[$(date '+%H:%M:%S')] Scheduled restart — back up in 3s..."
        sleep 3
    else
        NOW=$(date +%s)
        UPTIME=$(( NOW - START_TIME ))
        if [ "$UPTIME" -lt 30 ]; then
            echo "[$(date '+%H:%M:%S')] Bot died after ${UPTIME}s (possible socket exhaustion) — waiting ${SOCKET_BACKOFF}s..."
            sleep $SOCKET_BACKOFF
        else
            echo "[$(date '+%H:%M:%S')] Bot exited (code $EXIT_CODE) — restarting in ${CRASH_DELAY}s..."
            sleep $CRASH_DELAY
        fi
    fi
done

rm -f "$PID_FILE"
echo "[$(date '+%H:%M:%S')] Watchdog stopped."

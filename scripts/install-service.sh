#!/usr/bin/env bash
# Installs catabot as a systemd service so it:
#   - starts automatically on boot (after the network is up)
#   - restarts automatically if the watchdog process ever dies
#
# Run once on the VM:  sudo bash scripts/install-service.sh
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo bash $0"
    exit 1
fi

# Resolve the real user and install dir (works regardless of where it's cloned)
RUN_USER="${SUDO_USER:-$(whoami)}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=/etc/systemd/system/catabot.service

echo "Installing catabot.service"
echo "  user : $RUN_USER"
echo "  dir  : $DIR"

cat > "$UNIT" <<EOF
[Unit]
Description=Catabot Discord restock monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=/usr/bin/bash $DIR/scripts/watchdog.sh
Restart=always
RestartSec=10
StandardOutput=append:$DIR/logs/watchdog.log
StandardError=append:$DIR/logs/watchdog.log

[Install]
WantedBy=multi-user.target
EOF

# Remove the old @reboot crontab line so the bot doesn't start twice
if crontab -u "$RUN_USER" -l 2>/dev/null | grep -q 'watchdog.sh'; then
    echo "Removing old @reboot watchdog crontab entry..."
    crontab -u "$RUN_USER" -l 2>/dev/null | grep -v 'watchdog.sh' | crontab -u "$RUN_USER" -
fi

mkdir -p "$DIR/logs"
chown "$RUN_USER":"$RUN_USER" "$DIR/logs"

systemctl daemon-reload
systemctl enable catabot.service
systemctl restart catabot.service

echo ""
echo "Done. Useful commands:"
echo "  systemctl status catabot      # check state"
echo "  systemctl restart catabot     # manual restart"
echo "  systemctl stop catabot        # stop"
echo "  journalctl -u catabot -f      # follow service-level logs"
echo "  tail -f $DIR/logs/watchdog.log"

#!/usr/bin/env bash
# Run once as root/sudo on the Linux bot server.
# Equivalent of apply-tcp-tuning.ps1 — fixes socket exhaustion (WinError 10055 equivalent).
#
#   tcp_fin_timeout        — how long closed connections linger (default 60s → 30s)
#   ip_local_port_range    — ephemeral port range (default ~28000 ports → ~64000 ports)

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo bash $0"
    exit 1
fi

CONF="/etc/sysctl.d/99-catabot.conf"

echo "Applying Linux TCP tuning..."

cat > "$CONF" <<EOF
# catabot TCP tuning — applied by apply-sysctl-tuning.sh
net.ipv4.tcp_fin_timeout = 30
net.ipv4.ip_local_port_range = 1024 65535
EOF

sysctl --system > /dev/null

echo "  tcp_fin_timeout       = 30s  (was 60s)"
echo "  ip_local_port_range   = 1024–65535"
echo ""
echo "Done. Settings are persistent and take effect immediately."

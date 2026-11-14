#!/bin/bash
set -e
MPC_IP="${1:-192.168.4.2}"
MPC_USER="root"
WORKSPACE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SDK_DIR="$WORKSPACE_DIR/sdk"

echo "[MPCUTER] 1/3 Connecting to Hakai 3.10 at $MPC_USER@$MPC_IP..."
ssh "$MPC_USER@$MPC_IP" "mkdir -p /data/MPCUTER/bridge /data/MPCUTER/scripts /data/MPCUTER/plugins /data/MPCUTER/cardputer_sd_backup"
scp "$WORKSPACE_DIR/bridge/mpc_bridge_hakai.py" "$MPC_USER@$MPC_IP:/data/MPCUTER/bridge/mpc_bridge_hakai.py"

ssh "$MPC_USER@$MPC_IP" "bash -s" << 'EOF'
cat << 'SVCEOF' > /etc/systemd/system/mpcuter-bridge.service
[Unit]
Description=MPCUTER v1.0.0 Hakai OS Telemetry & Deploy Bridge
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/data/MPCUTER/bridge
ExecStart=/usr/bin/python3 /data/MPCUTER/bridge/mpc_bridge_hakai.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable mpcuter-bridge.service
systemctl restart mpcuter-bridge.service
echo "[OK] mpcuter-bridge.service active and running on port 5000!"
EOF

echo "[MPCUTER] 2/3 Building & Installing MPCUTER VST2 Plugins to Hakai 3.10 ($MPC_IP)..."
bash "$WORKSPACE_DIR/scripts_host/build_plugins.sh"

for HAKAI_PKG in "$WORKSPACE_DIR"/plugins/*/build/hakai-3.10/*; do
  if [ -d "$HAKAI_PKG" ] && [ -f "$HAKAI_PKG/install.sh" ]; then
    PKG_NAME="$(basename "$HAKAI_PKG")"
    echo ">>> Uploading $PKG_NAME to Hakai 3.10..."
    tar -cf - -C "$(dirname "$HAKAI_PKG")" "$PKG_NAME" | ssh "$MPC_USER@$MPC_IP" "tar -xf - -C /data/MPCUTER/plugins && sh /data/MPCUTER/plugins/$PKG_NAME/install.sh -y"
  fi
done

echo "[MPCUTER] 3/3 Deployment Complete!"

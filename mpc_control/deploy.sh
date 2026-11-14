#!/bin/bash
set -euo pipefail

MPC_HOST="${MPC_HOST:-10.101.132.233}"
MPC_USER="${MPC_USER:-root}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ZIG_PY="$WORKSPACE_DIR/sdk/.sdk-tools/zigenv/bin/python3"

echo ">>> Cross-compiling libmpc_hw_bridge.so for ARM32 (arm-linux-gnueabihf.2.31)..."
if [ -x "$ZIG_PY" ]; then
    "$ZIG_PY" -m ziglang cc \
        -target arm-linux-gnueabihf.2.31 \
        -mcpu=cortex_a17 \
        -shared -fPIC -O2 -Wall -Wextra \
        -o "$SCRIPT_DIR/libmpc_hw_bridge.so" \
        "$SCRIPT_DIR/mpc_hw_bridge.c" \
        -ldl -lpthread
elif command -v arm-linux-gnueabihf-gcc >/dev/null 2>&1; then
    arm-linux-gnueabihf-gcc \
        -shared -fPIC -O2 -Wall -Wextra \
        -o "$SCRIPT_DIR/libmpc_hw_bridge.so" \
        "$SCRIPT_DIR/mpc_hw_bridge.c" \
        -ldl -lpthread
else
    echo "ERROR: No ARM32 cross-compiler found (checked $ZIG_PY and arm-linux-gnueabihf-gcc)." >&2
    exit 1
fi

echo ">>> Deploying to $MPC_USER@$MPC_HOST:/data/mpc-control/ ..."
ssh -o ConnectTimeout=5 "$MPC_USER@$MPC_HOST" "mkdir -p /data/mpc-control /etc/systemd/system/acvs.service.d"
scp -o ConnectTimeout=5 "$SCRIPT_DIR/libmpc_hw_bridge.so" "$SCRIPT_DIR/launch-mpc.sh" "$MPC_USER@$MPC_HOST:/data/mpc-control/"
ssh "$MPC_USER@$MPC_HOST" 'chmod +x /data/mpc-control/launch-mpc.sh /data/mpc-control/libmpc_hw_bridge.so
cat > /etc/systemd/system/acvs.service.d/99-mpc-hw-bridge.conf << "EOF"
[Service]
ExecStart=
ExecStart=/data/mpc-control/launch-mpc.sh
EOF
systemctl daemon-reload
systemctl restart acvs
sleep 3
systemctl is-active acvs'

echo ">>> Verifying TCP bridge on $MPC_HOST:6731..."
python3 - <<PY
import socket, time
for _ in range(20):
    try:
        with socket.create_connection(("$MPC_HOST", 6731), timeout=3) as s:
            s.sendall(b"PING\n")
            resp = s.recv(4096).decode().strip()
            print("Bridge:", resp)
            if "OK mpc_hw_bridge" in resp:
                break
    except Exception as exc:
        time.sleep(1)
else:
    raise SystemExit("Bridge failed to respond on port 6731")
PY

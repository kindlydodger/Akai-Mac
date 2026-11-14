#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MPC_CONTROL_DIR="$REPO_ROOT/mpc_control"
BUILD_DIR="$SCRIPT_DIR/build"
MPC_HOST="${MPC_HOST:-root@mpc-live.local}"

ZIGPY="$REPO_ROOT/sdk/.sdk-tools/zigenv/bin/python3"
if [ ! -x "$ZIGPY" ]; then
    ZIGPY="$(command -v python3)"
fi

mkdir -p "$BUILD_DIR"

echo "[1/5] Compiling host ASCII boot preview binary..."
cc -O2 -Wall -Wextra -Werror "$SCRIPT_DIR/mpc_boot_anim.c" -lm -o "$BUILD_DIR/mpc_boot_anim_host"

echo "[2/5] Cross-compiling ARM32 Cortex-A17 binaries..."
"$ZIGPY" -m ziglang cc -target arm-linux-gnueabihf.2.31 -mcpu=cortex_a17 -O2 -s -Wall -Wextra \
    "$SCRIPT_DIR/mpc_boot_anim.c" -lm -o "$BUILD_DIR/mpc_boot_anim"
"$ZIGPY" -m ziglang cc -target arm-linux-gnueabihf.2.31 -mcpu=cortex_a17 -O2 -s -Wall -Wextra \
    -shared -fPIC "$SCRIPT_DIR/mpc_boot_splash.c" -ldl -lpthread -lm -o "$BUILD_DIR/libmpc_boot_splash.so"

echo "[3/5] Generating U-Boot Frame-0 ACV8 splash BGRA..."
"$BUILD_DIR/mpc_boot_anim_host" --messages "$SCRIPT_DIR/boot_messages.json" \
    --dump-frame0 "$BUILD_DIR/boot_ascii_frame0.bgra" \
    --progress 0.0 --stage "POWER ON // HAKAI BOOTLOADER..."

if [ "${1:-}" = "--build-only" ]; then
    echo "Build only complete in $BUILD_DIR"
    exit 0
fi

echo "[4/5] Deploying binaries, boot_messages.json, launch-mpc.sh & early-boot systemd unit to $MPC_HOST..."
ssh -o BatchMode=yes -o ConnectTimeout=8 "$MPC_HOST" \
    "mkdir -p /data/mpc-control /etc/systemd/system/mpc-addins-reload.service.d"

scp -o BatchMode=yes \
    "$BUILD_DIR/mpc_boot_anim" \
    "$BUILD_DIR/libmpc_boot_splash.so" \
    "$BUILD_DIR/boot_ascii_frame0.bgra" \
    "$SCRIPT_DIR/boot_messages.json" \
    "$MPC_CONTROL_DIR/launch-mpc.sh" \
    "$MPC_HOST:/data/mpc-control/"

scp -o BatchMode=yes \
    "$SCRIPT_DIR/mpc-boot-anim.service" \
    "$MPC_HOST:/etc/systemd/system/mpc-boot-anim.service"

scp -o BatchMode=yes \
    "$SCRIPT_DIR/10-boot-anim.conf" \
    "$MPC_HOST:/etc/systemd/system/mpc-addins-reload.service.d/10-boot-anim.conf"

ssh -o BatchMode=yes "$MPC_HOST" 'sh -s' <<'REMOTE_EOF'
set -eu
chmod 755 /data/mpc-control/mpc_boot_anim /data/mpc-control/libmpc_boot_splash.so /data/mpc-control/launch-mpc.sh
chmod 644 /data/mpc-control/boot_messages.json /etc/systemd/system/mpc-boot-anim.service /etc/systemd/system/mpc-addins-reload.service.d/10-boot-anim.conf

# Preserve original stock splash backup if missing (never overwrite existing backup!)
if [ ! -f /data/backups/splash_original_backup.bgra ] && [ -b /dev/mmcblk0p4 ]; then
    mkdir -p /data/backups
    dd if=/dev/mmcblk0p4 of=/data/backups/splash_original_backup.bgra bs=4096 count=1000
fi

# Flash Frame-0 ASCII BGRA to /dev/mmcblk0p4 (exact 4,096,000 bytes) for instant power-on match!
SZ=$(wc -c < /data/mpc-control/boot_ascii_frame0.bgra | tr -d ' ')
if [ -b /dev/mmcblk0p4 ] && [ "$SZ" -eq 4096000 ]; then
    dd if=/data/mpc-control/boot_ascii_frame0.bgra of=/dev/mmcblk0p4 bs=4096 count=1000
    sync
fi

systemctl daemon-reload
echo "Deployed and flashed Frame-0 splash cleanly."
REMOTE_EOF

echo "[5/5] Done! To preview on host run: $BUILD_DIR/mpc_boot_anim_host --ansi"

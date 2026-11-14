#!/bin/bash
set -e
cd "$(cd "$(dirname "$0")/.." && pwd)"
if [ "${1:-}" == "--ota" ]; then
    CARDPUTER_IP="${2:-192.168.4.1}"
    BIN_PATH=".pio/build/m5stack-cardputer/firmware.bin"
    if [ ! -f "$BIN_PATH" ]; then
        echo "[BUILD] Compiling firmware via PlatformIO..."
        pio run
    fi
    echo "[OTA] Uploading $BIN_PATH to http://$CARDPUTER_IP/update..."
    curl -F "update=@$BIN_PATH" "http://$CARDPUTER_IP/update"
    echo "[OK] Wireless OTA complete!"
else
    echo "[USB] Compiling and flashing M5Stack Cardputer over USB..."
    pio run -t upload
fi

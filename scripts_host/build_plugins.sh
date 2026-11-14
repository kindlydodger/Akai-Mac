#!/bin/bash
set -e
WORKSPACE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SDK_DIR="$WORKSPACE_DIR/sdk"

echo "========================================================================"
echo "  MPCUTER v1.0.0 — Native MPC VST2 Plugin Build & Verification Suite"
echo "========================================================================"

for PLUGIN_DIR in "$WORKSPACE_DIR"/plugins/*; do
  if [ -d "$PLUGIN_DIR" ] && [ -f "$PLUGIN_DIR/plugin.json" ]; then
    echo ""
    echo ">>> Building: $(basename "$PLUGIN_DIR")"
    python3 "$SDK_DIR/tools/build.py" "$PLUGIN_DIR"
    for DYLIB in "$PLUGIN_DIR"/build/native/*.dylib; do
      if [ -f "$DYLIB" ]; then
        echo ">>> Running host_sim real-time verification on $(basename "$DYLIB")..."
        "$SDK_DIR/.sdk-tools/host_sim" "$DYLIB" test
      fi
    done
    echo ">>> Generating Hakai 3.10 installer package..."
    python3 "$WORKSPACE_DIR/scripts_host/package_hakai.py" "$PLUGIN_DIR"
  fi
done

echo ""
echo "[OK] All MPCUTER plugins built, verified, and packaged for Hakai 3.10 + MPC 3.10.1+!"

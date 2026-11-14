---
name: mpcuter-hardware-ops
description: Compile, flash, and deploy MPCUTER v1.0.0 firmware to M5Stack Cardputer, build & validate native Akai MPC VST2 plugins with mpc-plugin-builder-sdk, and deploy over SSH to Akai MPC Live (Hakai OS).
---

# MPCUTER Hardware, Native Plugin & Deployment Workflows

## Workflow 1: Build & Verify Native MPC VST2 Plugins (`zig` + `host_sim`)
When the user asks to create, compile, or test an MPC standalone plugin:
1. Build native (`.dylib`), `arm32` (`.so`), `arm64` (`.so`), and `mpc60-green` `Plugin Skins/` (`TUI.json` + `Q-Links.json`):
   ```bash
   python3 sdk/tools/build.py plugins/<PluginFolder>
   ```
2. Run the automated MPC host simulator & real-time DSP contract verification suite:
   ```bash
   sdk/.sdk-tools/host_sim plugins/<PluginFolder>/build/native/<slug>.dylib test
   ```
3. Or build and test all workspace plugins at once:
   ```bash
   bash scripts_host/build_plugins.sh
   ```

## Workflow 2: Deploy Bridge & VST2 Plugins to Akai MPC (Hakai OS / SSH)
When the user asks to connect to the MPC, install plugins, or deploy the bridge:
1. Check MPC status, architecture, and mounted drives:
   ```bash
   python3 sdk/tools/mpc_remote.py --host 192.168.4.2 status
   ```
2. Install a single plugin and restart the `acvs` plugin service:
   ```bash
   python3 sdk/tools/mpc_remote.py --host 192.168.4.2 install plugins/<PluginFolder> --restart-acvs
   ```
3. Or run the full one-command installer (Hakai Bridge systemd service + all VST2 plugins):
   ```bash
   bash scripts_host/remote_install_mpc.sh 192.168.4.2
   ```

## Workflow 3: Compile & Flash M5Stack Cardputer (USB or Wireless OTA)
When the user asks to build or update the Cardputer firmware:
1. Check for connected USB serial ports on macOS:
   ```bash
   ls /dev/cu.usbmodem*
   ```
2. Compile and upload via PlatformIO (`pio run -t upload`) or use the helper script:
   ```bash
   bash scripts_host/flash_cardputer.sh
   ```
3. If the Cardputer is on Wi-Fi (`MPCUTER` SoftAP at `192.168.4.1`), push the compiled binary over Wireless OTA:
   ```bash
   bash scripts_host/flash_cardputer.sh --ota 192.168.4.1
   ```

## Workflow 4: Configure & Trigger Google Drive Cloud Backup (`rclone`)
When the user asks to set up or trigger SD + MPC HDD backup to Google Drive:
1. Copy local macOS `rclone.conf` to Hakai OS:
   ```bash
   scp ~/.config/rclone/rclone.conf root@192.168.4.2:/root/.config/rclone/rclone.conf
   ```
2. Trigger an immediate sync via the bridge API:
   ```bash
   curl -X POST http://192.168.4.2:5000/cloud-backup -H "Content-Type: application/json" -d '{"target":"all"}'
   ```

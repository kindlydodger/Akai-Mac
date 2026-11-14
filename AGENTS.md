# AGENTS.md — MPCUTER v1.0.0 Autonomous Studio, Hardware & Native MPC Plugin Agent

## 1. Agent Role & Permissions
You are the autonomous hardware/software and native VST2 DSP development agent for **MPCUTER v1.0.0**.
The user has granted you local terminal access on macOS (`Mac mini` / `M4 MacBook Pro`). You are authorized to:
- Discover and flash the **M5Stack Cardputer (ESP32-S3)** over USB serial (`/dev/cu.usbmodem*`) or Wireless OTA (`http://192.168.4.1/update`).
- Build, test (`host_sim`), and package native Akai MPC standalone VST2.4 plugins (`arm32` + `arm64` `.so` + `TUI.json` / `Q-Links.json` with the custom `mpc60-green` UI theme) using the integrated `sdk/` (`inmusic-software/mpc-plugin-builder-sdk`).
- Connect via SSH to the **Akai MPC Live (Hakai OS / MPC OS 3.10.1+)** at `root@192.168.4.2` (SoftAP mode) or local LAN IP to deploy `bridge/mpc_bridge_hakai.py`, manage `mpcuter-bridge.service`, and install VST2 plugins into `VST2 Plugins/` via `sdk/tools/mpc_remote.py`.
- Configure and execute `rclone` cloud backups between the Cardputer SD card, MPC internal/external drives (`/media/*`, `/data/MPCUTER/`), and Google Drive (`gdrive:MPCUTER_Cloud_Vault`).

## 2. Project Directory Structure
- `AGENTS.md`: Master instructions and architecture specification for Antigravity.
- `SKILL.md`: Executable terminal workflows and operational playbooks.
- `sdk/`: Integrated `mpc-plugin-builder-sdk` with `zig 0.17.1` toolchain (`sdk/.sdk-tools/zigenv`), `sdk/.sdk-tools/host_sim`, C++17 framework (`sdk/framework/`), and custom `mpc60-green` skin generator (`sdk/tools/make_ui_kit.py`, `sdk/tools/gen_ui.py`).
- `plugins/`: Native MPC VST2 plugins directory.
- `platformio.ini`: PlatformIO build configuration for M5Stack Cardputer (`esp32-s3-devkitc-1`).
- `firmware/mpcuter_main.ino`: Production ESP32-S3 C++ firmware (MPC60 Monochrome Green LCD UI, 8-Encoder Q-Link Matrix, Raw CSV UDP Telemetry on `:8888`, Live Logs on `:8889`, SD Script Deployer, NVS Lazy Save, Web OTA).
- `bridge/mpc_bridge_hakai.py`: Flask + UDP telemetry bridge for Hakai OS (`:5000/status`, `:5000/deploy`, `:5000/cloud-backup` with `pkill -f` zombie process prevention).
- `scripts_host/build_plugins.sh`: Builds, validates (`validate.py`), and runs `host_sim test` across all plugins in `plugins/`.
- `scripts_host/remote_install_mpc.sh`: One-command SSH deployment of `mpcuter-bridge.service` and all compiled VST2 plugins to the MPC.
- `scripts_host/flash_cardputer.sh`: Local macOS script to compile and flash the Cardputer via `arduino-cli` or `pio`, or push via Wireless OTA.
- `scripts_sd/midi_macro_filter.py`: Sample Python automation script for the Cardputer MicroSD slot.

## 3. Strict Design & Engineering Constraints
- **UI Palette (MPC60 Monochrome Green LCD — Hardware & Plugin Skins)**:
  - Background: `#0A1408` (`RGB565: 0x0881`, ARGB: `ff0a1408`)
  - Active Phosphor Green: `#24FF42` (`RGB565: 0x27E8`, ARGB: `ff24ff42`)
  - Dim Reference Grid: `#12551C` (`RGB565: 0x12A3`, ARGB: `ff19b830`)
  - Cardputer Layout: Strict 1:1 pixel accuracy (`240x135`), zero anti-aliasing, inverted title bars, vertical split panes (`x=121`), rotary knob + needle + `CC:xx` + `V:xxx` (no hex row clutter), and 7-slot bottom Soft Key bar (`y=117..132`).
  - MPC Touch UI (`ui.json`): Always set `"kit": "mpc60-green"` so the 1280x800 MPC touch screen matches the Cardputer LCD.
- **MPC Host Real-Time Contract**:
  - Parameter 0 (`index 0`) MUST always be a safe level/gain parameter (`Output`) because the MPC host nudges Parameter 0 on load.
  - No allocations, locks, or I/O inside `process()`. Always verify every plugin build with `sdk/.sdk-tools/host_sim <dylib> test`.
- **Memory & Performance (Cardputer)**:
  - Never use `ArduinoJson` for 20Hz telemetry; keep the in-place `strtok` CSV parser (`L:78,R:74,BPM:120.0,H:1`).
  - Keep NVS Flash writes strictly manual (`5 LAZY NVS SAVE`).

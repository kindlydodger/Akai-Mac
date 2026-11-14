#!/usr/bin/env python3
"""Build or refresh the double-clickable macOS Desktop app 'MPC Live Monitor.app'."""

from __future__ import annotations

import argparse
import math
import plistlib
import shutil
import stat
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path
from typing import Tuple

HERE = Path(__file__).resolve().parent
MONITOR_SCRIPT = HERE / "mpc_monitor.py"
DEFAULT_APP_PATH = Path.home() / "Desktop" / "MPC Live Monitor.app"

RGBA = Tuple[int, int, int, int]

# Sampled MPC 3.x Menu & Main colors
C_BG: RGBA = (10, 10, 13, 255)
C_BEZEL: RGBA = (47, 47, 52, 255)
C_RIM: RGBA = (78, 80, 88, 255)
C_LCD_BG: RGBA = (19, 21, 25, 255)
C_TILE_IDLE: RGBA = (36, 36, 40, 255)
C_CRIMSON: RGBA = (238, 23, 52, 255)   # MAIN MODE / REC ARM
C_EMERALD: RGBA = (45, 208, 108, 255)  # MEM/CPU / Drum 001
C_GOLD: RGBA = (226, 170, 2, 255)      # BROWSER folder
C_TEAL: RGBA = (27, 121, 133, 255)     # SAMPLE EDIT
C_MAGENTA: RGBA = (224, 72, 255, 255)  # ARRANGE
C_BLUE: RGBA = (14, 114, 214, 255)     # SONG / OTT


def _blend(dst: RGBA, src: RGBA) -> RGBA:
    sa = src[3] / 255.0
    if sa <= 0.0:
        return dst
    if sa >= 1.0 and dst[3] == 255:
        return src
    inv = 1.0 - sa
    return (
        int(src[0] * sa + dst[0] * inv),
        int(src[1] * sa + dst[1] * inv),
        int(src[2] * sa + dst[2] * inv),
        min(255, int(src[3] + dst[3] * inv)),
    )


def _rounded_rect_sdf(x: float, y: float, x0: float, y0: float, x1: float, y1: float, radius: float) -> float:
    cx = min(max(x, x0 + radius), x1 - radius)
    cy = min(max(y, y0 + radius), y1 - radius)
    return math.hypot(x - cx, y - cy) - radius


def _encode_png_rgba(width: int, height: int, pixels: bytearray) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw_rows = bytearray()
    stride = width * 4
    for y in range(height):
        raw_rows.append(0)
        off = y * stride
        raw_rows.extend(pixels[off : off + stride])

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), level=6)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def render_mpc_icon_png(size: int = 512) -> bytes:
    """Render a crisp 512x512 MPC 3.x Menu-themed pad grid + live LCD pulse icon."""
    buf = bytearray(size * size * 4)
    scale = size / 512.0

    # Pad color layout matching MPC 3.x Menu app accents
    pad_colors = {
        (0, 0): C_CRIMSON,
        (0, 1): C_EMERALD,
        (0, 2): C_GOLD,
        (0, 3): C_TEAL,
        (1, 0): C_CRIMSON,
        (1, 1): C_MAGENTA,
        (1, 2): C_TILE_IDLE,
        (1, 3): C_BLUE,
        (2, 0): C_TILE_IDLE,
        (2, 1): C_EMERALD,
        (2, 2): C_TILE_IDLE,
        (2, 3): C_TILE_IDLE,
        (3, 0): C_TILE_IDLE,
        (3, 1): C_TILE_IDLE,
        (3, 2): C_GOLD,
        (3, 3): C_CRIMSON,
    }

    margin = 34 * scale
    outer_r = 96 * scale
    rim_w = 6 * scale

    lcd_x0, lcd_y0, lcd_x1, lcd_y1 = 82 * scale, 74 * scale, 430 * scale, 154 * scale
    grid_x0, grid_y0 = 82 * scale, 178 * scale
    pad_sz, pad_gap = 75 * scale, 16 * scale

    for y in range(size):
        for x in range(size):
            px: RGBA = (0, 0, 0, 0)
            d_outer = _rounded_rect_sdf(x, y, margin, margin, size - margin, size - margin, outer_r)
            if d_outer < 1.2:
                alpha = int(max(0.0, min(1.0, 0.8 - d_outer)) * 255)
                if d_outer > -rim_w:
                    base = C_BEZEL if d_outer < -1.8 else C_RIM
                else:
                    base = C_BG
                px = (base[0], base[1], base[2], alpha)

                # Top OLED telemetry bar
                d_lcd = _rounded_rect_sdf(x, y, lcd_x0, lcd_y0, lcd_x1, lcd_y1, 18 * scale)
                if d_lcd <= 0:
                    px = _blend(px, C_LCD_BG)
                    # Waveform pulse + red live dot inside LCD
                    rel_x = (x - lcd_x0) / max(1.0, lcd_x1 - lcd_x0)
                    mid_y = (lcd_y0 + lcd_y1) * 0.5
                    wave = math.sin(rel_x * 16.0) * math.exp(-((rel_x - 0.45) ** 2) * 14.0) * 24 * scale
                    if rel_x > 0.14 and abs(y - (mid_y - wave)) < 3.2 * scale:
                        px = _blend(px, C_EMERALD)
                    if math.hypot(x - (lcd_x0 + 28 * scale), y - mid_y) < 9.5 * scale:
                        px = _blend(px, C_CRIMSON)

                # 4x4 Pad matrix
                if grid_x0 <= x <= grid_x0 + 4 * pad_sz + 3 * pad_gap and grid_y0 <= y <= grid_y0 + 4 * pad_sz + 3 * pad_gap:
                    col_i = int((x - grid_x0) // (pad_sz + pad_gap))
                    row_i = int((y - grid_y0) // (pad_sz + pad_gap))
                    px0 = grid_x0 + col_i * (pad_sz + pad_gap)
                    py0 = grid_y0 + row_i * (pad_sz + pad_gap)
                    if 0 <= col_i < 4 and 0 <= row_i < 4:
                        dp = _rounded_rect_sdf(x, y, px0, py0, px0 + pad_sz, py0 + pad_sz, 14 * scale)
                        if dp <= 0:
                            fill = pad_colors.get((row_i, col_i), C_TILE_IDLE)
                            if dp > -3.5 * scale:
                                # Crisp Akai pad rubber edge tint
                                fill = (min(255, fill[0] + 22), min(255, fill[1] + 22), min(255, fill[2] + 22), 255)
                            px = _blend(px, fill)

            off = (y * size + x) * 4
            buf[off : off + 4] = bytes(px)

    return _encode_png_rgba(size, size, buf)


def _build_icns(png_bytes: bytes, dest_icns: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src_png = root / "icon_512.png"
        iconset = root / "AppIcon.iconset"
        iconset.mkdir()
        src_png.write_bytes(png_bytes)

        sizes = [
            ("icon_16x16.png", 16),
            ("icon_16x16@2x.png", 32),
            ("icon_32x32.png", 32),
            ("icon_32x32@2x.png", 64),
            ("icon_128x128.png", 128),
            ("icon_128x128@2x.png", 256),
            ("icon_256x256.png", 256),
            ("icon_256x256@2x.png", 512),
            ("icon_512x512.png", 512),
        ]
        for fname, dim in sizes:
            subprocess.run(
                ["sips", "-z", str(dim), str(dim), str(src_png), "--out", str(iconset / fname)],
                check=True,
                capture_output=True,
            )
        shutil.copy2(src_png, iconset / "icon_512x512@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest_icns)], check=True)


def _launcher_script(monitor_script: Path) -> str:
    cmd_str = f'clear && /usr/bin/python3 "{monitor_script}"'
    return f"""#!/bin/bash
set -euo pipefail

/usr/bin/osascript <<'APPLESCRIPT'
set targetTitle to "MPC LIVE // CANLI MONİTÖR"
set launchCmd to {json_escape_applescript(cmd_str)}

tell application "Terminal"
    activate
    -- Focus existing monitor tab if already running
    repeat with w in windows
        repeat with t in tabs of w
            try
                if (custom title of t contains targetTitle) or (processes of t contains "Python" and (contents of t contains "MPC LIVE")) then
                    set selected of t to true
                    set index of w to 1
                    return
                end if
            end try
        end repeat
    end repeat

    set newTab to do script launchCmd
    set targetWin to window 1
    try
        repeat with w in windows
            if (selected tab of w) is equal to newTab then
                set targetWin to w
                exit repeat
            end if
        end repeat
        set number of columns of targetWin to 136
        set number of rows of targetWin to 40
    end try
    try
        set custom title of newTab to targetTitle
        set background color of newTab to {{0, 0, 0}}
        set normal text color of newTab to {{60138, 60652, 61680}}
        set bold text color of newTab to {{65535, 65535, 65535}}
        set cursor color of newTab to {{60909, 5911, 13364}}
    end try
end tell
APPLESCRIPT
"""


def json_escape_applescript(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_desktop_app(app_path: Path = DEFAULT_APP_PATH, monitor_script: Path = MONITOR_SCRIPT) -> Path:
    contents = app_path / "Contents"
    macos_dir = contents / "MacOS"
    res_dir = contents / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=True)
    res_dir.mkdir(parents=True, exist_ok=True)

    plist_data = {
        "CFBundleExecutable": "MPC Live Monitor",
        "CFBundleIconFile": "AppIcon",
        "CFBundleIdentifier": "com.mpcuter.mpclivemonitor",
        "CFBundleName": "MPC Live Monitor",
        "CFBundleDisplayName": "MPC Live Monitor",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0.0",
        "LSBackgroundOnly": True,
    }
    with (contents / "Info.plist").open("wb") as fh:
        plistlib.dump(plist_data, fh)

    _build_icns(render_mpc_icon_png(512), res_dir / "AppIcon.icns")

    exe_path = macos_dir / "MPC Live Monitor"
    exe_path.write_text(_launcher_script(monitor_script), encoding="utf-8")
    exe_path.chmod(exe_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    subprocess.run(["xattr", "-cr", str(app_path)], capture_output=True)
    lsregister = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    if Path(lsregister).exists():
        subprocess.run([lsregister, "-f", str(app_path)], capture_output=True)
    app_path.touch()
    return app_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Create Desktop launcher app for MPC Live Monitor.")
    parser.add_argument("--out", type=Path, default=DEFAULT_APP_PATH, help="Output .app bundle path")
    args = parser.parse_args()
    out = build_desktop_app(args.out)
    print(f"Created Desktop app: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

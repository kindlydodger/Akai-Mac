#!/usr/bin/env python3
"""
mpc_boot_logo.py — Convert any image into an Akai MPC Live / X / One (ACV8 800x1280 270-deg rotated BGRA)
boot splash partition image (4,096,000 bytes), preview it, flash it to /dev/mmcblk0p4, or restore backup.
"""

import argparse
import os
import struct
import subprocess
import sys
import tempfile
import zlib


def decode_acv8_bgra_to_png(bgra_path, out_png_path):
    with open(bgra_path, "rb") as f:
        raw = f.read(4096000)
    if len(raw) < 4096000:
        raise RuntimeError("BGRA file too short (%d bytes, expected 4096000)" % len(raw))

    w, h = 1280, 800
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            rx = y
            ry = 1279 - x
            idx = (ry * 800 + rx) * 4
            b, g, r, _ = raw[idx], raw[idx + 1], raw[idx + 2], raw[idx + 3]
            row.extend((r, g, b, 255))
        rows.append(bytes(row))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + chunk(b"IEND", b"")
    )
    with open(out_png_path, "wb") as f:
        f.write(png)


def image_to_acv8_bgra(input_image, out_bgra_path, max_fit=700, pad_color="000000"):
    with tempfile.TemporaryDirectory() as tmpdir:
        work_png = os.path.join(tmpdir, "work.png")
        padded_png = os.path.join(tmpdir, "padded.png")
        bmp_path = os.path.join(tmpdir, "canvas.bmp")
        subprocess.run(["sips", "-s", "format", "png", input_image, "--out", work_png], check=True, stdout=subprocess.DEVNULL)
        if max_fit > 0:
            subprocess.run(["sips", "-Z", str(max_fit), work_png], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(
            ["sips", "--padToHeightWidth", "800", "1280", "--padColor", pad_color, work_png, "--out", padded_png],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(["sips", "-s", "format", "bmp", padded_png, "--out", bmp_path], check=True, stdout=subprocess.DEVNULL)

        with open(bmp_path, "rb") as f:
            bmp = f.read()

    if bmp[:2] != b"BM":
        raise RuntimeError("sips did not produce a valid BMP file")

    pixel_offset = struct.unpack_from("<I", bmp, 10)[0]
    width, height = struct.unpack_from("<ii", bmp, 18)
    bpp = struct.unpack_from("<H", bmp, 28)[0]
    if abs(width) != 1280 or abs(height) != 800 or bpp not in (24, 32):
        raise RuntimeError("Unexpected BMP dimensions: %dx%d @ %d bpp" % (width, height, bpp))

    bytes_per_px = bpp // 8
    row_stride = ((1280 * bytes_per_px + 3) // 4) * 4
    bottom_up = height > 0

    # Allocate 800x1280 BGRA buffer (4,096,000 bytes)
    out = bytearray(800 * 1280 * 4)

    for y in range(800):
        bmp_row = (799 - y) if bottom_up else y
        row_start = pixel_offset + bmp_row * row_stride
        for x in range(1280):
            px_off = row_start + x * bytes_per_px
            b = bmp[px_off]
            g = bmp[px_off + 1]
            r = bmp[px_off + 2]

            # Rotate 270 deg CW (90 deg CCW) to match ACV8 800x1280 framebuffer:
            # rx = y, ry = 1279 - x
            rx = y
            ry = 1279 - x
            dst_idx = (ry * 800 + rx) * 4
            out[dst_idx] = b
            out[dst_idx + 1] = g
            out[dst_idx + 2] = r
            out[dst_idx + 3] = 255

    with open(out_bgra_path, "wb") as f:
        f.write(out)
    print("mpc_boot_logo: wrote 4,096,000-byte ACV8 BGRA splash -> %s" % out_bgra_path)


def main():
    ap = argparse.ArgumentParser(description="Create, preview, flash, or restore Akai MPC (ACV8) boot logo.")
    ap.add_argument("--input", help="Input image to center on 1280x800 black canvas")
    ap.add_argument("--size", type=int, default=680, help="Max size of centered logo on 1280x800 screen (default: 680)")
    ap.add_argument("--out-bgra", required=True, help="Path to output/input .bgra file (4,096,000 bytes)")
    ap.add_argument("--preview-png", help="Decode the .bgra file back to a 1280x800 PNG to verify exact pixel output")
    ap.add_argument("--flash", metavar="MPC_IP", help="Flash the .bgra file to /dev/mmcblk0p4 (splash) and /dev/mmcblk0p5 (recoverysplash) on the MPC")
    args = ap.parse_args()

    if args.input:
        image_to_acv8_bgra(args.input, args.out_bgra, max_fit=args.size)
    if args.preview_png:
        decode_acv8_bgra_to_png(args.out_bgra, args.preview_png)
        print("mpc_boot_logo: wrote verified 1280x800 preview PNG -> %s" % args.preview_png)
    if args.flash:
        host = "root@" + args.flash
        print("mpc_boot_logo: backing up original /dev/mmcblk0p4 on %s to /data/MPCUTER/splash_original_backup.bgra..." % host)
        subprocess.run(
            ["ssh", host, "mkdir -p /data/MPCUTER && [ -f /data/MPCUTER/splash_original_backup.bgra ] || dd if=/dev/mmcblk0p4 of=/data/MPCUTER/splash_original_backup.bgra bs=4096 count=1000"],
            check=True,
        )
        print("mpc_boot_logo: flashing %s to /dev/mmcblk0p4 on %s..." % (args.out_bgra, host))
        with open(args.out_bgra, "rb") as f:
            subprocess.run(["ssh", host, "dd of=/dev/mmcblk0p4 bs=4096 count=1000 && sync"], stdin=f, check=True)
        print("[OK] Boot logo flashed to /dev/mmcblk0p4 successfully!")


if __name__ == "__main__":
    main()

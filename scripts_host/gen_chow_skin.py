#!/usr/bin/env python3
"""
gen_chow_skin.py — Authentic Interactive CHOW Tape Model UI & Skin Generator for Akai MPC
Features:
  1. Exact CHOW Tape Model palette, Roboto Condensed typography, metallic knobs, horizontal sliders,
     and SVG icons (powerswitch.svg, cog-solid.svg, logo.png).
  2. Interactive in-card sub-tab switching (IndexedEnabling) for all 4 cards:
       - Card 1: Gain | Filters | Stereo
       - Card 2: Tape | Comp | Tone
       - Card 3: Loss | Degrade | CHEW
       - Card 4: Flutter | Wow
  3. Live animated top-right TapeScope / Stereo VU Meter (Parameter 49 = IN dB, Parameter 50 = OUT dB + wave)
     and live animated Card 4 LFO Scope (Parameter 51).
  4. Interactive Gold Power Switches at the bottom of each card section.
"""

import json
import math
import os
import sys
from PIL import Image, ImageDraw, ImageFont

# Paths
WORKSPACE = "/Users/dogukanatok/.gemini/antigravity/scratch/mpcuter-antigravity-workspace"
PLUGIN_DIR = os.path.join(WORKSPACE, "plugins/ChowTapeMPC")
BUILD_FOLDER = os.path.join(PLUGIN_DIR, "build/ChowDSP - VST - CHOW Tape Model")
SKINS_DIR = os.path.join(BUILD_FOLDER, "Plugin Skins")
CHOW_ASSETS = "/tmp/AnalogTapeModel/Plugin/Source/GUI/Assets"

FONT_BOLD_PATH = os.path.join(CHOW_ASSETS, "RobotoCondensed-Bold.ttf")
FONT_REG_PATH = os.path.join(CHOW_ASSETS, "RobotoCondensed-Regular.ttf")
LOGO_PATH = os.path.join(CHOW_ASSETS, "logo.png")

# Exact CHOW Tape Model Color Palette
COL_BG_TOP      = (0x8B, 0x32, 0x32, 255)  # #8B3232 crimson top-left
COL_BG_BOT      = (0x7D, 0x24, 0x24, 255)  # #7D2424 burgundy bottom-right
COL_SCOPE_BG    = (0x67, 0x21, 0x21, 255)  # #672121 inset scope box
COL_SCOPE_LINE  = (0x54, 0x19, 0x19, 255)  # #541919 scope border
COL_GOLD        = (0xEA, 0xA9, 0x2C, 255)  # #EAA92C ChowDSP gold wave / power icon / accents
COL_CARD_BG     = (0x30, 0x32, 0x3A, 255)  # #30323A dark slate section cards
COL_CARD_INNER  = (0x1C, 0x1D, 0x21, 255)  # #1C1D21 inset flutter/wow mini-scope
COL_DIVIDER     = (0x59, 0x5C, 0x6B, 255)  # #595C6B tab outline & unfilled slider/knob track
COL_TEAL        = (0x9C, 0xBC, 0xBD, 255)  # #9CBCBD active value arc & slider track
COL_FOOTER_BG   = (0x24, 0x26, 0x2D, 255)  # #24262D bottom bar
COL_PRESET_BOX  = (0x7E, 0x82, 0x8C, 255)  # #7E828C preset combo box
COL_TEXT_WHITE  = (0xFF, 0xFF, 0xFF, 255)  # #FFFFFF primary text & pointer
COL_TEXT_MUTED  = (0x94, 0x97, 0xA3, 255)  # #9497A3 inactive card tab title
COL_KNOB_LIGHT  = (0xB5, 0xB5, 0xBF, 255)  # #B5B5BF knob.svg gradient start
COL_KNOB_DARK   = (0x60, 0x60, 0x68, 255)  # #606068 knob.svg gradient stop


def get_font(bold=True, size=20):
    path = FONT_BOLD_PATH if bold else FONT_REG_PATH
    if os.path.isfile(path):
        return ImageFont.truetype(path, int(round(size)))
    return ImageFont.load_default()


_DUMMY_DRAW = ImageDraw.Draw(Image.new("RGBA", (16, 16)))


def fit_group_font(labels, max_w_px, max_h_px, start_px=16.0, min_px=10.0, bold=True, ss=4):
    """
    Finds the largest font size <= start_px * ss such that EVERY label in `labels`
    fits strictly within (max_w_px * ss, max_h_px * ss).
    """
    cur = int(round(start_px * ss))
    floor_sz = max(8, int(round(min_px * ss)))
    limit_w = max_w_px * ss
    limit_h = max_h_px * ss
    while cur > floor_sz:
        f = get_font(bold=bold, size=cur)
        fits_all = True
        for lbl in labels:
            bb = _DUMMY_DRAW.textbbox((0, 0), lbl, font=f)
            if (bb[2] - bb[0]) > limit_w or (bb[3] - bb[1]) > limit_h:
                fits_all = False
                break
        if fits_all:
            return f
        cur -= 1
    return get_font(bold=bold, size=floor_sz)


def draw_pixel_perfect_centered_text(d, box_x0, box_y0, box_x1, box_y1, text, font, fill):
    """
    Centers `text` horizontally using its exact glyph bounding box, and vertically
    using the font's cap-height reference ("HKX0") so all buttons in a row share
    an identical baseline and optical vertical center.
    """
    bbox = d.textbbox((0, 0), text, font=font)
    ref = d.textbbox((0, 0), "HKX0", font=font)
    cx = (box_x0 + box_x1) / 2.0
    cy = (box_y0 + box_y1) / 2.0
    tx = cx - (bbox[0] + bbox[2]) / 2.0
    ty = cy - (ref[1] + ref[3]) / 2.0
    d.text((tx, ty), text, font=font, fill=fill)


def lerp_color(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(round(c1[i] + (c2[i] - c1[i]) * t)) for i in range(4))


# ==============================================================================
# 1. Original CHOW Tape Icons & Interactive Power / Sub-Tab Buttons
# ==============================================================================
def render_power_button(w=48, h=32, active=True):
    """Render interactive CHOW Tape powerswitch.svg button (gold when ON, muted grey when OFF)."""
    ss = 4
    W, H = w * ss, h * ss
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    mcx, mcy = W / 2.0, H / 2.0
    r = 10.0 * ss
    stroke = max(2, int(2.3 * ss))
    color = COL_GOLD if active else COL_DIVIDER
    if active:
        # Subtle warm glow behind active power switch
        glow_r = 13.5 * ss
        d.ellipse([mcx - glow_r, mcy - glow_r, mcx + glow_r, mcy + glow_r], fill=(0xEA, 0xA9, 0x2C, 38))
    bbox = [mcx - r, mcy - r, mcx + r, mcy + r]
    d.arc(bbox, start=-55, end=235, fill=color, width=stroke)
    d.line([(mcx, mcy - r - 1.5 * ss), (mcx, mcy - 0.1 * r)], fill=color, width=stroke)
    return img.resize((w, h), Image.Resampling.LANCZOS)


def render_subtab_button(label, w, h=34, active=True, group_labels=None):
    """Render an interactive card header tab button matching CHOW Tape's TabbedComponent."""
    ss = 4
    W, H = w * ss, h * ss
    img = Image.new("RGBA", (W, H), COL_CARD_BG)
    d = ImageDraw.Draw(img)
    all_labels = group_labels if group_labels else [label]
    font = fit_group_font(all_labels, max_w_px=w - 12, max_h_px=h - 12, start_px=19.0, min_px=12.0, bold=True, ss=ss)
    col = COL_TEXT_WHITE if active else COL_TEXT_MUTED
    draw_pixel_perfect_centered_text(d, 4 * ss, 2 * ss, W - 4 * ss, H - 5 * ss, label, font, col)
    if active:
        # Highlight underline in CHOW gold
        pad = 10 * ss
        d.rounded_rectangle([pad, H - 3 * ss, W - pad, H - 1 * ss], radius=ss, fill=COL_GOLD)
    return img.resize((w, h), Image.Resampling.LANCZOS)


def render_pill_toggle(label, w=160, h=38, active=True):
    """Render an interactive toggle pill (e.g. Filter Makeup) with pixel-perfect fitted text."""
    ss = 4
    W, H = w * ss, h * ss
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill_col = (0x3F, 0x45, 0x54, 255) if active else (0x26, 0x28, 0x30, 255)
    border_col = COL_GOLD if active else (0x68, 0x6C, 0x7A, 255)
    d.rounded_rectangle(
        [2 * ss, 2 * ss, W - 2 * ss - 1, H - 2 * ss - 1],
        radius=8 * ss,
        fill=fill_col,
        outline=border_col,
        width=int(1.8 * ss),
    )
    # Indicator dot + pixel-perfect fitted label
    dot_r = 4.2 * ss
    dot_x = 18 * ss
    dot_y = H / 2.0
    dot_col = COL_GOLD if active else COL_DIVIDER
    d.ellipse([dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r], fill=dot_col)
    txt_on = f"{label}: ON"
    txt_off = f"{label}: OFF"
    txt = txt_on if active else txt_off
    text_box_x0 = dot_x + 8 * ss
    text_box_x1 = W - 8 * ss
    avail_w = (text_box_x1 - text_box_x0) / float(ss) - 6
    font = fit_group_font([txt_on, txt_off], max_w_px=avail_w, max_h_px=h - 12, start_px=16.5, min_px=11.0, bold=True, ss=ss)
    t_col = COL_TEXT_WHITE if active else (0xC8, 0xCC, 0xD8, 255)
    draw_pixel_perfect_centered_text(d, text_box_x0, 3 * ss, text_box_x1, H - 3 * ss, txt, font, t_col)
    return img.resize((w, h), Image.Resampling.LANCZOS)


def draw_cog_icon(canvas, cx, cy, radius=11, color=COL_TEXT_WHITE):
    """Render CHOW Tape's cog-solid.svg icon in the bottom-right footer."""
    ss = 4
    size = int((radius * 2 + 8) * ss)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    mc = size / 2.0
    r_outer = radius * ss
    r_body = radius * 0.76 * ss
    r_hole = radius * 0.34 * ss
    for i in range(8):
        ang = i * (math.pi / 4.0)
        dx, dy = math.cos(ang), math.sin(ang)
        px, py = -dy, dx
        tw = radius * 0.22 * ss
        pts = [
            (mc + dx * (r_body - 2) - px * tw, mc + dy * (r_body - 2) - py * tw),
            (mc + dx * r_outer - px * (tw * 0.75), mc + dy * r_outer - py * (tw * 0.75)),
            (mc + dx * r_outer + px * (tw * 0.75), mc + dy * r_outer + py * (tw * 0.75)),
            (mc + dx * (r_body - 2) + px * tw, mc + dy * (r_body - 2) + py * tw),
        ]
        d.polygon(pts, fill=color)
    d.ellipse([mc - r_body, mc - r_body, mc + r_body, mc + r_body], fill=color)
    d.ellipse([mc - r_hole, mc - r_hole, mc + r_hole, mc + r_hole], fill=COL_FOOTER_BG)
    icon = img.resize((size // ss, size // ss), Image.Resampling.LANCZOS)
    canvas.alpha_composite(icon, (int(cx - icon.width // 2), int(cy - icon.height // 2)))


# ==============================================================================
# 2. Live TapeScope / VU Meter Film-Strip & Live LFO Scope Film-Strip
# ==============================================================================
def render_scope_vu_strip(width=716, height=48, frames=65):
    """
    Renders a 65-frame live TapeScope + Horizontal VU Bar film-strip for the top-right box:
      - Frame 0 (-60 dB silence): flat golden center line + empty VU meter bar
      - Frames 1..64 (-59..+6 dB): animated golden tape hysteresis wave (#EAA92C) + glowing VU meter bar!
    """
    ss = 2
    W, H = width * ss, height * ss
    strip = Image.new("RGBA", (width, height * frames), (0, 0, 0, 0))

    for idx in range(frames):
        t = idx / float(frames - 1)  # 0.0 (-60 dB) .. 1.0 (+6 dB)
        frame_ss = Image.new("RGBA", (W, H), COL_SCOPE_BG)
        d = ImageDraw.Draw(frame_ss)

        # Subtle center zero-line
        cy = int(H * 0.42)
        d.line([(4 * ss, cy), (W - 4 * ss, cy)], fill=(0x82, 0x30, 0x30, 180), width=ss)

        # Animated golden tape wave (#EAA92C)
        amp = (H * 0.36) * (t ** 0.75)
        phase = idx * 0.38
        drive_shape = 1.0 + 2.4 * t
        pts = []
        for x in range(4 * ss, W - 4 * ss, 2 * ss):
            u = (x - 4 * ss) / float(W - 8 * ss)
            win = math.sin(u * math.pi) ** 0.35
            raw = math.sin(u * 2.0 * math.pi * 2.5 + phase) + 0.25 * math.sin(u * 2.0 * math.pi * 5.0 - phase * 0.5)
            sat = math.tanh(raw * drive_shape) / math.tanh(drive_shape)
            wy = cy - amp * win * sat
            pts.append((x, wy))
        if len(pts) >= 2:
            if t > 0.02:
                d.line(pts, fill=(0xEA, 0xA9, 0x2C, 90), width=4 * ss)
            d.line(pts, fill=COL_GOLD, width=2 * ss)

        # Bottom Horizontal VU Level Bar (with 0 dB marker at t = 60/66 = 0.909)
        bar_y0 = H - 8 * ss
        bar_y1 = H - 3 * ss
        bar_x0 = 6 * ss
        bar_x1 = W - 6 * ss
        d.rounded_rectangle([bar_x0, bar_y0, bar_x1, bar_y1], radius=2 * ss, fill=(0x46, 0x15, 0x15, 255))
        fill_w = int(round(t * (bar_x1 - bar_x0)))
        if fill_w > 2 * ss:
            bar_col = (0xFF, 0x55, 0x44, 255) if t > 0.91 else COL_GOLD
            d.rounded_rectangle([bar_x0, bar_y0, bar_x0 + fill_w, bar_y1], radius=2 * ss, fill=bar_col)
        # 0 dB tick mark
        zero_x = int(round(bar_x0 + (60.0 / 66.0) * (bar_x1 - bar_x0)))
        d.line([(zero_x, bar_y0 - 2 * ss), (zero_x, bar_y1 + 2 * ss)], fill=COL_TEXT_WHITE, width=ss)

        frame = frame_ss.resize((width, height), Image.Resampling.LANCZOS)
        strip.paste(frame, (0, idx * height))

    return strip


def render_lfo_scope_strip(width=243, height=74, frames=65):
    """
    Renders a 65-frame live Flutter/Wow LFO Scope film-strip for Card 4 (#1C1D21 inset box):
      - Animates the teal LFO waveform phase & excursion based on Parameter 51 (kMeterLFO).
    """
    ss = 2
    W, H = width * ss, height * ss
    strip = Image.new("RGBA", (width, height * frames), (0, 0, 0, 0))

    for idx in range(frames):
        t = idx / float(frames - 1)  # 0..1 LFO excursion
        bipolar = (t - 0.5) * 2.0    # -1..+1
        frame_ss = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(frame_ss)
        d.rounded_rectangle([0, 0, W - 1, H - 1], radius=5 * ss, fill=COL_CARD_INNER)

        cy = H / 2.0
        d.line([(6 * ss, cy), (W - 6 * ss, cy)], fill=(0x30, 0x33, 0x3C, 255), width=ss)

        amp = (H * 0.34) * (0.35 + 0.65 * abs(bipolar))
        phase = t * 2.0 * math.pi * 2.0
        pts = []
        for x in range(6 * ss, W - 6 * ss, 2 * ss):
            u = (x - 6 * ss) / float(W - 12 * ss)
            wy = cy - amp * (0.70 * math.sin(u * 2.0 * math.pi * 1.75 + phase) + 0.30 * math.cos(u * 2.0 * math.pi * 4.2 - phase))
            pts.append((x, wy))
        if len(pts) >= 2:
            d.line(pts, fill=COL_TEAL, width=2 * ss)

        # Glowing current LFO playhead dot on the right
        dot_x = W - 14 * ss
        dot_y = cy - bipolar * (H * 0.34)
        r = 3.5 * ss
        d.ellipse([dot_x - r, dot_y - r, dot_x + r, dot_y + r], fill=COL_GOLD)

        frame = frame_ss.resize((width, height), Image.Resampling.LANCZOS)
        strip.paste(frame, (0, idx * height))

    return strip


# ==============================================================================
# 3. Original CHOW Tape Rotary Knob & Horizontal Slider Film-Strips
# ==============================================================================
def make_metallic_disc(diam):
    img = Image.new("RGBA", (diam, diam), (0, 0, 0, 0))
    px = img.load()
    r = diam / 2.0
    cx = cy = r
    r_inner = r - 1.5
    r_rim = r - 3.2
    for y in range(diam):
        for x in range(diam):
            dx = (x + 0.5) - cx
            dy = (y + 0.5) - cy
            dist = math.hypot(dx, dy)
            if dist > r_inner + 0.7:
                continue
            t = ((dx + dy) / (math.sqrt(2.0) * r_inner) + 1.0) * 0.5
            body_col = lerp_color(COL_KNOB_LIGHT, COL_KNOB_DARK, t)
            rim_col = lerp_color((245, 245, 250, 255), (56, 56, 68, 255), t)
            if dist > r_rim:
                rt = max(0.0, min(1.0, (dist - r_rim) / (r_inner - r_rim)))
                col = lerp_color(body_col, rim_col, rt * 0.65)
            else:
                col = body_col
            cov = max(0.0, min(1.0, (r_inner + 0.5) - dist))
            px[x, y] = (col[0], col[1], col[2], int(round(255 * cov)))
    return img


def render_chow_knob_strip(size=92, frames=127, bipolar=False):
    ss = 3
    S = size * ss
    cx = cy = S / 2.0
    arc_outer = S * 0.46
    arc_width = max(4, int(round(S * 0.052)))
    cap_diam = int(round(S * 0.68))
    cap_img = make_metallic_disc(cap_diam)

    strip = Image.new("RGBA", (size, size * frames), (0, 0, 0, 0))
    ang_min_pil = 120.0
    ang_max_pil = 420.0
    ang_mid_pil = 270.0

    for idx in range(frames):
        t = idx / float(frames - 1)
        frame_ss = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(frame_ss)

        bbox = [cx - arc_outer, cy - arc_outer, cx + arc_outer, cy + arc_outer]
        d.arc(bbox, start=ang_min_pil, end=ang_max_pil, fill=COL_DIVIDER, width=arc_width)

        cur_pil = ang_min_pil + t * (ang_max_pil - ang_min_pil)
        if bipolar:
            a0, a1 = min(ang_mid_pil, cur_pil), max(ang_mid_pil, cur_pil)
            if a1 - a0 > 1.2:
                d.arc(bbox, start=a0, end=a1, fill=COL_TEAL, width=arc_width)
        else:
            if cur_pil - ang_min_pil > 1.2:
                d.arc(bbox, start=ang_min_pil, end=cur_pil, fill=COL_TEAL, width=arc_width)

        cap_off = int(round(cx - cap_diam / 2.0))
        frame_ss.alpha_composite(cap_img, (cap_off, cap_off))

        theta = math.radians(-150.0 + t * 300.0)
        sin_t, cos_t = math.sin(theta), -math.cos(theta)
        r_ptr = (cap_diam / 2.0) - 2.0 * ss
        pw = max(3, int(round(S * 0.042)))
        x1 = cx + sin_t * r_ptr
        y1 = cy + cos_t * r_ptr
        d.line([(cx, cy), (x1, y1)], fill=COL_TEXT_WHITE, width=pw)
        d.ellipse([cx - pw / 2.0, cy - pw / 2.0, cx + pw / 2.0, cy + pw / 2.0], fill=COL_TEXT_WHITE)

        frame = frame_ss.resize((size, size), Image.Resampling.LANCZOS)
        strip.paste(frame, (0, idx * size))

    return strip


def render_chow_slider_strip(width=252, height=24, frames=127):
    ss = 3
    W, H = width * ss, height * ss
    thumb_d = int(round(height * 0.74 * ss))
    thumb_img = make_metallic_disc(thumb_d)
    track_h = max(4, int(round(height * 0.24 * ss)))
    cy = H // 2
    x_min = thumb_d // 2 + 3 * ss
    x_max = W - thumb_d // 2 - 3 * ss

    strip = Image.new("RGBA", (width, height * frames), (0, 0, 0, 0))
    for idx in range(frames):
        t = idx / float(frames - 1)
        frame_ss = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(frame_ss)

        d.rounded_rectangle(
            [x_min, cy - track_h // 2, x_max, cy + track_h // 2],
            radius=track_h // 2,
            fill=COL_DIVIDER,
        )
        kx = int(round(x_min + t * (x_max - x_min)))
        if kx > x_min + 2:
            d.rounded_rectangle(
                [x_min, cy - track_h // 2, kx, cy + track_h // 2],
                radius=track_h // 2,
                fill=COL_TEAL,
            )
        frame_ss.alpha_composite(thumb_img, (kx - thumb_d // 2, cy - thumb_d // 2))

        frame = frame_ss.resize((width, height), Image.Resampling.LANCZOS)
        strip.paste(frame, (0, idx * height))

    return strip


def render_chow_seg_button(w=58, h=30, active=False, label=None, group_labels=None):
    ss = 4
    W, H = w * ss, h * ss
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill_col = (0x3F, 0x45, 0x54, 255) if active else (0x26, 0x28, 0x30, 255)
    border_col = COL_GOLD if active else (0x68, 0x6C, 0x7A, 255)
    d.rounded_rectangle(
        [2 * ss, 2 * ss, W - 2 * ss - 1, H - 2 * ss - 1],
        radius=7 * ss,
        fill=fill_col,
        outline=border_col,
        width=int(1.7 * ss) if active else int(1.4 * ss),
    )
    if label:
        all_lbls = group_labels if group_labels else [label]
        start_sz = 16.0 if h <= 30 else 17.0
        font = fit_group_font(all_lbls, max_w_px=w - 10, max_h_px=h - 10, start_px=start_sz, min_px=10.0, bold=True, ss=ss)
        t_col = COL_TEXT_WHITE if active else (0xC8, 0xCC, 0xD8, 255)
        draw_pixel_perfect_centered_text(d, 4 * ss, 3 * ss, W - 4 * ss, H - 3 * ss, label, font, t_col)
    return img.resize((w, h), Image.Resampling.LANCZOS)


# ==============================================================================
# 4. CHOW Tape 1280x628 Background Canvas Renderer
# ==============================================================================
CARD_BOUNDS = [
    (18,  92, 313, 578),  # Card 1 (width 295)
    (334, 92, 629, 578),  # Card 2 (width 295)
    (650, 92, 945, 578),  # Card 3 (width 295)
    (966, 92, 1261, 578), # Card 4 (width 295)
]


def render_chow_background(tab_subtitle="MAIN DECK", static_headers=None):
    W, H = 1280, 628
    bg = Image.new("RGBA", (W, H), COL_BG_TOP)
    px = bg.load()
    for y in range(H):
        ty = y / float(H - 1)
        for x in range(W):
            tx = x / float(W - 1)
            t = 0.55 * ty + 0.45 * tx
            px[x, y] = lerp_color(COL_BG_TOP, COL_BG_BOT, t)

    d = ImageDraw.Draw(bg)

    # Top Left Title & Subtitle
    if os.path.isfile(LOGO_PATH):
        logo = Image.open(LOGO_PATH).convert("RGBA").resize((42, 42), Image.Resampling.LANCZOS)
        bg.alpha_composite(logo, (20, 12))
        title_x = 70
    else:
        title_x = 22

    f_title = get_font(bold=True, size=40)
    f_sub_reg = get_font(bold=False, size=17)
    f_sub_bold = get_font(bold=True, size=17)

    d.text((title_x, 8), "Chow Tape Model", font=f_title, fill=COL_TEXT_WHITE)

    sx, sy = 22, 60
    for txt, fnt, col in [
        ("MPC-ARM, Standalone, ", f_sub_reg, COL_GOLD),
        ("v2.10.0 ", f_sub_bold, COL_TEXT_WHITE),
        ("~ DSP by ", f_sub_reg, COL_GOLD),
        ("chowdsp ", f_sub_bold, COL_TEXT_WHITE),
        ("~ ported by ", f_sub_reg, COL_GOLD),
        ("daki", f_sub_bold, COL_TEXT_WHITE),
    ]:
        d.text((sx, sy), txt, font=fnt, fill=col)
        sx += int(d.textlength(txt, font=fnt))

    # Top Right TapeScope Box (#672121) — live wave & IN/OUT dB widgets sit inside this box!
    scope_rect = [530, 8, 1261, 82]
    d.rounded_rectangle(scope_rect, radius=6, fill=COL_SCOPE_BG, outline=COL_SCOPE_LINE, width=1)

    # 4 Dark Slate Section Cards (#30323A)
    f_tab = get_font(bold=True, size=19)
    for ci, (x0, y0, x1, y1) in enumerate(CARD_BOUNDS):
        d.rounded_rectangle([x0, y0, x1, y1], radius=6, fill=COL_CARD_BG)
        if static_headers and ci < len(static_headers):
            tabs = static_headers[ci]
            slot_w = (x1 - x0) / float(len(tabs))
            for ti, (t_label, is_active) in enumerate(tabs):
                col = COL_TEXT_WHITE if is_active else COL_TEXT_MUTED
                draw_pixel_perfect_centered_text(
                    d,
                    x0 + ti * slot_w + 4,
                    y0 + 3,
                    x0 + (ti + 1) * slot_w - 4,
                    y0 + 30,
                    t_label,
                    f_tab,
                    col,
                )
                if is_active:
                    d.rounded_rectangle([x0 + ti * slot_w + 10, y0 + 33, x0 + (ti + 1) * slot_w - 10, y0 + 35], radius=1, fill=COL_GOLD)
        # Horizontal divider line under card tabs (#595C6B)
        d.line([(x0, y0 + 36), (x1, y0 + 36)], fill=COL_DIVIDER, width=1)

    # Bottom Footer Bar (#24262D) — clean, with interactive Oversampling & Hysteresis Mode selectors
    d.rectangle([0, 586, W, H], fill=COL_FOOTER_BG)
    f_foot = get_font(bold=True, size=18)
    ref_foot = d.textbbox((0, 0), "HKX0", font=f_foot)
    foot_ty = 607.0 - (ref_foot[1] + ref_foot[3]) / 2.0
    d.text((26, foot_ty), "Oversampling:", font=f_foot, fill=COL_TEXT_WHITE)
    d.text((380, foot_ty), "Hysteresis Mode:", font=f_foot, fill=COL_TEXT_WHITE)

    return bg


# ==============================================================================
# 5. Ordered JSON Writer (Preserves Duplicate "version" Keys Required by MPC)
# ==============================================================================
class Obj(list):
    pass


def obj(*pairs):
    return Obj(pairs)


def dump_json(value, indent=0):
    pad = " " * indent
    inner_pad = " " * (indent + 4)
    if isinstance(value, Obj):
        if not value:
            return "{}"
        parts = [inner_pad + json.dumps(str(k)) + ": " + dump_json(v, indent + 4) for k, v in value]
        return "{\n" + ",\n".join(parts) + "\n" + pad + "}"
    if isinstance(value, dict):
        return dump_json(Obj(value.items()), indent)
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        parts = [inner_pad + dump_json(v, indent + 4) for v in value]
        return "[\n" + ",\n".join(parts) + "\n" + pad + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


# ==============================================================================
# 6. TUI.json Component Definitions (Knobs, Sliders, Sub-Tabs, Scopes, Power)
# ==============================================================================
FONT_NAME = "Titillium Web"
QLINK_BOUNDS = ["18 92 295 486", "334 92 295 486", "650 92 295 486", "966 92 295 486"]


def action(on_action, handler, handle_name, additional=""):
    return obj(("version", 1), ("onAction", on_action), ("handler", handler),
               ("handleName", handle_name), ("additionalData", additional))


def background_data(colour):
    def side():
        return obj(("version", 1), ("colour", colour), ("image", ""))
    return obj(("version", 1), ("focussed", side()), ("unfocussed", side()))


def component_definition(actions, children, bg_colour, ignore_mouse=False, hide_qlink=False):
    pairs = [
        ("version", 2),
        ("actions", actions),
        ("backgroundData", background_data(bg_colour)),
        ("ignoreMousePresses", ignore_mouse),
        ("disableCoarseDataWheel", False),
    ]
    if hide_qlink:
        pairs.append(("hideQLinkBounds", True))
    pairs.append(("componentsData", children))
    return Obj(pairs)


def entry(name, type_name, data, bounds, accepts_focus, show_invalid, when_visible, remap=(), invalidating=None):
    if invalidating is not None:
        bounds_obj = obj(
            ("version", 2),
            ("acceptsHWFocus", accepts_focus),
            ("showWhenDataModelInvalid", show_invalid),
            ("whenVisible", when_visible),
            ("boundsType", "Absolute"),
            ("bounds", bounds),
            ("additionalInvalidatingHandles", list(invalidating)),
        )
    else:
        bounds_obj = obj(
            ("version", 1),
            ("acceptsHWFocus", accepts_focus),
            ("showWhenDataModelInvalid", show_invalid),
            ("whenVisible", when_visible),
            ("boundsType", "Absolute"),
            ("bounds", bounds),
        )
    return obj(
        ("version", 2),
        ("componentData", obj(("version", 1), ("name", name), ("type", type_name), ("data", data))),
        ("handle remapping", obj(("version", 1), ("map", [obj(("key", k), ("value", v)) for k, v in remap]))),
        ("bounds", bounds_obj),
    )


def label_data(label_type, colour, height=20.0, justification="horizontallyCentred verticallyCentred", text=None, style="SemiBold"):
    pairs = [
        ("version", 1),
        ("textStyle", obj(
            ("version", 1),
            ("font", obj(("version", 1), ("name", FONT_NAME), ("style", style), ("height", float(height)))),
            ("colour", colour),
            ("justification", justification),
            ("case", "Original"),
        )),
        ("type", label_type),
        ("handleName", "Data"),
    ]
    if text is not None:
        pairs.append(("text", text))
    return Obj(pairs)


def focus_data(w, h, accent="ff9cbcbd"):
    return entry(
        "focus", "Focus",
        obj(("version", 1), ("backgroundColour", "149cbcbd"), ("outlineColour", accent),
            ("backgroundInset", 2.0), ("outlineThickness", 1.5)),
        "0 0 %d %d" % (w, h), "No", "Show", "WhenFocussed",
    )


def control_actions(overlay_name, double_click=True):
    acts = [action("Mouse Down", "Q-Link", "Data", "")]
    if double_click and overlay_name:
        acts.append(action("Double Click", "Show Overlay", "Data", overlay_name))
    if overlay_name:
        acts.append(action("Enter Pressed", "Show Overlay", "Data", overlay_name))
    return acts


def chow_vertical_knob_def(image_file, bipolar=False, h=134):
    """Vertical CHOW Tape Model knob slot (136 x h)."""
    overlay = "knob overlay bipolar" if bipolar else "knob overlay"
    val_y = h - 22
    children = [
        focus_data(136, h),
        entry("name", "Label", label_data("Name", "ffffffff", 17.5, "horizontallyCentred verticallyCentred"), "0 0 136 20", "No", "Show", "Always"),
        entry("knob", "Knob", obj(("version", 2), ("knobType", "FilmStrip"), ("filmStrip", image_file), ("numFrames", 127), ("invert", False), ("handleName", "Data")), "22 18 92 92", "No", "Show", "Always"),
        entry("value", "Label", label_data("Value", "ffffffff", 19.0, "horizontallyCentred verticallyCentred"), "0 %d 136 22" % val_y, "No", "Show", "Always"),
    ]
    return component_definition(control_actions(overlay), children, "00000000")


def chow_horizontal_slider_def(image_file, h=64):
    """Horizontal CHOW Tape slider slot (265 x h)."""
    fader_y = 20 if h <= 64 else 22
    val_y = 44 if h <= 64 else 48
    children = [
        focus_data(265, h),
        entry("name", "Label", label_data("Name", "ffffffff", 17.0, "left"), "6 0 250 20", "No", "Show", "Always"),
        entry("fader", "Knob", obj(("version", 3), ("knobType", "FilmStrip"), ("filmStrip", image_file), ("numFrames", 127), ("invert", False), ("dragOrientation", "Horizontal"), ("handleName", "Data")), "6 %d 252 24" % fader_y, "No", "Show", "Always"),
        entry("value", "Label", label_data("Value", "ffffffff", 16.5, "left"), "14 %d 200 20" % val_y, "No", "Show", "Always"),
    ]
    return component_definition(control_actions("knob overlay"), children, "00000000")


def chow_labeled_radio_def(prefix, n, container_w, container_h, xs, btn_w, btn_h):
    """
    Segmented pill switch where each button has its own pixel-perfect labeled
    on/off PNGs (<prefix>_<i>_on.png / <prefix>_<i>_off.png).
    """
    children = [focus_data(container_w, container_h)]
    for i, x in enumerate(xs):
        on_img = "%s_%d_on.png" % (prefix, i)
        off_img = "%s_%d_off.png" % (prefix, i)
        children.append(entry(
            "seg %d" % i, "Button",
            obj(("version", 2), ("onImage", on_img), ("offImage", off_img),
                ("buttonId", i), ("numButtonsInGroup", n), ("handleName", "Data"), ("gestureBehaviour", "Instant")),
            "%d 2 %d %d" % (x, btn_w, btn_h), "No", "Show", "Always",
        ))
    return component_definition(control_actions("menu overlay", double_click=False), children, "00000000")


def chow_subtab_bar_def(card_idx, labels):
    """Interactive sub-tab header bar at the top of a card (291 x 34)."""
    n = len(labels)
    slot_w = 291 // n
    children = []
    for i in range(n):
        on_img = "c%d_tab_%d_on.png" % (card_idx, i)
        off_img = "c%d_tab_%d_off.png" % (card_idx, i)
        children.append(entry(
            "tab %d" % i, "Button",
            obj(("version", 2), ("onImage", on_img), ("offImage", off_img),
                ("buttonId", i), ("numButtonsInGroup", n), ("handleName", "Data"), ("gestureBehaviour", "Instant")),
            "%d 0 %d 34" % (i * slot_w, slot_w), "No", "Show", "Always",
        ))
    return component_definition([], children, "00000000", hide_qlink=True)


def chow_power_btn_def():
    """Interactive Gold Power Switch button at the bottom of a card (48 x 32)."""
    children = [
        entry(
            "power", "Button",
            obj(("version", 2), ("onImage", "power_on.png"), ("offImage", "power_off.png"),
                ("buttonId", 1), ("numButtonsInGroup", 1), ("handleName", "Data"), ("gestureBehaviour", "Instant")),
            "0 0 48 32", "No", "Show", "Always",
        )
    ]
    return component_definition([], children, "00000000", hide_qlink=True)


def chow_makeup_btn_def():
    """Interactive Filter Makeup toggle button (160 x 38)."""
    children = [
        focus_data(160, 38),
        entry(
            "makeup", "Button",
            obj(("version", 2), ("onImage", "makeup_on.png"), ("offImage", "makeup_off.png"),
                ("buttonId", 1), ("numButtonsInGroup", 1), ("handleName", "Data"), ("gestureBehaviour", "Instant")),
            "0 0 160 38", "No", "Show", "Always",
        ),
    ]
    return component_definition(control_actions(None, double_click=False), children, "00000000")


def chow_meter_strip_def(film_strip, w, h, frames=65):
    """Read-only animated FilmStrip scope/meter widget."""
    children = [
        entry(
            "Meter", "Knob",
            obj(("version", 5), ("knobType", "FilmStrip"), ("filmStrip", film_strip),
                ("numFrames", frames), ("invert", False), ("dragOrientation", "Vertical"), ("handleName", "Data")),
            "0 0 %d %d" % (w, h), "No", "Show", "Always", invalidating=[],
        )
    ]
    return component_definition([], children, "00000000", ignore_mouse=True, hide_qlink=True)


def chow_readout_def(w, h, align="left"):
    """Read-only live dB Value readout widget."""
    children = [
        entry(
            "Readout", "Label",
            label_data("Value", "fff2e8e8", 17.5, align),
            "0 0 %d %d" % (w, h), "No", "Show", "Always", invalidating=[],
        )
    ]
    return component_definition([], children, "00000000", ignore_mouse=True, hide_qlink=True)


# ==============================================================================
# 7. Build Interactive Tabs
# ==============================================================================
def add_control_with_labels(children, item):
    p_handle = "Parameter %d" % item["param_idx"]
    inv = item.get("invalidating")
    accepts = item.get("accepts_focus", "Yes")
    children.append(entry(
        item["param_name"], item["def"],
        obj(("version", 1), ("handleName", "Data")),
        item["bounds"], accepts, "Hide", "Always",
        remap=[("Data", p_handle)],
        invalidating=inv,
    ))


def build_tab(tab_name, tab_idx, bg_image, placements, include_interactive_headers=True):
    children = [
        entry(
            "Background", "Image",
            obj(("version", 2), ("imageType", "Regular"), ("colour", "0"), ("image", bg_image)),
            "0 0 1280 628", "No", "Show", "Always",
        ),
        # Live Top-Right TapeScope Golden Waveform & VU Meter Bar (Parameter 50 = kMeterOut)
        entry(
            "TapeScopeWave", "scopeWave",
            obj(("version", 1), ("handleName", "Data")),
            "538 30 716 48", "No", "Show", "Always",
            remap=[("Data", "Parameter 50")],
            invalidating=[],
        ),
        # Live IN: ... dB Readout (Parameter 49 = kMeterIn)
        entry(
            "ScopeInReadout", "scopeReadoutL",
            obj(("version", 1), ("handleName", "Data")),
            "542 10 220 20", "No", "Show", "Always",
            remap=[("Data", "Parameter 49")],
            invalidating=[],
        ),
        # Live OUT: ... dB Readout (Parameter 50 = kMeterOut)
        entry(
            "ScopeOutReadout", "scopeReadoutR",
            obj(("version", 1), ("handleName", "Data")),
            "1030 10 220 20", "No", "Show", "Always",
            remap=[("Data", "Parameter 50")],
            invalidating=[],
        ),
        # Live Card 4 Flutter/Wow LFO Scope (Parameter 51 = kMeterLFO)
        entry(
            "FlutterLFOScope", "lfoScope",
            obj(("version", 1), ("handleName", "Data")),
            "992 456 243 74", "No", "Show", "Always",
            remap=[("Data", "Parameter 51")],
            invalidating=[],
        ),
    ]

    if include_interactive_headers:
        # Interactive Sub-Tab Headers at top of each of the 4 Cards (Parameters 45..48)
        card_tab_configs = [
            (1, 45, "20 93 291 34"),
            (2, 46, "336 93 291 34"),
            (3, 47, "652 93 291 34"),
            (4, 48, "968 93 291 34"),
        ]
        for c_idx, p_idx, b_str in card_tab_configs:
            children.append(entry(
                "Card%dTabs" % c_idx, "card%dTabs" % c_idx,
                obj(("version", 1), ("handleName", "Data")),
                b_str, "No", "Show", "Always",
                remap=[("Data", "Parameter %d" % p_idx)],
                invalidating=[],
            ))

    for item in placements:
        add_control_with_labels(children, item)

    # Interactive Bottom Footer Selectors: Oversampling (1x/2x/4x) & Hysteresis Mode (RK2/RK4/NR4/NR8)
    add_control_with_labels(children, {
        "param_idx": 52, "param_name": "FooterOversampling", "def": "oversamplingRadio",
        "bounds": "152 590 198 34", "accepts_focus": "No",
    })
    add_control_with_labels(children, {
        "param_idx": 13, "param_name": "FooterHysteresis", "def": "hysteresisRadio",
        "bounds": "532 590 265 34", "accepts_focus": "No",
    })

    comp = component_definition([], children, "ff7d2424", hide_qlink=True)
    return obj(("version", 1), ("tabName", tab_name), ("fnKeyIndex", tab_idx), ("fnKeySubIndex", 0),
               ("componentDefinition", comp), ("qlinkBoundsData", list(QLINK_BOUNDS)))


def main():
    os.makedirs(SKINS_DIR, exist_ok=True)
    print("[CHOW Skin] Rendering interactive CHOW Tape Model film-strips, scopes & pixel-perfect labeled buttons...")

    # 1. Knobs & Sliders
    knob_uni = render_chow_knob_strip(size=92, frames=127, bipolar=False)
    knob_bi  = render_chow_knob_strip(size=92, frames=127, bipolar=True)
    fader_uni = render_chow_slider_strip(width=252, height=24, frames=127)

    knob_uni.save(os.path.join(SKINS_DIR, "knob_uni.png"))
    knob_bi.save(os.path.join(SKINS_DIR, "knob_bi.png"))
    fader_uni.save(os.path.join(SKINS_DIR, "fader_h_uni.png"))

    # 1b. Pixel-Perfect Labeled Segmented Radio Button Groups
    radio_groups_spec = [
        ("os",   ["1x", "2x", "4x"],          58,  30),
        ("hyst", ["RK2", "RK4", "NR4", "NR8"],58,  30),
        ("spd",  ["3.75", "7.5", "15", "30"], 58,  30),
        ("st",   ["Stereo", "Mid/Side"],      115, 34),
    ]
    for prefix, choices, bw, bh in radio_groups_spec:
        for i, ch_lbl in enumerate(choices):
            render_chow_seg_button(w=bw, h=bh, active=True,  label=ch_lbl, group_labels=choices).save(
                os.path.join(SKINS_DIR, "%s_%d_on.png" % (prefix, i))
            )
            render_chow_seg_button(w=bw, h=bh, active=False, label=ch_lbl, group_labels=choices).save(
                os.path.join(SKINS_DIR, "%s_%d_off.png" % (prefix, i))
            )

    # 2. Power Buttons & Makeup Toggle Pill
    render_power_button(48, 32, True).save(os.path.join(SKINS_DIR, "power_on.png"))
    render_power_button(48, 32, False).save(os.path.join(SKINS_DIR, "power_off.png"))
    render_pill_toggle("Makeup", 160, 38, True).save(os.path.join(SKINS_DIR, "makeup_on.png"))
    render_pill_toggle("Makeup", 160, 38, False).save(os.path.join(SKINS_DIR, "makeup_off.png"))

    # 3. Interactive Card Sub-Tab Header Button Images
    card_tabs_spec = {
        1: ["Gain", "Filters", "Stereo"],
        2: ["Tape", "Comp", "Tone"],
        3: ["Loss", "Degrade", "CHEW"],
        4: ["Flutter", "Wow"],
    }
    for c_idx, labels in card_tabs_spec.items():
        slot_w = 291 // len(labels)
        for ti, lbl in enumerate(labels):
            render_subtab_button(lbl, slot_w, 34, True,  group_labels=labels).save(os.path.join(SKINS_DIR, "c%d_tab_%d_on.png" % (c_idx, ti)))
            render_subtab_button(lbl, slot_w, 34, False, group_labels=labels).save(os.path.join(SKINS_DIR, "c%d_tab_%d_off.png" % (c_idx, ti)))

    # 4. Live Animated Oscilloscope / VU Meter Strip & LFO Scope Strip
    scope_strip = render_scope_vu_strip(width=716, height=48, frames=65)
    lfo_strip   = render_lfo_scope_strip(width=243, height=74, frames=65)
    scope_strip.save(os.path.join(SKINS_DIR, "chow_scope_wave.png"))
    lfo_strip.save(os.path.join(SKINS_DIR, "chow_lfo_scope.png"))

    # 5. Render Background Canvases
    bg0 = render_chow_background("INTERACTIVE DECK", static_headers=None)
    bg1 = render_chow_background("FILTERS • COMP • DEGRADE • WOW", static_headers=[
        [("Gain", False), ("Filters", True), ("Stereo", False)],
        [("Tape", False), ("Comp", True), ("Tone", False)],
        [("Loss", False), ("Degrade", True), ("CHEW", False)],
        [("Flutter", False), ("Wow", True)],
    ])
    bg2 = render_chow_background("STEREO • TONE • CHEW", static_headers=[
        [("Gain", False), ("Filters", False), ("Stereo", True)],
        [("Tape", False), ("Comp", False), ("Tone", True)],
        [("Loss", False), ("Degrade", False), ("CHEW", True)],
        [("Flutter", True), ("Wow", True)],
    ])
    bg0.save(os.path.join(SKINS_DIR, "chow_bg_0.png"))
    bg1.save(os.path.join(SKINS_DIR, "chow_bg_1.png"))
    bg2.save(os.path.join(SKINS_DIR, "chow_bg_2.png"))

    # ==========================================================================
    # 6. Define Complete Interactive Placements for Tab 1 (CHOW DECK)
    # ==========================================================================
    # Every card on Tab 1 has interactive sub-tab headers (Parameters 45..48)
    # and switches its controls in place using IndexedEnabling!
    tab0_placements = [
        # --- CARD 1 (x=18..313): Gain (0) | Filters (1) | Stereo (2) [Parameter 45] ---
        # Sub-Tab 0: Gain
        {"param_idx": 1,  "param_name": "Input Gain",     "def": "knobBi",          "bounds": "97 136 136 134", "qlink": 13, "invalidating": ["IndexedEnabling/0/3/Parameter 45"]},
        {"param_idx": 2,  "param_name": "Dry/Wet",        "def": "knobUni",         "bounds": "97 270 136 134", "qlink": 9,  "invalidating": ["IndexedEnabling/0/3/Parameter 45"]},
        {"param_idx": 0,  "param_name": "Output Gain",    "def": "knobBi",          "bounds": "97 404 136 134", "qlink": 5,  "invalidating": ["IndexedEnabling/0/3/Parameter 45"]},
        # Sub-Tab 1: Filters
        {"param_idx": 4,  "param_name": "Low Cut",        "def": "knobUni",         "bounds": "97 142 136 134", "invalidating": ["IndexedEnabling/1/3/Parameter 45"]},
        {"param_idx": 5,  "param_name": "High Cut",       "def": "knobUni",         "bounds": "97 290 136 134", "invalidating": ["IndexedEnabling/1/3/Parameter 45"]},
        {"param_idx": 6,  "param_name": "Filter Makeup",  "def": "makeupBtn",       "bounds": "85 454 160 38",  "invalidating": ["IndexedEnabling/1/3/Parameter 45"]},
        # Sub-Tab 2: Stereo
        {"param_idx": 7,  "param_name": "Stereo Mode",    "def": "stereoModeRadio", "bounds": "33 164 265 38",  "invalidating": ["IndexedEnabling/2/3/Parameter 45"]},
        {"param_idx": 8,  "param_name": "Stereo Balance", "def": "knobBi",          "bounds": "97 250 136 134", "invalidating": ["IndexedEnabling/2/3/Parameter 45"]},
        # Card 1 Power Button (Filters On = Parameter 3)
        {"param_idx": 3,  "param_name": "Filters On",     "def": "powerBtn",        "bounds": "141 542 48 32",  "qlink": 1,  "accepts_focus": "No"},

        # --- CARD 2 (x=334..629): Tape (0) | Comp (1) | Tone (2) [Parameter 46] ---
        # Sub-Tab 0: Tape
        {"param_idx": 12, "param_name": "Bias",           "def": "knobUni",         "bounds": "413 136 136 134", "qlink": 14, "invalidating": ["IndexedEnabling/0/3/Parameter 46"]},
        {"param_idx": 11, "param_name": "Saturation",     "def": "knobUni",         "bounds": "413 270 136 134", "qlink": 10, "invalidating": ["IndexedEnabling/0/3/Parameter 46"]},
        {"param_idx": 10, "param_name": "Drive",          "def": "knobUni",         "bounds": "413 404 136 134", "qlink": 6,  "invalidating": ["IndexedEnabling/0/3/Parameter 46"]},
        {"param_idx": 9,  "param_name": "Tape On",        "def": "powerBtn",        "bounds": "457 542 48 32",   "qlink": 2,  "accepts_focus": "No", "invalidating": ["IndexedEnabling/0/3/Parameter 46"]},
        # Sub-Tab 1: Comp
        {"param_idx": 15, "param_name": "Amount",         "def": "knobUni",         "bounds": "413 136 136 134", "invalidating": ["IndexedEnabling/1/3/Parameter 46"]},
        {"param_idx": 16, "param_name": "Attack",         "def": "knobUni",         "bounds": "413 270 136 134", "invalidating": ["IndexedEnabling/1/3/Parameter 46"]},
        {"param_idx": 17, "param_name": "Release",        "def": "knobUni",         "bounds": "413 404 136 134", "invalidating": ["IndexedEnabling/1/3/Parameter 46"]},
        {"param_idx": 14, "param_name": "Comp On",        "def": "powerBtn",        "bounds": "457 542 48 32",   "accepts_focus": "No", "invalidating": ["IndexedEnabling/1/3/Parameter 46"]},
        # Sub-Tab 2: Tone
        {"param_idx": 19, "param_name": "Bass",           "def": "knobBi",          "bounds": "413 136 136 134", "invalidating": ["IndexedEnabling/2/3/Parameter 46"]},
        {"param_idx": 20, "param_name": "Treble",         "def": "knobBi",          "bounds": "413 270 136 134", "invalidating": ["IndexedEnabling/2/3/Parameter 46"]},
        {"param_idx": 21, "param_name": "Transition Freq","def": "knobUni",         "bounds": "413 404 136 134", "invalidating": ["IndexedEnabling/2/3/Parameter 46"]},
        {"param_idx": 18, "param_name": "Tone On",        "def": "powerBtn",        "bounds": "457 542 48 32",   "accepts_focus": "No", "invalidating": ["IndexedEnabling/2/3/Parameter 46"]},

        # --- CARD 3 (x=650..945): Loss (0) | Degrade (1) | CHEW (2) [Parameter 47] ---
        # Sub-Tab 0: Loss (5 horizontal sliders + 3.75/7.5/15/30 pills)
        {"param_idx": 23, "param_name": "Gap [microns]",  "def": "faderComp",       "bounds": "665 134 265 64",  "qlink": 15, "invalidating": ["IndexedEnabling/0/3/Parameter 47"]},
        {"param_idx": 24, "param_name": "Thickness [um]", "def": "faderComp",       "bounds": "665 200 265 64",  "qlink": 11, "invalidating": ["IndexedEnabling/0/3/Parameter 47"]},
        {"param_idx": 25, "param_name": "Spacing [um]",   "def": "faderComp",       "bounds": "665 266 265 64",  "qlink": 7,  "invalidating": ["IndexedEnabling/0/3/Parameter 47"]},
        {"param_idx": 26, "param_name": "Azimuth [deg]",  "def": "faderComp",       "bounds": "665 332 265 64",  "invalidating": ["IndexedEnabling/0/3/Parameter 47"]},
        {"param_idx": 27, "param_name": "Speed [ips]",    "def": "faderComp",       "bounds": "665 398 265 64",  "qlink": 3,  "invalidating": ["IndexedEnabling/0/3/Parameter 47"]},
        {"param_idx": 28, "param_name": "Speed Preset",   "def": "speedPresetRadio","bounds": "665 472 265 34",  "invalidating": ["IndexedEnabling/0/3/Parameter 47"]},
        {"param_idx": 22, "param_name": "Loss On",        "def": "powerBtn",        "bounds": "773 542 48 32",   "accepts_focus": "No", "invalidating": ["IndexedEnabling/0/3/Parameter 47"]},
        # Sub-Tab 1: Degrade
        {"param_idx": 30, "param_name": "Degrade Depth",  "def": "faderUni",        "bounds": "665 142 265 76",  "invalidating": ["IndexedEnabling/1/3/Parameter 47"]},
        {"param_idx": 31, "param_name": "Degrade Amount", "def": "faderUni",        "bounds": "665 236 265 76",  "invalidating": ["IndexedEnabling/1/3/Parameter 47"]},
        {"param_idx": 32, "param_name": "Degrade Var",    "def": "faderUni",        "bounds": "665 330 265 76",  "invalidating": ["IndexedEnabling/1/3/Parameter 47"]},
        {"param_idx": 33, "param_name": "Degrade Env",    "def": "faderUni",        "bounds": "665 424 265 76",  "invalidating": ["IndexedEnabling/1/3/Parameter 47"]},
        {"param_idx": 29, "param_name": "Degrade On",     "def": "powerBtn",        "bounds": "773 542 48 32",   "accepts_focus": "No", "invalidating": ["IndexedEnabling/1/3/Parameter 47"]},
        # Sub-Tab 2: CHEW
        {"param_idx": 35, "param_name": "Chew Depth",     "def": "knobUni",         "bounds": "729 136 136 134", "invalidating": ["IndexedEnabling/2/3/Parameter 47"]},
        {"param_idx": 36, "param_name": "Chew Freq",      "def": "knobUni",         "bounds": "729 270 136 134", "invalidating": ["IndexedEnabling/2/3/Parameter 47"]},
        {"param_idx": 37, "param_name": "Chew Var",       "def": "knobUni",         "bounds": "729 404 136 134", "invalidating": ["IndexedEnabling/2/3/Parameter 47"]},
        {"param_idx": 34, "param_name": "Chew On",        "def": "powerBtn",        "bounds": "773 542 48 32",   "accepts_focus": "No", "invalidating": ["IndexedEnabling/2/3/Parameter 47"]},

        # --- CARD 4 (x=966..1261): Flutter (0) | Wow (1) [Parameter 48] ---
        # Sub-Tab 0: Flutter
        {"param_idx": 39, "param_name": "Depth",          "def": "knobUni",         "bounds": "1045 148 136 134", "qlink": 16, "invalidating": ["IndexedEnabling/0/2/Parameter 48"]},
        {"param_idx": 40, "param_name": "Rate",           "def": "knobUni",         "bounds": "1045 296 136 134", "qlink": 12, "invalidating": ["IndexedEnabling/0/2/Parameter 48"]},
        # Sub-Tab 1: Wow
        {"param_idx": 41, "param_name": "Wow Depth",      "def": "knobSmallU",      "bounds": "974 142 136 122",  "qlink": 8,  "invalidating": ["IndexedEnabling/1/2/Parameter 48"]},
        {"param_idx": 42, "param_name": "Wow Rate",       "def": "knobSmallU",      "bounds": "1116 142 136 122", "qlink": 4,  "invalidating": ["IndexedEnabling/1/2/Parameter 48"]},
        {"param_idx": 43, "param_name": "Wow Variance",   "def": "knobSmallU",      "bounds": "974 282 136 122",  "invalidating": ["IndexedEnabling/1/2/Parameter 48"]},
        {"param_idx": 44, "param_name": "Wow Drift",      "def": "knobSmallU",      "bounds": "1116 282 136 122", "invalidating": ["IndexedEnabling/1/2/Parameter 48"]},
        # Card 4 Power Button (Flutter On = Parameter 38)
        {"param_idx": 38, "param_name": "Flutter On",     "def": "powerBtn",        "bounds": "1089 542 48 32",  "accepts_focus": "No"},
    ]

    # Tab 2: Dedicated Hardware Q-Link Bank for Filters, Comp, Degrade & Wow
    tab1_placements = [
        # Card 1: Filters
        {"param_idx": 4,  "param_name": "Low Cut",        "def": "knobUni",    "bounds": "97 142 136 134",  "qlink": 13},
        {"param_idx": 5,  "param_name": "High Cut",       "def": "knobUni",    "bounds": "97 290 136 134",  "qlink": 9},
        {"param_idx": 6,  "param_name": "Filter Makeup",  "def": "makeupBtn",  "bounds": "85 454 160 38",   "qlink": 5},
        {"param_idx": 3,  "param_name": "Filters On",     "def": "powerBtn",   "bounds": "141 542 48 32",   "qlink": 1, "accepts_focus": "No"},
        # Card 2: Comp
        {"param_idx": 15, "param_name": "Amount",         "def": "knobUni",    "bounds": "413 136 136 134", "qlink": 14},
        {"param_idx": 16, "param_name": "Attack",         "def": "knobUni",    "bounds": "413 270 136 134", "qlink": 10},
        {"param_idx": 17, "param_name": "Release",        "def": "knobUni",    "bounds": "413 404 136 134", "qlink": 6},
        {"param_idx": 14, "param_name": "Comp On",        "def": "powerBtn",   "bounds": "457 542 48 32",   "qlink": 2, "accepts_focus": "No"},
        # Card 3: Degrade
        {"param_idx": 30, "param_name": "Degrade Depth",  "def": "faderUni",   "bounds": "665 142 265 76",  "qlink": 15},
        {"param_idx": 31, "param_name": "Degrade Amount", "def": "faderUni",   "bounds": "665 236 265 76",  "qlink": 11},
        {"param_idx": 32, "param_name": "Degrade Var",    "def": "faderUni",   "bounds": "665 330 265 76",  "qlink": 7},
        {"param_idx": 33, "param_name": "Degrade Env",    "def": "faderUni",   "bounds": "665 424 265 76",  "qlink": 3},
        {"param_idx": 29, "param_name": "Degrade On",     "def": "powerBtn",   "bounds": "773 542 48 32",   "accepts_focus": "No"},
        # Card 4: Wow
        {"param_idx": 41, "param_name": "Wow Depth",      "def": "knobSmallU", "bounds": "974 142 136 122", "qlink": 16},
        {"param_idx": 42, "param_name": "Wow Rate",       "def": "knobSmallU", "bounds": "1116 142 136 122","qlink": 12},
        {"param_idx": 43, "param_name": "Wow Variance",   "def": "knobSmallU", "bounds": "974 282 136 122", "qlink": 8},
        {"param_idx": 44, "param_name": "Wow Drift",      "def": "knobSmallU", "bounds": "1116 282 136 122","qlink": 4},
        {"param_idx": 38, "param_name": "Flutter On",     "def": "powerBtn",   "bounds": "1089 542 48 32",  "accepts_focus": "No"},
    ]

    # Tab 3: Dedicated Hardware Q-Link Bank for Stereo, Tone & CHEW
    tab2_placements = [
        # Card 1: Stereo & Gain
        {"param_idx": 7,  "param_name": "Stereo Mode",    "def": "stereoModeRadio", "bounds": "33 154 265 38",   "qlink": 13},
        {"param_idx": 8,  "param_name": "Stereo Balance", "def": "knobBi",          "bounds": "97 224 136 134",  "qlink": 9},
        {"param_idx": 2,  "param_name": "Dry/Wet",        "def": "knobUni",         "bounds": "97 378 136 134",  "qlink": 5},
        {"param_idx": 0,  "param_name": "Output Gain",    "def": "faderComp",       "bounds": "33 514 265 64",   "qlink": 1},
        # Card 2: Tone
        {"param_idx": 19, "param_name": "Bass",           "def": "knobBi",          "bounds": "413 136 136 134", "qlink": 14},
        {"param_idx": 20, "param_name": "Treble",         "def": "knobBi",          "bounds": "413 270 136 134", "qlink": 10},
        {"param_idx": 21, "param_name": "Transition Freq","def": "knobUni",         "bounds": "413 404 136 134", "qlink": 6},
        {"param_idx": 18, "param_name": "Tone On",        "def": "powerBtn",        "bounds": "457 542 48 32",   "qlink": 2, "accepts_focus": "No"},
        # Card 3: CHEW
        {"param_idx": 35, "param_name": "Chew Depth",     "def": "knobUni",         "bounds": "729 136 136 134", "qlink": 15},
        {"param_idx": 36, "param_name": "Chew Freq",      "def": "knobUni",         "bounds": "729 270 136 134", "qlink": 11},
        {"param_idx": 37, "param_name": "Chew Var",       "def": "knobUni",         "bounds": "729 404 136 134", "qlink": 7},
        {"param_idx": 34, "param_name": "Chew On",        "def": "powerBtn",        "bounds": "773 542 48 32",   "qlink": 3, "accepts_focus": "No"},
        # Card 4: Azimuth + Speed + Flutter
        {"param_idx": 26, "param_name": "Azimuth [deg]",  "def": "faderComp",       "bounds": "981 142 265 64",  "qlink": 16},
        {"param_idx": 27, "param_name": "Speed [ips]",    "def": "faderComp",       "bounds": "981 218 265 64",  "qlink": 12},
        {"param_idx": 39, "param_name": "Flutter Depth",  "def": "faderComp",       "bounds": "981 294 265 64",  "qlink": 8},
        {"param_idx": 41, "param_name": "Wow Depth",      "def": "faderComp",       "bounds": "981 370 265 64",  "qlink": 4},
        {"param_idx": 38, "param_name": "Flutter On",     "def": "powerBtn",        "bounds": "1089 542 48 32",  "accepts_focus": "No"},
    ]

    # 7. Assemble TUI.json
    defs = [
        ("knobUni",           chow_vertical_knob_def("knob_uni.png", False, 134)),
        ("knobBi",            chow_vertical_knob_def("knob_bi.png", True, 134)),
        ("knobSmallU",        chow_vertical_knob_def("knob_uni.png", False, 122)),
        ("faderUni",          chow_horizontal_slider_def("fader_h_uni.png", 76)),
        ("faderComp",         chow_horizontal_slider_def("fader_h_uni.png", 64)),
        ("oversamplingRadio", chow_labeled_radio_def("os",   3, 198, 34, [4, 69, 134],      58,  30)),
        ("hysteresisRadio",   chow_labeled_radio_def("hyst", 4, 265, 34, [6, 71, 136, 201], 58,  30)),
        ("speedPresetRadio",  chow_labeled_radio_def("spd",  4, 265, 34, [6, 71, 136, 201], 58,  30)),
        ("stereoModeRadio",   chow_labeled_radio_def("st",   2, 265, 38, [12, 138],         115, 34)),
        ("powerBtn",          chow_power_btn_def()),
        ("makeupBtn",         chow_makeup_btn_def()),
        ("card1Tabs",         chow_subtab_bar_def(1, ["Gain", "Filters", "Stereo"])),
        ("card2Tabs",         chow_subtab_bar_def(2, ["Tape", "Comp", "Tone"])),
        ("card3Tabs",         chow_subtab_bar_def(3, ["Loss", "Degrade", "CHEW"])),
        ("card4Tabs",         chow_subtab_bar_def(4, ["Flutter", "Wow"])),
        ("scopeWave",         chow_meter_strip_def("chow_scope_wave.png", 716, 48, 65)),
        ("lfoScope",          chow_meter_strip_def("chow_lfo_scope.png", 243, 74, 65)),
        ("scopeReadoutL",     chow_readout_def(220, 20, "left")),
        ("scopeReadoutR",     chow_readout_def(220, 20, "right")),
        ("knob overlay",      component_definition([], [entry("overlay", "KnobOverlay", obj(("version", 2), ("version", 1), ("actions", [action("Focus Lost", "Focus Lost", "Focus Lost", "")]), ("overlayType", "Knob With Header"), ("scaleType", "Linear"), ("polarityType", "Unipolar"), ("showMinusSign", True), ("handleName", "Data")), "0 0 1280 800", "No", "Show", "Always")], "ff24262d", ignore_mouse=True)),
        ("knob overlay bipolar", component_definition([], [entry("overlay", "KnobOverlay", obj(("version", 2), ("version", 1), ("actions", [action("Focus Lost", "Focus Lost", "Focus Lost", "")]), ("overlayType", "Knob With Header"), ("scaleType", "Linear"), ("polarityType", "Bipolar"), ("showMinusSign", True), ("handleName", "Data")), "0 0 1280 800", "No", "Show", "Always")], "ff24262d", ignore_mouse=True)),
        ("menu overlay",      component_definition([], [entry("overlay", "MenuOverlay", obj(("version", 1), ("version", 1), ("actions", [action("Focus Lost", "Focus Lost", "Focus Lost", "")]), ("withNumbers", False), ("handleName", "Data")), "0 0 1280 800", "No", "Show", "Always")], "ff24262d", ignore_mouse=True)),
    ]

    tabs = [
        build_tab("CHOW DECK",  0, "chow_bg_0.png", tab0_placements, include_interactive_headers=True),
        build_tab("FILT/COMP",  1, "chow_bg_1.png", tab1_placements, include_interactive_headers=False),
        build_tab("STER/CHEW",  2, "chow_bg_2.png", tab2_placements, include_interactive_headers=False),
    ]

    replacement_strings = [
        obj(("key", "Parameter 7"),  ("value", ["Stereo", "Mid/Side"])),
        obj(("key", "Parameter 13"), ("value", ["RK2", "RK4", "NR4", "NR8"])),
        obj(("key", "Parameter 28"), ("value", ["3.75", "7.5", "15", "30"])),
        obj(("key", "Parameter 45"), ("value", ["Gain", "Filters", "Stereo"])),
        obj(("key", "Parameter 46"), ("value", ["Tape", "Comp", "Tone"])),
        obj(("key", "Parameter 47"), ("value", ["Loss", "Degrade", "CHEW"])),
        obj(("key", "Parameter 48"), ("value", ["Flutter", "Wow"])),
        obj(("key", "Parameter 52"), ("value", ["1x", "2x", "4x"])),
    ]

    tui = obj(("pageData", obj(
        ("version", 1),
        ("componentDefinitions", obj(("version", 2), ("importFiles", []),
                                     ("localComponentDefinitions", [obj(("key", k), ("value", v)) for k, v in defs]))),
        ("replacementStrings", replacement_strings),
        ("info", obj(("version", 1), ("type", "CompleteDescription"))),
        ("tabs", tabs),
    )))

    with open(os.path.join(SKINS_DIR, "TUI.json"), "w", encoding="utf-8") as f:
        f.write(dump_json(tui) + "\n")

    # 8. Build Q-Links.json
    q_entries = []
    for ti, plist in enumerate([tab0_placements, tab1_placements, tab2_placements]):
        assigned = {item["qlink"]: item["param_idx"] for item in plist if "qlink" in item}
        qmap = Obj(("Q-Link %d" % q, assigned.get(q, -1)) for q in range(1, 17))
        q_entries.append(obj(("Tab", ti + 1), ("SubTab", 1), ("Bank Direction", "Column"), ("Q-Links", qmap)))

    program_q_list = [1, 2, 0, 3, 12, 11, 10, 13, 23, 24, 25, 27, 39, 40, 41, 42]
    program_q = Obj(("Q-Link %d" % (q + 1), program_q_list[q]) for q in range(16))
    qlinks = obj(
        ("version", 4),
        ("info", obj(("version", 1), ("type", "CompleteDescription"))),
        ("Screen Mode Q-Links", obj(("version", 4), ("map", q_entries))),
        ("Program Mode Q-Links", program_q),
    )
    with open(os.path.join(SKINS_DIR, "Q-Links.json"), "w", encoding="utf-8") as f:
        f.write(dump_json(qlinks) + "\n")

    # 9. Ensure version.xml includes <uiversion>0.1.0.0</uiversion>
    version_xml_path = os.path.join(BUILD_FOLDER, "version.xml")
    with open(version_xml_path, "w", encoding="utf-8") as f:
        f.write("<data>\n  <version>2.10.0.0</version>\n  <uiversion>0.1.0.0</uiversion>\n</data>\n")

    # 10. Render Composite Preview Image using the EXACT generated button PNGs!
    preview = bg0.copy()
    pd = ImageDraw.Draw(preview)
    # Composite interactive tab header buttons onto preview
    for c_idx, x_start in [(1, 20), (2, 336), (3, 652), (4, 968)]:
        labels = card_tabs_spec[c_idx]
        slot_w = 291 // len(labels)
        for ti in range(len(labels)):
            img_name = "c%d_tab_%d_%s.png" % (c_idx, ti, "on" if ti == 0 else "off")
            timg = Image.open(os.path.join(SKINS_DIR, img_name))
            preview.alpha_composite(timg, (x_start + ti * slot_w, 93))

    # Composite live TapeScope wave & readouts
    scope_frame = scope_strip.crop((0, 46 * 48, 716, 47 * 48))
    preview.alpha_composite(scope_frame, (538, 30))
    f_scope = get_font(bold=True, size=17)
    pd.text((542, 10), "IN: -8.4 dB", font=f_scope, fill=(242, 232, 232, 255))
    pd.text((1144, 10), "OUT: -7.9 dB", font=f_scope, fill=(242, 232, 232, 255))

    # Composite live LFO scope in Card 4
    lfo_frame = lfo_strip.crop((0, 42 * 74, 243, 43 * 74))
    preview.alpha_composite(lfo_frame, (992, 456))

    # Composite Gold Power Switches at bottom of all 4 cards
    pwr_on = Image.open(os.path.join(SKINS_DIR, "power_on.png"))
    for px_x in (141, 457, 773, 1089):
        preview.alpha_composite(pwr_on, (px_x, 542))

    # Composite default sub-tab 0 controls
    f_lbl = get_font(bold=True, size=17)
    f_val = get_font(bold=True, size=17)
    knob_mid = knob_uni.crop((0, 63 * 92, 92, 64 * 92))
    knob_75  = knob_uni.crop((0, 95 * 92, 92, 96 * 92))
    knob_30  = knob_uni.crop((0, 38 * 92, 92, 39 * 92))
    fader_35 = fader_uni.crop((0, 44 * 24, 252, 45 * 24))

    for bx, by, lbl, val, kimg in [
        (97,  136, "Input Gain",  "0.0 dB", knob_mid),
        (97,  270, "Dry/Wet",     "100%",   knob_75),
        (97,  404, "Output Gain", "0.0 dB", knob_mid),
        (413, 136, "Bias",        "0.50",   knob_mid),
        (413, 270, "Saturation",  "0.50",   knob_mid),
        (413, 404, "Drive",       "0.50",   knob_mid),
        (1045,148, "Depth",       "0.20",   knob_30),
        (1045,296, "Rate",        "0.30",   knob_30),
    ]:
        lw = pd.textlength(lbl, font=f_lbl)
        pd.text((bx + (136 - lw) / 2.0, by + 1), lbl, font=f_lbl, fill=COL_TEXT_WHITE)
        preview.alpha_composite(kimg, (bx + 22, by + 18))
        vw = pd.textlength(val, font=f_val)
        pd.text((bx + (136 - vw) / 2.0, by + 112), val, font=f_val, fill=COL_TEXT_WHITE)

    # Interactive Footer Buttons: composite actual os_* and hyst_* button PNGs!
    for ci, ox in enumerate([4, 69, 134]):
        bimg = Image.open(os.path.join(SKINS_DIR, "os_%d_%s.png" % (ci, "on" if ci == 1 else "off")))
        preview.alpha_composite(bimg, (152 + ox, 592))

    for ci, ox in enumerate([6, 71, 136, 201]):
        bimg = Image.open(os.path.join(SKINS_DIR, "hyst_%d_%s.png" % (ci, "on" if ci == 2 else "off")))
        preview.alpha_composite(bimg, (532 + ox, 592))

    # Loss 5 sliders + Speed presets on Card 3 (using actual spd_* button PNGs!)
    for by, lbl, val in [
        (134, "Gap [microns]",  "1.00"),
        (200, "Thickness [um]", "0.10"),
        (266, "Spacing [um]",   "0.10"),
        (332, "Azimuth [deg]",  "0.0 deg"),
        (398, "Speed [ips]",    "15.00 ips"),
    ]:
        pd.text((671, by), lbl, font=f_lbl, fill=COL_TEXT_WHITE)
        preview.alpha_composite(fader_35, (671, by + 20))
        pd.text((679, by + 44), val, font=f_val, fill=COL_TEXT_WHITE)

    for ci, ox in enumerate([6, 71, 136, 201]):
        bimg = Image.open(os.path.join(SKINS_DIR, "spd_%d_%s.png" % (ci, "on" if ci == 2 else "off")))
        preview.alpha_composite(bimg, (665 + ox, 474))

    preview_path = "/Users/dogukanatok/.gemini/antigravity/brain/b735e560-a7bf-4c76-b450-a42ec86c804b/chow_tape_mpc_skin_preview.png"
    preview.save(preview_path)
    print("[OK] Wrote interactive 53-param CHOW Tape Model MPC skin to:", SKINS_DIR)
    print("[OK] Updated preview:", preview_path)


if __name__ == "__main__":
    main()

#pragma once
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "flow_drm.h"
#include "flow_font_atlas.h"
#include "flow_http.h"

// Logical Hitboxes for Akai Menu Mode badges (aligned to native TopBar & Grid)
#define MENU_TOP_BTN_X  120
#define MENU_TOP_BTN_Y  30
#define MENU_TOP_BTN_W  196
#define MENU_TOP_BTN_H  48

#define MENU_P2_TILE_X  352
#define MENU_P2_TILE_Y  260
#define MENU_P2_TILE_W  124
#define MENU_P2_TILE_H  108

// Color palette (ARGB8888, authentic Akai MPC 3 Live dark charcoal/crimson/blue theme)
#define COL_TRANS       0x00000000U
#define COL_BG          0xFF111114U
#define COL_TOPBAR      0xFF2F2F34U
#define COL_PANEL       0xFF18181CU
#define COL_PANEL_ALT   0xFF24242AU
#define COL_PANEL_HI    0xFF32323AU
#define COL_GRID        0xFF383842U
#define COL_BORDER      0xFF4A4A55U
#define COL_ACCENT      0xFFD31145U
#define COL_ACCENT_DARK 0xFF3C121EU
#define COL_NEON        0xFF20C968U
#define COL_NEON_DARK   0xFF123622U
#define COL_CYAN        0xFF2894F0U
#define COL_CYAN_DARK   0xFF132D46U
#define COL_AMBER       0xFFE5A91AU
#define COL_AMBER_DARK  0xFF3A2D10U
#define COL_RED         0xFFE0243CU
#define COL_RED_DARK    0xFF3A121AU
#define COL_TEXT        0xFFF0F0F4U
#define COL_TEXT_DIM    0xFF8E8E98U
#define COL_BLACK       0xFF0A0A0CU

typedef struct { int x, y, w, h; } FlowRect;

typedef enum {
    ACT_NONE = 0,
    ACT_CLOSE,
    ACT_MODE_KIT,
    ACT_MODE_LOOP,
    ACT_OPEN_BROWSER,
    ACT_TOGGLE_MAC,
    ACT_BPM_DEC,
    ACT_BPM_INC,
    ACT_KEY_CYCLE,
    ACT_GENERATE,
    ACT_PRESET_BASE,   // 0..7 -> +idx
    ACT_PAD_BASE = 20, // 0..15 -> +idx
    ACT_RECENT_BASE = 40, // 0..2 -> +idx
    ACT_KEY_SPACE = 60,
    ACT_KEY_BACK,
    ACT_KEY_CLEAR,
    ACT_KEY_SHIFT,
    ACT_CHAR_BASE = 100, // ASCII code
} FlowActionId;

static const char *const FLOW_KEY_NAMES[] = {
    "C min", "D min", "F min", "G min", "A min",
    "C maj", "G maj", "D maj", "F maj", "Bb maj",
};
#define FLOW_NUM_KEYS (int)(sizeof(FLOW_KEY_NAMES) / sizeof(FLOW_KEY_NAMES[0]))

typedef struct { const char *label; const char *prompt; float bpm; const char *key; } FlowPresetDef;
static const FlowPresetDef FLOW_PRESETS[8] = {
    {"90s Boom Bap",  "90s boom bap dusty vinyl drum kit",          88.0f,  "C min"},
    {"Dark Trap 808", "dark metro boomin 808 trap drum kit",        140.0f, "F min"},
    {"Lo-Fi Dusty",   "warm lofi sp404 dusty chill drum kit",       84.0f,  "D min"},
    {"UK Drill",      "uk drill hard 808 sliding kick snare kit",   142.0f, "C min"},
    {"House 909",     "classic chicago house tr909 club drum kit",  124.0f, "A min"},
    {"Memphis Phonk", "memphis phonk cowbell 808 crunchy drum kit", 136.0f, "G min"},
    {"Jazz Brush",    "acoustic jazz brushed snare upright drum kit",105.0f, "F maj"},
    {"Afro Perc",     "afrobeats log drum conga rim shaker kit",    112.0f, "D min"},
};

typedef struct {
    const char *pad_id;
    const char *def_name;
    uint32_t color;
} FlowDefaultPad;

static const FlowDefaultPad FLOW_DEF_PADS[16] = {
    {"A01", "KICK 1",  0xFFD31145U},
    {"A02", "SNARE 1", 0xFFE5A91AU},
    {"A03", "CL HAT",  0xFF2894F0U},
    {"A04", "OP HAT",  0xFF3B82F6U},
    {"A05", "808 SUB", 0xFFA855F7U},
    {"A06", "CLAP",    0xFFF59E0BU},
    {"A07", "RIM",     0xFFD97706U},
    {"A08", "SNARE 2", 0xFFEA580CU},
    {"A09", "KICK 2",  0xFFE0243CU},
    {"A10", "TOM LO",  0xFF8B5CF6U},
    {"A11", "TOM MID", 0xFF6366F1U},
    {"A12", "PERC",    0xFF20C968U},
    {"A13", "SHAKER",  0xFF14B8A6U},
    {"A14", "CRASH",   0xFF38BDF8U},
    {"A15", "BELL",    0xFF60A5FAU},
    {"A16", "FX STAB", 0xFF20C968U},
};

// Fixed UI Button Geometry
static const FlowRect R_BTN_CLOSE   = {16,   10, 128, 38};
static const FlowRect R_BTN_MAC     = {414,  10, 118, 38};
static const FlowRect R_BTN_KIT     = {540,  10, 176, 38};
static const FlowRect R_BTN_LOOP    = {724,  10, 146, 38};
static const FlowRect R_BTN_BROWSER = {882,  10, 200, 38};
static const FlowRect R_BTN_X       = {1094, 10, 170, 38};

static const FlowRect R_PROMPT_BOX  = {16,   64, 756, 68};
static const FlowRect R_BPM_DEC     = {784,  64,  48, 68};
static const FlowRect R_BPM_BOX     = {834,  64,  76, 68};
static const FlowRect R_BPM_INC     = {912,  64,  48, 68};
static const FlowRect R_KEY_BTN     = {968,  64, 114, 68};
static const FlowRect R_GEN_BTN     = {1090, 64, 174, 68};

// 8 preset chips across y=144..188
static inline FlowRect flow_preset_rect(int idx) {
    int gap = 10;
    int w = (FLOW_LOG_W - 32 - 7 * gap) / 8; // ~148px each
    return (FlowRect){16 + idx * (w + gap), 144, w, 44};
}

// Left Panel 4x4 Drum Pads (A13..A16 top row, A01..A04 bottom row, matching MPC pads!)
static inline FlowRect flow_pad_rect(int pad_idx_0_15) {
    int col = pad_idx_0_15 % 4;
    int mpc_row_from_bottom = pad_idx_0_15 / 4; // 0 = bottom A01..A04
    int vis_row = 3 - mpc_row_from_bottom;
    int gx = 18 + col * 127;
    int gy = 250 + vis_row * 120;
    return (FlowRect){gx, gy, 119, 112};
}

// Right Panel Recent Kits (y=200..248)
static inline FlowRect flow_recent_rect(int idx) {
    int w = 228;
    return (FlowRect){546 + idx * (w + 10), 200, w, 46};
}

// QWERTY Keyboard Rows (Right side x=546..1264, y=318..784 -> 5 crisp touch rows!)
static const char *const KB_ROW0 = "1234567890";
static const char *const KB_ROW1 = "QWERTYUIOP";
static const char *const KB_ROW2 = "ASDFGHJKL-";
static const char *const KB_ROW3 = "ZXCVBNM,.";

static inline int flow_in_rect(int x, int y, FlowRect r) {
    return x >= r.x && x < (r.x + r.w) && y >= r.y && y < (r.y + r.h);
}

static inline uint32_t flow_blend_px(uint32_t dst, uint32_t src, uint8_t a) {
    if (a == 255) return 0xFF000000U | (src & 0x00FFFFFFU);
    if (a == 0) return dst;
    uint32_t da = (dst >> 24) & 0xff;
    uint32_t sr = (src >> 16) & 0xff, sg = (src >> 8) & 0xff, sb = src & 0xff;
    uint32_t dr = (dst >> 16) & 0xff, dg = (dst >> 8) & 0xff, db = dst & 0xff;
    uint32_t inv = 255 - a;
    uint32_t r = (sr * a + dr * inv) / 255;
    uint32_t g = (sg * a + dg * inv) / 255;
    uint32_t b = (sb * a + db * inv) / 255;
    uint32_t oa = a + (da * inv) / 255;
    return (oa << 24) | (r << 16) | (g << 8) | b;
}

static inline void flow_fill_rect(uint32_t *cv, int x, int y, int w, int h, uint32_t col) {
    int x0 = x < 0 ? 0 : x, y0 = y < 0 ? 0 : y;
    int x1 = x + w > FLOW_LOG_W ? FLOW_LOG_W : x + w;
    int y1 = y + h > FLOW_LOG_H ? FLOW_LOG_H : y + h;
    uint8_t a = (col >> 24) & 0xff;
    for (int yy = y0; yy < y1; yy++) {
        uint32_t *row = cv + yy * FLOW_LOG_W;
        if (a == 255 || a == 0) {
            for (int xx = x0; xx < x1; xx++) row[xx] = col;
        } else {
            for (int xx = x0; xx < x1; xx++) row[xx] = flow_blend_px(row[xx], col, a);
        }
    }
}

static inline void flow_fill_round_rect(uint32_t *cv, FlowRect r, int rad, uint32_t fill, uint32_t border) {
    int x0 = r.x < 0 ? 0 : r.x, y0 = r.y < 0 ? 0 : r.y;
    int x1 = r.x + r.w > FLOW_LOG_W ? FLOW_LOG_W : r.x + r.w;
    int y1 = r.y + r.h > FLOW_LOG_H ? FLOW_LOG_H : r.y + r.h;
    int r2 = rad * rad;
    int ir = rad > 2 ? rad - 2 : 0;
    int ir2 = ir * ir;

    for (int yy = y0; yy < y1; yy++) {
        uint32_t *row = cv + yy * FLOW_LOG_W;
        int dy = 0;
        if (yy < r.y + rad) dy = (r.y + rad - 1) - yy;
        else if (yy >= r.y + r.h - rad) dy = yy - (r.y + r.h - rad);
        for (int xx = x0; xx < x1; xx++) {
            int dx = 0;
            if (xx < r.x + rad) dx = (r.x + rad - 1) - xx;
            else if (xx >= r.x + r.w - rad) dx = xx - (r.x + r.w - rad);
            if (dx > 0 && dy > 0 && (dx * dx + dy * dy > r2)) continue;
            int is_border = (border != 0) && (
                xx < r.x + 2 || xx >= r.x + r.w - 2 ||
                yy < r.y + 2 || yy >= r.y + r.h - 2 ||
                (dx > 0 && dy > 0 && (dx * dx + dy * dy >= ir2))
            );
            row[xx] = is_border ? border : fill;
        }
    }
}

static inline void flow_fill_grad_round_rect(uint32_t *cv, FlowRect r, int rad, uint32_t top_col, uint32_t bot_col, uint32_t top_rim, uint32_t border) {
    int x0 = r.x < 0 ? 0 : r.x, y0 = r.y < 0 ? 0 : r.y;
    int x1 = r.x + r.w > FLOW_LOG_W ? FLOW_LOG_W : r.x + r.w;
    int y1 = r.y + r.h > FLOW_LOG_H ? FLOW_LOG_H : r.y + r.h;
    int r2 = rad * rad;
    int ir = rad > 1 ? rad - 1 : 0;
    int ir2 = ir * ir;
    int denom = (r.h > 1) ? (r.h - 1) : 1;
    uint32_t ta = (top_col >> 24) & 0xff, tr = (top_col >> 16) & 0xff, tg = (top_col >> 8) & 0xff, tb = top_col & 0xff;
    uint32_t ba = (bot_col >> 24) & 0xff, br = (bot_col >> 16) & 0xff, bg = (bot_col >> 8) & 0xff, bb = bot_col & 0xff;

    for (int yy = y0; yy < y1; yy++) {
        uint32_t *row = cv + yy * FLOW_LOG_W;
        int dy = 0;
        if (yy < r.y + rad) dy = (r.y + rad - 1) - yy;
        else if (yy >= r.y + r.h - rad) dy = yy - (r.y + r.h - rad);
        int t = ((yy - r.y) * 255) / denom;
        int inv = 255 - t;
        uint32_t gcol = (((ta * inv + ba * t) / 255) << 24) |
                        (((tr * inv + br * t) / 255) << 16) |
                        (((tg * inv + bg * t) / 255) << 8)  |
                        ((tb * inv + bb * t) / 255);
        for (int xx = x0; xx < x1; xx++) {
            int dx = 0;
            if (xx < r.x + rad) dx = (r.x + rad - 1) - xx;
            else if (xx >= r.x + r.w - rad) dx = xx - (r.x + r.w - rad);
            if (dx > 0 && dy > 0 && (dx * dx + dy * dy > r2)) continue;
            int is_top = (yy == r.y) || (dx > 0 && dy > 0 && yy < r.y + rad && (dx * dx + dy * dy >= ir2));
            int is_bdr = (xx == r.x || xx == r.x + r.w - 1 || yy == r.y + r.h - 1 ||
                          (dx > 0 && dy > 0 && (dx * dx + dy * dy >= ir2)));
            if (yy == r.y && top_rim) row[xx] = top_rim;
            else if (is_bdr && border) row[xx] = (is_top && top_rim) ? top_rim : border;
            else row[xx] = gcol;
        }
    }
}

static inline void flow_get_font(int sz, const FlowGlyph **glyphs, const uint8_t **data, int *line_h) {
    if (sz >= 22) { *glyphs = flow_atlas_25_glyphs; *data = flow_atlas_25_data; *line_h = 26; }
    else if (sz >= 17) { *glyphs = flow_atlas_19_glyphs; *data = flow_atlas_19_data; *line_h = 20; }
    else { *glyphs = flow_atlas_14_glyphs; *data = flow_atlas_14_data; *line_h = 15; }
}

static inline int flow_text_width(int sz, const char *s) {
    const FlowGlyph *gl; const uint8_t *dat; int lh;
    flow_get_font(sz, &gl, &dat, &lh);
    int w = 0;
    for (int i = 0; s && s[i]; i++) {
        unsigned char c = (unsigned char)s[i];
        int idx = (c >= 32 && c <= 126) ? (c - 32) : ('?' - 32);
        w += gl[idx].adv;
    }
    return w;
}

static inline void flow_draw_text(uint32_t *cv, int x, int y, int sz, uint32_t col, const char *s) {
    if (!s) return;
    const FlowGlyph *gl; const uint8_t *dat; int lh;
    flow_get_font(sz, &gl, &dat, &lh);
    uint8_t base_a = (col >> 24) & 0xff;
    if (base_a == 0) base_a = 255;
    int cx = x;
    for (int i = 0; s[i]; i++) {
        unsigned char c = (unsigned char)s[i];
        int idx = (c >= 32 && c <= 126) ? (c - 32) : ('?' - 32);
        const FlowGlyph *g = &gl[idx];
        const uint8_t *bmp = dat + g->off;
        int gy0 = y + g->yoff;
        for (int row = 0; row < g->h; row++) {
            int yy = gy0 + row;
            if (yy < 0 || yy >= FLOW_LOG_H) continue;
            uint32_t *dst_row = cv + yy * FLOW_LOG_W;
            for (int cc = 0; cc < g->w; cc++) {
                int xx = cx + cc;
                if (xx < 0 || xx >= FLOW_LOG_W) continue;
                uint8_t ga = bmp[row * g->w + cc];
                if (ga < 4) continue;
                uint8_t a = (uint8_t)(((uint32_t)ga * base_a) / 255);
                dst_row[xx] = flow_blend_px(dst_row[xx], col, a);
            }
        }
        cx += g->adv;
    }
}

static inline void flow_draw_text_centered(uint32_t *cv, FlowRect r, int sz, uint32_t col, const char *s) {
    int tw = flow_text_width(sz, s);
    int lh = (sz >= 22) ? 24 : ((sz >= 17) ? 18 : 14);
    int tx = r.x + (r.w - tw) / 2;
    int ty = r.y + (r.h - lh) / 2 - 2;
    flow_draw_text(cv, tx, ty, sz, col, s);
}

// Render subtle native Akai Menu badges onto transparent AR24 canvas
static inline void flow_render_menu_badges(uint32_t *cv, int menu_page, int bridge_online) {
    // Clear tight union bounds of Page 1 + Page 2 overlay crop (lx=[116..480), ly=[28..372))
    for (int yy = 28; yy < 372; yy++) {
        memset(cv + (size_t)yy * FLOW_LOG_W + 116, 0, (480 - 116) * sizeof(uint32_t));
    }

    // 1) Compact native Akai TopBar launcher pill in empty charcoal bar (y=30..78, aligned to native divider)
    FlowRect tb = {MENU_TOP_BTN_X, MENU_TOP_BTN_Y, MENU_TOP_BTN_W, MENU_TOP_BTN_H};
    flow_fill_grad_round_rect(cv, tb, 6, 0xFF28282EU, 0xFF1F1F24U, 0xFF474750U, 0xFF3E3E46U);
    // Clean 2x2 Akai mini pad emblem (Crimson / Blue / Amber / Emerald)
    const uint32_t mcols[4] = {0xFFD31145U, 0xFF2894F0U, 0xFFE5A91AU, 0xFF20C968U};
    for (int py = 0; py < 2; py++) {
        for (int px = 0; px < 2; px++) {
            flow_fill_rect(cv, tb.x + 16 + px * 9, tb.y + 15 + py * 9, 7, 7, mcols[py * 2 + px]);
        }
    }
    flow_draw_text(cv, tb.x + 42, tb.y + 13, 19, COL_TEXT, "FLOW AI");
    // Tiny status LED dot (Emerald when Mac bridge online, muted grey when offline)
    flow_fill_round_rect(cv, (FlowRect){tb.x + tb.w - 20, tb.y + 20, 8, 8}, 4,
                         bridge_online ? COL_NEON : 0xFF5A5A64U, 0);

    // 2) Akai-matched Row 2 Col 1 metallic icon tile on Menu Page 2 (aligned with KEY RANGES & LIST EDIT)
    if (menu_page == 2) {
        FlowRect ib = {385, 265, 58, 58};
        flow_fill_grad_round_rect(cv, ib, 8, 0xFF404148U, 0xFF1B1C20U, 0xFF585962U, 0xFF2E2F36U);
        // 3x3 miniature Akai MPC pad grid inside tile (no garish outer outline)
        static const uint32_t gcols[9] = {
            0xFFD31145U, 0xFFE0243CU, 0xFFFF4D6DU,
            0xFF2894F0U, 0xFF1CB5B0U, 0xFF20C968U,
            0xFFE5A91AU, 0xFFF59E0BU, 0xFF9B5CF6U
        };
        for (int r = 0; r < 3; r++) {
            for (int c = 0; c < 3; c++) {
                flow_fill_round_rect(cv, (FlowRect){ib.x + 10 + c * 14, ib.y + 10 + r * 14, 10, 10}, 2, gcols[r * 3 + c], 0);
            }
        }
        FlowRect lr = {MENU_P2_TILE_X, 342, MENU_P2_TILE_W, 22};
        flow_draw_text_centered(cv, lr, 14, 0xFFEAEAEFU, "FLOW AI");
    }
}

static inline void flow_render_full_ui(uint32_t *cv, const FlowAppState *st, uint64_t now_ms) {
    // Deep Akai MPC 3 Live slate canvas (clean, no neon matrix grid)
    flow_fill_rect(cv, 0, 0, FLOW_LOG_W, FLOW_LOG_H, COL_BG);

    // Top Header Bar (y=0..58, exact Akai #2F2F34 slate)
    flow_fill_rect(cv, 0, 0, FLOW_LOG_W, 57, COL_TOPBAR);
    flow_fill_rect(cv, 0, 57, FLOW_LOG_W, 1, 0xFF060608U);
    flow_fill_rect(cv, 0, 58, FLOW_LOG_W, 1, 0xFF28282EU);

    flow_fill_grad_round_rect(cv, R_BTN_CLOSE, 6, 0xFF2A2A30U, 0xFF1F1F24U, 0xFF4C4C55U, 0xFF3A3A42U);
    flow_draw_text_centered(cv, R_BTN_CLOSE, 19, COL_TEXT, "< MENU");

    // Brand wordmark + tiny Akai crimson accent tag
    flow_draw_text(cv, 158, 13, 25, COL_TEXT, "FLOW AI");
    flow_fill_round_rect(cv, (FlowRect){266, 15, 134, 28}, 5, 0xFF222228U, 0xFF3E3E46U);
    flow_fill_round_rect(cv, (FlowRect){274, 24, 9, 9}, 4, st->bridge_online ? COL_NEON : COL_RED, 0);
    flow_draw_text(cv, 288, 21, 14, st->bridge_online ? COL_TEXT : COL_RED,
                   st->bridge_online ? "MAC BRIDGE ON" : "BRIDGE OFF");

    // Mac Speaker Monitor toggle (default OFF so Mac stays quiet on pad tap!)
    flow_fill_grad_round_rect(cv, R_BTN_MAC, 6,
                              st->mac_audition ? 0xFF193C5EU : 0xFF25252BU,
                              st->mac_audition ? 0xFF122B44U : 0xFF1D1D22U,
                              st->mac_audition ? COL_CYAN : 0xFF42424AU,
                              st->mac_audition ? COL_CYAN : 0xFF383842U);
    flow_draw_text_centered(cv, R_BTN_MAC, 14, st->mac_audition ? COL_TEXT : COL_TEXT_DIM,
                            st->mac_audition ? "MAC SPK: ON" : "MAC SPK: OFF");

    flow_fill_grad_round_rect(cv, R_BTN_KIT, 6,
                              st->mode_kit ? 0xFFD31145U : 0xFF25252BU,
                              st->mode_kit ? 0xFFA60D36U : 0xFF1D1D22U,
                              st->mode_kit ? 0xFFF0406CU : 0xFF42424AU,
                              st->mode_kit ? 0xFFE0244CU : 0xFF383842U);
    flow_draw_text_centered(cv, R_BTN_KIT, 19, st->mode_kit ? COL_TEXT : COL_TEXT_DIM, "DRUM KIT .XPM");

    flow_fill_grad_round_rect(cv, R_BTN_LOOP, 6,
                              !st->mode_kit ? 0xFF2894F0U : 0xFF25252BU,
                              !st->mode_kit ? 0xFF1C6BB2U : 0xFF1D1D22U,
                              !st->mode_kit ? 0xFF5FB0F8U : 0xFF42424AU,
                              !st->mode_kit ? 0xFF389EFFU : 0xFF383842U);
    flow_draw_text_centered(cv, R_BTN_LOOP, 19, !st->mode_kit ? COL_TEXT : COL_TEXT_DIM, "LOOP BANK");

    flow_fill_grad_round_rect(cv, R_BTN_BROWSER, 6, 0xFF28282EU, 0xFF1E1E24U, 0xFF4A4A54U, COL_AMBER_DARK);
    flow_fill_rect(cv, R_BTN_BROWSER.x + 12, R_BTN_BROWSER.y + 11, 14, 15, COL_AMBER);
    flow_fill_rect(cv, R_BTN_BROWSER.x + 12, R_BTN_BROWSER.y + 9, 8, 4, COL_AMBER);
    flow_draw_text(cv, R_BTN_BROWSER.x + 34, R_BTN_BROWSER.y + 9, 19, COL_AMBER, "MPC BROWSER");

    flow_fill_grad_round_rect(cv, R_BTN_X, 6, 0xFF3C141EU, 0xFF2B0E15U, 0xFF6A2434U, COL_ACCENT);
    flow_draw_text_centered(cv, R_BTN_X, 19, COL_TEXT, "CLOSE [X]");

    // Prompt Bar + Controls (y=66..134, crisp inset boxes with Akai Crimson active focus border)
    flow_fill_round_rect(cv, R_PROMPT_BOX, 8, COL_BLACK, COL_ACCENT);
    flow_draw_text(cv, R_PROMPT_BOX.x + 14, R_PROMPT_BOX.y + 6, 14, COL_TEXT_DIM,
                   st->mode_kit ? "PROMPT (DRUM KIT -> AKAI .XPM)" : "PROMPT (MELODIC / SAMPLE LOOP -> BANK 1)");
    char show_prompt[200];
    snprintf(show_prompt, sizeof(show_prompt), "%s%s", st->prompt[0] ? st->prompt : "Tap preset or type kit style...",
             ((now_ms / 500) % 2 == 0) ? "|" : "");
    // Clip prompt string visually to box width
    int plen = (int)strlen(show_prompt);
    const char *pview = show_prompt;
    while (plen > 8 && flow_text_width(25, pview) > R_PROMPT_BOX.w - 28) {
        pview++;
        plen--;
    }
    flow_draw_text(cv, R_PROMPT_BOX.x + 14, R_PROMPT_BOX.y + 26, 25,
                   st->prompt[0] ? COL_TEXT : COL_TEXT_DIM, pview);

    flow_fill_grad_round_rect(cv, R_BPM_DEC, 6, 0xFF292930U, 0xFF1F1F25U, 0xFF44444EU, COL_GRID);
    flow_draw_text_centered(cv, R_BPM_DEC, 25, COL_TEXT, "-");

    flow_fill_round_rect(cv, R_BPM_BOX, 6, COL_BLACK, COL_ACCENT);
    char bpm_s[24];
    snprintf(bpm_s, sizeof(bpm_s), "%d", (int)(st->bpm + 0.5f));
    flow_draw_text_centered(cv, (FlowRect){R_BPM_BOX.x, R_BPM_BOX.y + 5, R_BPM_BOX.w, 34}, 25, COL_TEXT, bpm_s);
    flow_draw_text_centered(cv, (FlowRect){R_BPM_BOX.x, R_BPM_BOX.y + 39, R_BPM_BOX.w, 22}, 14, COL_TEXT_DIM, "BPM");

    flow_fill_grad_round_rect(cv, R_BPM_INC, 6, 0xFF292930U, 0xFF1F1F25U, 0xFF44444EU, COL_GRID);
    flow_draw_text_centered(cv, R_BPM_INC, 25, COL_TEXT, "+");

    flow_fill_round_rect(cv, R_KEY_BTN, 6, COL_BLACK, COL_GRID);
    flow_draw_text_centered(cv, (FlowRect){R_KEY_BTN.x, R_KEY_BTN.y + 5, R_KEY_BTN.w, 34}, 19, COL_CYAN,
                            FLOW_KEY_NAMES[st->key_idx % FLOW_NUM_KEYS]);
    flow_draw_text_centered(cv, (FlowRect){R_KEY_BTN.x, R_KEY_BTN.y + 39, R_KEY_BTN.w, 22}, 14, COL_TEXT_DIM, "KEY");

    flow_fill_grad_round_rect(cv, R_GEN_BTN, 8,
                              st->job_running ? 0xFF9E6B10U : 0xFFE0184DU,
                              st->job_running ? 0xFF6B4608U : 0xFFA60D36U,
                              st->job_running ? COL_AMBER : 0xFFFF4D78U,
                              st->job_running ? COL_AMBER : COL_ACCENT);
    char gen_lbl[32];
    if (st->job_running) snprintf(gen_lbl, sizeof(gen_lbl), "GEN %d%%", st->job_pct);
    else snprintf(gen_lbl, sizeof(gen_lbl), st->mode_kit ? "BUILD KIT" : "GEN LOOP");
    flow_draw_text_centered(cv, R_GEN_BTN, 19, COL_TEXT, gen_lbl);

    // Preset Style Quick Chips (y=144..188)
    for (int i = 0; i < 8; i++) {
        FlowRect cr = flow_preset_rect(i);
        int active = (strstr(st->prompt, FLOW_PRESETS[i].prompt) != NULL);
        flow_fill_grad_round_rect(cv, cr, 6,
                                  active ? 0xFF4A1826U : 0xFF26262DU,
                                  active ? 0xFF32101AU : 0xFF1D1D23U,
                                  active ? 0xFF8A2844U : 0xFF40404AU,
                                  active ? COL_ACCENT : COL_GRID);
        flow_draw_text_centered(cv, cr, 14, active ? COL_TEXT : COL_TEXT_DIM, FLOW_PRESETS[i].label);
    }

    // LEFT PANEL: 4x4 Akai MPC Pad Grid (x=14..534, y=200..786)
    flow_fill_round_rect(cv, (FlowRect){14, 200, 520, 586}, 8, COL_PANEL, COL_GRID);
    char kit_hdr[72];
    if (st->has_kit && st->active_kit.kit_name[0]) {
        snprintf(kit_hdr, sizeof(kit_hdr), "KIT: %s.xpm  (%d BPM / %s)",
                 st->active_kit.kit_name, (int)(st->active_kit.bpm + 0.5f), st->active_kit.key);
    } else {
        snprintf(kit_hdr, sizeof(kit_hdr), "AKAI MPC 16-PAD DRUM KIT MAP (A01-A16)");
    }
    flow_draw_text(cv, 26, 212, 19, COL_TEXT, kit_hdr);

    for (int i = 0; i < 16; i++) {
        FlowRect pr = flow_pad_rect(i);
        int flashing = (st->flash_pad == (i + 1) && now_ms < st->flash_until_ms);
        uint32_t base_col = FLOW_DEF_PADS[i].color;
        if (flashing) {
            flow_fill_round_rect(cv, pr, 7, base_col, 0xFFFFFFFFU);
        } else {
            // Rubberized MPC pad card with colored Akai LED top bar
            flow_fill_grad_round_rect(cv, pr, 7, 0xFF282830U, 0xFF1C1C22U, 0xFF42424CU, 0xFF34343EU);
            flow_fill_rect(cv, pr.x + 8, pr.y + 7, pr.w - 16, 5, base_col);
        }

        const char *sname = (st->has_kit && st->active_kit.pads[i].short_name[0])
            ? st->active_kit.pads[i].short_name : FLOW_DEF_PADS[i].def_name;
        const char *pid = (st->has_kit && st->active_kit.pads[i].pad_id[0])
            ? st->active_kit.pads[i].pad_id : FLOW_DEF_PADS[i].pad_id;
        const char *src_tag = (st->has_kit && st->active_kit.pads[i].source[0])
            ? (strncmp(st->active_kit.pads[i].source, "lyria", 5) == 0 ? "LYRIA" : "DSP") : "READY";

        flow_draw_text(cv, pr.x + 10, pr.y + 18, 14, flashing ? COL_BLACK : base_col, pid);
        flow_draw_text(cv, pr.x + pr.w - 50, pr.y + 18, 14, flashing ? COL_BLACK : COL_TEXT_DIM, src_tag);
        flow_draw_text_centered(cv, (FlowRect){pr.x + 4, pr.y + 42, pr.w - 8, 52}, 19, flashing ? COL_BLACK : COL_TEXT, sname);
    }

    char xpm_line[128];
    snprintf(xpm_line, sizeof(xpm_line), "PATH: %s",
             (st->has_kit && st->active_kit.xpm_path[0]) ? st->active_kit.xpm_path : "/media/okcomputer/FlowKits/<KitName>.xpm");
    flow_draw_text(cv, 26, 742, 14, COL_TEXT_DIM, xpm_line);

    // RIGHT PANEL: Recent Kits + Progress Banner + 5-Row Touch Keyboard (x=542..1266, y=200..786)
    for (int i = 0; i < 3; i++) {
        FlowRect rr = flow_recent_rect(i);
        int valid = (i < st->num_recent && st->recent_names[i][0]);
        int sel = (valid && st->has_kit && strcmp(st->recent_names[i], st->active_kit.kit_name) == 0);
        flow_fill_grad_round_rect(cv, rr, 6,
                                  sel ? 0xFF1A3A58U : (valid ? 0xFF282830U : 0xFF18181CU),
                                  sel ? 0xFF13293EU : (valid ? 0xFF1E1E24U : 0xFF141418U),
                                  sel ? COL_CYAN : (valid ? 0xFF42424CU : 0xFF2C2C34U),
                                  sel ? COL_CYAN : (valid ? COL_GRID : 0xFF26262EU));
        flow_draw_text_centered(cv, rr, 14, valid ? COL_TEXT : COL_TEXT_DIM, valid ? st->recent_names[i] : "--");
    }

    FlowRect pbox = {546, 254, 718, 48};
    flow_fill_round_rect(cv, pbox, 6, COL_BLACK, st->job_running ? COL_AMBER : COL_GRID);
    if (st->job_pct > 0 && st->job_pct <= 100) {
        int bw = ((pbox.w - 8) * st->job_pct) / 100;
        flow_fill_rect(cv, pbox.x + 4, pbox.y + 38, bw, 5, st->job_running ? COL_AMBER : COL_NEON);
    }
    flow_draw_text(cv, pbox.x + 12, pbox.y + 10, 14, st->job_running ? COL_AMBER : (st->has_kit ? COL_NEON : COL_TEXT_DIM),
                   st->status_msg[0] ? st->status_msg : "TAP PRESET OR TYPE PROMPT -> BUILD KIT (MPC HARDWARE OUT)");

    // Touch QWERTY Keyboard (y=314..786, Akai slate keycaps)
    int kb_x = 546, kb_w = 720, kb_y = 314;
    const char *rows[4] = {KB_ROW0, KB_ROW1, KB_ROW2, KB_ROW3};
    int row_len[4] = {10, 10, 10, 9};
    for (int r = 0; r < 4; r++) {
        int cnt = row_len[r];
        int gap = 6;
        int kw = (kb_w - (cnt - 1) * gap) / cnt;
        int ry = kb_y + r * 92;
        if (r == 3) {
            cnt = 11;
            kw = (kb_w - (cnt - 1) * gap) / cnt;
            FlowRect sr = {kb_x, ry, kw, 82};
            flow_fill_grad_round_rect(cv, sr, 6,
                                      st->shift_on ? COL_ACCENT : 0xFF32323AU,
                                      st->shift_on ? 0xFFA20D35U : 0xFF232329U,
                                      st->shift_on ? 0xFFFF4D78U : 0xFF4C4C56U,
                                      st->shift_on ? COL_ACCENT : COL_BORDER);
            flow_draw_text_centered(cv, sr, 14, COL_TEXT, "SHIFT");
            for (int c = 0; c < 9; c++) {
                FlowRect kr = {kb_x + (c + 1) * (kw + gap), ry, kw, 82};
                char ch[2] = {st->shift_on ? rows[r][c] : (rows[r][c] >= 'A' && rows[r][c] <= 'Z' ? (char)(rows[r][c] + 32) : rows[r][c]), 0};
                flow_fill_grad_round_rect(cv, kr, 6, 0xFF2C2C33U, 0xFF202026U, 0xFF45454FU, COL_GRID);
                flow_draw_text_centered(cv, kr, 25, COL_TEXT, ch);
            }
            FlowRect br = {kb_x + 10 * (kw + gap), ry, kb_w - 10 * (kw + gap), 82};
            flow_fill_grad_round_rect(cv, br, 6, 0xFF421822U, 0xFF2C1017U, 0xFF6A2838U, COL_RED_DARK);
            flow_draw_text_centered(cv, br, 19, COL_TEXT, "DEL");
        } else {
            for (int c = 0; c < cnt; c++) {
                FlowRect kr = {kb_x + c * (kw + gap), ry, kw, 82};
                char ch0 = rows[r][c];
                char ch[2] = {(r > 0 && !st->shift_on && ch0 >= 'A' && ch0 <= 'Z') ? (char)(ch0 + 32) : ch0, 0};
                flow_fill_grad_round_rect(cv, kr, 6, 0xFF2C2C33U, 0xFF202026U, 0xFF45454FU, COL_GRID);
                flow_draw_text_centered(cv, kr, 25, COL_TEXT, ch);
            }
        }
    }

    // Bottom Action Row (y=682..774)
    int by = kb_y + 4 * 92;
    FlowRect r_clr = {kb_x,       by, 130, 92};
    FlowRect r_spc = {kb_x + 138, by, 374, 92};
    FlowRect r_go  = {kb_x + 520, by, 200, 92};
    flow_fill_grad_round_rect(cv, r_clr, 7, 0xFF421822U, 0xFF2C1017U, 0xFF6A2838U, COL_RED_DARK);
    flow_draw_text_centered(cv, r_clr, 19, COL_TEXT, "CLEAR");
    flow_fill_grad_round_rect(cv, r_spc, 7, 0xFF292930U, 0xFF1F1F25U, 0xFF44444EU, COL_GRID);
    flow_draw_text_centered(cv, r_spc, 19, COL_TEXT_DIM, "SPACE");
    flow_fill_grad_round_rect(cv, r_go,  7, 0xFF24D672U, 0xFF169E4EU, 0xFF56F09AU, COL_NEON);
    flow_draw_text_centered(cv, r_go, 19, COL_BLACK, "GENERATE >");
}

static inline int flow_hit_test(int x, int y, const FlowAppState *st) {
    if (flow_in_rect(x, y, R_BTN_CLOSE) || flow_in_rect(x, y, R_BTN_X)) return ACT_CLOSE;
    if (flow_in_rect(x, y, R_BTN_MAC)) return ACT_TOGGLE_MAC;
    if (flow_in_rect(x, y, R_BTN_KIT)) return ACT_MODE_KIT;
    if (flow_in_rect(x, y, R_BTN_LOOP)) return ACT_MODE_LOOP;
    if (flow_in_rect(x, y, R_BTN_BROWSER)) return ACT_OPEN_BROWSER;
    if (flow_in_rect(x, y, R_BPM_DEC)) return ACT_BPM_DEC;
    if (flow_in_rect(x, y, R_BPM_INC)) return ACT_BPM_INC;
    if (flow_in_rect(x, y, R_KEY_BTN)) return ACT_KEY_CYCLE;
    if (flow_in_rect(x, y, R_GEN_BTN)) return ACT_GENERATE;

    for (int i = 0; i < 8; i++) {
        if (flow_in_rect(x, y, flow_preset_rect(i))) return ACT_PRESET_BASE + i;
    }
    for (int i = 0; i < 16; i++) {
        if (flow_in_rect(x, y, flow_pad_rect(i))) return ACT_PAD_BASE + i;
    }
    for (int i = 0; i < 3; i++) {
        if (flow_in_rect(x, y, flow_recent_rect(i))) return ACT_RECENT_BASE + i;
    }

    int kb_x = 546, kb_w = 720, kb_y = 314;
    const char *rows[4] = {KB_ROW0, KB_ROW1, KB_ROW2, KB_ROW3};
    int row_len[4] = {10, 10, 10, 9};
    for (int r = 0; r < 4; r++) {
        int gap = 6;
        int ry = kb_y + r * 92;
        if (y < ry || y >= ry + 82) continue;
        if (r == 3) {
            int cnt = 11;
            int kw = (kb_w - (cnt - 1) * gap) / cnt;
            FlowRect sr = {kb_x, ry, kw, 82};
            if (flow_in_rect(x, y, sr)) return ACT_KEY_SHIFT;
            for (int c = 0; c < 9; c++) {
                FlowRect kr = {kb_x + (c + 1) * (kw + gap), ry, kw, 82};
                if (flow_in_rect(x, y, kr)) {
                    char ch = rows[r][c];
                    if (!st->shift_on && ch >= 'A' && ch <= 'Z') ch = (char)(ch + 32);
                    return ACT_CHAR_BASE + (unsigned char)ch;
                }
            }
            FlowRect br = {kb_x + 10 * (kw + gap), ry, kb_w - 10 * (kw + gap), 82};
            if (flow_in_rect(x, y, br)) return ACT_KEY_BACK;
        } else {
            int cnt = row_len[r];
            int kw = (kb_w - (cnt - 1) * gap) / cnt;
            for (int c = 0; c < cnt; c++) {
                FlowRect kr = {kb_x + c * (kw + gap), ry, kw, 82};
                if (flow_in_rect(x, y, kr)) {
                    char ch = rows[r][c];
                    if (r > 0 && !st->shift_on && ch >= 'A' && ch <= 'Z') ch = (char)(ch + 32);
                    return ACT_CHAR_BASE + (unsigned char)ch;
                }
            }
        }
    }

    int by = kb_y + 4 * 92;
    if (flow_in_rect(x, y, (FlowRect){kb_x, by, 130, 92})) return ACT_KEY_CLEAR;
    if (flow_in_rect(x, y, (FlowRect){kb_x + 138, by, 374, 92})) return ACT_KEY_SPACE;
    if (flow_in_rect(x, y, (FlowRect){kb_x + 520, by, 200, 92})) return ACT_GENERATE;

    return ACT_NONE;
}

// mpc_boot_ascii_art.h — Polar-arc procedural ASCII renderer for MPC Live boot screen.
// Faithful to the user's logo (asymmetric glowing eye circles + sweeping Cheshire double-row fang grin),
// animated continuously as a wicked/mischievous smirk ("pis pis sırıtış") scaled to boot progress.
#pragma once

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "mpc_boot_font.h"

#define BOOT_SCR_W 1280
#define BOOT_SCR_H 800

typedef struct {
    char ch;
    uint8_t r;
    uint8_t g;
    uint8_t b;
} BootCell;

static inline float boot_clampf(float v, float lo, float hi) {
    return (v < lo) ? lo : ((v > hi) ? hi : v);
}

static inline float boot_smoothstep(float e0, float e1, float x) {
    float t = boot_clampf((x - e0) / (e1 - e0), 0.0f, 1.0f);
    return t * t * (3.0f - 2.0f * t);
}

static inline const char *boot_pick_splash_quote(uint32_t seed, float anim_t, float progress, float *out_fade) {
    static const char *const k_quotes[28] = {
        "16 PADS, ZERO SLEEP!",
        "SWING SET TO 62% (DON'T TELL THE METRONOME)",
        "DIGGING THROUGH DIGITAL CRATES...",
        "STANK FACE INTENSITY: MAXIMUM",
        "DO NOT QUANTIZE THIS GROOVE!",
        "TURNING DUSTY VINYL INTO GOLD...",
        "NOTE REPEAT READY FOR TAKEOFF!",
        "CHOPPING 16 SLICES BEFORE BREAKFAST...",
        "HEAD NODDING AT 92 BPM...",
        "ROGER LINN APPROVED THIS SWING!",
        "NO SAMPLES WERE CLEARED DURING BOOT",
        "ADDING WARM ANALOG GRIT...",
        "LOW-PASSING THE HATER FREQUENCIES...",
        "BOOM BAP ENGINE: ENGAGED!",
        "WAIT TILL YOU HEAR THE 808...",
        "PITCHED DOWN -3 SEMITONES FOR EXTRA SOUL",
        "PAD 1 IS THE KICK. ALWAYS HAS BEEN.",
        "RESAMPLING THROUGH PURE HAKAI VIBES...",
        "J DILLA SENT HIS REGARDS",
        "FULL LEVEL: ON. MERCY: OFF.",
        "16 LEVELS OF PURE NECK SNAP!",
        "CALIBRATING HI-HAT POCKET...",
        "FINDING THAT ONE SNARE THAT HITS DIFFERENT...",
        "YES, THAT LOOP IS IN C MINOR",
        "OVERDRIVING THE OUTPUT BUS (RESPECTFULLY)",
        "Q-LINKS OILED, FADERS LOCKED!",
        "KEEPING IT STRICTLY HARDWARE (SORRY DAW)",
        "BOOTING THE BEAT MACHINE... GRAB A MIC!"
    };
    if (progress >= 0.99f) {
        if (out_fade) *out_fade = 1.0f;
        return "BEAT MACHINE ARMED -- DROP THE NEEDLE!";
    }
    const float slot_dur = 4.0f;
    float t = anim_t < 0.0f ? 0.0f : anim_t;
    int slot = (int)(t / slot_dur);
    float local_t = t - (float)slot * slot_dur;
    float fade = 1.0f;
    if (local_t < 0.25f) {
        fade = (slot == 0) ? 1.0f : (local_t / 0.25f);
    } else if (local_t > slot_dur - 0.25f) {
        fade = (slot_dur - local_t) / 0.25f;
    }
    if (out_fade) *out_fade = boot_clampf(fade, 0.0f, 1.0f);
    uint32_t idx = ((uint32_t)slot * 11U + (seed % 28U)) % 28U;
    return k_quotes[idx];
}

static inline BootCell boot_eval_face_cell(int col, int row, float progress, float anim_t, int wink_mode) {
    BootCell out = {' ', 0, 0, 0};

    if (col < 18 || col > 106 || row < 2 || row > 33) return out;

    float px = (float)BOOT_GRID_X0 + ((float)col + 0.5f) * (float)BOOT_FONT_W;
    float py = (float)BOOT_GRID_Y0 + ((float)row + 0.5f) * (float)BOOT_FONT_H;

    // -----------------------------------------------------------------------
    // 1) Two glowing circular eyes (matching uploaded logo asymmetry!)
    //    Left eye is slightly smaller & lower; right eye is slightly larger & higher.
    // -----------------------------------------------------------------------
    float wake = boot_smoothstep(0.0f, 0.16f, progress);
    float eye_open = 0.38f + 0.62f * wake;

    // Periodic smug/scheming half-squint ("pis pis bakis")
    float cycle = fmodf(anim_t, 6.2f);
    float squint = 0.0f;
    if (progress > 0.25f && progress < 0.98f && cycle > 4.4f && cycle < 5.2f) {
        squint = sinf(((cycle - 4.4f) / 0.8f) * 3.14159265f);
    }
    float glance_x = (progress > 0.20f) ? (5.0f * sinf(anim_t * 1.4f)) : 0.0f;

    float cxL = 482.0f + glance_x * 0.35f;
    float cyL = 222.0f + 4.0f * (1.0f - wake);
    float rL  = 57.0f;

    float cxR = 796.0f + glance_x * 0.35f;
    float cyR = 208.0f + 4.0f * (1.0f - wake);
    float rR  = 67.0f;

    // Finale left-eye sly wink at 100% ready (crisp unbroken cheeky crescent arc)
    if (wink_mode) {
        float wx = (px - cxL) / (rL * 1.02f);
        if (fabsf(wx) <= 1.0f) {
            float arc_y = cyL + 6.0f - 16.0f * cosf(wx * 1.570796f);
            float dyw = fabsf(py - arc_y);
            if (dyw < 9.6f) {
                char ch = '=';
                if (wx < -0.58f) ch = '/';
                else if (wx > 0.58f) ch = '\\';
                else if (dyw < 5.5f && fabsf(wx) < 0.36f) ch = '@';
                out.ch = ch;
                out.r = 250; out.g = 250; out.b = 252;
                return out;
            }
        }
    } else {
        float dxL = px - cxL;
        float dyL = (py - cyL) / fmaxf(0.22f, eye_open * (1.0f - 0.32f * squint));
        float dL  = sqrtf(dxL * dxL + dyL * dyL) / rL;
        float lid_yL = cyL - rL * (0.82f - 0.52f * squint);
        int lid_cutL = (squint > 0.22f && py < lid_yL);

        if (dL <= 1.03f && !lid_cutL) {
            char ch;
            if (eye_open < 0.36f) {
                ch = (dL < 0.55f) ? '=' : '-';
            } else if (dL < 0.52f) {
                ch = '@';
            } else if (dL < 0.74f) {
                ch = '#';
            } else if (dL < 0.87f) {
                ch = '%';
            } else if (dL < 0.96f) {
                ch = '*';
            } else {
                ch = ':';
            }
            float b = boot_clampf(0.58f + 0.48f * (1.03f - dL), 0.58f, 1.0f);
            uint8_t v = (uint8_t)(255.0f * b);
            out.ch = ch; out.r = v; out.g = v; out.b = v;
            return out;
        } else if (lid_cutL && fabsf(dxL) < rL * 0.94f && fabsf(py - lid_yL) < 9.5f) {
            out.ch = '-';
            out.r = 215; out.g = 215; out.b = 220;
            return out;
        }
    }

    // Right eye
    {
        float dxR = px - cxR;
        float dyR = (py - cyR) / fmaxf(0.22f, eye_open * (1.0f - 0.12f * squint));
        float dR  = sqrtf(dxR * dxR + dyR * dyR) / rR;
        if (dR <= 1.03f) {
            char ch;
            if (eye_open < 0.36f) {
                ch = (dR < 0.55f) ? '=' : '-';
            } else if (dR < 0.54f) {
                ch = '@';
            } else if (dR < 0.75f) {
                ch = '#';
            } else if (dR < 0.88f) {
                ch = '%';
            } else if (dR < 0.96f) {
                ch = '*';
            } else {
                ch = ':';
            }
            float b = boot_clampf(0.58f + 0.48f * (1.03f - dR), 0.58f, 1.0f);
            uint8_t v = (uint8_t)(255.0f * b);
            out.ch = ch; out.r = v; out.g = v; out.b = v;
            return out;
        }
    }

    // -----------------------------------------------------------------------
    // 2) Sweeping Polar-Crescent Double-Row Grin
    //    Parameterized around focal point (mx, my) so corner teeth sweep smoothly
    //    into upward cheek hooks EXACTLY like media_1794570213468_a33de162.jpg!
    // -----------------------------------------------------------------------
    float grin_reach = 0.44f + 0.56f * boot_smoothstep(0.03f, 0.50f, progress);
    float teeth_open = boot_smoothstep(0.07f, 0.44f, progress);

    // Subtle sinister corner smirk asymmetry + rhythmic jaw snicker
    float asym_smirk = (progress > 0.10f)
        ? (0.14f * (1.0f - boot_smoothstep(0.45f, 0.88f, progress)) + 0.055f * sinf(anim_t * 1.75f))
        : 0.0f;
    float snicker_lfo = 0.5f + 0.5f * sinf(anim_t * 2.85f);

    // Smile ellipse focal center sits between & slightly below the eyes
    const float foc_x = 638.0f;
    const float foc_y = 224.0f;
    const float Rx    = 355.0f * grin_reach; // Half-width reaches cols ~28..100 at full open
    const float Ry    = 232.0f;              // Center bottom ~y=456; corners curl to ~y=245 beside eye bottoms!

    float nx = (px - foc_x) / fmaxf(60.0f, Rx);
    float ny = (py - foc_y) / Ry;

    // Polar angle from bottom center: theta = 0 at straight down, +/- 1.47 rad sweeps cleanly into cheek corners
    float theta = atan2f(nx, ny); // [-PI, PI]
    float max_theta = 1.05f + 0.42f * boot_smoothstep(0.06f, 0.62f, progress);
    // Asymmetric right-corner hook pulls slightly higher when smirking
    float side_bonus = (theta > 0.0f ? 1.0f : -0.6f) * asym_smirk * 0.18f;
    float lim_theta = max_theta + side_bonus;

    if (fabsf(theta) <= lim_theta + 0.04f && ny > -0.15f) {
        // Ellipse radius along this angle in normalized units
        float r_cell = sqrtf(nx * nx + ny * ny);
        float u = theta / fmaxf(0.5f, lim_theta); // Normalized arc position [-1..+1]
        float au = fabsf(u);

        // Curve slightly flattens in bottom middle and flares cleanly into wide Cheshire cheeks
        float r_spine = 1.0f + 0.030f * sinf(au * 3.14159f) - 0.05f * powf(au, 2.8f);

        // Teeth size tapers sharply from big fierce center incisors/canines down to tiny cheek fangs
        float scale_u = fmaxf(0.18f, 1.0f - 0.80f * powf(au, 1.70f));

        // Upper gumline vs lower gumline in pixel-equivalent radial distance around r_spine
        float approx_px_per_r = 265.0f;
        float dist_from_spine_px = (r_cell - r_spine) * approx_px_per_r;

        // Clear dark mouth cleft separating upper teeth row from lower teeth row
        float cleft_px = (12.0f + 18.0f * teeth_open + 8.0f * snicker_lfo * teeth_open) * scale_u;
        float tooth_len = (16.0f + 50.0f * teeth_open) * scale_u;

        // Early boot thin smirk line (0%..10%)
        if (teeth_open < 0.10f) {
            if (fabsf(dist_from_spine_px) < 9.5f && au < 0.96f) {
                out.ch = (au > 0.72f) ? (u > 0.0f ? '/' : '\\') : ((fabsf(dist_from_spine_px) < 4.5f) ? '=' : '-');
                out.r = 225; out.g = 225; out.b = 228;
                return out;
            }
        }

        // Glint sweep from left fang to right fang
        float glint_pos = -1.25f + fmodf(anim_t * 1.05f, 2.5f);
        float gleam = expf(-((u - glint_pos) * (u - glint_pos)) * 16.0f);

        // 15 distinct razor teeth across full smile crescent with crisp dark valleys
        const float NUM_TEETH_HALF = 7.5f;
        float tooth_coord_top = (u * NUM_TEETH_HALF);
        float tooth_coord_bot = (u * NUM_TEETH_HALF + 0.5f); // Offset half-tooth so lower teeth interlock cleanly!

        // Upper row: sits on INNER side of spine, pointing DOWNWARD (`V`)
        float u_gum = -cleft_px * 0.45f - tooth_len;
        float u_tip = -cleft_px * 0.22f;
        if (dist_from_spine_px >= u_gum && dist_from_spine_px <= u_tip && teeth_open > 0.04f) {
            float fy = (dist_from_spine_px - u_gum) / fmaxf(1.0f, u_tip - u_gum); // 0=root, 1=sharp point
            float ph = tooth_coord_top - 0.09f * u * fy;
            float frac = ph - floorf(ph);
            float dx = (frac - 0.5f) * 2.0f; // [-1..+1] across single tooth pitch
            float half_w = 0.78f * powf(fmaxf(0.0f, 1.0f - fy * 0.90f), 0.78f);

            if (fabsf(dx) <= half_w) {
                float edge = fabsf(dx) / fmaxf(0.05f, half_w);
                char ch;
                if (fy > 0.72f) {
                    ch = 'V';
                } else if (edge > 0.68f && scale_u > 0.26f) {
                    ch = (dx < 0.0f) ? '\\' : '/';
                } else if (edge < 0.45f) {
                    ch = (gleam > 0.40f || scale_u > 0.52f) ? '@' : '#';
                } else {
                    ch = (scale_u > 0.36f) ? '#' : '*';
                }
                int br = (int)(205.0f + 45.0f * (1.0f - edge * 0.55f) + 35.0f * gleam);
                if (br > 255) br = 255;
                out.ch = ch;
                out.r = (uint8_t)br; out.g = (uint8_t)br; out.b = (uint8_t)br;
                return out;
            }
        }

        // Lower row: sits on OUTER side of spine, pointing UPWARD (`^`)
        float l_tip = cleft_px * 0.22f;
        float l_gum = cleft_px * 0.45f + tooth_len * 0.96f;
        if (dist_from_spine_px >= l_tip && dist_from_spine_px <= l_gum && teeth_open > 0.12f) {
            float fy = (l_gum - dist_from_spine_px) / fmaxf(1.0f, l_gum - l_tip); // 0=root, 1=sharp upward point
            float ph = tooth_coord_bot - 0.07f * u * fy;
            float frac = ph - floorf(ph);
            float dx = (frac - 0.5f) * 2.0f;
            float half_w = 0.76f * powf(fmaxf(0.0f, 1.0f - fy * 0.90f), 0.78f);

            if (fabsf(dx) <= half_w) {
                float edge = fabsf(dx) / fmaxf(0.05f, half_w);
                char ch;
                if (fy > 0.72f) {
                    ch = '^';
                } else if (edge > 0.68f && scale_u > 0.26f) {
                    ch = (dx < 0.0f) ? '/' : '\\';
                } else if (edge < 0.45f) {
                    ch = (gleam > 0.40f || scale_u > 0.50f) ? '@' : '#';
                } else {
                    ch = (scale_u > 0.34f) ? '%' : '*';
                }
                int br = (int)(192.0f + 50.0f * (1.0f - edge * 0.55f) + 38.0f * gleam);
                if (br > 255) br = 255;
                out.ch = ch;
                out.r = (uint8_t)br; out.g = (uint8_t)br; out.b = (uint8_t)br;
                return out;
            }
        }

        // Crisp cheek corner curl tips (`)` / `(`) matching the signature curled tips in logo
        if (au > 0.92f && fabsf(dist_from_spine_px) < 12.0f && teeth_open > 0.18f) {
            out.ch = (u > 0.0f) ? ')' : '(';
            out.r = 210; out.g = 210; out.b = 215;
            return out;
        }
    }

    return out;
}

static inline void boot_render_frame_seeded(
    uint32_t *canvas_1280x800,
    float progress,
    float anim_t,
    float elapsed_s,
    const char *stage_label,
    float fade_alpha,
    uint32_t splash_seed
) {
    float fa = boot_clampf(fade_alpha, 0.0f, 1.0f);
    uint32_t pa = (uint32_t)(fa * 255.0f + 0.5f);
    uint32_t bg_px = (pa << 24);
    uint64_t bg_64 = ((uint64_t)bg_px << 32) | (uint64_t)bg_px;
    uint64_t *c64 = (uint64_t *)canvas_1280x800;
    const int n64 = (BOOT_SCR_W * BOOT_SCR_H) / 2;
    for (int i = 0; i < n64; i++) {
        c64[i] = bg_64;
    }

    progress = boot_clampf(progress, 0.0f, 1.0f);
    int pct_int = (int)(progress * 100.0f + 0.5f);
    if (pct_int > 100) pct_int = 100;
    int wink_mode = (progress >= 0.99f) ? 1 : 0;

    char hud_r35[BOOT_GRID_COLS + 4];
    char hud_r39[BOOT_GRID_COLS + 4];
    char hud_r40[BOOT_GRID_COLS + 4];
    char hud_r41[BOOT_GRID_COLS + 4];
    memset(hud_r35, ' ', sizeof(hud_r35));
    memset(hud_r39, ' ', sizeof(hud_r39));
    memset(hud_r40, ' ', sizeof(hud_r40));
    memset(hud_r41, ' ', sizeof(hud_r41));

    float quote_fade = 1.0f;
    const char *quote = boot_pick_splash_quote(splash_seed, anim_t, progress, &quote_fade);
    int qlen = (int)strlen(quote);
    if (qlen > BOOT_GRID_COLS - 8) qlen = BOOT_GRID_COLS - 8;
    int q_left = (BOOT_GRID_COLS - qlen) / 2;
    for (int i = 0; i < qlen; i++) {
        hud_r35[q_left + i] = quote[i];
    }
    float pulse = 0.86f + 0.14f * sinf(anim_t * 4.5f);
    uint8_t q_r = (uint8_t)boot_clampf((wink_mode ? 130.0f : 255.0f) * pulse * quote_fade, 0.0f, 255.0f);
    uint8_t q_g = (uint8_t)boot_clampf((wink_mode ? 255.0f : 224.0f) * pulse * quote_fade, 0.0f, 255.0f);
    uint8_t q_b = (uint8_t)boot_clampf((wink_mode ? 160.0f : 64.0f) * pulse * quote_fade, 0.0f, 255.0f);

    const int bar_left = 20;
    const int bar_w = 72;
    int filled = (int)(progress * (float)bar_w + 0.5f);
    if (filled > bar_w) filled = bar_w;

    char line39[128];
    snprintf(line39, sizeof(line39), "%-66s ELAPSED %6.1fs", "MPC LIVE // HAKAI OS", elapsed_s);
    for (int i = 0; line39[i] && (bar_left + i) < BOOT_GRID_COLS; i++) {
        hud_r39[bar_left + i] = line39[i];
    }

    hud_r40[bar_left] = '[';
    for (int i = 0; i < bar_w; i++) {
        if (i < filled) {
            hud_r40[bar_left + 1 + i] = (i == filled - 1 && pct_int < 100) ? '>' : '@';
        } else {
            hud_r40[bar_left + 1 + i] = ':';
        }
    }
    hud_r40[bar_left + 1 + bar_w] = ']';
    char pct_tag[24];
    snprintf(pct_tag, sizeof(pct_tag), "  [%3d%%]", pct_int);
    for (int i = 0; pct_tag[i] && (bar_left + bar_w + 2 + i) < BOOT_GRID_COLS; i++) {
        hud_r40[bar_left + bar_w + 2 + i] = pct_tag[i];
    }

    char line41[128];
    snprintf(line41, sizeof(line41), "> %s", stage_label ? stage_label : "BOOTING MPC...");
    for (int i = 0; line41[i] && (bar_left + i) < BOOT_GRID_COLS; i++) {
        hud_r41[bar_left + i] = line41[i];
    }

    uint32_t pa_shift = (pa << 24);

    for (int row = 2; row < BOOT_GRID_ROWS; row++) {
        int c_lo = 0, c_hi = BOOT_GRID_COLS;
        if (row <= 33) {
            c_lo = 18; c_hi = 107;
        } else if (row == 35) {
            c_lo = q_left; c_hi = q_left + qlen;
        } else if (row >= 39) {
            c_lo = bar_left; c_hi = BOOT_GRID_COLS;
        } else {
            continue;
        }
        for (int col = c_lo; col < c_hi; col++) {
            BootCell c = {' ', 0, 0, 0};
            if (row <= 33) {
                c = boot_eval_face_cell(col, row, progress, anim_t, wink_mode);
            } else if (row == 35 && hud_r35[col] != ' ') {
                c.ch = hud_r35[col];
                c.r = q_r; c.g = q_g; c.b = q_b;
            } else if (row == 39 && hud_r39[col] != ' ') {
                c.ch = hud_r39[col];
                c.r = (col >= bar_left + 40) ? 235 : 190;
                c.g = (col >= bar_left + 40) ? 245 : 195;
                c.b = (col >= bar_left + 40) ? 255 : 210;
            } else if (row == 40 && hud_r40[col] != ' ') {
                c.ch = hud_r40[col];
                if (col > bar_left && col <= bar_left + filled) {
                    if (pct_int >= 100) {
                        c.r = 80; c.g = 240; c.b = 140;
                    } else if (col >= bar_left + filled - 1) {
                        c.r = 255; c.g = 255; c.b = 255;
                    } else {
                        c.r = 225; c.g = 36; c.b = 76;
                    }
                } else if (col == bar_left || col == bar_left + 1 + bar_w) {
                    c.r = 210; c.g = 215; c.b = 225;
                } else if (col > bar_left + 1 + bar_w) {
                    c.r = (pct_int >= 100) ? 80 : 230;
                    c.g = (pct_int >= 100) ? 240 : 235;
                    c.b = (pct_int >= 100) ? 140 : 245;
                } else {
                    c.r = 65; c.g = 68; c.b = 78;
                }
            } else if (row == 41 && hud_r41[col] != ' ') {
                c.ch = hud_r41[col];
                c.r = 145; c.g = 152; c.b = 166;
            }

            if (c.ch <= ' ' || c.ch > '~') continue;

            uint32_t cr = (uint32_t)((float)c.r * fa);
            uint32_t cg = (uint32_t)((float)c.g * fa);
            uint32_t cb = (uint32_t)((float)c.b * fa);

            const uint8_t *glyph = mpc_boot_font[(int)c.ch - 32];
            int gx0 = BOOT_GRID_X0 + col * BOOT_FONT_W;
            int gy0 = BOOT_GRID_Y0 + row * BOOT_FONT_H;

            for (int gy = 0; gy < BOOT_FONT_H; gy++) {
                uint32_t *dst = canvas_1280x800 + (size_t)(gy0 + gy) * BOOT_SCR_W + (size_t)gx0;
                const uint8_t *grow = glyph + (size_t)gy * BOOT_FONT_W;
                for (int gx = 0; gx < BOOT_FONT_W; gx++) {
                    uint8_t a = grow[gx];
                    if (a < 8) continue;
                    if (a >= 250) {
                        dst[gx] = pa_shift | (cr << 16) | (cg << 8) | cb;
                    } else {
                        uint32_t pr = (cr * (uint32_t)a) >> 8;
                        uint32_t pg = (cg * (uint32_t)a) >> 8;
                        uint32_t pb = (cb * (uint32_t)a) >> 8;
                        dst[gx] = pa_shift | (pr << 16) | (pg << 8) | pb;
                    }
                }
            }
        }
    }
}

static inline void boot_render_frame(
    uint32_t *canvas_1280x800,
    float progress,
    float anim_t,
    float elapsed_s,
    const char *stage_label,
    float fade_alpha
) {
    boot_render_frame_seeded(canvas_1280x800, progress, anim_t, elapsed_s, stage_label, fade_alpha, 1U);
}

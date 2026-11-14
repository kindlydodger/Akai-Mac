// mpc_boot_splash_shared.h — Adaptive boot timing, milestone detection, SHM handoff, and fast 270-deg rotation.
#pragma once

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "mpc_boot_ascii_art.h"

#define MPC_PHYS_W 800
#define MPC_PHYS_H 1280

#define MPC_BOOT_TIMING_FILE    "/data/mpc-control/boot_anim_timing.dat"
#define MPC_BOOT_SHM_STATE      "/dev/shm/.mpc_boot_state"
#define MPC_BOOT_SHM_DRM_ACTIVE "/dev/shm/.mpc_boot_drm_active"

typedef struct {
    float cold_total_s;
    float warm_total_s;
    uint32_t cold_samples;
    uint32_t warm_samples;
} BootTimingProfile;

typedef struct {
    uint32_t magic;        // 0x4D504332 'MPC2'
    uint32_t is_warm;      // 0 = cold kernel boot (use /proc/uptime), 1 = service restart
    uint32_t splash_seed;  // Deterministic per-boot seed for Minecraft-style splash quotes
    uint32_t reserved;
    double   start_mono_s; // CLOCK_MONOTONIC at sequence start
    double   start_uptime; // /proc/uptime at sequence start
} BootShmState;

#define BOOT_SHM_MAGIC 0x4D504332U

static inline double boot_now_mono_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static inline void boot_pace_frame(double *next_deadline_s, double interval_s) {
    double now_m = boot_now_mono_s();
    if (*next_deadline_s <= 0.0) {
        *next_deadline_s = now_m + interval_s;
    } else {
        *next_deadline_s += interval_s;
        if (*next_deadline_s < now_m) {
            *next_deadline_s = now_m; // Resync cleanly without burst catchup if stalled
        }
    }
    double rem = *next_deadline_s - now_m;
    if (rem > 0.0005) {
        struct timespec req;
        req.tv_sec = (time_t)rem;
        req.tv_nsec = (long)((rem - (double)req.tv_sec) * 1e9);
        nanosleep(&req, NULL);
    }
}

static inline uint32_t boot_gen_random_seed(void) {
    uint32_t s = (uint32_t)(boot_now_mono_s() * 1000.0) ^ (uint32_t)getpid();
    int fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
    if (fd >= 0) {
        uint32_t r = 0;
        if (read(fd, &r, sizeof(r)) == (ssize_t)sizeof(r)) s ^= r;
        close(fd);
    }
    return s ? s : 1U;
}

static inline double boot_read_proc_uptime_s(void) {
    FILE *f = fopen("/proc/uptime", "r");
    if (!f) return -1.0;
    double up = -1.0;
    if (fscanf(f, "%lf", &up) != 1) up = -1.0;
    fclose(f);
    return up;
}

static inline void boot_load_timing_profile(BootTimingProfile *prof, const char *path) {
    prof->cold_total_s = 23.0f;
    prof->warm_total_s = 9.0f;
    prof->cold_samples = 0;
    prof->warm_samples = 0;
    if (!path) path = MPC_BOOT_TIMING_FILE;
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[128];
    while (fgets(line, sizeof(line), f)) {
        float fv = 0.0f;
        uint32_t uv = 0;
        if (sscanf(line, "cold_total_s=%f", &fv) == 1) {
            prof->cold_total_s = boot_clampf(fv, 8.0f, 60.0f);
        } else if (sscanf(line, "warm_total_s=%f", &fv) == 1) {
            prof->warm_total_s = boot_clampf(fv, 4.0f, 35.0f);
        } else if (sscanf(line, "cold_samples=%u", &uv) == 1) {
            prof->cold_samples = uv;
        } else if (sscanf(line, "warm_samples=%u", &uv) == 1) {
            prof->warm_samples = uv;
        }
    }
    fclose(f);
}

static inline void boot_save_timing_sample(BootTimingProfile *prof, const char *path, int is_warm, float observed_s) {
    if (!prof || observed_s < 3.0f || observed_s > 60.0f) return;
    if (!path) path = MPC_BOOT_TIMING_FILE;
    if (is_warm) {
        if (prof->warm_samples == 0) prof->warm_total_s = observed_s;
        else prof->warm_total_s = 0.65f * prof->warm_total_s + 0.35f * observed_s;
        prof->warm_total_s = boot_clampf(prof->warm_total_s, 4.5f, 35.0f);
        prof->warm_samples++;
    } else {
        if (prof->cold_samples == 0) prof->cold_total_s = observed_s;
        else prof->cold_total_s = 0.65f * prof->cold_total_s + 0.35f * observed_s;
        prof->cold_total_s = boot_clampf(prof->cold_total_s, 10.0f, 50.0f);
        prof->cold_samples++;
    }
    char tmp_path[256];
    snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", path);
    FILE *f = fopen(tmp_path, "w");
    if (!f) return;
    fprintf(f, "cold_total_s=%.3f\nwarm_total_s=%.3f\ncold_samples=%u\nwarm_samples=%u\n",
            prof->cold_total_s, prof->warm_total_s, prof->cold_samples, prof->warm_samples);
    fclose(f);
    rename(tmp_path, path);
}

static inline int boot_read_shm_state(BootShmState *st, const char *shm_path) {
    if (!shm_path) shm_path = MPC_BOOT_SHM_STATE;
    FILE *f = fopen(shm_path, "rb");
    if (!f) return 0;
    size_t n = fread(st, 1, sizeof(*st), f);
    fclose(f);
    return (n == sizeof(*st) && st->magic == BOOT_SHM_MAGIC);
}

static inline void boot_write_shm_state(const BootShmState *st, const char *shm_path) {
    if (!shm_path) shm_path = MPC_BOOT_SHM_STATE;
    char tmp[256];
    snprintf(tmp, sizeof(tmp), "%s.tmp", shm_path);
    FILE *f = fopen(tmp, "wb");
    if (!f) return;
    fwrite(st, 1, sizeof(*st), f);
    fclose(f);
    rename(tmp, shm_path);
}

static inline void boot_init_or_resume_state(BootShmState *st, const char *shm_path, int allow_cold_uptime_resume) {
    double now_m = boot_now_mono_s();
    double up = boot_read_proc_uptime_s();
    if (allow_cold_uptime_resume && boot_read_shm_state(st, shm_path)) {
        double age = now_m - st->start_mono_s;
        if (age >= 0.0 && age < 60.0) {
            return;
        }
    }
    st->magic = BOOT_SHM_MAGIC;
    st->start_mono_s = now_m;
    st->start_uptime = (up > 0.0) ? up : 0.0;
    st->splash_seed = boot_gen_random_seed();
    st->reserved = 0;
    // Heuristic: if /proc/uptime is under 16s at splash start, this is a genuine cold boot;
    // otherwise it's an acvs service restart after the board is already up.
    st->is_warm = (up > 16.0 || up < 0.0) ? 1 : 0;
    boot_write_shm_state(st, shm_path);
}

// Compute current elapsed seconds on the unified boot timeline.
static inline float boot_compute_elapsed_s(const BootShmState *st) {
    double now_m = boot_now_mono_s();
    double dt = now_m - st->start_mono_s;
    if (dt < 0.0) dt = 0.0;
    if (!st->is_warm && st->start_uptime > 0.0 && st->start_uptime < 18.0) {
        return (float)(st->start_uptime + dt);
    }
    return (float)dt;
}

// Fast non-blocking check whether hw:1,0,1 ALSA bridge has bound in the CURRENT run inside /tmp/mpc_hw_bridge.log
// Cached for 0.20s so 30 FPS rendering never triggers repeated file I/O every frame.
static inline int boot_check_bridge_ready(void) {
    static double last_check_m = -1.0;
    static int last_res = 0;
    double now_m = boot_now_mono_s();
    if (last_res == 1 || (last_check_m > 0.0 && (now_m - last_check_m) < 0.20)) {
        return last_res;
    }
    last_check_m = now_m;
    FILE *f = fopen("/tmp/mpc_hw_bridge.log", "r");
    if (!f) return 0;
    if (fseek(f, 0, SEEK_END) == 0) {
        long sz = ftell(f);
        if (sz > 4096) fseek(f, sz - 4096, SEEK_SET);
        else fseek(f, 0, SEEK_SET);
    }
    char buf[4096];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = '\0';
    // Scan from the last "mpc_hw_bridge listening" header forward so old runs never falsely trigger ready!
    const char *cur = buf;
    const char *p = buf;
    while ((p = strstr(p, "mpc_hw_bridge listening")) != NULL) {
        cur = p;
        p += 20;
    }
    last_res = (strstr(cur, "priv_in=") != NULL || strstr(cur, "bound priv") != NULL) ? 1 : 0;
    return last_res;
}

// Evaluate proportional progress [0.0 .. 0.97] (or smoothly toward 1.0 when ui_ready) + real stage label.
static inline float boot_eval_progress_and_stage(
    const BootShmState *st,
    const BootTimingProfile *prof,
    int drm_active,
    int ui_ready,
    float *out_elapsed_s,
    char *stage_buf,
    size_t stage_cap
) {
    double now_m = boot_now_mono_s();
    double dt = now_m - st->start_mono_s;
    if (dt < 0.0) dt = 0.0;
    float elapsed = boot_compute_elapsed_s(st);
    if (out_elapsed_s) *out_elapsed_s = elapsed;

    float expected = st->is_warm ? prof->warm_total_s : prof->cold_total_s;
    if (expected < 4.5f) expected = 4.5f;

    float prog;
    if (ui_ready) {
        prog = 1.0f;
    } else if (elapsed <= expected * 0.90f) {
        prog = elapsed / expected;
    } else {
        // Gentle asymptotic advance from 90% -> 97% so bar stays visibly alive if boot runs slightly long
        float over = elapsed - expected * 0.90f;
        prog = 0.90f + 0.07f * (1.0f - expf(-over / 4.5f));
        if (prog > 0.97f) prog = 0.97f;
    }
    // Smooth cold-boot catchup from U-Boot Frame 0 over the first 0.85s so 0% doesn't jump straight to ~17%!
    if (!ui_ready && !st->is_warm && st->start_uptime > 0.5 && dt < 0.85) {
        float t = boot_clampf((float)dt / 0.85f, 0.0f, 1.0f);
        float ease = t * t * (3.0f - 2.0f * t);
        prog *= (0.08f + 0.92f * ease);
    }

    int bridge_ok = boot_check_bridge_ready();
    const char *msg;
    if (ui_ready) {
        msg = "MPC LIVE READY";
    } else if (prog < 0.22f && !drm_active) {
        msg = "MOUNTING OVERLAYFS & KERNEL...";
    } else if (!drm_active && prog < 0.42f) {
        msg = st->is_warm ? "WARMING UP ACVS SERVICE..." : "INITIALIZING DRM & DISPLAY...";
    } else if (!drm_active) {
        msg = "STARTING HAKAI / MPC CORE...";
    } else if (!bridge_ok && prog < 0.78f) {
        msg = "SCANNING SYNTH PLUGINS & ALSA...";
    } else if (!bridge_ok) {
        msg = "BINDING CONTROL SURFACE & MIDI...";
    } else {
        msg = "RENDERING JUCE WORKSPACE UI...";
    }
    if (stage_buf && stage_cap > 0) {
        snprintf(stage_buf, stage_cap, "%s", msg);
    }
    return prog;
}

// Fast 16x16 L1-cache-tiled 270-degree CCW rotation (1280x800 -> 800x1280 physical, little-endian XRGB/ARGB8888).
// Keeps src cache lines resident in L1 while emitting sequential 16-pixel contiguous framebuffer stores!
static inline void boot_rotate_canvas_to_phys(
    const uint32_t * restrict canvas_1280x800,
    uint32_t * restrict dst_phys,
    int dst_stride_u32
) {
    for (int by = 0; by < MPC_PHYS_H; by += 16) {
        for (int bx = 0; bx < MPC_PHYS_W; bx += 16) {
            for (int py = by; py < by + 16; py++) {
                int lx = (BOOT_SCR_W - 1) - py;
                uint32_t * restrict dst_row = dst_phys + (size_t)py * (size_t)dst_stride_u32 + bx;
                const uint32_t * restrict src_col = canvas_1280x800 + (size_t)bx * BOOT_SCR_W + lx;
                for (int dx = 0; dx < 16; dx++) {
                    dst_row[dx] = src_col[(size_t)dx * BOOT_SCR_W];
                }
            }
        }
    }
}

// Write exact 4,096,000-byte raw BGRA ACV8 splash partition image (`1280 * 800` physical height)
static inline int boot_write_acv8_bgra(const char *out_path, const uint32_t *canvas_1280x800) {
    uint32_t *phys = (uint32_t *)malloc((size_t)MPC_PHYS_W * MPC_PHYS_H * sizeof(uint32_t));
    if (!phys) return -1;
    boot_rotate_canvas_to_phys(canvas_1280x800, phys, MPC_PHYS_W);
    FILE *f = fopen(out_path, "wb");
    if (!f) {
        free(phys);
        return -1;
    }
    size_t expected = (size_t)MPC_PHYS_W * MPC_PHYS_H;
    size_t written = fwrite(phys, sizeof(uint32_t), expected, f);
    fclose(f);
    free(phys);
    return (written == expected) ? 0 : -1;
}

// =========================================================================
// 4-Layer Minecraft-Style Boot Message Bank (JSON Loader & Priority Selector)
// =========================================================================
#define MPC_BOOT_MESSAGES_FILE   "/data/mpc-control/boot_messages.json"
#define MPC_BOOT_MAX_STAGES      32
#define MPC_BOOT_MAX_TIPS        64
#define MPC_BOOT_MAX_SPLASHES    64
#define MPC_BOOT_MAX_TRIGGERS    16
#define MPC_BOOT_MAX_DATES       8
#define MPC_BOOT_MAX_TRIG_MSGS   8
#define MPC_BOOT_MSG_LEN         112

typedef enum {
    BOOT_TRIG_NONE = 0,
    BOOT_TRIG_DATE = 1,
    BOOT_TRIG_TIME_RANGE = 2
} BootTriggerType;

typedef struct {
    char id[32];
    int priority;
    BootTriggerType type;
    char dates[MPC_BOOT_MAX_DATES][8];
    int n_dates;
    int start_hour;
    int end_hour;
    char messages[MPC_BOOT_MAX_TRIG_MSGS][MPC_BOOT_MSG_LEN];
    int n_messages;
} BootSpecialTrigger;

typedef struct {
    char stages[MPC_BOOT_MAX_STAGES][MPC_BOOT_MSG_LEN];
    int n_stages;
    char tips[MPC_BOOT_MAX_TIPS][MPC_BOOT_MSG_LEN];
    int n_tips;
    char splashes[MPC_BOOT_MAX_SPLASHES][MPC_BOOT_MSG_LEN];
    int n_splashes;
    BootSpecialTrigger triggers[MPC_BOOT_MAX_TRIGGERS];
    int n_triggers;
    int loaded_from_file;
} BootMessageBank;

static inline void boot_copy_str(char *dst, size_t cap, const char *src) {
    if (!dst || cap == 0) return;
    if (!src) {
        dst[0] = '\0';
        return;
    }
    size_t n = strlen(src);
    if (n >= cap) n = cap - 1;
    memcpy(dst, src, n);
    dst[n] = '\0';
}

static inline void boot_json_skip_ws(const char **p) {
    const char *s = *p;
    while (*s == ' ' || *s == '\t' || *s == '\r' || *s == '\n') s++;
    *p = s;
}

static inline int boot_json_expect(const char **p, char ch) {
    boot_json_skip_ws(p);
    if (**p != ch) return -1;
    (*p)++;
    return 0;
}

static inline int boot_json_parse_string(const char **p, char *out, int cap) {
    boot_json_skip_ws(p);
    const char *s = *p;
    if (*s != '"') return -1;
    s++;
    int len = 0;
    while (*s && *s != '"') {
        unsigned char ch = (unsigned char)*s++;
        if (ch == '\\' && *s) {
            char esc = *s++;
            if (esc == '"' || esc == '\\' || esc == '/') ch = (unsigned char)esc;
            else if (esc == 'n' || esc == 'r' || esc == 't') ch = ' ';
            else if (esc == 'b' || esc == 'f') ch = ' ';
            else if (esc == 'u') {
                for (int k = 0; k < 4 && *s; k++) s++;
                ch = '?';
            } else {
                ch = (unsigned char)esc;
            }
        }
        if (out && len + 1 < cap) {
            out[len++] = (char)ch;
        }
    }
    if (out && cap > 0) out[len] = '\0';
    if (*s != '"') return -1;
    *p = s + 1;
    return 0;
}

static inline int boot_json_parse_int(const char **p, int *out) {
    boot_json_skip_ws(p);
    char *endptr = NULL;
    long v = strtol(*p, &endptr, 10);
    if (endptr == *p) return -1;
    *out = (int)v;
    *p = endptr;
    return 0;
}

static inline int boot_json_skip_value(const char **p, int depth) {
    if (depth > 16) return -1;
    boot_json_skip_ws(p);
    char ch = **p;
    if (ch == '"') {
        return boot_json_parse_string(p, NULL, 0);
    }
    if (ch == '{') {
        (*p)++;
        boot_json_skip_ws(p);
        if (**p == '}') { (*p)++; return 0; }
        while (**p) {
            if (boot_json_parse_string(p, NULL, 0) != 0) return -1;
            if (boot_json_expect(p, ':') != 0) return -1;
            if (boot_json_skip_value(p, depth + 1) != 0) return -1;
            boot_json_skip_ws(p);
            if (**p == ',') { (*p)++; continue; }
            if (**p == '}') { (*p)++; return 0; }
            return -1;
        }
        return -1;
    }
    if (ch == '[') {
        (*p)++;
        boot_json_skip_ws(p);
        if (**p == ']') { (*p)++; return 0; }
        while (**p) {
            if (boot_json_skip_value(p, depth + 1) != 0) return -1;
            boot_json_skip_ws(p);
            if (**p == ',') { (*p)++; continue; }
            if (**p == ']') { (*p)++; return 0; }
            return -1;
        }
        return -1;
    }
    const char *s = *p;
    while (*s && *s != ',' && *s != '}' && *s != ']' &&
           *s != ' ' && *s != '\t' && *s != '\r' && *s != '\n') {
        s++;
    }
    if (s == *p) return -1;
    *p = s;
    return 0;
}

static inline int boot_json_parse_str_list(
    const char **p, char *dst_base, int stride, int *count, int max_count
) {
    if (boot_json_expect(p, '[') != 0) return -1;
    boot_json_skip_ws(p);
    if (**p == ']') { (*p)++; return 0; }
    while (**p) {
        if (*count < max_count) {
            char *slot = dst_base + (*count) * stride;
            if (boot_json_parse_string(p, slot, stride) != 0) return -1;
            if (slot[0]) (*count)++;
        } else {
            if (boot_json_skip_value(p, 0) != 0) return -1;
        }
        boot_json_skip_ws(p);
        if (**p == ',') { (*p)++; continue; }
        if (**p == ']') { (*p)++; return 0; }
        return -1;
    }
    return -1;
}

static inline int boot_json_parse_trigger(const char **p, BootSpecialTrigger *tr) {
    memset(tr, 0, sizeof(*tr));
    if (boot_json_expect(p, '{') != 0) return -1;
    boot_json_skip_ws(p);
    if (**p == '}') { (*p)++; return 0; }

    while (**p) {
        char key[32];
        if (boot_json_parse_string(p, key, (int)sizeof(key)) != 0) return -1;
        if (boot_json_expect(p, ':') != 0) return -1;

        if (strcmp(key, "id") == 0) {
            if (boot_json_parse_string(p, tr->id, (int)sizeof(tr->id)) != 0) return -1;
        } else if (strcmp(key, "priority") == 0) {
            if (boot_json_parse_int(p, &tr->priority) != 0) return -1;
        } else if (strcmp(key, "type") == 0) {
            char tbuf[24];
            if (boot_json_parse_string(p, tbuf, (int)sizeof(tbuf)) != 0) return -1;
            if (strcmp(tbuf, "date") == 0) tr->type = BOOT_TRIG_DATE;
            else if (strcmp(tbuf, "time_range") == 0) tr->type = BOOT_TRIG_TIME_RANGE;
        } else if (strcmp(key, "dates") == 0) {
            if (boot_json_parse_str_list(p, &tr->dates[0][0], 8, &tr->n_dates, MPC_BOOT_MAX_DATES) != 0) return -1;
        } else if (strcmp(key, "start_hour") == 0) {
            if (boot_json_parse_int(p, &tr->start_hour) != 0) return -1;
        } else if (strcmp(key, "end_hour") == 0) {
            if (boot_json_parse_int(p, &tr->end_hour) != 0) return -1;
        } else if (strcmp(key, "messages") == 0) {
            if (boot_json_parse_str_list(p, &tr->messages[0][0], MPC_BOOT_MSG_LEN,
                                         &tr->n_messages, MPC_BOOT_MAX_TRIG_MSGS) != 0) return -1;
        } else {
            if (boot_json_skip_value(p, 0) != 0) return -1;
        }

        boot_json_skip_ws(p);
        if (**p == ',') { (*p)++; continue; }
        if (**p == '}') { (*p)++; return 0; }
        return -1;
    }
    return -1;
}

static inline void boot_populate_bank_fallbacks(BootMessageBank *bank) {
    static const char *const fb_stages[] = {
        "Waking Cortex-A17 & checking sample RAM...",
        "Calibrating 16 velocity-sensitive pads...",
        "Warming up DSP engine & 44.1kHz clock...",
        "Scanning SD crates & internal sample vault...",
        "Binding MIDI clock & control surface...",
        "Mounting JUCE workspace... drop it!"
    };
    static const char *const fb_tips[] = {
        "Hold 16 LEVELS to spread one crisp drum hit across a full melodic scale.",
        "Dial Swing between 54% and 62% for classic head-nod without rushing.",
        "Slice slightly ahead of transients, or turn on Zero-Crossing to kill clicks."
    };
    static const char *const fb_splashes[] = {
        "16 pads, zero sleep!",
        "62% swing or bust!",
        "Dusty vinyl approved!",
        "Also try SP-404!"
    };
    if (bank->n_stages == 0) {
        for (int i = 0; i < (int)(sizeof(fb_stages) / sizeof(fb_stages[0])); i++) {
            boot_copy_str(bank->stages[bank->n_stages++], MPC_BOOT_MSG_LEN, fb_stages[i]);
        }
    }
    if (bank->n_tips == 0) {
        for (int i = 0; i < (int)(sizeof(fb_tips) / sizeof(fb_tips[0])); i++) {
            boot_copy_str(bank->tips[bank->n_tips++], MPC_BOOT_MSG_LEN, fb_tips[i]);
        }
    }
    if (bank->n_splashes == 0) {
        for (int i = 0; i < (int)(sizeof(fb_splashes) / sizeof(fb_splashes[0])); i++) {
            boot_copy_str(bank->splashes[bank->n_splashes++], MPC_BOOT_MSG_LEN, fb_splashes[i]);
        }
    }
}

static inline int boot_parse_messages_json(BootMessageBank *bank, const char *json_text) {
    const char *p = json_text;
    if (boot_json_expect(&p, '{') != 0) return -1;
    boot_json_skip_ws(&p);
    if (*p == '}') return 0;

    while (*p) {
        char key[32];
        if (boot_json_parse_string(&p, key, (int)sizeof(key)) != 0) return -1;
        if (boot_json_expect(&p, ':') != 0) return -1;

        if (strcmp(key, "boot_stages") == 0) {
            if (boot_json_parse_str_list(&p, &bank->stages[0][0], MPC_BOOT_MSG_LEN,
                                         &bank->n_stages, MPC_BOOT_MAX_STAGES) != 0) return -1;
        } else if (strcmp(key, "splashes") == 0) {
            if (boot_json_parse_str_list(&p, &bank->splashes[0][0], MPC_BOOT_MSG_LEN,
                                         &bank->n_splashes, MPC_BOOT_MAX_SPLASHES) != 0) return -1;
        } else if (strcmp(key, "tips") == 0) {
            if (boot_json_expect(&p, '{') != 0) return -1;
            boot_json_skip_ws(&p);
            if (*p != '}') {
                while (*p) {
                    char cat[32];
                    if (boot_json_parse_string(&p, cat, (int)sizeof(cat)) != 0) return -1;
                    if (boot_json_expect(&p, ':') != 0) return -1;
                    if (boot_json_parse_str_list(&p, &bank->tips[0][0], MPC_BOOT_MSG_LEN,
                                                 &bank->n_tips, MPC_BOOT_MAX_TIPS) != 0) return -1;
                    boot_json_skip_ws(&p);
                    if (*p == ',') { p++; continue; }
                    if (*p == '}') { p++; break; }
                    return -1;
                }
            } else {
                p++;
            }
        } else if (strcmp(key, "special_triggers") == 0) {
            if (boot_json_expect(&p, '[') != 0) return -1;
            boot_json_skip_ws(&p);
            if (*p != ']') {
                while (*p) {
                    if (bank->n_triggers < MPC_BOOT_MAX_TRIGGERS) {
                        BootSpecialTrigger tr;
                        if (boot_json_parse_trigger(&p, &tr) != 0) return -1;
                        if (tr.type != BOOT_TRIG_NONE && tr.n_messages > 0) {
                            bank->triggers[bank->n_triggers++] = tr;
                        }
                    } else {
                        if (boot_json_skip_value(&p, 0) != 0) return -1;
                    }
                    boot_json_skip_ws(&p);
                    if (*p == ',') { p++; continue; }
                    if (*p == ']') { p++; break; }
                    return -1;
                }
            } else {
                p++;
            }
        } else {
            if (boot_json_skip_value(&p, 0) != 0) return -1;
        }

        boot_json_skip_ws(&p);
        if (*p == ',') { p++; continue; }
        if (*p == '}') break;
        return -1;
    }

    // Insertion-sort special triggers by descending priority
    for (int i = 1; i < bank->n_triggers; i++) {
        BootSpecialTrigger key_tr = bank->triggers[i];
        int j = i - 1;
        while (j >= 0 && bank->triggers[j].priority < key_tr.priority) {
            bank->triggers[j + 1] = bank->triggers[j];
            j--;
        }
        bank->triggers[j + 1] = key_tr;
    }
    return 0;
}

static inline void boot_load_message_bank(BootMessageBank *bank, const char *path) {
    memset(bank, 0, sizeof(*bank));
    const char *candidates[4] = {
        path,
        MPC_BOOT_MESSAGES_FILE,
        "boot_messages.json",
        "mpc_control/boot_anim/boot_messages.json"
    };
    FILE *f = NULL;
    for (int i = 0; i < 4; i++) {
        if (!candidates[i] || !candidates[i][0]) continue;
        f = fopen(candidates[i], "rb");
        if (f) break;
    }
    if (f) {
        if (fseek(f, 0, SEEK_END) == 0) {
            long sz = ftell(f);
            if (sz > 2 && sz <= 262144 && fseek(f, 0, SEEK_SET) == 0) {
                char *buf = (char *)malloc((size_t)sz + 1);
                if (buf) {
                    size_t nread = fread(buf, 1, (size_t)sz, f);
                    buf[nread] = '\0';
                    if (boot_parse_messages_json(bank, buf) == 0) {
                        bank->loaded_from_file = 1;
                    } else {
                        memset(bank, 0, sizeof(*bank));
                    }
                    free(buf);
                }
            }
        }
        fclose(f);
    }
    boot_populate_bank_fallbacks(bank);
}

static inline const char *boot_resolve_special_trigger(
    const BootMessageBank *bank,
    const struct tm *lt,
    uint32_t seed,
    float anim_t,
    const char **out_id
) {
    if (!bank || !lt || bank->n_triggers <= 0) return NULL;
    char mm_dd[8];
    snprintf(mm_dd, sizeof(mm_dd), "%02d-%02d", lt->tm_mon + 1, lt->tm_mday);
    int hour = lt->tm_hour;

    for (int i = 0; i < bank->n_triggers; i++) {
        const BootSpecialTrigger *tr = &bank->triggers[i];
        if (tr->n_messages <= 0) continue;
        int matched = 0;
        if (tr->type == BOOT_TRIG_DATE) {
            for (int d = 0; d < tr->n_dates; d++) {
                if (strcmp(mm_dd, tr->dates[d]) == 0) {
                    matched = 1;
                    break;
                }
            }
        } else if (tr->type == BOOT_TRIG_TIME_RANGE) {
            if (tr->start_hour <= tr->end_hour) {
                matched = (hour >= tr->start_hour && hour < tr->end_hour);
            } else {
                matched = (hour >= tr->start_hour || hour < tr->end_hour);
            }
        }
        if (matched) {
            if (out_id) *out_id = tr->id;
            uint32_t step = (anim_t >= 0.0f) ? (uint32_t)(anim_t / 7.0f) : 0U;
            uint32_t idx = (seed ^ (step * 2654435761U)) % (uint32_t)tr->n_messages;
            return tr->messages[idx];
        }
    }
    return NULL;
}

static inline void boot_select_from_bank(
    const BootMessageBank *bank,
    uint32_t seed,
    float anim_t,
    float progress,
    const struct tm *override_tm,
    char *out_stage,
    size_t stage_cap,
    char *out_splash,
    size_t splash_cap,
    char *out_tip,
    size_t tip_cap
) {
    if (!bank) return;
    struct tm local_tm;
    const struct tm *lt = override_tm;
    if (!lt) {
        time_t now_t = time(NULL);
        localtime_r(&now_t, &local_tm);
        lt = &local_tm;
    }

    float p = boot_clampf(progress, 0.0f, 1.0f);
    if (out_stage && stage_cap > 0 && bank->n_stages > 0) {
        int s_idx = (p >= 0.99f) ? (bank->n_stages - 1) : (int)(p * (float)bank->n_stages);
        if (s_idx < 0) s_idx = 0;
        if (s_idx >= bank->n_stages) s_idx = bank->n_stages - 1;
        boot_copy_str(out_stage, stage_cap, bank->stages[s_idx]);
    }

    if (out_splash && splash_cap > 0) {
        const char *sp = boot_resolve_special_trigger(bank, lt, seed, anim_t, NULL);
        if (!sp && bank->n_splashes > 0) {
            uint32_t step = (anim_t >= 0.0f) ? (uint32_t)(anim_t / 5.5f) : 0U;
            uint32_t idx = (seed + step * 7U) % (uint32_t)bank->n_splashes;
            sp = bank->splashes[idx];
        }
        boot_copy_str(out_splash, splash_cap, sp ? sp : "16 pads, zero sleep!");
    }

    if (out_tip && tip_cap > 0 && bank->n_tips > 0) {
        uint32_t step = (anim_t >= 0.0f) ? (uint32_t)(anim_t / 8.0f) : 0U;
        uint32_t h = seed * 2654435761U + step * 1597334677U;
        uint32_t t_idx = h % (uint32_t)bank->n_tips;
        boot_copy_str(out_tip, tip_cap, bank->tips[t_idx]);
    }
}


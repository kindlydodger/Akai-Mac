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

// mpc_boot_splash.c — LD_PRELOAD DRM overlay boot animator (plane[38]) for /usr/bin/MPC.
// Picks up seamlessly from early /dev/fb0 mpc_boot_anim, animates proportional to real
// boot duration & plugin/ALSA milestones, cross-dissolves into MPC UI, and zeroes overhead.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <drm/drm.h>
#include <drm/drm_fourcc.h>
#include <drm/drm_mode.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#include "mpc_boot_splash_shared.h"

typedef struct _drmModeAtomicReq *drmModeAtomicReqPtr;

typedef drmModeAtomicReqPtr (*fn_drmModeAtomicAlloc)(void);
typedef void (*fn_drmModeAtomicFree)(drmModeAtomicReqPtr req);
typedef int (*fn_drmModeAtomicAddProperty)(drmModeAtomicReqPtr req, uint32_t object_id,
                                           uint32_t property_id, uint64_t value);
typedef int (*fn_drmModeAtomicCommit)(int fd, drmModeAtomicReqPtr req, uint32_t flags, void *user_data);
typedef int (*fn_drmModeSetCrtc)(int fd, uint32_t crtcId, uint32_t bufferId, uint32_t x, uint32_t y,
                                 uint32_t *connectors, int count, void *mode);

#ifndef DRM_MODE_OBJECT_PLANE
#define DRM_MODE_OBJECT_PLANE 0xeeeeeeee
#endif
#ifndef DRM_CLIENT_CAP_UNIVERSAL_PLANES
#define DRM_CLIENT_CAP_UNIVERSAL_PLANES 2
#endif
#ifndef DRM_MODE_PROP_IMMUTABLE
#define DRM_MODE_PROP_IMMUTABLE (1 << 2)
#endif
#ifndef DRM_MODE_ATOMIC_NONBLOCK
#define DRM_MODE_ATOMIC_NONBLOCK 0x0200
#endif

typedef struct {
    uint32_t fb_id;
    uint32_t handle;
    uint32_t pitch;
    uint64_t size;
    uint32_t *map;
} SplashDumbBo;

typedef struct {
    uint32_t fb_id;
    uint32_t crtc_id;
    uint32_t src_x, src_y, src_w, src_h;
    uint32_t crtc_x, crtc_y, crtc_w, crtc_h;
    uint32_t zpos;
    int zpos_writable;
    uint64_t type_val;
    int64_t zpos_val;
} PlanePropIds;

static pthread_once_t g_syms_once = PTHREAD_ONCE_INIT;
static pthread_mutex_t g_drm_lock = PTHREAD_MUTEX_INITIALIZER;

static fn_drmModeAtomicAlloc real_atomic_alloc = NULL;
static fn_drmModeAtomicFree real_atomic_free = NULL;
static fn_drmModeAtomicAddProperty real_atomic_add_prop = NULL;
static fn_drmModeAtomicCommit real_atomic_commit = NULL;
static fn_drmModeSetCrtc real_set_crtc = NULL;

static volatile int g_splash_done = 0;
static volatile int g_splash_inited = 0;
static volatile int g_thread_started = 0;

static int g_drm_fd = -1;
static uint32_t g_crtc_id = 0;
static uint32_t g_primary_plane_id = 0;
static uint32_t g_overlay_plane_id = 0;
static PlanePropIds g_ov_props;
static SplashDumbBo g_bos[2];
static volatile int g_front_bo = 0;
static uint32_t g_canvas[BOOT_SCR_W * BOOT_SCR_H];

static void splash_log(const char *fmt, ...) {
    FILE *f = fopen("/tmp/mpc_boot_splash.log", "a");
    if (!f) return;
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

static void resolve_drm_syms(void) {
    real_atomic_commit = (fn_drmModeAtomicCommit)dlsym(RTLD_NEXT, "drmModeAtomicCommit");
    real_atomic_add_prop = (fn_drmModeAtomicAddProperty)dlsym(RTLD_NEXT, "drmModeAtomicAddProperty");
    real_set_crtc = (fn_drmModeSetCrtc)dlsym(RTLD_NEXT, "drmModeSetCrtc");

    void *libdrm = dlopen("libdrm.so.2", RTLD_LAZY | RTLD_NOLOAD);
    if (!libdrm) libdrm = dlopen("libdrm.so.2", RTLD_LAZY | RTLD_GLOBAL);
    if (libdrm) {
        real_atomic_alloc = (fn_drmModeAtomicAlloc)dlsym(libdrm, "drmModeAtomicAlloc");
        real_atomic_free = (fn_drmModeAtomicFree)dlsym(libdrm, "drmModeAtomicFree");
        if (!real_atomic_commit) real_atomic_commit = (fn_drmModeAtomicCommit)dlsym(libdrm, "drmModeAtomicCommit");
        if (!real_atomic_add_prop) real_atomic_add_prop = (fn_drmModeAtomicAddProperty)dlsym(libdrm, "drmModeAtomicAddProperty");
    } else {
        real_atomic_alloc = (fn_drmModeAtomicAlloc)dlsym(RTLD_DEFAULT, "drmModeAtomicAlloc");
        real_atomic_free = (fn_drmModeAtomicFree)dlsym(RTLD_DEFAULT, "drmModeAtomicFree");
    }
}

static int drm_ioctl_retry(int fd, unsigned long req, void *arg) {
    int ret;
    do {
        ret = ioctl(fd, req, arg);
    } while (ret == -1 && (errno == EINTR || errno == EAGAIN));
    return ret;
}

static int query_plane_props(int fd, uint32_t plane_id, PlanePropIds *out) {
    memset(out, 0, sizeof(*out));
    out->type_val = 999;
    out->zpos_val = -1;

    uint32_t ids[48];
    uint64_t vals[48];
    struct drm_mode_obj_get_properties gp;
    memset(&gp, 0, sizeof(gp));
    gp.props_ptr = (uint64_t)(uintptr_t)ids;
    gp.prop_values_ptr = (uint64_t)(uintptr_t)vals;
    gp.count_props = 48;
    gp.obj_id = plane_id;
    gp.obj_type = DRM_MODE_OBJECT_PLANE;

    if (drm_ioctl_retry(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &gp) != 0) return -1;
    uint32_t n = gp.count_props < 48 ? gp.count_props : 48;

    for (uint32_t i = 0; i < n; i++) {
        struct drm_mode_get_property pr;
        memset(&pr, 0, sizeof(pr));
        pr.prop_id = ids[i];
        if (drm_ioctl_retry(fd, DRM_IOCTL_MODE_GETPROPERTY, &pr) != 0) continue;

        if (strcmp(pr.name, "type") == 0) {
            out->type_val = vals[i];
        } else if (strcmp(pr.name, "zpos") == 0) {
            out->zpos_val = (int64_t)vals[i];
            out->zpos = ids[i];
            out->zpos_writable = ((pr.flags & DRM_MODE_PROP_IMMUTABLE) == 0);
        } else if (strcmp(pr.name, "normalized-zpos") == 0 && out->zpos_val < 0) {
            out->zpos_val = (int64_t)vals[i];
        } else if (strcmp(pr.name, "FB_ID") == 0) {
            out->fb_id = ids[i];
        } else if (strcmp(pr.name, "CRTC_ID") == 0) {
            out->crtc_id = ids[i];
        } else if (strcmp(pr.name, "SRC_X") == 0) {
            out->src_x = ids[i];
        } else if (strcmp(pr.name, "SRC_Y") == 0) {
            out->src_y = ids[i];
        } else if (strcmp(pr.name, "SRC_W") == 0) {
            out->src_w = ids[i];
        } else if (strcmp(pr.name, "SRC_H") == 0) {
            out->src_h = ids[i];
        } else if (strcmp(pr.name, "CRTC_X") == 0) {
            out->crtc_x = ids[i];
        } else if (strcmp(pr.name, "CRTC_Y") == 0) {
            out->crtc_y = ids[i];
        } else if (strcmp(pr.name, "CRTC_W") == 0) {
            out->crtc_w = ids[i];
        } else if (strcmp(pr.name, "CRTC_H") == 0) {
            out->crtc_h = ids[i];
        }
    }
    return 0;
}

static int find_crtc_and_planes(int fd) {
    struct drm_set_client_cap cap = {DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1};
    drm_ioctl_retry(fd, DRM_IOCTL_SET_CLIENT_CAP, &cap);

    uint32_t crtc_ids[16];
    memset(crtc_ids, 0, sizeof(crtc_ids));
    struct drm_mode_card_res res;
    memset(&res, 0, sizeof(res));
    res.crtc_id_ptr = (uint64_t)(uintptr_t)crtc_ids;
    res.count_crtcs = 16;
    if (drm_ioctl_retry(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) != 0) return -1;

    int target_crtc_idx = -1;
    uint32_t n_crtcs = res.count_crtcs < 16 ? res.count_crtcs : 16;
    for (uint32_t i = 0; i < n_crtcs; i++) {
        struct drm_mode_crtc c;
        memset(&c, 0, sizeof(c));
        c.crtc_id = crtc_ids[i];
        if (drm_ioctl_retry(fd, DRM_IOCTL_MODE_GETCRTC, &c) == 0 && c.mode_valid) {
            if (g_crtc_id == 0 || g_crtc_id == c.crtc_id) {
                g_crtc_id = c.crtc_id;
                target_crtc_idx = (int)i;
                break;
            }
        }
    }
    if (g_crtc_id == 0 && n_crtcs > 0) {
        g_crtc_id = crtc_ids[0];
        target_crtc_idx = 0;
    }
    if (g_crtc_id == 0 || target_crtc_idx < 0) return -1;

    uint32_t plane_ids[32];
    struct drm_mode_get_plane_res pr;
    memset(&pr, 0, sizeof(pr));
    pr.plane_id_ptr = (uint64_t)(uintptr_t)plane_ids;
    pr.count_planes = 32;
    if (drm_ioctl_retry(fd, DRM_IOCTL_MODE_GETPLANERESOURCES, &pr) != 0) return -1;
    uint32_t n_planes = pr.count_planes < 32 ? pr.count_planes : 32;

    int64_t best_z = -999;
    uint32_t best_ov_id = 0;
    PlanePropIds best_props;
    memset(&best_props, 0, sizeof(best_props));

    for (uint32_t i = 0; i < n_planes; i++) {
        struct drm_mode_get_plane gp;
        memset(&gp, 0, sizeof(gp));
        gp.plane_id = plane_ids[i];
        if (drm_ioctl_retry(fd, DRM_IOCTL_MODE_GETPLANE, &gp) != 0) continue;
        if (!(gp.possible_crtcs & (1U << target_crtc_idx))) continue;

        PlanePropIds props;
        if (query_plane_props(fd, plane_ids[i], &props) != 0) continue;
        splash_log("cand plane=%u possible=0x%x type=%llu zpos=%lld wr=%d",
                   plane_ids[i], gp.possible_crtcs,
                   (unsigned long long)props.type_val,
                   (long long)props.zpos_val, props.zpos_writable);

        if (props.type_val == 1) { // DRM_PLANE_TYPE_PRIMARY
            if (gp.crtc_id == g_crtc_id || g_primary_plane_id == 0) {
                g_primary_plane_id = plane_ids[i];
            }
        } else if (props.type_val == 0) { // DRM_PLANE_TYPE_OVERLAY
            // Rockchip VOP: prefer plane[38] (normalized-zpos=1 above primary) or strictly highest zpos overlay
            int better = 0;
            if (props.fb_id && props.crtc_id) {
                if (best_ov_id == 0) better = 1;
                else if (plane_ids[i] == 38 && best_z <= 1) better = 1;
                else if (best_ov_id != 38 && props.zpos_val > best_z) better = 1;
            }
            if (better) {
                best_z = props.zpos_val;
                best_ov_id = plane_ids[i];
                best_props = props;
            }
        }
    }

    if (best_ov_id == 0) return -1;
    g_overlay_plane_id = best_ov_id;
    g_ov_props = best_props;
    return 0;
}

static int create_dumb_bo(int fd, SplashDumbBo *bo) {
    memset(bo, 0, sizeof(*bo));
    struct drm_mode_create_dumb creq;
    memset(&creq, 0, sizeof(creq));
    creq.width = MPC_PHYS_W;
    creq.height = MPC_PHYS_H;
    creq.bpp = 32;
    if (drm_ioctl_retry(fd, DRM_IOCTL_MODE_CREATE_DUMB, &creq) != 0) return -1;

    struct drm_mode_fb_cmd2 fbcmd;
    memset(&fbcmd, 0, sizeof(fbcmd));
    fbcmd.width = MPC_PHYS_W;
    fbcmd.height = MPC_PHYS_H;
    fbcmd.pixel_format = DRM_FORMAT_ARGB8888;
    fbcmd.handles[0] = creq.handle;
    fbcmd.pitches[0] = creq.pitch;
    if (drm_ioctl_retry(fd, DRM_IOCTL_MODE_ADDFB2, &fbcmd) != 0) {
        fbcmd.pixel_format = DRM_FORMAT_XRGB8888;
        if (drm_ioctl_retry(fd, DRM_IOCTL_MODE_ADDFB2, &fbcmd) != 0) {
            struct drm_mode_destroy_dumb dreq = {creq.handle};
            drm_ioctl_retry(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &dreq);
            return -1;
        }
    }

    struct drm_mode_map_dumb mreq;
    memset(&mreq, 0, sizeof(mreq));
    mreq.handle = creq.handle;
    if (drm_ioctl_retry(fd, DRM_IOCTL_MODE_MAP_DUMB, &mreq) != 0) {
        drm_ioctl_retry(fd, DRM_IOCTL_MODE_RMFB, &fbcmd.fb_id);
        struct drm_mode_destroy_dumb dreq = {creq.handle};
        drm_ioctl_retry(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &dreq);
        return -1;
    }

    void *ptr = mmap(NULL, creq.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, mreq.offset);
    if (ptr == MAP_FAILED) {
        drm_ioctl_retry(fd, DRM_IOCTL_MODE_RMFB, &fbcmd.fb_id);
        struct drm_mode_destroy_dumb dreq = {creq.handle};
        drm_ioctl_retry(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &dreq);
        return -1;
    }

    bo->handle = creq.handle;
    bo->fb_id = fbcmd.fb_id;
    bo->pitch = creq.pitch;
    bo->size = creq.size;
    bo->map = (uint32_t *)ptr;
    return 0;
}

static void destroy_dumb_bo(int fd, SplashDumbBo *bo) {
    if (bo->map && bo->map != MAP_FAILED) munmap(bo->map, bo->size);
    if (bo->fb_id) drm_ioctl_retry(fd, DRM_IOCTL_MODE_RMFB, &bo->fb_id);
    if (bo->handle) {
        struct drm_mode_destroy_dumb dreq = {bo->handle};
        drm_ioctl_retry(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &dreq);
    }
    memset(bo, 0, sizeof(*bo));
}

static void inject_overlay_props(drmModeAtomicReqPtr req, uint32_t fb_id) {
    if (!real_atomic_add_prop || g_overlay_plane_id == 0) return;
    if (fb_id == 0) {
        real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.fb_id, 0);
        real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.crtc_id, 0);
        real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.src_w, 0);
        real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.src_h, 0);
        real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.crtc_w, 0);
        real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.crtc_h, 0);
        return;
    }
    real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.fb_id, fb_id);
    real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.crtc_id, g_crtc_id);
    real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.src_x, 0);
    real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.src_y, 0);
    real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.src_w, ((uint64_t)MPC_PHYS_W) << 16);
    real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.src_h, ((uint64_t)MPC_PHYS_H) << 16);
    real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.crtc_x, 0);
    real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.crtc_y, 0);
    real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.crtc_w, MPC_PHYS_W);
    real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.crtc_h, MPC_PHYS_H);
    if (g_ov_props.zpos_writable && g_ov_props.zpos) {
        real_atomic_add_prop(req, g_overlay_plane_id, g_ov_props.zpos, 2);
    }
}

static void render_to_bo(SplashDumbBo *bo, float progress, float anim_t, float elapsed_s,
                         const char *stage, float fade_alpha, uint32_t splash_seed) {
    boot_render_frame_seeded(g_canvas, progress, anim_t, elapsed_s, stage, fade_alpha, splash_seed);
    int stride_u32 = (int)(bo->pitch / 4U);
    if (stride_u32 < MPC_PHYS_W) stride_u32 = MPC_PHYS_W;
    boot_rotate_canvas_to_phys(g_canvas, bo->map, stride_u32);
}

// Inspect plane[33] primary FB pixels directly to verify JUCE workspace UI is actually rendered
static int check_primary_plane_ui_ready(int fd) {
    if (g_primary_plane_id == 0) return 0;
    struct drm_mode_get_plane gp;
    memset(&gp, 0, sizeof(gp));
    gp.plane_id = g_primary_plane_id;
    if (drm_ioctl_retry(fd, DRM_IOCTL_MODE_GETPLANE, &gp) != 0 || gp.fb_id == 0) return 0;

    struct drm_mode_fb_cmd fb;
    memset(&fb, 0, sizeof(fb));
    fb.fb_id = gp.fb_id;
    if (drm_ioctl_retry(fd, DRM_IOCTL_MODE_GETFB, &fb) != 0 || fb.handle == 0) return 0;

    int ui_ready = 0;
    if (fb.width == MPC_PHYS_W && fb.height == MPC_PHYS_H && fb.bpp == 32 && fb.pitch >= MPC_PHYS_W * 4) {
        struct drm_mode_map_dumb mreq;
        memset(&mreq, 0, sizeof(mreq));
        mreq.handle = fb.handle;
        if (drm_ioctl_retry(fd, DRM_IOCTL_MODE_MAP_DUMB, &mreq) == 0) {
            size_t map_sz = (size_t)fb.pitch * fb.height;
            uint8_t *p = (uint8_t *)mmap(NULL, map_sz, PROT_READ, MAP_SHARED, fd, mreq.offset);
            if (p != MAP_FAILED) {
                static const int sample_x[5] = {24, 320, 640, 960, 1256};
                static const int sample_y[4] = {24, 200, 460, 760};
                int nonblack = 0;
                for (int yi = 0; yi < 4; yi++) {
                    for (int xi = 0; xi < 5; xi++) {
                        int lx = sample_x[xi];
                        int ly = sample_y[yi];
                        int px = ly;
                        int py = (BOOT_SCR_W - 1) - lx;
                        uint32_t px32 = *(const uint32_t *)(p + (size_t)py * fb.pitch + (size_t)px * 4U);
                        uint32_t r = (px32 >> 16) & 0xFFU;
                        uint32_t g = (px32 >> 8) & 0xFFU;
                        uint32_t b = px32 & 0xFFU;
                        if (r + g + b >= 24U) nonblack++;
                    }
                }
                ui_ready = (nonblack >= 14) ? 1 : 0;
                munmap(p, map_sz);
            }
        }
    }

    struct drm_gem_close creq = {fb.handle, 0};
    drm_ioctl_retry(fd, DRM_IOCTL_GEM_CLOSE, &creq);
    return ui_ready;
}

static void *splash_anim_thread(void *arg) {
    (void)arg;
    BootTimingProfile prof;
    boot_load_timing_profile(&prof, MPC_BOOT_TIMING_FILE);

    BootShmState st;
    boot_init_or_resume_state(&st, MPC_BOOT_SHM_STATE, 1);

    // Notify /dev/fb0 mpc_boot_anim daemon that DRM plane[38] has seamlessly taken over!
    int flag_fd = open(MPC_BOOT_SHM_DRM_ACTIVE, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (flag_fd >= 0) close(flag_fd);

    double fb_ready_since = 0.0;
    double finale_start = 0.0;
    float prog_at_finale = 0.0f;
    float recorded_elapsed = 0.0f;
    int saved_timing = 0;
    int tick = 0;
    double next_deadline = 0.0;

    while (!g_splash_done) {
        double now_m = boot_now_mono_s();
        float elapsed_s = boot_compute_elapsed_s(&st);
        // Unified timeline: matches mpc_boot_anim identically so eyes/teeth/quote have zero phase jump!
        float anim_t = elapsed_s;

        char stage[96];
        float dummy_el = 0.0f;
        float raw_prog = boot_eval_progress_and_stage(&st, &prof, 1, 0, &dummy_el, stage, sizeof(stage));

        int ui_done = (finale_start > 0.0);
        if (!ui_done && elapsed_s >= 42.0f) {
            ui_done = 1;
            finale_start = now_m;
            prog_at_finale = raw_prog;
            recorded_elapsed = elapsed_s;
            splash_log("ui_done via timeout elapsed=%.2f", elapsed_s);
        } else if (!ui_done && elapsed_s >= 3.2f && (tick % 4) == 0) {
            if (fb_ready_since == 0.0 && check_primary_plane_ui_ready(g_drm_fd)) {
                fb_ready_since = now_m;
                splash_log("primary fb UI pixels ready elapsed=%.2f bridge=%d", elapsed_s, boot_check_bridge_ready());
            }
            if (fb_ready_since > 0.0) {
                if (boot_check_bridge_ready() || (now_m - fb_ready_since) >= 2.2) {
                    ui_done = 1;
                    finale_start = now_m;
                    prog_at_finale = raw_prog;
                    recorded_elapsed = elapsed_s;
                    splash_log("ui_done via primary+bridge elapsed=%.2f", elapsed_s);
                }
            }
        }

        if (ui_done && !saved_timing && recorded_elapsed > 0.0f && recorded_elapsed < 41.5f) {
            saved_timing = 1;
            boot_save_timing_sample(&prof, MPC_BOOT_TIMING_FILE, (int)st.is_warm, recorded_elapsed);
        }

        float prog = raw_prog;
        float fade_alpha = 1.0f;

        if (finale_start > 0.0) {
            double f_dt = now_m - finale_start;
            if (f_dt < 0.30) {
                // Smoothly sweep progress bar from current % -> 100% over 0.30s instead of snapping!
                float r = boot_smoothstep(0.0f, 0.30f, (float)f_dt);
                prog = prog_at_finale + (1.0f - prog_at_finale) * r;
            } else {
                prog = 1.0f;
            }
            if (prog >= 0.99f) {
                snprintf(stage, sizeof(stage), "MPC LIVE READY");
            }
            if (f_dt >= 1.15) {
                break;
            } else if (f_dt > 0.75) {
                fade_alpha = 1.0f - (float)((f_dt - 0.75) / 0.40);
            }
        }

        int back = g_front_bo ^ 1;
        render_to_bo(&g_bos[back], prog, anim_t, elapsed_s, stage, fade_alpha, st.splash_seed);

        pthread_mutex_lock(&g_drm_lock);
        if (!g_splash_done && real_atomic_alloc && real_atomic_commit) {
            drmModeAtomicReqPtr r = real_atomic_alloc();
            if (r) {
                inject_overlay_props(r, g_bos[back].fb_id);
                int rc = real_atomic_commit(g_drm_fd, r, DRM_MODE_ATOMIC_NONBLOCK, NULL);
                if (rc != 0 && (errno == EBUSY || rc == -EBUSY || errno == EAGAIN)) {
                    rc = real_atomic_commit(g_drm_fd, r, 0, NULL);
                }
                if (rc == 0) {
                    g_front_bo = back;
                } else if (tick < 5) {
                    splash_log("thread atomic_commit failed tick=%d rc=%d errno=%d", tick, rc, errno);
                }
                real_atomic_free(r);
            }
        }
        pthread_mutex_unlock(&g_drm_lock);

        tick++;
        boot_pace_frame(&next_deadline, 1.0 / 30.0); // Locked smooth 30 FPS
    }

    splash_log("finale finished elapsed=%.2f, tearing down plane[38]", boot_compute_elapsed_s(&st));

    pthread_mutex_lock(&g_drm_lock);
    g_splash_done = 1;
    if (real_atomic_alloc && real_atomic_commit && g_drm_fd >= 0) {
        drmModeAtomicReqPtr r = real_atomic_alloc();
        if (r) {
            inject_overlay_props(r, 0);
            real_atomic_commit(g_drm_fd, r, 0, NULL);
            real_atomic_free(r);
        }
    }
    destroy_dumb_bo(g_drm_fd, &g_bos[0]);
    destroy_dumb_bo(g_drm_fd, &g_bos[1]);
    pthread_mutex_unlock(&g_drm_lock);
    return NULL;
}

int drmModeAtomicAddProperty(drmModeAtomicReqPtr req, uint32_t object_id,
                             uint32_t property_id, uint64_t value) {
    pthread_once(&g_syms_once, resolve_drm_syms);
    if (__builtin_expect(g_splash_done, 1)) {
        return real_atomic_add_prop ? real_atomic_add_prop(req, object_id, property_id, value) : -ENOSYS;
    }
    if (g_crtc_id == 0 && property_id != 0 && value > 0 && value < 256 && object_id > 0) {
        // Opportunistically observe CRTC_ID early
        (void)object_id;
    }
    return real_atomic_add_prop ? real_atomic_add_prop(req, object_id, property_id, value) : -ENOSYS;
}

int drmModeAtomicCommit(int fd, drmModeAtomicReqPtr req, uint32_t flags, void *user_data) {
    pthread_once(&g_syms_once, resolve_drm_syms);
    if (__builtin_expect(g_splash_done, 1)) {
        return real_atomic_commit ? real_atomic_commit(fd, req, flags, user_data) : -ENOSYS;
    }

    pthread_mutex_lock(&g_drm_lock);
    int first_commit = 0;
    if (!g_splash_done && !g_splash_inited) {
        g_splash_inited = 1;
        first_commit = 1;
        g_drm_fd = fd;
        unlink("/tmp/mpc_boot_splash.log");
        int fc = find_crtc_and_planes(fd);
        int b0 = (fc == 0) ? create_dumb_bo(fd, &g_bos[0]) : -1;
        int b1 = (b0 == 0) ? create_dumb_bo(fd, &g_bos[1]) : -1;
        splash_log("init fd=%d fc=%d crtc=%u pri=%u ov=%u z_wr=%d b0=%d b1=%d",
                   fd, fc, g_crtc_id, g_primary_plane_id, g_overlay_plane_id,
                   g_ov_props.zpos_writable, b0, b1);
        if (fc == 0 && b0 == 0 && b1 == 0) {
            BootTimingProfile prof;
            BootShmState st;
            boot_load_timing_profile(&prof, MPC_BOOT_TIMING_FILE);
            boot_init_or_resume_state(&st, MPC_BOOT_SHM_STATE, 1);
            float el = 0.0f;
            char stage[96];
            float p = boot_eval_progress_and_stage(&st, &prof, 1, 0, &el, stage, sizeof(stage));
            render_to_bo(&g_bos[0], p, el, el, stage, 1.0f, st.splash_seed);
            g_front_bo = 0;

            if (!g_thread_started) {
                g_thread_started = 1;
                pthread_t tid;
                pthread_attr_t attr;
                pthread_attr_init(&attr);
                pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
                pthread_create(&tid, &attr, splash_anim_thread, NULL);
                pthread_attr_destroy(&attr);
            }
        } else {
            g_splash_done = 1;
        }
    }

    if (!g_splash_done && g_overlay_plane_id != 0 && g_bos[g_front_bo].fb_id != 0) {
        inject_overlay_props(req, g_bos[g_front_bo].fb_id);
    }

    int ret = real_atomic_commit ? real_atomic_commit(fd, req, flags, user_data) : -ENOSYS;
    if (first_commit) {
        splash_log("first MPC atomic_commit flags=0x%x -> ret=%d errno=%d", flags, ret, errno);
    }
    pthread_mutex_unlock(&g_drm_lock);
    return ret;
}

int drmModeSetCrtc(int fd, uint32_t crtcId, uint32_t bufferId, uint32_t x, uint32_t y,
                   uint32_t *connectors, int count, void *mode) {
    pthread_once(&g_syms_once, resolve_drm_syms);
    if (!g_splash_done && crtcId != 0 && g_crtc_id == 0) {
        g_crtc_id = crtcId;
    }
    return real_set_crtc ? real_set_crtc(fd, crtcId, bufferId, x, y, connectors, count, mode) : -ENOSYS;
}

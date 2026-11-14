#pragma once
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#define FLOW_PHYS_W 800
#define FLOW_PHYS_H 1280
#define FLOW_LOG_W  1280
#define FLOW_LOG_H  800

#define FLOW_DRM_FORMAT_AR24 0x34325241U
#define FLOW_DRM_MODE_OBJECT_PLANE 0xeeeeeeeeU
#define FLOW_DRM_MODE_ATOMIC_NONBLOCK 0x0200U
#define FLOW_DRM_MODE_ATOMIC_TEST_ONLY 0x0100U

#define FLOW_IOCTL_GETPLANERESOURCES 0xc01064b5U
#define FLOW_IOCTL_GETPLANE          0xc02064b6U
#define FLOW_IOCTL_OBJ_GETPROPERTIES 0xc02064b9U
#define FLOW_IOCTL_GETPROPERTY       0xc04064aaU
#define FLOW_IOCTL_GETFB             0xc01c64adU
#define FLOW_IOCTL_CREATE_DUMB       0xc02064b2U
#define FLOW_IOCTL_MAP_DUMB          0xc01064b3U
#define FLOW_IOCTL_RMFB              0xc00464afU
#define FLOW_IOCTL_DESTROY_DUMB      0xc00464b4U
#define FLOW_IOCTL_ADDFB2            0xc06864b8U
#define FLOW_IOCTL_ATOMIC            0xc03864bcU

typedef struct {
    uint64_t plane_id_ptr;
    uint32_t count_planes;
} flow_drm_get_plane_res;

typedef struct {
    uint32_t plane_id, crtc_id, fb_id, possible_crtcs, gamma_size, count_format_types;
    uint64_t format_type_ptr;
} flow_drm_get_plane;

typedef struct {
    uint64_t props_ptr, prop_values_ptr;
    uint32_t count_props, obj_id, obj_type;
} flow_drm_obj_get_props;

typedef struct {
    uint64_t values_ptr, enum_blob_ptr;
    uint32_t prop_id, flags;
    char name[32];
    uint32_t count_values, count_enum_blobs;
} flow_drm_get_prop;

typedef struct {
    uint32_t fb_id, width, height, pitch, bpp, depth, handle;
} flow_drm_fb_cmd;

typedef struct {
    uint32_t height, width, bpp, flags, handle, pitch;
    uint64_t size;
} flow_drm_create_dumb;

typedef struct {
    uint32_t handle, pad;
    uint64_t offset;
} flow_drm_map_dumb;

typedef struct {
    uint32_t fb_id, width, height, pixel_format, flags;
    uint32_t handles[4], pitches[4], offsets[4];
    uint64_t modifier[4];
} flow_drm_fb_cmd2;

typedef struct {
    uint32_t flags, count_objs;
    uint64_t objs_ptr, count_props_ptr, props_ptr, prop_values_ptr, reserved, user_data;
} flow_drm_atomic;

typedef struct {
    uint32_t fb_id;
    uint32_t handle;
    uint32_t width;
    uint32_t height;
    uint32_t pitch;
    uint64_t size;
    uint32_t *map;
} flow_dumb_fb;

typedef struct {
    int inited;
    int available;
    int drm_fd;
    uint32_t overlay_plane_id;
    uint32_t primary_plane_id;
    uint32_t crtc_id;
    uint32_t prop_crtc_id;
    uint32_t prop_fb_id;
    uint32_t prop_crtc_x;
    uint32_t prop_crtc_y;
    uint32_t prop_crtc_w;
    uint32_t prop_crtc_h;
    uint32_t prop_src_x;
    uint32_t prop_src_y;
    uint32_t prop_src_w;
    uint32_t prop_src_h;
    uint32_t prop_zpos;
    uint64_t zpos_val;
    flow_dumb_fb ov[2];
    int front_idx;
    int ov_dirty;
    int last_committed_vis;
    uint32_t ov_px, ov_py, ov_pw, ov_ph;
    uint32_t primary_fb_id;
    int last_detect_err;
    uint32_t last_detect_fb;
    uint32_t last_detect_px[4];
    flow_dumb_fb prim_cache[8];
    int prim_cache_cnt;
} FlowDrmState;

static inline int flow_drm_create_ar24_fb(int fd, uint32_t w, uint32_t h, flow_dumb_fb *out) {
    memset(out, 0, sizeof(*out));
    flow_drm_create_dumb cd;
    memset(&cd, 0, sizeof(cd));
    cd.width = w;
    cd.height = h;
    cd.bpp = 32;
    if (ioctl(fd, FLOW_IOCTL_CREATE_DUMB, &cd) < 0) return -1;

    flow_drm_map_dumb md;
    memset(&md, 0, sizeof(md));
    md.handle = cd.handle;
    if (ioctl(fd, FLOW_IOCTL_MAP_DUMB, &md) < 0) return -1;

    void *ptr = mmap(NULL, (size_t)cd.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, (off_t)md.offset);
    if (ptr == MAP_FAILED) return -1;
    memset(ptr, 0, (size_t)cd.size);

    flow_drm_fb_cmd2 f2;
    memset(&f2, 0, sizeof(f2));
    f2.width = w;
    f2.height = h;
    f2.pixel_format = FLOW_DRM_FORMAT_AR24;
    f2.handles[0] = cd.handle;
    f2.pitches[0] = cd.pitch;
    if (ioctl(fd, FLOW_IOCTL_ADDFB2, &f2) < 0) {
        munmap(ptr, (size_t)cd.size);
        return -1;
    }

    out->fb_id = f2.fb_id;
    out->handle = cd.handle;
    out->width = w;
    out->height = h;
    out->pitch = cd.pitch;
    out->size = cd.size;
    out->map = (uint32_t *)ptr;
    return 0;
}

static inline int flow_drm_init_planes(FlowDrmState *st, int fd) {
    if (st->inited) return st->available ? 0 : -1;
    st->inited = 1;
    st->drm_fd = fd;
    st->crtc_id = 37;
    st->primary_plane_id = 33;

    flow_drm_get_plane_res pres;
    memset(&pres, 0, sizeof(pres));
    uint32_t pids[16] = {0};
    if (ioctl(fd, FLOW_IOCTL_GETPLANERESOURCES, &pres) < 0 || pres.count_planes == 0) return -1;
    if (pres.count_planes > 16) pres.count_planes = 16;
    pres.plane_id_ptr = (uint64_t)(uintptr_t)pids;
    if (ioctl(fd, FLOW_IOCTL_GETPLANERESOURCES, &pres) < 0) return -1;

    uint32_t chosen = 0;
    for (uint32_t i = 0; i < pres.count_planes; i++) {
        flow_drm_get_plane gp;
        memset(&gp, 0, sizeof(gp));
        gp.plane_id = pids[i];
        if (ioctl(fd, FLOW_IOCTL_GETPLANE, &gp) < 0) continue;
        if (gp.crtc_id != 0 && gp.fb_id != 0) {
            st->primary_plane_id = gp.plane_id;
            st->primary_fb_id = gp.fb_id;
            st->crtc_id = gp.crtc_id;
            continue;
        }
        // Prefer plane 38 (plane-2) leaving plane 35 free for mpc_cursor.so
        if (gp.plane_id == 35) continue;
        uint32_t fmts[32] = {0};
        if (gp.count_format_types > 32) gp.count_format_types = 32;
        gp.format_type_ptr = (uint64_t)(uintptr_t)fmts;
        if (ioctl(fd, FLOW_IOCTL_GETPLANE, &gp) < 0) continue;
        int has_ar24 = 0;
        for (uint32_t f = 0; f < gp.count_format_types; f++) {
            if (fmts[f] == FLOW_DRM_FORMAT_AR24) { has_ar24 = 1; break; }
        }
        if (has_ar24 && (chosen == 0 || gp.plane_id == 38)) {
            chosen = gp.plane_id;
        }
    }
    if (chosen == 0) return -1;
    st->overlay_plane_id = chosen;

    uint32_t prop_ids[32] = {0};
    uint64_t prop_vals[32] = {0};
    flow_drm_obj_get_props gp;
    memset(&gp, 0, sizeof(gp));
    gp.obj_id = chosen;
    gp.obj_type = FLOW_DRM_MODE_OBJECT_PLANE;
    gp.count_props = 32;
    gp.props_ptr = (uint64_t)(uintptr_t)prop_ids;
    gp.prop_values_ptr = (uint64_t)(uintptr_t)prop_vals;
    if (ioctl(fd, FLOW_IOCTL_OBJ_GETPROPERTIES, &gp) < 0) return -1;

    for (uint32_t i = 0; i < gp.count_props && i < 32; i++) {
        flow_drm_get_prop mp;
        memset(&mp, 0, sizeof(mp));
        mp.prop_id = prop_ids[i];
        if (ioctl(fd, FLOW_IOCTL_GETPROPERTY, &mp) < 0) continue;
        if (strcmp(mp.name, "CRTC_ID") == 0) st->prop_crtc_id = mp.prop_id;
        else if (strcmp(mp.name, "FB_ID") == 0) st->prop_fb_id = mp.prop_id;
        else if (strcmp(mp.name, "CRTC_X") == 0) st->prop_crtc_x = mp.prop_id;
        else if (strcmp(mp.name, "CRTC_Y") == 0) st->prop_crtc_y = mp.prop_id;
        else if (strcmp(mp.name, "CRTC_W") == 0) st->prop_crtc_w = mp.prop_id;
        else if (strcmp(mp.name, "CRTC_H") == 0) st->prop_crtc_h = mp.prop_id;
        else if (strcmp(mp.name, "SRC_X") == 0) st->prop_src_x = mp.prop_id;
        else if (strcmp(mp.name, "SRC_Y") == 0) st->prop_src_y = mp.prop_id;
        else if (strcmp(mp.name, "SRC_W") == 0) st->prop_src_w = mp.prop_id;
        else if (strcmp(mp.name, "SRC_H") == 0) st->prop_src_h = mp.prop_id;
        else if (strcmp(mp.name, "zpos") == 0) {
            st->prop_zpos = mp.prop_id;
            st->zpos_val = 2;
        }
    }
    if (!st->prop_crtc_id || !st->prop_fb_id) return -1;

    if (flow_drm_create_ar24_fb(fd, FLOW_PHYS_W, FLOW_PHYS_H, &st->ov[0]) < 0) return -1;
    if (flow_drm_create_ar24_fb(fd, FLOW_PHYS_W, FLOW_PHYS_H, &st->ov[1]) < 0) return -1;

    st->available = 1;
    return 0;
}

static inline const flow_dumb_fb *flow_drm_map_primary_fb(FlowDrmState *st, int fd, uint32_t fb_id) {
    if (fb_id == 0) return NULL;
    for (int i = 0; i < st->prim_cache_cnt; i++) {
        if (st->prim_cache[i].fb_id == fb_id) return &st->prim_cache[i];
    }
    if (st->prim_cache_cnt >= 8) {
        munmap(st->prim_cache[0].map, st->prim_cache[0].size);
        memmove(&st->prim_cache[0], &st->prim_cache[1], 7 * sizeof(st->prim_cache[0]));
        st->prim_cache_cnt = 7;
    }
    flow_drm_fb_cmd cmd;
    memset(&cmd, 0, sizeof(cmd));
    cmd.fb_id = fb_id;
    if (ioctl(fd, FLOW_IOCTL_GETFB, &cmd) < 0 || cmd.handle == 0) return NULL;
    if (cmd.width != FLOW_PHYS_W || cmd.height != FLOW_PHYS_H) return NULL;

    flow_drm_map_dumb md;
    memset(&md, 0, sizeof(md));
    md.handle = cmd.handle;
    if (ioctl(fd, FLOW_IOCTL_MAP_DUMB, &md) < 0) return NULL;
    size_t sz = (size_t)cmd.pitch * (size_t)cmd.height;
    void *ptr = mmap(NULL, sz, PROT_READ, MAP_SHARED, fd, (off_t)md.offset);
    if (ptr == MAP_FAILED) return NULL;

    flow_dumb_fb *c = &st->prim_cache[st->prim_cache_cnt++];
    c->fb_id = fb_id;
    c->handle = cmd.handle;
    c->width = cmd.width;
    c->height = cmd.height;
    c->pitch = cmd.pitch;
    c->size = sz;
    c->map = (uint32_t *)ptr;
    return c;
}

// Read logical (lx, ly) pixel from primary physical XR24 scanout buffer (800x1280 CCW rotated)
static inline uint32_t flow_read_log_px(const flow_dumb_fb *fb, int lx, int ly) {
    int px = ly;
    int py = (FLOW_LOG_W - 1) - lx;
    if (px < 0 || px >= FLOW_PHYS_W || py < 0 || py >= FLOW_PHYS_H) return 0;
    return fb->map[(size_t)py * (fb->pitch >> 2) + (size_t)px];
}

// Detect Menu Page from primary DRM buffer (returns 0 = not menu, 1 = Menu Page 1, 2 = Menu Page 2)
static inline int flow_detect_menu_page(FlowDrmState *st, int fd, uint32_t fb_id) {
    if (fb_id == 0 && st->primary_plane_id != 0) {
        flow_drm_get_plane gp;
        memset(&gp, 0, sizeof(gp));
        gp.plane_id = st->primary_plane_id;
        if (ioctl(fd, FLOW_IOCTL_GETPLANE, &gp) == 0 && gp.fb_id != 0) {
            fb_id = gp.fb_id;
            st->primary_fb_id = fb_id;
        }
    }
    st->last_detect_fb = fb_id;
    const flow_dumb_fb *fb = flow_drm_map_primary_fb(st, fd, fb_id);
    if (!fb || !fb->map) { st->last_detect_err = 1; return 0; }

    // 1) Top-left white back arrow + TopBar slate + Yellow BROWSER folder (skip animated XYFX dock)
    uint32_t p_back = flow_read_log_px(fb, 55, 40);
    uint32_t p_top  = flow_read_log_px(fb, 500, 34);
    uint32_t p_fold = flow_read_log_px(fb, 112, 155);
    st->last_detect_px[0] = p_back;
    st->last_detect_px[1] = p_top;
    st->last_detect_px[2] = p_fold;
    st->last_detect_px[3] = 0;

    int back_r = (p_back >> 16) & 0xff, back_g = (p_back >> 8) & 0xff, back_b = p_back & 0xff;
    int top_r  = (p_top >> 16) & 0xff,  top_g  = (p_top >> 8) & 0xff,  top_b  = p_top & 0xff;
    int fld_r  = (p_fold >> 16) & 0xff, fld_g  = (p_fold >> 8) & 0xff, fld_b  = p_fold & 0xff;

    if (back_r < 200 || back_g < 200 || back_b < 200) { st->last_detect_err = 2; return 0; }
    if (top_r < 30 || top_r > 70 || top_g < 30 || top_g > 70 || top_b < 32 || top_b > 75) { st->last_detect_err = 3; return 0; }
    if (fld_r < 150 || fld_g < 100 || fld_b > 45) { st->last_detect_err = 4; return 0; }

    // 2) Bottom-center page dots: flanked by black at x=742, x=770
    uint32_t fl_l = flow_read_log_px(fb, 742, 790) & 0x00ffffffU;
    uint32_t fl_r = flow_read_log_px(fb, 770, 790) & 0x00ffffffU;
    if (fl_l != 0 || fl_r != 0) { st->last_detect_err = 6; return 0; }

    uint32_t d1 = flow_read_log_px(fb, 750, 790);
    uint32_t d2 = flow_read_log_px(fb, 762, 790);
    st->last_detect_px[2] = d1;
    st->last_detect_px[3] = d2;
    int d1r = (d1 >> 16) & 0xff, d1g = (d1 >> 8) & 0xff, d1b = d1 & 0xff;
    int d2r = (d2 >> 16) & 0xff, d2g = (d2 >> 8) & 0xff, d2b = d2 & 0xff;

    int d1_red  = (d1r > 150 && d1g < 55 && d1b < 95);
    int d2_red  = (d2r > 150 && d2g < 55 && d2b < 95);
    int d1_gray = (d1r > 70 && d1r < 145 && abs(d1r - d1b) < 25);
    int d2_gray = (d2r > 70 && d2r < 145 && abs(d2r - d2b) < 25);

    if (d1_red && d2_gray) { st->last_detect_err = 0; return 1; }
    if (d2_red && d1_gray) { st->last_detect_err = 0; return 2; }
    st->last_detect_err = 7;
    return 0;
}

typedef int (*flow_atomic_add_prop_fn)(void *req, uint32_t obj_id, uint32_t prop_id, uint64_t val);

static inline void flow_drm_set_crop(FlowDrmState *st, uint32_t px, uint32_t py, uint32_t pw, uint32_t ph) {
    if (px >= FLOW_PHYS_W) px = 0;
    if (py >= FLOW_PHYS_H) py = 0;
    if (pw == 0 || px + pw > FLOW_PHYS_W) pw = FLOW_PHYS_W - px;
    if (ph == 0 || py + ph > FLOW_PHYS_H) ph = FLOW_PHYS_H - py;
    st->ov_px = px;
    st->ov_py = py;
    st->ov_pw = pw;
    st->ov_ph = ph;
}

static inline void flow_drm_splice_overlay(FlowDrmState *st, void *req, flow_atomic_add_prop_fn add_prop, int visible) {
    if (!st->available || !req || !add_prop) return;
    if (visible) {
        uint32_t fb_id = st->ov[st->front_idx].fb_id;
        uint32_t px = st->ov_pw ? st->ov_px : 0;
        uint32_t py = st->ov_ph ? st->ov_py : 0;
        uint32_t pw = st->ov_pw ? st->ov_pw : FLOW_PHYS_W;
        uint32_t ph = st->ov_ph ? st->ov_ph : FLOW_PHYS_H;
        add_prop(req, st->overlay_plane_id, st->prop_crtc_id, st->crtc_id);
        add_prop(req, st->overlay_plane_id, st->prop_fb_id, fb_id);
        add_prop(req, st->overlay_plane_id, st->prop_crtc_x, px);
        add_prop(req, st->overlay_plane_id, st->prop_crtc_y, py);
        add_prop(req, st->overlay_plane_id, st->prop_crtc_w, pw);
        add_prop(req, st->overlay_plane_id, st->prop_crtc_h, ph);
        add_prop(req, st->overlay_plane_id, st->prop_src_x, ((uint64_t)px) << 16);
        add_prop(req, st->overlay_plane_id, st->prop_src_y, ((uint64_t)py) << 16);
        add_prop(req, st->overlay_plane_id, st->prop_src_w, ((uint64_t)pw) << 16);
        add_prop(req, st->overlay_plane_id, st->prop_src_h, ((uint64_t)ph) << 16);
        if (st->prop_zpos) add_prop(req, st->overlay_plane_id, st->prop_zpos, st->zpos_val);
    } else {
        add_prop(req, st->overlay_plane_id, st->prop_crtc_id, 0);
        add_prop(req, st->overlay_plane_id, st->prop_fb_id, 0);
    }
    st->last_committed_vis = visible ? 1 : 0;
}

static inline int flow_drm_commit_direct(FlowDrmState *st, int visible) {
    if (!st->available || st->drm_fd < 0) return -1;
    uint32_t pids[12];
    uint64_t pvals[12];
    uint32_t n = 0;

    pids[n] = st->prop_crtc_id; pvals[n++] = visible ? st->crtc_id : 0;
    pids[n] = st->prop_fb_id;   pvals[n++] = visible ? st->ov[st->front_idx].fb_id : 0;
    if (visible) {
        uint32_t px = st->ov_pw ? st->ov_px : 0;
        uint32_t py = st->ov_ph ? st->ov_py : 0;
        uint32_t pw = st->ov_pw ? st->ov_pw : FLOW_PHYS_W;
        uint32_t ph = st->ov_ph ? st->ov_ph : FLOW_PHYS_H;
        pids[n] = st->prop_crtc_x; pvals[n++] = px;
        pids[n] = st->prop_crtc_y; pvals[n++] = py;
        pids[n] = st->prop_crtc_w; pvals[n++] = pw;
        pids[n] = st->prop_crtc_h; pvals[n++] = ph;
        pids[n] = st->prop_src_x;  pvals[n++] = ((uint64_t)px) << 16;
        pids[n] = st->prop_src_y;  pvals[n++] = ((uint64_t)py) << 16;
        pids[n] = st->prop_src_w;  pvals[n++] = ((uint64_t)pw) << 16;
        pids[n] = st->prop_src_h;  pvals[n++] = ((uint64_t)ph) << 16;
        if (st->prop_zpos) { pids[n] = st->prop_zpos; pvals[n++] = st->zpos_val; }
    }

    uint32_t obj_id = st->overlay_plane_id;
    flow_drm_atomic at;
    memset(&at, 0, sizeof(at));
    at.flags = FLOW_DRM_MODE_ATOMIC_NONBLOCK;
    at.count_objs = 1;
    at.objs_ptr = (uint64_t)(uintptr_t)&obj_id;
    at.count_props_ptr = (uint64_t)(uintptr_t)&n;
    at.props_ptr = (uint64_t)(uintptr_t)pids;
    at.prop_values_ptr = (uint64_t)(uintptr_t)pvals;
    int rc = ioctl(st->drm_fd, FLOW_IOCTL_ATOMIC, &at);
    if (rc == 0) st->last_committed_vis = visible ? 1 : 0;
    return rc;
}

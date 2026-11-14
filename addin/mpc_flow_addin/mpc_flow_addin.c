#define _GNU_SOURCE
#undef _FILE_OFFSET_BITS

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "flow_drm.h"
#include "flow_gfx.h"
#include "flow_http.h"

#define DEFAULT_BRIDGE_HOST "10.101.132.112"
#define DEFAULT_BRIDGE_PORT 6767
#define CONF_PATH "/data/mpc-addins/flowgen/mpc_flow_addin.conf"

#define REQ_REFRESH      (1 << 0)
#define REQ_START_JOB    (1 << 1)
#define REQ_AUDITION     (1 << 2)
#define REQ_OPEN_BROWSER (1 << 3)

typedef int     (*open_fn_t)(const char *, int, ...);
typedef int     (*openat_fn_t)(int, const char *, int, ...);
typedef ssize_t (*read_fn_t)(int, void *, size_t);
typedef int     (*close_fn_t)(int);
typedef int     (*atomic_commit_fn_t)(int, void *, uint32_t, void *);
typedef int     (*atomic_add_prop_fn_t)(void *, uint32_t, uint32_t, uint64_t);

static open_fn_t            real_open = NULL;
static open_fn_t            real_open64 = NULL;
static openat_fn_t          real_openat = NULL;
static read_fn_t            real_read = NULL;
static close_fn_t           real_close = NULL;
static atomic_commit_fn_t   real_atomic_commit = NULL;
static atomic_add_prop_fn_t real_atomic_add_prop = NULL;

static int g_is_mpc = -1;
static int g_enabled = 1;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_net_cond = PTHREAD_COND_INITIALIZER;
static pthread_t       g_net_tid;
static int             g_net_started = 0;
static unsigned int    g_net_req = 0;
static int             g_audition_pad = 1;
static char            g_audition_kit[40] = "";

static FlowDrmState g_drm;
static FlowAppState g_app;
static FlowUiKit g_recent_kits[3];
static uint32_t g_canvas[FLOW_LOG_W * FLOW_LOG_H];

// Menu & UI Visibility State
static int g_menu_page = 0;       // 0 = none, 1 = Menu Page 1, 2 = Menu Page 2
static int g_prev_menu_page = 0;
static int g_rendered_menu_page = -1;
static int g_rendered_online = -1;
static int g_rendered_full = 0;
static unsigned int g_last_caret = 2U;
static int g_flow_open = 0;
static int g_ui_dirty = 1;
static int g_menu_zero_streak = 0;
static uint64_t g_last_menu_ms = 0;
static uint64_t g_last_detect_ms = 0;
static uint64_t g_last_touch_ms = 0;
static uint32_t g_last_detect_fb_id = 0;
static int g_detect_force = 1;
static uint32_t g_cached_pri_plane = 0;
static uint32_t g_cached_prop_fb = 0;

// Touch Tracking State
static int g_ev0_fds[32];
static int g_ev0_cnt = 0;
static int g_abs_inited = 0;
static int g_min_x = 0, g_max_x = 1280;
static int g_min_y = 0, g_max_y = 720;
static int g_raw_x = 0, g_raw_y = 0;
static int g_touch_down = 0;
static int g_prev_touch_down = 0;
static int g_badge_grabbed = 0;
static int g_ui_grabbed = 0;

static __thread int t_in_open_hook = 0;

static uint64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)(ts.tv_nsec / 1000000L);
}

static void flow_dbg(const char *fmt, ...) {
    static int dbg_bytes = 0;
    if (!real_open || dbg_bytes > 16384 || access("/tmp/flow.dbg", F_OK) != 0) return;
    int fd = real_open("/tmp/flow_addin.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0) return;
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (n > 0) {
        if (n >= (int)sizeof(buf)) n = (int)sizeof(buf) - 1;
        ssize_t unused = write(fd, buf, (size_t)n);
        (void)unused;
        dbg_bytes += n;
    }
    if (real_close) real_close(fd);
}

static void init_hooks(void) {
    if (!real_open)            real_open = (open_fn_t)dlsym(RTLD_NEXT, "open");
    if (!real_open64)          real_open64 = (open_fn_t)dlsym(RTLD_NEXT, "open64");
    if (!real_openat)          real_openat = (openat_fn_t)dlsym(RTLD_NEXT, "openat");
    if (!real_read)            real_read = (read_fn_t)dlsym(RTLD_NEXT, "read");
    if (!real_close)           real_close = (close_fn_t)dlsym(RTLD_NEXT, "close");
    if (!real_atomic_commit)   real_atomic_commit = (atomic_commit_fn_t)dlsym(RTLD_NEXT, "drmModeAtomicCommit");
    if (!real_atomic_add_prop) real_atomic_add_prop = (atomic_add_prop_fn_t)dlsym(RTLD_NEXT, "drmModeAtomicAddProperty");
}

static int check_is_mpc_process(void) {
    if (g_is_mpc >= 0) return g_is_mpc;
    char exe[512] = {0};
    ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (n <= 0) { g_is_mpc = 0; return 0; }
    exe[n] = '\0';
    const char *base = strrchr(exe, '/');
    base = base ? base + 1 : exe;
    g_is_mpc = (strcmp(base, "MPC") == 0) ? 1 : 0;
    return g_is_mpc;
}

static void load_flow_config(void) {
    snprintf(g_app.bridge_host, sizeof(g_app.bridge_host), "%s", DEFAULT_BRIDGE_HOST);
    g_app.bridge_port = DEFAULT_BRIDGE_PORT;
    g_app.mode_kit = 1;
    g_app.bpm = 92.0f;
    g_app.key_idx = 0;
    snprintf(g_app.prompt, sizeof(g_app.prompt), "%s", FLOW_PRESETS[0].prompt);
    snprintf(g_app.status_msg, sizeof(g_app.status_msg), "READY -> TAP PRESET OR TYPE PROMPT");

    FILE *f = fopen(CONF_PATH, "r");
    if (!f) return;
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        char *eq = strchr(line, '=');
        if (!eq || line[0] == '#') continue;
        *eq = '\0';
        char *k = line;
        char *v = eq + 1;
        size_t vl = strlen(v);
        while (vl > 0 && (v[vl - 1] == '\n' || v[vl - 1] == '\r' || v[vl - 1] == ' ')) v[--vl] = '\0';
        if (strcmp(k, "enabled") == 0) g_enabled = atoi(v);
        else if (strcmp(k, "bridge_host") == 0 && v[0]) snprintf(g_app.bridge_host, sizeof(g_app.bridge_host), "%s", v);
        else if (strcmp(k, "bridge_port") == 0 && atoi(v) > 0) g_app.bridge_port = atoi(v);
    }
    fclose(f);
}

static void ev0_add_fd(int fd) {
    for (int i = 0; i < g_ev0_cnt; i++) {
        if (g_ev0_fds[i] == fd) return;
    }
    if (g_ev0_cnt < 32) g_ev0_fds[g_ev0_cnt++] = fd;
    if (!g_abs_inited) {
        struct input_absinfo ai;
        if (ioctl(fd, EVIOCGABS(ABS_MT_POSITION_X), &ai) == 0 && ai.maximum > ai.minimum) {
            g_min_x = ai.minimum; g_max_x = ai.maximum;
        } else if (ioctl(fd, EVIOCGABS(ABS_X), &ai) == 0 && ai.maximum > ai.minimum) {
            g_min_x = ai.minimum; g_max_x = ai.maximum;
        }
        if (ioctl(fd, EVIOCGABS(ABS_MT_POSITION_Y), &ai) == 0 && ai.maximum > ai.minimum) {
            g_min_y = ai.minimum; g_max_y = ai.maximum;
        } else if (ioctl(fd, EVIOCGABS(ABS_Y), &ai) == 0 && ai.maximum > ai.minimum) {
            g_min_y = ai.minimum; g_max_y = ai.maximum;
        }
        g_abs_inited = 1;
    }
    flow_dbg("ev0_add fd=%d min=%d,%d max=%d,%d\n", fd, g_min_x, g_min_y, g_max_x, g_max_y);
}

static void ev0_remove_fd(int fd) {
    for (int i = 0; i < g_ev0_cnt; i++) {
        if (g_ev0_fds[i] == fd) {
            g_ev0_fds[i] = g_ev0_fds[--g_ev0_cnt];
            return;
        }
    }
}

static int ev0_has_fd(int fd) {
    for (int i = 0; i < g_ev0_cnt; i++) {
        if (g_ev0_fds[i] == fd) return 1;
    }
    return 0;
}

static void scan_existing_ev0_fds(void) {
    for (int fd = 3; fd < 256; fd++) {
        char linkp[64], target[128] = {0};
        snprintf(linkp, sizeof(linkp), "/proc/self/fd/%d", fd);
        ssize_t n = readlink(linkp, target, sizeof(target) - 1);
        if (n > 0) {
            target[n] = '\0';
            if (strcmp(target, "/dev/input/event0") == 0) {
                int fl = fcntl(fd, F_GETFL);
                if (fl >= 0 && (fl & O_ACCMODE) != O_WRONLY) ev0_add_fd(fd);
            }
        }
    }
}

// Fast sequential-write rotate-blit: logical 1280x800 ARGB canvas -> physical 800x1280 AR24 dumb buffer (locked!)
// Iterating py outer / px inner yields 100% linear sequential stores into uncached WC dumb GEM memory.
static void flow_blit_and_prepare_unlocked(void) {
    if (!g_drm.available) return;
    int back = g_drm.front_idx ^ 1;
    uint32_t *dst = g_drm.ov[back].map;
    uint32_t pitch_w = g_drm.ov[back].pitch >> 2;
    if (!dst) return;

    if (g_flow_open) {
        uint64_t now = now_ms();
        unsigned int caret = (unsigned int)((now / 500ULL) & 1U);
        if (!g_ui_dirty && g_rendered_full && caret == g_last_caret) {
            return;
        }
        flow_render_full_ui(g_canvas, &g_app, now);
        for (int py = 0; py < FLOW_PHYS_H; py++) {
            int lx = (FLOW_LOG_W - 1) - py;
            uint32_t *drow = dst + (size_t)py * pitch_w;
            const uint32_t *scol = g_canvas + lx;
            for (int px = 0; px < FLOW_PHYS_W; px++) {
                drow[px] = scol[(size_t)px * FLOW_LOG_W];
            }
        }
        flow_drm_set_crop(&g_drm, 0, 0, FLOW_PHYS_W, FLOW_PHYS_H);
        g_drm.front_idx = back;
        g_ui_dirty = 0;
        g_rendered_full = 1;
        g_last_caret = caret;
        g_rendered_menu_page = -1;
    } else if (g_menu_page > 0) {
        g_rendered_full = 0;
        if (g_ui_dirty || g_rendered_menu_page != g_menu_page || g_rendered_online != g_app.bridge_online) {
            flow_render_menu_badges(g_canvas, g_menu_page, g_app.bridge_online);
            // Physical tight cropped bounds aligned to 4 pixels:
            // Page 1: logical lx=[116..324), ly=[28..80)   -> px=28, pw=52,  py=956, ph=208
            // Page 2: union w/ P2 tile lx=[116..480), ly=[28..372) -> px=28, pw=344, py=800, ph=364
            uint32_t cpx = 28;
            uint32_t cpy = (g_menu_page == 2) ? 800U : 956U;
            uint32_t cpw = (g_menu_page == 2) ? 344U : 52U;
            uint32_t cph = (g_menu_page == 2) ? 364U : 208U;
            for (uint32_t py = cpy; py < cpy + cph; py++) {
                int lx = (FLOW_LOG_W - 1) - (int)py;
                uint32_t *drow = dst + (size_t)py * pitch_w;
                const uint32_t *scol = g_canvas + lx;
                for (uint32_t px = cpx; px < cpx + cpw; px++) {
                    drow[px] = scol[(size_t)px * FLOW_LOG_W];
                }
            }
            flow_drm_set_crop(&g_drm, cpx, cpy, cpw, cph);
            g_drm.front_idx = back;
            g_rendered_menu_page = g_menu_page;
            g_rendered_online = g_app.bridge_online;
            g_ui_dirty = 0;
        }
    } else {
        g_rendered_full = 0;
    }
}

static void flow_queue_net_unlocked(unsigned int flags) {
    g_net_req |= flags;
    if (g_net_started) pthread_cond_signal(&g_net_cond);
}

static void flow_handle_action_unlocked(int act) {
    if (act == ACT_NONE) return;
    if (act == ACT_CLOSE) {
        g_flow_open = 0;
        g_ui_dirty = 1;
        return;
    }
    if (act == ACT_MODE_KIT) { g_app.mode_kit = 1; g_ui_dirty = 1; return; }
    if (act == ACT_MODE_LOOP) { g_app.mode_kit = 0; g_ui_dirty = 1; return; }
    if (act == ACT_OPEN_BROWSER) {
        g_flow_open = 0;
        g_ui_dirty = 1;
        flow_queue_net_unlocked(REQ_OPEN_BROWSER);
        return;
    }
    if (act == ACT_TOGGLE_MAC) {
        g_app.mac_audition = g_app.mac_audition ? 0 : 1;
        snprintf(g_app.status_msg, sizeof(g_app.status_msg),
                 g_app.mac_audition ? "MAC SPEAKER PREVIEW: ON" : "MAC SPEAKER PREVIEW: OFF (MPC PAD ONLY)");
        g_ui_dirty = 1;
        return;
    }
    if (act == ACT_BPM_DEC) {
        g_app.bpm = (g_app.bpm > 45.0f) ? (g_app.bpm - 2.0f) : 45.0f;
        g_ui_dirty = 1;
        return;
    }
    if (act == ACT_BPM_INC) {
        g_app.bpm = (g_app.bpm < 200.0f) ? (g_app.bpm + 2.0f) : 200.0f;
        g_ui_dirty = 1;
        return;
    }
    if (act == ACT_KEY_CYCLE) {
        g_app.key_idx = (g_app.key_idx + 1) % FLOW_NUM_KEYS;
        g_ui_dirty = 1;
        return;
    }
    if (act == ACT_GENERATE) {
        if (!g_app.job_running && g_app.prompt[0]) {
            g_app.job_running = 1;
            g_app.job_pct = 5;
            snprintf(g_app.status_msg, sizeof(g_app.status_msg), "CONNECTING MAC FLOW BRIDGE...");
            flow_queue_net_unlocked(REQ_START_JOB);
            g_ui_dirty = 1;
        }
        return;
    }
    if (act >= ACT_PRESET_BASE && act < ACT_PRESET_BASE + 8) {
        int idx = act - ACT_PRESET_BASE;
        snprintf(g_app.prompt, sizeof(g_app.prompt), "%s", FLOW_PRESETS[idx].prompt);
        g_app.bpm = FLOW_PRESETS[idx].bpm;
        for (int k = 0; k < FLOW_NUM_KEYS; k++) {
            if (strcmp(FLOW_KEY_NAMES[k], FLOW_PRESETS[idx].key) == 0) { g_app.key_idx = k; break; }
        }
        snprintf(g_app.status_msg, sizeof(g_app.status_msg), "PRESET LOADED -> TAP BUILD KIT");
        g_ui_dirty = 1;
        return;
    }
    if (act >= ACT_PAD_BASE && act < ACT_PAD_BASE + 16) {
        int p = (act - ACT_PAD_BASE) + 1;
        g_app.flash_pad = p;
        g_app.flash_until_ms = now_ms() + 180ULL;
        g_audition_pad = p;
        snprintf(g_audition_kit, sizeof(g_audition_kit), "%s", g_app.active_kit.kit_name);
        flow_queue_net_unlocked(REQ_AUDITION);
        g_ui_dirty = 1;
        return;
    }
    if (act >= ACT_RECENT_BASE && act < ACT_RECENT_BASE + 3) {
        int idx = act - ACT_RECENT_BASE;
        if (idx < g_app.num_recent && g_recent_kits[idx].kit_name[0]) {
            g_app.active_kit = g_recent_kits[idx];
            g_app.has_kit = 1;
            snprintf(g_app.status_msg, sizeof(g_app.status_msg), "LOADED KIT: %s", g_app.active_kit.kit_name);
            g_ui_dirty = 1;
        }
        return;
    }
    if (act == ACT_KEY_SPACE) {
        size_t len = strlen(g_app.prompt);
        if (len + 1 < sizeof(g_app.prompt)) { g_app.prompt[len] = ' '; g_app.prompt[len + 1] = '\0'; }
        g_ui_dirty = 1;
        return;
    }
    if (act == ACT_KEY_BACK) {
        size_t len = strlen(g_app.prompt);
        if (len > 0) g_app.prompt[len - 1] = '\0';
        g_ui_dirty = 1;
        return;
    }
    if (act == ACT_KEY_CLEAR) {
        g_app.prompt[0] = '\0';
        g_ui_dirty = 1;
        return;
    }
    if (act == ACT_KEY_SHIFT) {
        g_app.shift_on = !g_app.shift_on;
        g_ui_dirty = 1;
        return;
    }
    if (act >= ACT_CHAR_BASE && act < ACT_CHAR_BASE + 127) {
        size_t len = strlen(g_app.prompt);
        if (len + 1 < sizeof(g_app.prompt)) {
            g_app.prompt[len] = (char)(act - ACT_CHAR_BASE);
            g_app.prompt[len + 1] = '\0';
        }
        g_ui_dirty = 1;
    }
}

static void flow_sync_recent_from_kits_json(const char *json) {
    const char *cur = strstr(json, "\"kits\"");
    if (!cur) return;
    g_app.num_recent = 0;
    for (int i = 0; i < 3; i++) {
        const char *kp = strstr(cur, "\"kit_name\"");
        if (!kp) break;
        const char *obj = kp;
        while (obj > cur && *obj != '{') obj--;
        flow_parse_kit_from_json(*obj == '{' ? obj : kp, &g_recent_kits[i]);
        snprintf(g_app.recent_names[i], sizeof(g_app.recent_names[i]), "%s", g_recent_kits[i].kit_name);
        if (g_app.recent_names[i][0]) g_app.num_recent = i + 1;
        cur = kp + 10;
    }
    if (g_app.num_recent > 0 && !g_app.has_kit && g_recent_kits[0].kit_name[0]) {
        g_app.active_kit = g_recent_kits[0];
        g_app.has_kit = 1;
    }
}

static void *flow_net_worker(void *arg) {
    (void)arg;
    char *resp = (char *)malloc(32768);
    if (!resp) return NULL;

    while (1) {
        char host[64];
        int port;
        unsigned int reqs = 0;
        char prompt_esc[400];
        char job_id[48];
        char kit_name[40];
        float bpm;
        const char *key_name;
        int mode_kit;
        int aud_pad;
        int mac_aud;

        pthread_mutex_lock(&g_lock);
        while (g_net_req == 0 && !g_app.job_running) {
            struct timespec ts;
            clock_gettime(CLOCK_REALTIME, &ts);
            if (g_app.flash_pad) {
                ts.tv_nsec += 120000000L;
                if (ts.tv_nsec >= 1000000000L) { ts.tv_sec += 1; ts.tv_nsec -= 1000000000L; }
            } else if (g_flow_open) {
                ts.tv_nsec += 500000000L;
                if (ts.tv_nsec >= 1000000000L) { ts.tv_sec += 1; ts.tv_nsec -= 1000000000L; }
            } else {
                ts.tv_sec += 3;
            }
            int wrc = pthread_cond_timedwait(&g_net_cond, &g_lock, &ts);
            if (wrc != 0 && (g_flow_open || (g_app.flash_pad && now_ms() >= g_app.flash_until_ms))) break;
        }
        reqs = g_net_req;
        g_net_req &= ~(REQ_START_JOB | REQ_AUDITION | REQ_OPEN_BROWSER | REQ_REFRESH);
        snprintf(host, sizeof(host), "%s", g_app.bridge_host);
        port = g_app.bridge_port;
        bpm = g_app.bpm;
        key_name = FLOW_KEY_NAMES[g_app.key_idx % FLOW_NUM_KEYS];
        mode_kit = g_app.mode_kit;
        aud_pad = g_audition_pad;
        mac_aud = g_app.mac_audition;
        snprintf(kit_name, sizeof(kit_name), "%s", g_audition_kit);
        snprintf(job_id, sizeof(job_id), "%s", g_app.job_id);
        flow_json_escape(g_app.prompt, prompt_esc, sizeof(prompt_esc));
        int job_running = g_app.job_running;
        pthread_mutex_unlock(&g_lock);

        int need_commit = 0;

        if (reqs & REQ_OPEN_BROWSER) {
            flow_http_exchange(host, port, "POST", "/api/macro", "{\"action\":\"open_browser\",\"okcomputer\":true}", resp, 32768);
        }

        if (reqs & REQ_AUDITION) {
            char body[256];
            snprintf(body, sizeof(body), "{\"kit_name\":\"%s\",\"pad\":%d,\"play_mac\":%s,\"trigger_mpc\":true}",
                     kit_name, aud_pad, mac_aud ? "true" : "false");
            flow_http_exchange(host, port, "POST", "/api/flow/audition", body, resp, 32768);
        }

        if (reqs & REQ_REFRESH) {
            if (flow_http_exchange(host, port, "GET", "/api/flow/kits", NULL, resp, 32768) == 0) {
                pthread_mutex_lock(&g_lock);
                g_app.bridge_online = 1;
                flow_sync_recent_from_kits_json(resp);
                g_ui_dirty = 1;
                need_commit = 1;
                pthread_mutex_unlock(&g_lock);
            } else {
                pthread_mutex_lock(&g_lock);
                g_app.bridge_online = 0;
                g_ui_dirty = 1;
                need_commit = 1;
                pthread_mutex_unlock(&g_lock);
            }
        }

        if (reqs & REQ_START_JOB) {
            char body[640];
            snprintf(
                body, sizeof(body),
                "{\"mode\":\"%s\",\"prompt\":\"%s\",\"bpm\":%.1f,\"key\":\"%s\",\"deploy\":true}",
                mode_kit ? "kit" : "loop", prompt_esc, bpm, key_name
            );
            if (flow_http_exchange(host, port, "POST", "/api/flow/job", body, resp, 32768) == 0) {
                char jid[48] = {0}, msg[128] = {0};
                flow_json_find_str(resp, "job_id", jid, sizeof(jid));
                flow_json_find_str(resp, "message", msg, sizeof(msg));
                pthread_mutex_lock(&g_lock);
                g_app.bridge_online = 1;
                snprintf(g_app.job_id, sizeof(g_app.job_id), "%s", jid);
                snprintf(g_app.status_msg, sizeof(g_app.status_msg), "%s", msg[0] ? msg : "Generating...");
                g_app.job_pct = flow_json_find_int(resp, "pct", 10);
                g_ui_dirty = 1;
                need_commit = 1;
                pthread_mutex_unlock(&g_lock);
                job_running = 1;
                snprintf(job_id, sizeof(job_id), "%s", jid);
            } else {
                pthread_mutex_lock(&g_lock);
                g_app.bridge_online = 0;
                g_app.job_running = 0;
                snprintf(g_app.status_msg, sizeof(g_app.status_msg), "BRIDGE OFFLINE (%s:%d)", host, port);
                g_ui_dirty = 1;
                need_commit = 1;
                pthread_mutex_unlock(&g_lock);
                job_running = 0;
            }
        }

        if (job_running && job_id[0]) {
            char path[96];
            snprintf(path, sizeof(path), "/api/flow/job?id=%s", job_id);
            if (flow_http_exchange(host, port, "GET", path, NULL, resp, 32768) == 0) {
                char status[24] = {0}, msg[128] = {0};
                flow_json_find_str(resp, "status", status, sizeof(status));
                flow_json_find_str(resp, "message", msg, sizeof(msg));
                int pct = flow_json_find_int(resp, "pct", 10);

                pthread_mutex_lock(&g_lock);
                g_app.bridge_online = 1;
                g_app.job_pct = pct;
                if (msg[0]) snprintf(g_app.status_msg, sizeof(g_app.status_msg), "%s", msg);
                if (strcmp(status, "done") == 0) {
                    g_app.job_running = 0;
                    g_app.job_pct = 100;
                    const char *res_obj = strstr(resp, "\"result\"");
                    FlowUiKit ktmp;
                    flow_parse_kit_from_json(res_obj ? res_obj : resp, &ktmp);
                    if (ktmp.kit_name[0]) {
                        g_app.active_kit = ktmp;
                        g_app.has_kit = 1;
                        g_net_req |= REQ_REFRESH;
                    }
                } else if (strcmp(status, "error") == 0) {
                    g_app.job_running = 0;
                    g_app.job_pct = 0;
                }
                g_ui_dirty = 1;
                need_commit = 1;
                pthread_mutex_unlock(&g_lock);
                usleep(350000);
            } else {
                usleep(450000);
            }
        }

        // Refresh prompt caret / pad flash / commit immediately if open UI needs update
        pthread_mutex_lock(&g_lock);
        if (g_app.flash_pad && now_ms() >= g_app.flash_until_ms) {
            g_app.flash_pad = 0;
            g_ui_dirty = 1;
            need_commit = 1;
        }
        if (g_flow_open && g_rendered_full && ((unsigned int)((now_ms() / 500ULL) & 1U) != g_last_caret)) {
            need_commit = 1;
        }
        if (need_commit && g_drm.available) {
            int vis = (g_flow_open || g_menu_page > 0) ? 1 : 0;
            if (vis) flow_blit_and_prepare_unlocked();
            if (vis || g_drm.last_committed_vis) flow_drm_commit_direct(&g_drm, vis);
        }
        pthread_mutex_unlock(&g_lock);
    }
    return NULL;
}

static void ensure_worker_started_locked(void) {
    if (g_net_started) return;
    g_net_started = 1;
    pthread_create(&g_net_tid, NULL, flow_net_worker, NULL);
    pthread_detach(g_net_tid);
    g_net_req |= REQ_REFRESH;
}

__attribute__((constructor))
static void flow_addin_ctor(void) {
    init_hooks();
    if (!check_is_mpc_process()) return;
    load_flow_config();
    scan_existing_ev0_fds();
}

// ---------------------------------------------------------------------------
// Hooked Libc Open / Read / Close for Touch Interception on /dev/input/event0
// ---------------------------------------------------------------------------

static void flow_maybe_track_ev0(int fd, const char *pathname, int flags) {
    if (fd >= 0 && !t_in_open_hook && check_is_mpc_process() && g_enabled) {
        if (strcmp(pathname, "/dev/input/event0") == 0 && (flags & O_ACCMODE) != O_WRONLY) {
            pthread_mutex_lock(&g_lock);
            ev0_add_fd(fd);
            pthread_mutex_unlock(&g_lock);
        }
    }
}

int open(const char *pathname, int flags, ...) {
    init_hooks();
    mode_t mode = 0;
    int has_mode = (flags & O_CREAT) != 0;
#ifdef O_TMPFILE
    has_mode = has_mode || ((flags & O_TMPFILE) == O_TMPFILE);
#endif
    if (has_mode) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
    }
    int fd = has_mode ? real_open(pathname, flags, mode) : real_open(pathname, flags);
    flow_maybe_track_ev0(fd, pathname, flags);
    return fd;
}

int open64(const char *pathname, int flags, ...) {
    init_hooks();
    mode_t mode = 0;
    int has_mode = (flags & O_CREAT) != 0;
#ifdef O_TMPFILE
    has_mode = has_mode || ((flags & O_TMPFILE) == O_TMPFILE);
#endif
    if (has_mode) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
    }
    int fd = has_mode ? real_open64(pathname, flags, mode) : real_open64(pathname, flags);
    flow_maybe_track_ev0(fd, pathname, flags);
    return fd;
}

int openat(int dirfd, const char *pathname, int flags, ...) {
    init_hooks();
    mode_t mode = 0;
    int has_mode = (flags & O_CREAT) != 0;
#ifdef O_TMPFILE
    has_mode = has_mode || ((flags & O_TMPFILE) == O_TMPFILE);
#endif
    if (has_mode) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
    }
    int fd = has_mode ? real_openat(dirfd, pathname, flags, mode) : real_openat(dirfd, pathname, flags);
    flow_maybe_track_ev0(fd, pathname, flags);
    return fd;
}

int close(int fd) {
    init_hooks();
    if (check_is_mpc_process() && g_ev0_cnt > 0) {
        pthread_mutex_lock(&g_lock);
        ev0_remove_fd(fd);
        pthread_mutex_unlock(&g_lock);
    }
    return real_close(fd);
}

static int ev0_coords_to_logical(int *out_lx, int *out_ly) {
    int dx = g_max_x - g_min_x;
    int dy = g_max_y - g_min_y;
    if (dx <= 0) dx = 1280;
    if (dy <= 0) dy = 720;
    // Physical ILI2116 touch panel axes on MPC Live (CCW rotated):
    //   raw_x [0..max_x] -> logical Y [0..800]
    //   raw_y [0..max_y] -> inverted logical X [1280..0]
    int ly = (int)(((int64_t)(g_raw_x - g_min_x) * FLOW_LOG_H + dx / 2) / dx);
    int lx = (FLOW_LOG_W - 1) - (int)(((int64_t)(g_raw_y - g_min_y) * (FLOW_LOG_W - 1) + dy / 2) / dy);
    if (lx < 0) lx = 0;
    if (lx >= FLOW_LOG_W) lx = FLOW_LOG_W - 1;
    if (ly < 0) ly = 0;
    if (ly >= FLOW_LOG_H) ly = FLOW_LOG_H - 1;
    *out_lx = lx;
    *out_ly = ly;
    return 0;
}

ssize_t read(int fd, void *buf, size_t count) {
    init_hooks();
    ssize_t n = real_read(fd, buf, count);
    if (n <= 0 || !check_is_mpc_process() || !g_enabled) return n;

    int is_ev0 = 0;
    pthread_mutex_lock(&g_lock);
    is_ev0 = ev0_has_fd(fd);
    pthread_mutex_unlock(&g_lock);
    if (!is_ev0 || (size_t)n < sizeof(struct input_event)) return n;

    size_t nev = (size_t)n / sizeof(struct input_event);
    struct input_event *evs = (struct input_event *)buf;
    size_t kept_bytes = 0;
    size_t pkt_start = 0;

    pthread_mutex_lock(&g_lock);
    for (size_t i = 0; i < nev; i++) {
        struct input_event *e = &evs[i];
        if (e->type == EV_ABS) {
            if (e->code == ABS_X || e->code == ABS_MT_POSITION_X) g_raw_x = e->value;
            else if (e->code == ABS_Y || e->code == ABS_MT_POSITION_Y) g_raw_y = e->value;
            else if (e->code == ABS_MT_TRACKING_ID) g_touch_down = (e->value >= 0) ? 1 : 0;
        } else if (e->type == EV_KEY && e->code == BTN_TOUCH) {
            g_touch_down = (e->value != 0) ? 1 : 0;
        } else if (e->type == EV_SYN && e->code == SYN_REPORT) {
            int lx = 0, ly = 0;
            ev0_coords_to_logical(&lx, &ly);
            int down_edge = (g_touch_down && !g_prev_touch_down);
            int up_edge   = (!g_touch_down && g_prev_touch_down);
            g_prev_touch_down = g_touch_down;
            flow_dbg("SYN fd=%d raw=(%d,%d) log=(%d,%d) dn=%d up=%d page=%d open=%d\n",
                     fd, g_raw_x, g_raw_y, lx, ly, down_edge, up_edge, g_menu_page, g_flow_open);

            int intercept = 0;
            if (down_edge || up_edge) {
                g_last_touch_ms = now_ms();
                g_detect_force = 1;
            }
            if (g_flow_open) {
                intercept = 1;
                if (down_edge) {
                    g_ui_grabbed = 1;
                    int act = flow_hit_test(lx, ly, &g_app);
                    if (act != ACT_NONE) {
                        flow_handle_action_unlocked(act);
                        if (g_drm.available) {
                            flow_blit_and_prepare_unlocked();
                            flow_drm_commit_direct(&g_drm, (g_flow_open || g_menu_page > 0) ? 1 : 0);
                        }
                    }
                } else if (up_edge) {
                    g_ui_grabbed = 0;
                }
            } else if (g_menu_page > 0) {
                int in_top = flow_in_rect(lx, ly, (FlowRect){MENU_TOP_BTN_X, MENU_TOP_BTN_Y, MENU_TOP_BTN_W, MENU_TOP_BTN_H});
                int in_tile = (g_menu_page == 2) && flow_in_rect(lx, ly, (FlowRect){MENU_P2_TILE_X, MENU_P2_TILE_Y, MENU_P2_TILE_W, MENU_P2_TILE_H});
                if (down_edge && (in_top || in_tile)) {
                    g_badge_grabbed = 1;
                    intercept = 1;
                } else if (g_badge_grabbed) {
                    intercept = 1;
                    if (up_edge) {
                        g_badge_grabbed = 0;
                        if (in_top || in_tile) {
                            g_flow_open = 1;
                            g_ui_dirty = 1;
                            flow_queue_net_unlocked(REQ_REFRESH);
                            if (g_drm.available) {
                                flow_blit_and_prepare_unlocked();
                                flow_drm_commit_direct(&g_drm, 1);
                            }
                        }
                    }
                }
            }

            if (!intercept) {
                size_t chunk_len = ((i - pkt_start) + 1) * sizeof(struct input_event);
                if (kept_bytes != pkt_start * sizeof(struct input_event)) {
                    memmove((uint8_t *)buf + kept_bytes, &evs[pkt_start], chunk_len);
                }
                kept_bytes += chunk_len;
            }
            pkt_start = i + 1;
        }
    }
    // Preserve trailing partial packet if not split mid-packet cleanly
    if (pkt_start < nev && !g_flow_open && !g_badge_grabbed) {
        size_t rem = (nev - pkt_start) * sizeof(struct input_event);
        if (kept_bytes != pkt_start * sizeof(struct input_event)) {
            memmove((uint8_t *)buf + kept_bytes, &evs[pkt_start], rem);
        }
        kept_bytes += rem;
    }
    pthread_mutex_unlock(&g_lock);

    if (kept_bytes == 0) {
        errno = EAGAIN;
        return -1;
    }
    return (ssize_t)kept_bytes;
}

// ---------------------------------------------------------------------------
// Hooked LibDRM Atomic Helpers (Plane[38] Overlay & Menu Page Detection)
// ---------------------------------------------------------------------------

int drmModeAtomicAddProperty(void *req, uint32_t obj_id, uint32_t prop_id, uint64_t val) {
    init_hooks();
    uint32_t pri = __atomic_load_n(&g_cached_pri_plane, __ATOMIC_RELAXED);
    uint32_t pfb = __atomic_load_n(&g_cached_prop_fb, __ATOMIC_RELAXED);
    if (pri && obj_id == pri && prop_id == pfb && val != 0 && check_is_mpc_process() && g_enabled) {
        __atomic_store_n(&g_drm.primary_fb_id, (uint32_t)val, __ATOMIC_RELAXED);
    }
    return real_atomic_add_prop(req, obj_id, prop_id, val);
}

int drmModeAtomicCommit(int fd, void *req, uint32_t flags, void *user_data) {
    init_hooks();
    if (check_is_mpc_process() && g_enabled && !(flags & FLOW_DRM_MODE_ATOMIC_TEST_ONLY)) {
        pthread_mutex_lock(&g_lock);
        if (!g_drm.inited) {
            flow_drm_init_planes(&g_drm, fd);
            __atomic_store_n(&g_cached_pri_plane, g_drm.primary_plane_id, __ATOMIC_RELEASE);
            __atomic_store_n(&g_cached_prop_fb, g_drm.prop_fb_id, __ATOMIC_RELEASE);
            ensure_worker_started_locked();
            flow_dbg("drm init avail=%d pri=%u ov=%u ev0_cnt=%d\n",
                     g_drm.available, g_drm.primary_plane_id, g_drm.overlay_plane_id, g_ev0_cnt);
        }
        if (g_ev0_cnt == 0) {
            scan_existing_ev0_fds();
        }
        if (g_drm.available) {
            uint64_t now = now_ms();
            uint32_t cur_fb = __atomic_load_n(&g_drm.primary_fb_id, __ATOMIC_RELAXED);
            int recent_touch = ((now - g_last_touch_ms) < 280ULL);
            int need_detect = g_detect_force || recent_touch || (g_menu_zero_streak > 0) ||
                              ((cur_fb != g_last_detect_fb_id) && ((now - g_last_detect_ms) >= 60ULL)) ||
                              ((now - g_last_detect_ms) >= 160ULL);
            // When full FLOW AI panel is open, only allow MENU button/close exit paths or sparse check
            if (g_flow_open && !g_detect_force && (now - g_last_detect_ms) < 250ULL) {
                need_detect = 0;
            }
            if (need_detect) {
                int page = flow_detect_menu_page(&g_drm, fd, cur_fb);
                g_last_detect_ms = now;
                g_last_detect_fb_id = g_drm.primary_fb_id;
                g_detect_force = 0;
                g_prev_menu_page = g_menu_page;
                if (page > 0) {
                    g_menu_page = page;
                    g_menu_zero_streak = 0;
                    g_last_menu_ms = now;
                } else if (g_menu_page > 0) {
                    g_menu_zero_streak++;
                    if (g_menu_zero_streak >= 6 && (now - g_last_menu_ms) > 350ULL) {
                        flow_dbg("menu_exit streak=%d err=%d fb=%u px=%06x/%06x/%06x/%06x\n",
                                 g_menu_zero_streak, g_drm.last_detect_err, g_drm.last_detect_fb,
                                 g_drm.last_detect_px[0] & 0xffffffU, g_drm.last_detect_px[1] & 0xffffffU,
                                 g_drm.last_detect_px[2] & 0xffffffU, g_drm.last_detect_px[3] & 0xffffffU);
                        g_menu_page = 0;
                        if (g_flow_open) g_flow_open = 0;
                    }
                } else {
                    g_menu_page = 0;
                }
            }
            int show = (g_flow_open || g_menu_page > 0) ? 1 : 0;
            if (show) flow_blit_and_prepare_unlocked();
            if (show || g_drm.last_committed_vis) {
                flow_drm_splice_overlay(&g_drm, req, real_atomic_add_prop, show);
            }
        }
        pthread_mutex_unlock(&g_lock);
    }
    return real_atomic_commit(fd, req, flags, user_data);
}

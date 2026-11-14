#pragma once
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

typedef struct {
    int pad;
    char pad_id[8];
    char short_name[20];
    char role[16];
    char source[16];
} FlowUiPad;

typedef struct {
    char kit_name[40];
    char xpm_path[128];
    float bpm;
    char key[24];
    int lyria_hits;
    FlowUiPad pads[16];
} FlowUiKit;

typedef struct {
    char bridge_host[64];
    int bridge_port;
    int bridge_online;
    int mode_kit;          // 1 = Drum Kit (.xpm), 0 = Loop Bank
    float bpm;
    int key_idx;
    char prompt[192];
    int cursor_pos;
    int shift_on;
    int mac_audition;      // 0 = MPC hardware/MIDI only (default), 1 = also preview via Mac speakers
    int flash_pad;
    uint64_t flash_until_ms;

    // Live Job / Status
    int job_running;
    int job_pct;
    char job_id[48];
    char status_msg[128];

    // Active kit displayed on the 4x4 pad grid
    int has_kit;
    FlowUiKit active_kit;

    // Quick recent kits (up to 3)
    int num_recent;
    char recent_names[3][40];
} FlowAppState;

static inline const char *flow_json_find_str(const char *json, const char *key, char *out, size_t out_sz) {
    char pat[80];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) { if (out_sz) out[0] = '\0'; return NULL; }
    p += strlen(pat);
    while (*p == ' ' || *p == '\t' || *p == ':') p++;
    if (*p != '\"') { if (out_sz) out[0] = '\0'; return NULL; }
    p++;
    size_t i = 0;
    while (*p && *p != '\"' && i + 1 < out_sz) {
        if (*p == '\\' && p[1]) p++;
        out[i++] = *p++;
    }
    if (out_sz) out[i] = '\0';
    return out;
}

static inline int flow_json_find_int(const char *json, const char *key, int fallback) {
    char pat[80];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) return fallback;
    p += strlen(pat);
    while (*p == ' ' || *p == '\t' || *p == ':') p++;
    if (*p == '-' || (*p >= '0' && *p <= '9')) return atoi(p);
    return fallback;
}

static inline float flow_json_find_float(const char *json, const char *key, float fallback) {
    char pat[80];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) return fallback;
    p += strlen(pat);
    while (*p == ' ' || *p == '\t' || *p == ':') p++;
    if (*p == '-' || (*p >= '0' && *p <= '9')) return (float)atof(p);
    return fallback;
}

static inline void flow_json_escape(const char *src, char *dst, size_t dst_sz) {
    size_t o = 0;
    for (size_t i = 0; src[i] && o + 3 < dst_sz; i++) {
        char c = src[i];
        if (c == '\"' || c == '\\') { dst[o++] = '\\'; dst[o++] = c; }
        else if ((unsigned char)c >= 0x20) { dst[o++] = c; }
    }
    if (dst_sz) dst[o] = '\0';
}

static inline int flow_http_exchange(
    const char *host, int port, const char *method, const char *path,
    const char *body, char *resp_buf, size_t resp_cap
) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;

    struct timeval tv;
    tv.tv_sec = 2;
    tv.tv_usec = 500000;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) {
        close(fd);
        return -1;
    }
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        return -1;
    }

    char hdr[1024];
    int blen = body ? (int)strlen(body) : 0;
    int hlen = snprintf(
        hdr, sizeof(hdr),
        "%s %s HTTP/1.1\r\nHost: %s:%d\r\nConnection: close\r\n"
        "%s%sContent-Length: %d\r\n\r\n",
        method, path, host, port,
        body ? "Content-Type: application/json\r\n" : "",
        "",
        blen
    );
    if (send(fd, hdr, (size_t)hlen, MSG_NOSIGNAL) < 0) { close(fd); return -1; }
    if (body && blen > 0 && send(fd, body, (size_t)blen, MSG_NOSIGNAL) < 0) { close(fd); return -1; }

    size_t tot = 0;
    while (tot + 1 < resp_cap) {
        ssize_t n = recv(fd, resp_buf + tot, resp_cap - 1 - tot, 0);
        if (n <= 0) break;
        tot += (size_t)n;
    }
    resp_buf[tot] = '\0';
    close(fd);

    const char *sep = strstr(resp_buf, "\r\n\r\n");
    if (!sep) return -1;
    const char *json = sep + 4;
    memmove(resp_buf, json, strlen(json) + 1);
    return 0;
}

static inline void flow_parse_kit_from_json(const char *json, FlowUiKit *kit) {
    memset(kit, 0, sizeof(*kit));
    flow_json_find_str(json, "kit_name", kit->kit_name, sizeof(kit->kit_name));
    flow_json_find_str(json, "xpm_path", kit->xpm_path, sizeof(kit->xpm_path));
    flow_json_find_str(json, "key", kit->key, sizeof(kit->key));
    kit->bpm = flow_json_find_float(json, "bpm", 92.0f);
    kit->lyria_hits = flow_json_find_int(json, "lyria_hits", 0);

    const char *pads_arr = strstr(json, "\"pads\"");
    if (!pads_arr) return;
    const char *cur = pads_arr;
    for (int i = 0; i < 16; i++) {
        const char *obj = strchr(cur, '{');
        if (!obj) break;
        const char *end = strchr(obj, '}');
        if (!end) break;
        size_t len = (size_t)(end - obj + 1);
        if (len > 511) len = 511;
        char seg[512];
        memcpy(seg, obj, len);
        seg[len] = '\0';
        int p = flow_json_find_int(seg, "pad", i + 1);
        if (p >= 1 && p <= 16) {
            FlowUiPad *dst = &kit->pads[p - 1];
            dst->pad = p;
            flow_json_find_str(seg, "pad_id", dst->pad_id, sizeof(dst->pad_id));
            flow_json_find_str(seg, "short", dst->short_name, sizeof(dst->short_name));
            flow_json_find_str(seg, "role", dst->role, sizeof(dst->role));
            flow_json_find_str(seg, "source", dst->source, sizeof(dst->source));
        }
        cur = end + 1;
    }
}

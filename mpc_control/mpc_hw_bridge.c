#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

typedef struct _snd_rawmidi snd_rawmidi_t;
typedef struct _snd_rawmidi_info snd_rawmidi_info_t;

#define CTRL_PORT 6731
#define MAX_PKT_BYTES 256
#define MAX_INJ_PKTS 256
#define MAX_CAP_EVENTS 512
#define MAX_CLIENTS 16

typedef struct {
    int len;
    int offset;
    uint8_t data[MAX_PKT_BYTES];
} InjPkt;

typedef struct {
    uint64_t seq;
    uint64_t ts_ms;
    int dir; /* 0 = RX (hardware -> MPC), 1 = TX (MPC -> hardware LEDs) */
    int len;
    uint8_t data[64];
} CapEvent;

static void *g_alsa_handle = NULL;
static int (*real_rawmidi_open)(snd_rawmidi_t **, snd_rawmidi_t **, const char *, int) = NULL;
static int (*real_rawmidi_close)(snd_rawmidi_t *) = NULL;
static ssize_t (*real_rawmidi_read)(snd_rawmidi_t *, void *, size_t) = NULL;
static ssize_t (*real_rawmidi_write)(snd_rawmidi_t *, const void *, size_t) = NULL;
static int (*real_poll_count)(snd_rawmidi_t *) = NULL;
static int (*real_poll_descs)(snd_rawmidi_t *, struct pollfd *, unsigned int) = NULL;
static int (*real_poll_revents)(snd_rawmidi_t *, struct pollfd *, unsigned int, unsigned short *) = NULL;
static int (*real_nonblock)(snd_rawmidi_t *, int) = NULL;

static pthread_once_t g_resolve_once = PTHREAD_ONCE_INIT;
static pthread_once_t g_server_once = PTHREAD_ONCE_INIT;
static pthread_mutex_t g_inj_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t g_cap_lock = PTHREAD_MUTEX_INITIALIZER;

static snd_rawmidi_t *volatile g_priv_in = NULL;
static snd_rawmidi_t *volatile g_priv_out = NULL;
static char g_priv_name[128] = {0};

static int g_inj_pipe[2] = {-1, -1};
static InjPkt g_inj_q[MAX_INJ_PKTS];
static int g_inj_head = 0;
static int g_inj_tail = 0;
static int g_inj_count = 0;

static CapEvent g_cap_ring[MAX_CAP_EVENTS];
static uint64_t g_cap_seq = 0;
static volatile int g_sniff_leds = 0;

static uint64_t g_stat_reads = 0;
static uint64_t g_stat_polldesc = 0;
static uint64_t g_stat_rx_pkts = 0;
static uint64_t g_stat_tx_inj = 0;

static int g_is_mpc_proc = 0;

static uint64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)(ts.tv_nsec / 1000000ULL);
}

static void log_msg(const char *msg) {
    if (!g_is_mpc_proc) return;
    int fd = open("/tmp/mpc_hw_bridge.log", O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (fd >= 0) {
        char buf[512];
        int n = snprintf(buf, sizeof(buf), "[%llu] %s\n", (unsigned long long)now_ms(), msg);
        if (n > 0) {
            ssize_t unused = write(fd, buf, (size_t)n);
            (void)unused;
        }
        close(fd);
    }
}

static void *resolve_sym(const char *sym) {
    void *fn = dlsym(RTLD_NEXT, sym);
    if (!fn) {
        if (!g_alsa_handle) {
            g_alsa_handle = dlopen("libasound.so.2", RTLD_LAZY | RTLD_LOCAL);
        }
        if (g_alsa_handle) {
            fn = dlsym(g_alsa_handle, sym);
        }
    }
    return fn;
}

static void resolve_all(void) {
    char exe[256] = {0};
    ssize_t el = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (el > 0) {
        exe[el] = '\0';
        const char *base = strrchr(exe, '/');
        base = base ? base + 1 : exe;
        if (strcmp(base, "MPC") == 0) {
            g_is_mpc_proc = 1;
        }
    }

    real_rawmidi_open = resolve_sym("snd_rawmidi_open");
    real_rawmidi_close = resolve_sym("snd_rawmidi_close");
    real_rawmidi_read = resolve_sym("snd_rawmidi_read");
    real_rawmidi_write = resolve_sym("snd_rawmidi_write");
    real_poll_count = resolve_sym("snd_rawmidi_poll_descriptors_count");
    real_poll_descs = resolve_sym("snd_rawmidi_poll_descriptors");
    real_poll_revents = resolve_sym("snd_rawmidi_poll_descriptors_revents");
    real_nonblock = resolve_sym("snd_rawmidi_nonblock");

    if (g_is_mpc_proc && g_inj_pipe[0] < 0) {
        if (pipe2(g_inj_pipe, O_NONBLOCK | O_CLOEXEC) == 0) {
            log_msg("mpc_hw_bridge resolved ALSA symbols & pipe initialized inside /usr/bin/MPC");
        }
    }
}

static void record_cap(int dir, const uint8_t *data, int len) {
    if (len <= 0) return;
    pthread_mutex_lock(&g_cap_lock);
    uint64_t seq = ++g_cap_seq;
    CapEvent *ev = &g_cap_ring[(seq - 1) % MAX_CAP_EVENTS];
    ev->seq = seq;
    ev->ts_ms = now_ms();
    ev->dir = dir;
    ev->len = (len > 64) ? 64 : len;
    memcpy(ev->data, data, (size_t)ev->len);
    if (dir == 0) g_stat_rx_pkts++;
    pthread_mutex_unlock(&g_cap_lock);
}

static void drain_inj_pipe_locked(void) {
    if (g_inj_pipe[0] < 0) return;
    char dump[64];
    while (read(g_inj_pipe[0], dump, sizeof(dump)) > 0) {}
}

static int queue_inj_packet(const uint8_t *data, int len) {
    if (len <= 0 || len > MAX_PKT_BYTES) return -1;
    pthread_mutex_lock(&g_inj_lock);
    if (g_inj_count >= MAX_INJ_PKTS) {
        pthread_mutex_unlock(&g_inj_lock);
        return -2;
    }
    InjPkt *p = &g_inj_q[g_inj_tail];
    p->len = len;
    p->offset = 0;
    memcpy(p->data, data, (size_t)len);
    g_inj_tail = (g_inj_tail + 1) % MAX_INJ_PKTS;
    g_inj_count++;
    g_stat_tx_inj++;
    if (g_inj_pipe[1] >= 0) {
        char b = 1;
        ssize_t unused = write(g_inj_pipe[1], &b, 1);
        (void)unused;
    }
    pthread_mutex_unlock(&g_inj_lock);
    return len;
}

static int hex_val(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int parse_hex_bytes(const char *s, uint8_t *out, int max_len) {
    int n = 0;
    while (*s && n < max_len) {
        while (*s == ' ' || *s == '\t' || *s == ',' || *s == ':') s++;
        if (!*s) break;
        int hi = hex_val(s[0]);
        int lo = s[1] ? hex_val(s[1]) : -1;
        if (hi >= 0 && lo >= 0) {
            out[n++] = (uint8_t)((hi << 4) | lo);
            s += 2;
        } else if (hi >= 0 && (s[1] == ' ' || s[1] == '\t' || s[1] == ',' || s[1] == '\0')) {
            out[n++] = (uint8_t)hi;
            s += 1;
        } else {
            return -1;
        }
    }
    return n;
}

static void send_str(int fd, const char *s) {
    size_t len = strlen(s);
    size_t off = 0;
    while (off < len) {
        ssize_t w = send(fd, s + off, len - off, MSG_NOSIGNAL);
        if (w <= 0) break;
        off += (size_t)w;
    }
}

static void handle_cmd(int cfd, char *line) {
    size_t L = strlen(line);
    while (L > 0 && (line[L - 1] == '\n' || line[L - 1] == '\r' || line[L - 1] == ' ')) {
        line[--L] = '\0';
    }
    if (L == 0) return;

    char resp[512];
    if (strcmp(line, "PING") == 0 || strcmp(line, "STATUS") == 0) {
        snprintf(resp, sizeof(resp),
                 "OK mpc_hw_bridge 1.0 priv_in=%p priv_name=%s reads=%llu polldesc=%llu rx_pkts=%llu tx_inj=%llu sniff_leds=%d\n",
                 (void *)g_priv_in,
                 g_priv_name[0] ? g_priv_name : "none",
                 (unsigned long long)g_stat_reads,
                 (unsigned long long)g_stat_polldesc,
                 (unsigned long long)g_stat_rx_pkts,
                 (unsigned long long)g_stat_tx_inj,
                 g_sniff_leds);
        send_str(cfd, resp);
        return;
    }

    if (strncmp(line, "INJECT ", 7) == 0) {
        if (!g_priv_in) {
            send_str(cfd, "ERR no_priv_in\n");
            return;
        }
        uint8_t bytes[MAX_PKT_BYTES];
        int n = parse_hex_bytes(line + 7, bytes, MAX_PKT_BYTES);
        if (n <= 0) {
            send_str(cfd, "ERR bad_hex\n");
            return;
        }
        int rc = queue_inj_packet(bytes, n);
        if (rc > 0) {
            snprintf(resp, sizeof(resp), "OK %d\n", rc);
            send_str(cfd, resp);
        } else {
            send_str(cfd, "ERR queue_full\n");
        }
        return;
    }

    if (strncmp(line, "SNIFF_LEDS ", 11) == 0) {
        g_sniff_leds = atoi(line + 11) ? 1 : 0;
        snprintf(resp, sizeof(resp), "OK sniff_leds=%d\n", g_sniff_leds);
        send_str(cfd, resp);
        return;
    }

    if (strcmp(line, "CLEAR") == 0) {
        pthread_mutex_lock(&g_cap_lock);
        uint64_t cur = g_cap_seq;
        pthread_mutex_unlock(&g_cap_lock);
        snprintf(resp, sizeof(resp), "OK %llu\n", (unsigned long long)cur);
        send_str(cfd, resp);
        return;
    }

    if (strncmp(line, "EVENTS", 6) == 0) {
        uint64_t since = 0;
        if (line[6] == ' ') since = strtoull(line + 7, NULL, 10);

        CapEvent snap[MAX_CAP_EVENTS];
        int count = 0;
        uint64_t latest = 0;

        pthread_mutex_lock(&g_cap_lock);
        latest = g_cap_seq;
        uint64_t start = (latest > MAX_CAP_EVENTS) ? (latest - MAX_CAP_EVENTS + 1) : 1;
        if (since + 1 > start) start = since + 1;
        for (uint64_t s = start; s <= latest && count < MAX_CAP_EVENTS; s++) {
            snap[count++] = g_cap_ring[(s - 1) % MAX_CAP_EVENTS];
        }
        pthread_mutex_unlock(&g_cap_lock);

        for (int i = 0; i < count; i++) {
            char linebuf[320];
            int pos = snprintf(linebuf, sizeof(linebuf), "EV %llu %llu %s ",
                               (unsigned long long)snap[i].seq,
                               (unsigned long long)snap[i].ts_ms,
                               snap[i].dir == 0 ? "RX" : "TX");
            for (int j = 0; j < snap[i].len && pos < (int)sizeof(linebuf) - 4; j++) {
                pos += snprintf(linebuf + pos, sizeof(linebuf) - (size_t)pos, "%02X", snap[i].data[j]);
                if (j + 1 < snap[i].len) linebuf[pos++] = ' ';
            }
            linebuf[pos++] = '\n';
            linebuf[pos] = '\0';
            send_str(cfd, linebuf);
        }
        snprintf(resp, sizeof(resp), "END %llu\n", (unsigned long long)latest);
        send_str(cfd, resp);
        return;
    }

    send_str(cfd, "ERR unknown_cmd\n");
}

typedef struct {
    int fd;
    int len;
    char buf[1024];
} ClientState;

static void *server_thread(void *arg) {
    (void)arg;
    int lfd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (lfd < 0) {
        log_msg("ERROR socket failed");
        return NULL;
    }
    int opt = 1;
    setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(CTRL_PORT);

    if (bind(lfd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        char msg[128];
        snprintf(msg, sizeof(msg), "ERROR bind port %d failed: %s", CTRL_PORT, strerror(errno));
        log_msg(msg);
        close(lfd);
        return NULL;
    }
    if (listen(lfd, 8) < 0) {
        log_msg("ERROR listen failed");
        close(lfd);
        return NULL;
    }

    log_msg("mpc_hw_bridge listening on TCP port 6731");

    ClientState clients[MAX_CLIENTS];
    for (int i = 0; i < MAX_CLIENTS; i++) {
        clients[i].fd = -1;
        clients[i].len = 0;
    }

    struct pollfd pfds[1 + MAX_CLIENTS];
    for (;;) {
        int nfds = 0;
        int map[MAX_CLIENTS];
        pfds[nfds].fd = lfd;
        pfds[nfds].events = POLLIN;
        pfds[nfds].revents = 0;
        nfds++;

        for (int i = 0; i < MAX_CLIENTS; i++) {
            if (clients[i].fd >= 0) {
                map[nfds - 1] = i;
                pfds[nfds].fd = clients[i].fd;
                pfds[nfds].events = POLLIN;
                pfds[nfds].revents = 0;
                nfds++;
            }
        }

        int rc = poll(pfds, (nfds_t)nfds, -1);
        if (rc < 0) {
            if (errno == EINTR) continue;
            break;
        }

        if (pfds[0].revents & POLLIN) {
            for (;;) {
                int cfd = accept4(lfd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
                if (cfd < 0) break;
                int nd = 1;
                setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &nd, sizeof(nd));
                int placed = 0;
                for (int i = 0; i < MAX_CLIENTS; i++) {
                    if (clients[i].fd < 0) {
                        clients[i].fd = cfd;
                        clients[i].len = 0;
                        placed = 1;
                        break;
                    }
                }
                if (!placed) close(cfd);
            }
        }

        for (int p = 1; p < nfds; p++) {
            if (!(pfds[p].revents & (POLLIN | POLLERR | POLLHUP))) continue;
            ClientState *cl = &clients[map[p - 1]];
            ssize_t r = recv(cl->fd, cl->buf + cl->len, sizeof(cl->buf) - 1 - (size_t)cl->len, 0);
            if (r <= 0) {
                if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) continue;
                close(cl->fd);
                cl->fd = -1;
                cl->len = 0;
                continue;
            }
            cl->len += (int)r;
            cl->buf[cl->len] = '\0';

            char *start = cl->buf;
            char *nl;
            while ((nl = strchr(start, '\n')) != NULL) {
                *nl = '\0';
                handle_cmd(cl->fd, start);
                start = nl + 1;
            }
            int rem = (int)(cl->buf + cl->len - start);
            if (rem > 0 && start != cl->buf) {
                memmove(cl->buf, start, (size_t)rem);
            }
            cl->len = (rem < (int)sizeof(cl->buf) - 1) ? rem : 0;
        }
    }
    close(lfd);
    return NULL;
}

static void start_server_once(void) {
    if (!g_is_mpc_proc) return;
    pthread_t tid;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    pthread_create(&tid, &attr, server_thread, NULL);
    pthread_attr_destroy(&attr);
}

__attribute__((constructor))
static void bridge_ctor(void) {
    pthread_once(&g_resolve_once, resolve_all);
    if (g_is_mpc_proc) {
        pthread_once(&g_server_once, start_server_once);
    }
}

int snd_rawmidi_open(snd_rawmidi_t **inputp, snd_rawmidi_t **outputp, const char *name, int mode) {
    pthread_once(&g_resolve_once, resolve_all);
    if (g_is_mpc_proc) {
        pthread_once(&g_server_once, start_server_once);
    }
    if (!real_rawmidi_open) return -ENOSYS;

    int rc = real_rawmidi_open(inputp, outputp, name, mode);
    if (rc >= 0 && g_is_mpc_proc) {
        char msg[256];
        snprintf(msg, sizeof(msg), "snd_rawmidi_open(name=%s, mode=%d) -> rc=%d in=%p out=%p",
                 name ? name : "(null)", mode, rc,
                 inputp ? (void *)*inputp : NULL,
                 outputp ? (void *)*outputp : NULL);
        log_msg(msg);

        int is_priv = 0;
        if (name && (strstr(name, "hw:1,0,1") || strstr(name, ",1") || strstr(name, "Private"))) {
            is_priv = 1;
        }
        if (inputp && *inputp && (is_priv || !g_priv_in)) {
            g_priv_in = *inputp;
            snprintf(g_priv_name, sizeof(g_priv_name), "%s", name ? name : "unknown");
        }
        if (outputp && *outputp && (is_priv || !g_priv_out)) {
            g_priv_out = *outputp;
        }
    }
    return rc;
}

int snd_rawmidi_close(snd_rawmidi_t *rmidi) {
    pthread_once(&g_resolve_once, resolve_all);
    if (rmidi == g_priv_in) g_priv_in = NULL;
    if (rmidi == g_priv_out) g_priv_out = NULL;
    if (!real_rawmidi_close) return -ENOSYS;
    return real_rawmidi_close(rmidi);
}

int snd_rawmidi_nonblock(snd_rawmidi_t *rmidi, int nonblock) {
    pthread_once(&g_resolve_once, resolve_all);
    if (g_is_mpc_proc && rmidi == g_priv_in) {
        char msg[128];
        snprintf(msg, sizeof(msg), "snd_rawmidi_nonblock(priv_in, nonblock=%d)", nonblock);
        log_msg(msg);
    }
    if (!real_nonblock) return -ENOSYS;
    return real_nonblock(rmidi, nonblock);
}

int snd_rawmidi_poll_descriptors_count(snd_rawmidi_t *rmidi) {
    pthread_once(&g_resolve_once, resolve_all);
    if (!real_poll_count) return -ENOSYS;
    int c = real_poll_count(rmidi);
    if (g_is_mpc_proc && rmidi == g_priv_in) {
        if (g_stat_polldesc == 0) {
            char msg[128];
            snprintf(msg, sizeof(msg), "first snd_rawmidi_poll_descriptors_count -> real=%d", c);
            log_msg(msg);
        }
        g_stat_polldesc++;
        if (c >= 0 && g_inj_pipe[0] >= 0) {
            return c + 1;
        }
    }
    return c;
}

int snd_rawmidi_poll_descriptors(snd_rawmidi_t *rmidi, struct pollfd *pfds, unsigned int space) {
    pthread_once(&g_resolve_once, resolve_all);
    if (!real_poll_descs) return -ENOSYS;
    int n = real_poll_descs(rmidi, pfds, space);
    if (g_is_mpc_proc && rmidi == g_priv_in && n >= 0 && g_inj_pipe[0] >= 0) {
        if ((unsigned int)n < space) {
            pfds[n].fd = g_inj_pipe[0];
            pfds[n].events = POLLIN;
            pfds[n].revents = 0;
            return n + 1;
        }
    }
    return n;
}

int snd_rawmidi_poll_descriptors_revents(snd_rawmidi_t *rmidi, struct pollfd *pfds, unsigned int nfds, unsigned short *revents) {
    pthread_once(&g_resolve_once, resolve_all);
    if (!real_poll_revents) return -ENOSYS;

    if (g_is_mpc_proc && rmidi == g_priv_in && nfds > 1 && g_inj_pipe[0] >= 0 && pfds[nfds - 1].fd == g_inj_pipe[0]) {
        unsigned short pipe_rev = pfds[nfds - 1].revents;
        int rc = real_poll_revents(rmidi, pfds, nfds - 1, revents);
        if (rc == 0 && ((pipe_rev & POLLIN) || g_inj_count > 0)) {
            *revents |= POLLIN;
        }
        return rc;
    }

    int rc = real_poll_revents(rmidi, pfds, nfds, revents);
    if (rc == 0 && g_is_mpc_proc && rmidi == g_priv_in && g_inj_count > 0) {
        *revents |= POLLIN;
    }
    return rc;
}

ssize_t snd_rawmidi_read(snd_rawmidi_t *rmidi, void *buffer, size_t size) {
    pthread_once(&g_resolve_once, resolve_all);
    if (g_is_mpc_proc && rmidi == g_priv_in) {
        if (g_stat_reads == 0) {
            char msg[128];
            snprintf(msg, sizeof(msg), "first snd_rawmidi_read(priv_in, size=%zu)", size);
            log_msg(msg);
        }
        g_stat_reads++;

        if (size > 0 && buffer) {
            pthread_mutex_lock(&g_inj_lock);
            if (g_inj_count > 0) {
                InjPkt *p = &g_inj_q[g_inj_head];
                int avail = p->len - p->offset;
                int n = ((size_t)avail > size) ? (int)size : avail;
                memcpy(buffer, p->data + p->offset, (size_t)n);
                p->offset += n;
                if (p->offset >= p->len) {
                    g_inj_head = (g_inj_head + 1) % MAX_INJ_PKTS;
                    g_inj_count--;
                }
                if (g_inj_count == 0) {
                    drain_inj_pipe_locked();
                }
                pthread_mutex_unlock(&g_inj_lock);
                return (ssize_t)n;
            }
            pthread_mutex_unlock(&g_inj_lock);
        }
    }

    if (!real_rawmidi_read) return -ENOSYS;
    ssize_t ret = real_rawmidi_read(rmidi, buffer, size);
    if (ret > 0 && g_is_mpc_proc && rmidi == g_priv_in) {
        record_cap(0, (const uint8_t *)buffer, (int)ret);
    }
    return ret;
}

ssize_t snd_rawmidi_write(snd_rawmidi_t *rmidi, const void *buffer, size_t size) {
    pthread_once(&g_resolve_once, resolve_all);
    if (g_sniff_leds && size > 0 && g_is_mpc_proc && rmidi == g_priv_out) {
        record_cap(1, (const uint8_t *)buffer, (int)size);
    }
    if (!real_rawmidi_write) return -ENOSYS;
    return real_rawmidi_write(rmidi, buffer, size);
}

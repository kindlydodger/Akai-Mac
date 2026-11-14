/*
 * mpc_sample_indexer.c
 *
 * Ultra-fast, zero-external-build-dependency C99 audio sample analyzer and
 * multi-drive incremental watcher for Akai MPC Live (ARMv7l Linux & macOS).
 *
 * Capabilities:
 * - Recursively discovers mounted user drives under /media (deduplicated by st_dev,
 *   skipping OS overlay /media/acvs-content, hidden/AppleDouble ._* entries, and
 *   indexer output directories).
 * - Parses WAV (PCM 8/16/24/32, IEEE float 32/64, Extensible), AIFF/AIFC, and MP3
 *   (via runtime dlopen of libmpg123.so.0 / libmpg123.dylib) locally at disk speed.
 * - Flags remaining compressed extensions (.flac, .ogg, .m4a) as pending_host_decode
 *   so mpc_library.py resolves them incrementally on the Mac host.
 * - Skips BPM detection for one-shots and ultra-short hits while running FFT
 *   chroma + KS/Temperley Key & Camelot detection on pitched/longer samples and
 *   multi-band spectral-flux autocorrelation BPM detection on loops/phrases/tracks.
 * - Persists crash-safe incremental library.jsonl + stats.json + library.csv and
 *   mirrors cleanly into /media/okcomputer/MPC_Sample_Library/.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <ctype.h>
#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <libgen.h>
#include <math.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define DEFAULT_ROOTS "/media"
#define DEFAULT_OUT_DIR "/data/mpc-control/sample_library"
#define DEFAULT_MIRROR_DIR "/media/okcomputer/MPC_Sample_Library"
#define DEFAULT_ONESHOT_MAX_S 3.0
#define DEFAULT_JOBS 2
#define MAX_ANALYZE_SECONDS 18.0
#define ULTRA_SHORT_KEY_MIN_S 0.14
#define ULTRA_SHORT_CAT_S 0.22
#define SILENCE_PEAK_THRESH 1e-4f

static volatile sig_atomic_t g_stop = 0;

static void handle_signal(int sig) {
    (void)sig;
    g_stop = 1;
}

/* -------------------------------------------------------------------------- */
/* Key & Camelot tables (Temperley + Krumhansl-Schmuckler blended weights)    */
/* -------------------------------------------------------------------------- */

static const char *NOTE_NAMES[12] = {
    "C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"
};
static const char *CAMELOT_MAJOR[12] = {
    "8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B"
};
static const char *CAMELOT_MINOR[12] = {
    "5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A"
};

static const double PROF_MAJOR[12] = {
    5.675, 2.115, 3.490, 2.165, 4.440, 4.045, 2.260, 4.845, 2.195, 3.580, 1.895, 3.440
};
static const double PROF_MINOR[12] = {
    5.665, 2.340, 3.510, 4.940, 2.300, 3.765, 2.270, 4.625, 3.740, 2.345, 3.320, 3.585
};

/* -------------------------------------------------------------------------- */
/* Runtime libmpg123 bindings (loaded optionally via dlopen)                   */
/* -------------------------------------------------------------------------- */

typedef struct mpg123_handle_s mpg123_handle_t;
typedef int (*fn_mpg123_init)(void);
typedef void (*fn_mpg123_exit)(void);
typedef mpg123_handle_t *(*fn_mpg123_new)(const char *decoder, int *error);
typedef void (*fn_mpg123_delete)(mpg123_handle_t *mh);
typedef int (*fn_mpg123_open)(mpg123_handle_t *mh, const char *path);
typedef int (*fn_mpg123_close)(mpg123_handle_t *mh);
typedef int (*fn_mpg123_getformat)(mpg123_handle_t *mh, long *rate, int *channels, int *encoding);
typedef int (*fn_mpg123_format_none)(mpg123_handle_t *mh);
typedef int (*fn_mpg123_format)(mpg123_handle_t *mh, long rate, int channels, int encodings);
typedef int (*fn_mpg123_read)(mpg123_handle_t *mh, unsigned char *outmemory, size_t outmemsize, size_t *done);
typedef int64_t (*fn_mpg123_length64)(mpg123_handle_t *mh);

static struct {
    bool probed;
    bool ok;
    void *dl;
    fn_mpg123_init init;
    fn_mpg123_exit exit_fn;
    fn_mpg123_new create;
    fn_mpg123_delete destroy;
    fn_mpg123_open open;
    fn_mpg123_close close;
    fn_mpg123_getformat getformat;
    fn_mpg123_format_none format_none;
    fn_mpg123_format format;
    fn_mpg123_read read;
    fn_mpg123_length64 length64;
} g_mpg = {0};

static pthread_mutex_t g_mpg_mu = PTHREAD_MUTEX_INITIALIZER;

static bool ensure_mpg123(void) {
    pthread_mutex_lock(&g_mpg_mu);
    if (g_mpg.probed) {
        bool ok = g_mpg.ok;
        pthread_mutex_unlock(&g_mpg_mu);
        return ok;
    }
    g_mpg.probed = true;
    const char *candidates[] = {
        "/usr/lib/libmpg123.so.0",
        "libmpg123.so.0",
        "/opt/homebrew/lib/libmpg123.dylib",
        "/usr/local/lib/libmpg123.dylib",
        NULL
    };
    for (int i = 0; candidates[i]; i++) {
        g_mpg.dl = dlopen(candidates[i], RTLD_LAZY);
        if (g_mpg.dl) break;
    }
    if (!g_mpg.dl) {
        pthread_mutex_unlock(&g_mpg_mu);
        return false;
    }
    g_mpg.init = (fn_mpg123_init)dlsym(g_mpg.dl, "mpg123_init");
    g_mpg.exit_fn = (fn_mpg123_exit)dlsym(g_mpg.dl, "mpg123_exit");
    g_mpg.create = (fn_mpg123_new)dlsym(g_mpg.dl, "mpg123_new");
    g_mpg.destroy = (fn_mpg123_delete)dlsym(g_mpg.dl, "mpg123_delete");
    g_mpg.open = (fn_mpg123_open)dlsym(g_mpg.dl, "mpg123_open");
    g_mpg.close = (fn_mpg123_close)dlsym(g_mpg.dl, "mpg123_close");
    g_mpg.getformat = (fn_mpg123_getformat)dlsym(g_mpg.dl, "mpg123_getformat");
    g_mpg.format_none = (fn_mpg123_format_none)dlsym(g_mpg.dl, "mpg123_format_none");
    g_mpg.format = (fn_mpg123_format)dlsym(g_mpg.dl, "mpg123_format");
    g_mpg.read = (fn_mpg123_read)dlsym(g_mpg.dl, "mpg123_read");
    g_mpg.length64 = (fn_mpg123_length64)dlsym(g_mpg.dl, "mpg123_length64");
    if (!g_mpg.init || !g_mpg.create || !g_mpg.destroy || !g_mpg.open ||
        !g_mpg.close || !g_mpg.getformat || !g_mpg.format_none ||
        !g_mpg.format || !g_mpg.read) {
        pthread_mutex_unlock(&g_mpg_mu);
        return false;
    }
    if (g_mpg.init() != 0) {
        pthread_mutex_unlock(&g_mpg_mu);
        return false;
    }
    g_mpg.ok = true;
    pthread_mutex_unlock(&g_mpg_mu);
    return true;
}

/* -------------------------------------------------------------------------- */
/* Helpers: Endianness, String, JSON Escaping, Path Utils                     */
/* -------------------------------------------------------------------------- */

static inline uint16_t rd_le16(const uint8_t *p) {
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}
static inline uint32_t rd_le32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static inline uint64_t rd_le64(const uint8_t *p) {
    return (uint64_t)rd_le32(p) | ((uint64_t)rd_le32(p + 4) << 32);
}
static inline uint16_t rd_be16(const uint8_t *p) {
    return ((uint16_t)p[0] << 8) | (uint16_t)p[1];
}
static inline uint32_t rd_be32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static double rd_be_ieee80(const uint8_t *b) {
    int sign = (b[0] & 0x80) ? -1 : 1;
    int exp = ((b[0] & 0x7F) << 8) | b[1];
    uint32_t hi = rd_be32(b + 2);
    uint32_t lo = rd_be32(b + 6);
    if (exp == 0 && hi == 0 && lo == 0) return 0.0;
    if (exp == 0x7FFF) return 0.0;
    exp -= 16383;
    double f = ldexp((double)hi, exp - 31) + ldexp((double)lo, exp - 63);
    return sign * f;
}

static int mkdir_p(const char *path) {
    char tmp[1024];
    snprintf(tmp, sizeof(tmp), "%s", path);
    size_t len = strlen(tmp);
    if (len == 0) return -1;
    if (len > 1 && tmp[len - 1] == '/') tmp[len - 1] = '\0';
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            if (mkdir(tmp, 0755) != 0 && errno != EEXIST) return -1;
            *p = '/';
        }
    }
    if (mkdir(tmp, 0755) != 0 && errno != EEXIST) return -1;
    return 0;
}

static void json_write_str(FILE *f, const char *s) {
    fputc('"', f);
    for (const unsigned char *p = (const unsigned char *)s; p && *p; p++) {
        unsigned char c = *p;
        if (c == '"') fputs("\\\"", f);
        else if (c == '\\') fputs("\\\\", f);
        else if (c == '\b') fputs("\\b", f);
        else if (c == '\f') fputs("\\f", f);
        else if (c == '\n') fputs("\\n", f);
        else if (c == '\r') fputs("\\r", f);
        else if (c == '\t') fputs("\\t", f);
        else if (c < 0x20) fprintf(f, "\\u%04x", c);
        else fputc(c, f);
    }
    fputc('"', f);
}

static void csv_write_str(FILE *f, const char *s) {
    bool quote = false;
    for (const char *p = s ? s : ""; *p; p++) {
        if (*p == ',' || *p == '"' || *p == '\n' || *p == '\r') {
            quote = true;
            break;
        }
    }
    if (!quote) {
        fputs(s ? s : "", f);
        return;
    }
    fputc('"', f);
    for (const char *p = s ? s : ""; *p; p++) {
        if (*p == '"') fputs("\"\"", f);
        else fputc(*p, f);
    }
    fputc('"', f);
}

static void lowercase_copy(char *dst, size_t cap, const char *src) {
    size_t i = 0;
    for (; src[i] && i + 1 < cap; i++) {
        dst[i] = (char)tolower((unsigned char)src[i]);
    }
    dst[i] = '\0';
}

static bool token_match_ci(const char *hay_lower, const char *needle) {
    size_t nlen = strlen(needle);
    const char *p = hay_lower;
    while ((p = strstr(p, needle)) != NULL) {
        bool left_ok = (p == hay_lower) || !isalnum((unsigned char)p[-1]);
        bool right_ok = (p[nlen] == '\0') || !isalnum((unsigned char)p[nlen]);
        if (left_ok && right_ok) return true;
        p += nlen;
    }
    return false;
}

static void get_ext_lower(const char *path, char *ext, size_t cap) {
    const char *slash = strrchr(path, '/');
    const char *base = slash ? slash + 1 : path;
    const char *dot = strrchr(base, '.');
    if (!dot || dot == base) {
        ext[0] = '\0';
        return;
    }
    lowercase_copy(ext, cap, dot + 1);
}

static bool is_direct_audio_ext(const char *ext) {
    return strcmp(ext, "wav") == 0 || strcmp(ext, "aif") == 0 ||
           strcmp(ext, "aiff") == 0 || strcmp(ext, "aifc") == 0 ||
           strcmp(ext, "mp3") == 0;
}

static bool is_host_audio_ext(const char *ext) {
    return strcmp(ext, "flac") == 0 || strcmp(ext, "ogg") == 0 ||
           strcmp(ext, "m4a") == 0 || strcmp(ext, "opus") == 0;
}

/* -------------------------------------------------------------------------- */
/* Heuristic + Duration Category Classifier                                   */
/* -------------------------------------------------------------------------- */

static bool has_trailing_note_tag(const char *path) {
    const char *slash = strrchr(path, '/');
    const char *base = slash ? slash + 1 : path;
    const char *dot = strrchr(base, '.');
    size_t len = dot ? (size_t)(dot - base) : strlen(base);
    if (len < 3) return false;
    /* Match patterns like "-F#4", "_Gb6", "-C3", " A2" before extension */
    if (!isdigit((unsigned char)base[len - 1])) return false;
    size_t i = len - 1;
    if (i >= 1 && (base[i - 1] == '#' || tolower((unsigned char)base[i - 1]) == 'b') && i >= 2) {
        char n = (char)toupper((unsigned char)base[i - 2]);
        char sep = (i >= 3) ? base[i - 3] : ' ';
        if ((n >= 'A' && n <= 'G') && (sep == '-' || sep == '_' || sep == ' ')) return true;
    } else if (i >= 1) {
        char n = (char)toupper((unsigned char)base[i - 1]);
        char sep = (i >= 2) ? base[i - 2] : ' ';
        if ((n >= 'A' && n <= 'G') && (sep == '-' || sep == '_' || sep == ' ')) return true;
    }
    return false;
}

static bool path_has_oneshot_hint(const char *path) {
    char low[2048];
    lowercase_copy(low, sizeof(low), path);

    const char *loop_words[] = {
        "loop", "loops", "break", "breaks", "groove", "bpm", "beat",
        "toploop", "full", "track", "song", "stem", "stems", "acapella",
        "instrumentals", "instrumental", "pattern", "melody", "chords", NULL
    };
    for (int i = 0; loop_words[i]; i++) {
        if (token_match_ci(low, loop_words[i])) return false;
    }
    if (has_trailing_note_tag(path)) return true;

    const char *hit_words[] = {
        "oneshot", "one-shot", "one_shot", "hits", "hit", "kick", "kicks",
        "snare", "snares", "clap", "claps", "rim", "rimshot", "hihat",
        "hihats", "hats", "hat", "cymbal", "cymbals", "crash", "ride",
        "tom", "toms", "perc", "percs", "percussion", "shaker", "shakers",
        "tamb", "cowbell", "riser", "impact", "downer", "sfx", "shot", NULL
    };
    for (int i = 0; hit_words[i]; i++) {
        if (token_match_ci(low, hit_words[i])) return true;
    }
    return false;
}

static const char *classify_sample(
    double duration_s,
    const char *path,
    double oneshot_max_s,
    bool *out_skip_bpm
) {
    if (duration_s < ULTRA_SHORT_CAT_S) {
        *out_skip_bpm = true;
        return "ultra_short";
    }
    if (duration_s < oneshot_max_s) {
        *out_skip_bpm = true;
        return "one_shot";
    }
    if (duration_s < 8.5 && path_has_oneshot_hint(path)) {
        *out_skip_bpm = true;
        return "one_shot";
    }
    *out_skip_bpm = false;
    if (duration_s < 20.0) return "loop";
    if (duration_s < 60.0) return "phrase";
    return "track";
}

/* -------------------------------------------------------------------------- */
/* Decoded Decimated Mono Buffer                                              */
/* -------------------------------------------------------------------------- */

typedef struct {
    float *samples;
    size_t n_samples;
    double decimated_sr;
    uint32_t orig_sr;
    uint16_t orig_ch;
    uint16_t orig_bits;
    double duration_s;
    float peak;
    bool decode_ok;
} DecodedAudio;

static void free_decoded_audio(DecodedAudio *da) {
    free(da->samples);
    da->samples = NULL;
    da->n_samples = 0;
}

static inline float pcm_sample_to_float(const uint8_t *p, uint16_t fmt, uint16_t bits, bool big_endian) {
    if (fmt == 3 && bits == 32) {
        uint32_t u = big_endian ? rd_be32(p) : rd_le32(p);
        float f;
        memcpy(&f, &u, sizeof(f));
        return isfinite(f) ? f : 0.0f;
    }
    if (fmt == 3 && bits == 64) {
        uint64_t u = big_endian
            ? (((uint64_t)rd_be32(p) << 32) | rd_be32(p + 4))
            : rd_le64(p);
        double d;
        memcpy(&d, &u, sizeof(d));
        return isfinite(d) ? (float)d : 0.0f;
    }
    if (bits == 8) {
        return big_endian ? ((int8_t)p[0]) / 128.0f : ((int)p[0] - 128) / 128.0f;
    }
    if (bits == 16) {
        int16_t s = (int16_t)(big_endian ? rd_be16(p) : rd_le16(p));
        return s / 32768.0f;
    }
    if (bits == 24) {
        int32_t s = big_endian
            ? (((int32_t)(int8_t)p[0] << 16) | ((int32_t)p[1] << 8) | p[2])
            : (((int32_t)(int8_t)p[2] << 16) | ((int32_t)p[1] << 8) | p[0]);
        return s / 8388608.0f;
    }
    if (bits == 32) {
        int32_t s = (int32_t)(big_endian ? rd_be32(p) : rd_le32(p));
        return (float)(s / 2147483648.0);
    }
    return 0.0f;
}

typedef struct {
    float *buf;
    size_t len;
    size_t cap;
    size_t max_len;
    int decim;
    int acc_count;
    double acc_sum;
    float peak;
} DecimCollector;

static void collector_init(DecimCollector *c, uint32_t sr, double max_seconds) {
    memset(c, 0, sizeof(*c));
    int decim = (int)(sr / 11025U);
    if (decim < 1) decim = 1;
    if (sr > 20000 && decim < 2) decim = 2;
    c->decim = decim;
    double out_sr = (double)sr / decim;
    c->max_len = (size_t)ceil(max_seconds * out_sr) + 64;
    c->cap = c->max_len < 65536 ? c->max_len : 65536;
    if (c->cap < 4096) c->cap = 4096;
    c->buf = (float *)malloc(c->cap * sizeof(float));
}

static inline bool collector_push_mono(DecimCollector *c, float v) {
    if (!c->buf || c->len >= c->max_len) return false;
    float av = fabsf(v);
    if (av > c->peak) c->peak = av;
    c->acc_sum += v;
    c->acc_count++;
    if (c->acc_count >= c->decim) {
        if (c->len >= c->cap) {
            size_t new_cap = c->cap * 2;
            if (new_cap > c->max_len) new_cap = c->max_len;
            float *nb = (float *)realloc(c->buf, new_cap * sizeof(float));
            if (!nb) return false;
            c->buf = nb;
            c->cap = new_cap;
        }
        c->buf[c->len++] = (float)(c->acc_sum / c->decim);
        c->acc_sum = 0.0;
        c->acc_count = 0;
    }
    return c->len < c->max_len;
}

static void collector_flush(DecimCollector *c) {
    if (c->buf && c->acc_count > 0 && c->len < c->max_len) {
        if (c->len >= c->cap) {
            float *nb = (float *)realloc(c->buf, (c->cap + 1) * sizeof(float));
            if (nb) {
                c->buf = nb;
                c->cap++;
            }
        }
        if (c->len < c->cap) {
            c->buf[c->len++] = (float)(c->acc_sum / c->acc_count);
        }
        c->acc_count = 0;
        c->acc_sum = 0.0;
    }
}

static bool decode_wav(const char *path, DecodedAudio *out) {
    FILE *f = fopen(path, "rb");
    if (!f) return false;

    uint8_t hdr[12];
    if (fread(hdr, 1, 12, f) != 12) {
        fclose(f);
        return false;
    }
    bool is_riff = (memcmp(hdr, "RIFF", 4) == 0 || memcmp(hdr, "RF64", 4) == 0);
    if (!is_riff || memcmp(hdr + 8, "WAVE", 4) != 0) {
        fclose(f);
        return false;
    }

    bool got_fmt = false, got_data = false;
    uint16_t audio_fmt = 0, ch = 0, block_align = 0, bits = 0;
    uint32_t sr = 0;
    long long data_off = 0;
    uint64_t data_bytes = 0;
    uint64_t ds64_data_size = 0;

    uint8_t chdr[8];
    while (fread(chdr, 1, 8, f) == 8) {
        uint32_t csize = rd_le32(chdr + 4);
        if (memcmp(chdr, "ds64", 4) == 0 && csize >= 24) {
            uint8_t ds[24];
            if (fread(ds, 1, 24, f) == 24) {
                ds64_data_size = rd_le64(ds + 8);
                if (csize > 24) fseeko(f, (off_t)(csize - 24), SEEK_CUR);
            } else break;
        } else if (memcmp(chdr, "fmt ", 4) == 0 && csize >= 16) {
            uint8_t fb[40];
            size_t to_rd = csize < sizeof(fb) ? csize : sizeof(fb);
            if (fread(fb, 1, to_rd, f) != to_rd) break;
            if (csize > to_rd) fseeko(f, (off_t)(csize - to_rd), SEEK_CUR);
            audio_fmt = rd_le16(fb);
            ch = rd_le16(fb + 2);
            sr = rd_le32(fb + 4);
            block_align = rd_le16(fb + 12);
            bits = rd_le16(fb + 14);
            if (audio_fmt == 0xFFFE && to_rd >= 26) {
                audio_fmt = rd_le16(fb + 24);
            }
            got_fmt = true;
        } else if (memcmp(chdr, "data", 4) == 0) {
            data_off = ftello(f);
            data_bytes = (csize == 0xFFFFFFFFU && ds64_data_size > 0) ? ds64_data_size : csize;
            got_data = true;
            break;
        } else {
            fseeko(f, (off_t)csize, SEEK_CUR);
        }
        if (csize & 1) fseeko(f, 1, SEEK_CUR);
    }

    if (!got_fmt || !got_data || sr == 0 || ch == 0 || bits == 0 ||
        (audio_fmt != 1 && audio_fmt != 3)) {
        fclose(f);
        return false;
    }

    uint16_t bps = (uint16_t)(bits / 8);
    if (bps == 0) {
        fclose(f);
        return false;
    }
    if (block_align < ch * bps) block_align = (uint16_t)(ch * bps);
    uint64_t total_frames = data_bytes / block_align;
    out->orig_sr = sr;
    out->orig_ch = ch;
    out->orig_bits = bits;
    out->duration_s = (double)total_frames / (double)sr;

    if (out->duration_s < ULTRA_SHORT_KEY_MIN_S) {
        fclose(f);
        out->decode_ok = true;
        return true;
    }

    fseeko(f, (off_t)data_off, SEEK_SET);
    /* For long tracks (>50s), jump ~20% past intro to capture main musical groove */
    if (out->duration_s > 50.0) {
        uint64_t skip_frames = total_frames / 5;
        if (skip_frames < total_frames && (total_frames - skip_frames) >= (uint64_t)(sr * 24.0)) {
            fseeko(f, (off_t)(skip_frames * block_align), SEEK_CUR);
            total_frames -= skip_frames;
        }
    }

    DecimCollector col;
    collector_init(&col, sr, MAX_ANALYZE_SECONDS);
    if (!col.buf) {
        fclose(f);
        return false;
    }

    size_t max_frames_read = (size_t)ceil(MAX_ANALYZE_SECONDS * (double)sr);
    if (total_frames < max_frames_read) max_frames_read = (size_t)total_frames;

    const size_t chunk_frames = 2048;
    size_t io_bytes = chunk_frames * block_align;
    uint8_t *io = (uint8_t *)malloc(io_bytes);
    if (!io) {
        free(col.buf);
        fclose(f);
        return false;
    }

    size_t done_frames = 0;
    while (done_frames < max_frames_read) {
        size_t want = max_frames_read - done_frames;
        if (want > chunk_frames) want = chunk_frames;
        size_t got = fread(io, block_align, want, f);
        if (got == 0) break;
        bool keep = true;
        for (size_t i = 0; i < got; i++) {
            const uint8_t *fp = io + i * block_align;
            float mono = 0.0f;
            for (uint16_t k = 0; k < ch; k++) {
                mono += pcm_sample_to_float(fp + k * bps, audio_fmt, bits, false);
            }
            mono /= (float)ch;
            if (!collector_push_mono(&col, mono)) {
                keep = false;
                break;
            }
        }
        done_frames += got;
        if (!keep) break;
    }
    free(io);
    fclose(f);

    collector_flush(&col);
    out->samples = col.buf;
    out->n_samples = col.len;
    out->decimated_sr = (double)sr / col.decim;
    out->peak = col.peak;
    out->decode_ok = true;
    return true;
}

static bool decode_aiff(const char *path, DecodedAudio *out) {
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    uint8_t hdr[12];
    if (fread(hdr, 1, 12, f) != 12) {
        fclose(f);
        return false;
    }
    if (memcmp(hdr, "FORM", 4) != 0 ||
        (memcmp(hdr + 8, "AIFF", 4) != 0 && memcmp(hdr + 8, "AIFC", 4) != 0)) {
        fclose(f);
        return false;
    }
    bool is_aifc = (memcmp(hdr + 8, "AIFC", 4) == 0);
    bool got_comm = false, got_ssnd = false;
    bool little_endian = false;
    uint16_t audio_fmt = 1, ch = 0, bits = 0;
    uint32_t total_frames = 0, sr = 0;
    long long data_off = 0;

    uint8_t chdr[8];
    while (fread(chdr, 1, 8, f) == 8) {
        uint32_t csize = rd_be32(chdr + 4);
        if (memcmp(chdr, "COMM", 4) == 0 && csize >= 18) {
            uint8_t cb[32];
            size_t to_rd = csize < sizeof(cb) ? csize : sizeof(cb);
            if (fread(cb, 1, to_rd, f) != to_rd) break;
            if (csize > to_rd) fseeko(f, (off_t)(csize - to_rd), SEEK_CUR);
            ch = rd_be16(cb);
            total_frames = rd_be32(cb + 2);
            bits = rd_be16(cb + 6);
            sr = (uint32_t)llround(rd_be_ieee80(cb + 8));
            if (is_aifc && to_rd >= 22) {
                if (memcmp(cb + 18, "sowt", 4) == 0) {
                    little_endian = true;
                } else if (memcmp(cb + 18, "fl32", 4) == 0 || memcmp(cb + 18, "FL32", 4) == 0) {
                    audio_fmt = 3;
                } else if (memcmp(cb + 18, "NONE", 4) != 0) {
                    fclose(f);
                    return false;
                }
            }
            got_comm = true;
        } else if (memcmp(chdr, "SSND", 4) == 0 && csize >= 8) {
            uint8_t sb[8];
            if (fread(sb, 1, 8, f) != 8) break;
            uint32_t off = rd_be32(sb);
            if (off > 0) fseeko(f, (off_t)off, SEEK_CUR);
            data_off = ftello(f);
            got_ssnd = true;
            break;
        } else {
            fseeko(f, (off_t)csize, SEEK_CUR);
        }
        if (csize & 1) fseeko(f, 1, SEEK_CUR);
    }

    if (!got_comm || !got_ssnd || sr == 0 || ch == 0 || bits == 0) {
        fclose(f);
        return false;
    }

    uint16_t bps = (uint16_t)((bits + 7) / 8);
    uint16_t frame_bytes = (uint16_t)(ch * bps);
    out->orig_sr = sr;
    out->orig_ch = ch;
    out->orig_bits = bits;
    out->duration_s = (double)total_frames / (double)sr;

    if (out->duration_s < ULTRA_SHORT_KEY_MIN_S) {
        fclose(f);
        out->decode_ok = true;
        return true;
    }

    fseeko(f, (off_t)data_off, SEEK_SET);
    DecimCollector col;
    collector_init(&col, sr, MAX_ANALYZE_SECONDS);
    if (!col.buf) {
        fclose(f);
        return false;
    }

    size_t max_frames_read = (size_t)ceil(MAX_ANALYZE_SECONDS * (double)sr);
    if (total_frames < max_frames_read) max_frames_read = total_frames;

    const size_t chunk_frames = 2048;
    uint8_t *io = (uint8_t *)malloc(chunk_frames * frame_bytes);
    if (!io) {
        free(col.buf);
        fclose(f);
        return false;
    }

    size_t done_frames = 0;
    while (done_frames < max_frames_read) {
        size_t want = max_frames_read - done_frames;
        if (want > chunk_frames) want = chunk_frames;
        size_t got = fread(io, frame_bytes, want, f);
        if (got == 0) break;
        bool keep = true;
        for (size_t i = 0; i < got; i++) {
            const uint8_t *fp = io + i * frame_bytes;
            float mono = 0.0f;
            for (uint16_t k = 0; k < ch; k++) {
                mono += pcm_sample_to_float(fp + k * bps, audio_fmt, bits, !little_endian);
            }
            mono /= (float)ch;
            if (!collector_push_mono(&col, mono)) {
                keep = false;
                break;
            }
        }
        done_frames += got;
        if (!keep) break;
    }
    free(io);
    fclose(f);

    collector_flush(&col);
    out->samples = col.buf;
    out->n_samples = col.len;
    out->decimated_sr = (double)sr / col.decim;
    out->peak = col.peak;
    out->decode_ok = true;
    return true;
}

static bool decode_mp3(const char *path, DecodedAudio *out) {
    if (!ensure_mpg123()) return false;
    int err = 0;
    pthread_mutex_lock(&g_mpg_mu);
    mpg123_handle_t *mh = g_mpg.create(NULL, &err);
    pthread_mutex_unlock(&g_mpg_mu);
    if (!mh) return false;
    if (g_mpg.open(mh, path) != 0) {
        pthread_mutex_lock(&g_mpg_mu);
        g_mpg.destroy(mh);
        pthread_mutex_unlock(&g_mpg_mu);
        return false;
    }

    long rate = 0;
    int ch = 0, enc = 0;
    if (g_mpg.getformat(mh, &rate, &ch, &enc) != 0 || rate <= 0 || ch <= 0) {
        g_mpg.close(mh);
        pthread_mutex_lock(&g_mpg_mu);
        g_mpg.destroy(mh);
        pthread_mutex_unlock(&g_mpg_mu);
        return false;
    }
    if (ch > 2) ch = 2;
    g_mpg.format_none(mh);
    g_mpg.format(mh, rate, ch, 0xD0 /* MPG123_ENC_SIGNED_16 */);

    int64_t total_frames = g_mpg.length64 ? g_mpg.length64(mh) : -1;
    out->orig_sr = (uint32_t)rate;
    out->orig_ch = (uint16_t)ch;
    out->orig_bits = 16;
    out->duration_s = (total_frames > 0) ? ((double)total_frames / (double)rate) : 0.0;

    DecimCollector col;
    collector_init(&col, (uint32_t)rate, MAX_ANALYZE_SECONDS);
    if (!col.buf) {
        g_mpg.close(mh);
        pthread_mutex_lock(&g_mpg_mu);
        g_mpg.destroy(mh);
        pthread_mutex_unlock(&g_mpg_mu);
        return false;
    }

    int16_t pcm[4096 * 2];
    size_t decoded_frames = 0;
    size_t max_frames_read = (size_t)ceil(MAX_ANALYZE_SECONDS * (double)rate);
    bool eof_reached = false;
    while (decoded_frames < max_frames_read) {
        size_t done = 0;
        int rc = g_mpg.read(mh, (unsigned char *)pcm, sizeof(pcm), &done);
        if (rc == -11 /* MPG123_NEW_FORMAT */) {
            g_mpg.getformat(mh, &rate, &ch, &enc);
            if (ch > 2) ch = 2;
            continue;
        }
        size_t n_frames = done / (size_t)(ch * 2);
        for (size_t i = 0; i < n_frames; i++) {
            float mono = 0.0f;
            for (int k = 0; k < ch; k++) {
                mono += pcm[i * ch + k] / 32768.0f;
            }
            mono /= (float)ch;
            if (!collector_push_mono(&col, mono)) {
                decoded_frames = max_frames_read;
                break;
            }
        }
        decoded_frames += n_frames;
        if (rc == -12 /* MPG123_DONE */) {
            eof_reached = true;
            break;
        }
        if (rc != 0 && done == 0) break;
    }
    g_mpg.close(mh);
    pthread_mutex_lock(&g_mpg_mu);
    g_mpg.destroy(mh);
    pthread_mutex_unlock(&g_mpg_mu);

    if (out->duration_s <= 0.0 || eof_reached) {
        out->duration_s = (double)decoded_frames / (double)rate;
    }
    collector_flush(&col);
    out->samples = col.buf;
    out->n_samples = col.len;
    out->decimated_sr = (double)rate / col.decim;
    out->peak = col.peak;
    out->decode_ok = true;
    return true;
}

/* -------------------------------------------------------------------------- */
/* Fast In-Place Radix-2 FFT + Chromagram Key Detection                       */
/* -------------------------------------------------------------------------- */

static void fft_radix2(float *re, float *im, int n) {
    int j = 0;
    for (int i = 1; i < n; i++) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            float tr = re[i]; re[i] = re[j]; re[j] = tr;
            float ti = im[i]; im[i] = im[j]; im[j] = ti;
        }
    }
    for (int len = 2; len <= n; len <<= 1) {
        double ang = -2.0 * M_PI / len;
        float wlen_r = (float)cos(ang);
        float wlen_i = (float)sin(ang);
        int half = len >> 1;
        for (int i = 0; i < n; i += len) {
            float wr = 1.0f, wi = 0.0f;
            for (int k = 0; k < half; k++) {
                int idx1 = i + k;
                int idx2 = idx1 + half;
                float ur = re[idx1], ui = im[idx1];
                float vr = re[idx2] * wr - im[idx2] * wi;
                float vi = re[idx2] * wi + im[idx2] * wr;
                re[idx1] = ur + vr;
                im[idx1] = ui + vi;
                re[idx2] = ur - vr;
                im[idx2] = ui - vi;
                float nwr = wr * wlen_r - wi * wlen_i;
                wi = wr * wlen_i + wi * wlen_r;
                wr = nwr;
            }
        }
    }
}

typedef struct {
    bool valid;
    bool atonal;
    bool silence;
    char key[24];
    char short_key[12];
    char camelot[8];
    double confidence;
} KeyResult;

static KeyResult detect_key_from_buffer(const float *sig, size_t n, double sr) {
    KeyResult res;
    memset(&res, 0, sizeof(res));
    if (!sig || n < 512 || sr <= 1000.0) return res;

    /* Find max energy to skip dead silence and detect empty files */
    double total_sq = 0.0;
    float peak = 0.0f;
    for (size_t i = 0; i < n; i++) {
        float a = fabsf(sig[i]);
        if (a > peak) peak = a;
        total_sq += (double)sig[i] * sig[i];
    }
    if (peak < SILENCE_PEAK_THRESH || total_sq / n < 1e-9) {
        res.valid = true;
        res.silence = true;
        res.atonal = true;
        strcpy(res.key, "Silence");
        strcpy(res.short_key, "--");
        strcpy(res.camelot, "--");
        res.confidence = 0.0;
        return res;
    }

    const int N = 4096;
    const int hop = 2048;
    float *re = (float *)calloc((size_t)N, sizeof(float));
    float *im = (float *)calloc((size_t)N, sizeof(float));
    float *hann = (float *)malloc((size_t)N * sizeof(float));
    int *k_to_bin = (int *)malloc((size_t)(N / 2) * sizeof(int));
    if (!re || !im || !hann || !k_to_bin) {
        free(re); free(im); free(hann); free(k_to_bin);
        return res;
    }
    for (int i = 0; i < N; i++) {
        hann[i] = 0.5f * (1.0f - cosf((float)(2.0 * M_PI * i / (N - 1))));
    }
    int k_min = (int)ceil(55.0 * N / sr);
    int k_max = (int)floor(2000.0 * N / sr);
    if (k_min < 1) k_min = 1;
    if (k_max >= N / 2) k_max = N / 2 - 1;
    for (int k = 0; k < N / 2; k++) {
        if (k < k_min || k > k_max) {
            k_to_bin[k] = -1;
        } else {
            double freq = (double)k * sr / N;
            double midi = 69.0 + 12.0 * log2(freq / 440.0);
            int bin36 = (int)llround(midi * 3.0) % 36;
            if (bin36 < 0) bin36 += 36;
            k_to_bin[k] = bin36;
        }
    }

    /* 36 bins per octave (3 sub-bins per semitone for +-33 cent tuning lock) */
    double fine_chroma[36];
    memset(fine_chroma, 0, sizeof(fine_chroma));
    int frames_used = 0;

    if (n <= (size_t)N) {
        /* Short one-shot: window active region and zero-pad into N=4096 */
        for (size_t i = 0; i < n; i++) {
            float w = 0.5f * (1.0f - cosf((float)(2.0 * M_PI * i / (n > 1 ? n - 1 : 1))));
            re[i] = sig[i] * w;
        }
        fft_radix2(re, im, N);
        for (int k = k_min; k <= k_max; k++) {
            int b = k_to_bin[k];
            if (b < 0) continue;
            float mag = hypotf(re[k], im[k]);
            if (mag <= 1e-6f) continue;
            fine_chroma[b] += log1pf(8.0f * mag);
        }
        frames_used = 1;
    } else {
        size_t total_possible = (n - (size_t)N) / (size_t)hop + 1;
        size_t step_frames = total_possible > 36 ? total_possible / 36 : 1;

        for (size_t fidx = 0; fidx < total_possible; fidx += step_frames) {
            size_t off = fidx * (size_t)hop;
            double f_rms = 0.0;
            for (int i = 0; i < N; i++) {
                float s = sig[off + (size_t)i];
                f_rms += (double)s * s;
                re[i] = s * hann[i];
                im[i] = 0.0f;
            }
            if (f_rms / N < (total_sq / n) * 0.08) continue; /* skip quiet pauses */

            fft_radix2(re, im, N);
            double frame_fine[36] = {0};
            double fsum = 0.0;
            for (int k = k_min; k <= k_max; k++) {
                int b = k_to_bin[k];
                if (b < 0) continue;
                float mag = hypotf(re[k], im[k]);
                if (mag <= 1e-6f) continue;
                double w = log1pf(6.0f * mag);
                frame_fine[b] += w;
                fsum += w * w;
            }
            if (fsum > 1e-9) {
                double norm = sqrt(fsum);
                for (int b = 0; b < 36; b++) fine_chroma[b] += frame_fine[b] / norm;
                frames_used++;
            }
        }
    }

    free(k_to_bin);
    free(re);
    free(im);
    free(hann);

    if (frames_used == 0) {
        res.valid = true;
        res.atonal = true;
        return res;
    }

    /* Choose tuning center among 3 sub-bins per semitone (mod 3) */
    double phase_energy[3] = {0.0, 0.0, 0.0};
    for (int b = 0; b < 36; b++) phase_energy[b % 3] += fine_chroma[b];
    int best_phase = 0;
    if (phase_energy[1] > phase_energy[best_phase]) best_phase = 1;
    if (phase_energy[2] > phase_energy[best_phase]) best_phase = 2;

    double chroma[12];
    double c_sum = 0.0, c_max = 0.0, c_min = 1e30;
    for (int pc = 0; pc < 12; pc++) {
        int center = (pc * 3 + best_phase) % 36;
        int left = (center + 35) % 36;
        int right = (center + 1) % 36;
        double v = 0.65 * fine_chroma[center] + 0.175 * fine_chroma[left] + 0.175 * fine_chroma[right];
        chroma[pc] = v;
        c_sum += v;
        if (v > c_max) c_max = v;
        if (v < c_min) c_min = v;
    }
    double c_mean = c_sum / 12.0;
    double contrast = (c_mean > 1e-9) ? ((c_max - c_min) / c_mean) : 0.0;

    /* Pearson correlation against 24 Major / Minor profiles */
    double prof_maj_mean = 0.0, prof_min_mean = 0.0;
    for (int i = 0; i < 12; i++) {
        prof_maj_mean += PROF_MAJOR[i];
        prof_min_mean += PROF_MINOR[i];
    }
    prof_maj_mean /= 12.0;
    prof_min_mean /= 12.0;

    double best_r = -2.0, second_r = -2.0;
    int best_pc = 0;
    bool best_minor = false;

    for (int root = 0; root < 12; root++) {
        for (int is_min = 0; is_min <= 1; is_min++) {
            const double *prof = is_min ? PROF_MINOR : PROF_MAJOR;
            double pmean = is_min ? prof_min_mean : prof_maj_mean;
            double num = 0.0, dx2 = 0.0, dy2 = 0.0;
            for (int k = 0; k < 12; k++) {
                double dx = chroma[(root + k) % 12] - c_mean;
                double dy = prof[k] - pmean;
                num += dx * dy;
                dx2 += dx * dx;
                dy2 += dy * dy;
            }
            double denom = sqrt(dx2 * dy2);
            double r = (denom > 1e-12) ? (num / denom) : 0.0;
            if (r > best_r) {
                second_r = best_r;
                best_r = r;
                best_pc = root;
                best_minor = (is_min == 1);
            } else if (r > second_r) {
                second_r = r;
            }
        }
    }

    res.valid = true;
    /* Noise snares / hihats produce very flat chroma and weak template match */
    if (best_r < 0.34 && contrast < 0.60) {
        res.atonal = true;
        res.confidence = fmax(0.0, fmin(0.4, (best_r + 1.0) * 0.2));
        return res;
    }

    snprintf(res.key, sizeof(res.key), "%s %s", NOTE_NAMES[best_pc], best_minor ? "minor" : "major");
    snprintf(res.short_key, sizeof(res.short_key), "%s%s", NOTE_NAMES[best_pc], best_minor ? "m" : "");
    snprintf(res.camelot, sizeof(res.camelot), "%s",
             best_minor ? CAMELOT_MINOR[best_pc] : CAMELOT_MAJOR[best_pc]);

    double margin = fmax(0.0, best_r - second_r);
    double conf = 0.45 + 0.38 * fmax(0.0, best_r) + 0.65 * margin;
    if (conf > 0.99) conf = 0.99;
    if (conf < 0.15) conf = 0.15;
    res.confidence = conf;
    return res;
}

/* -------------------------------------------------------------------------- */
/* Multi-Band Spectral Flux + Beat-Comb Tempogram BPM Detection               */
/* -------------------------------------------------------------------------- */

typedef struct {
    bool valid;
    double bpm;
    double confidence;
} BpmResult;

static BpmResult detect_bpm_from_buffer(const float *sig, size_t n, double sr) {
    BpmResult res = {false, 0.0, 0.0};
    const int N = 512;
    const int hop = 128;
    if (!sig || n < (size_t)(N * 8) || sr <= 1000.0) return res;

    size_t n_frames = (n - (size_t)N) / (size_t)hop + 1;
    if (n_frames > 2000) n_frames = 2000;
    if (n_frames < 48) return res;

    float *re = (float *)malloc((size_t)N * sizeof(float));
    float *im = (float *)malloc((size_t)N * sizeof(float));
    float *hann = (float *)malloc((size_t)N * sizeof(float));
    float prev_band[3] = {0.0f, 0.0f, 0.0f};
    float *flux = (float *)calloc(n_frames, sizeof(float));
    if (!re || !im || !hann || !flux) {
        free(re); free(im); free(hann); free(flux);
        return res;
    }

    for (int i = 0; i < N; i++) {
        hann[i] = 0.5f * (1.0f - cosf((float)(2.0 * M_PI * i / (N - 1))));
    }

    int k_low_end = (int)floor(250.0 * N / sr);
    int k_mid_end = (int)floor(2200.0 * N / sr);
    if (k_low_end < 2) k_low_end = 2;
    if (k_mid_end >= N / 2) k_mid_end = N / 2 - 2;

    for (size_t t = 0; t < n_frames; t++) {
        size_t off = t * (size_t)hop;
        for (int i = 0; i < N; i++) {
            re[i] = sig[off + (size_t)i] * hann[i];
            im[i] = 0.0f;
        }
        fft_radix2(re, im, N);
        float b[3] = {0.0f, 0.0f, 0.0f};
        for (int k = 1; k < N / 2; k++) {
            float mag = hypotf(re[k], im[k]);
            float lm = log1pf(10.0f * mag);
            if (k <= k_low_end) b[0] += lm;
            else if (k <= k_mid_end) b[1] += lm;
            else b[2] += lm;
        }
        float df = 0.0f;
        if (t > 0) {
            float d0 = b[0] - prev_band[0]; if (d0 > 0) df += 1.5f * d0;
            float d1 = b[1] - prev_band[1]; if (d1 > 0) df += 1.0f * d1;
            float d2 = b[2] - prev_band[2]; if (d2 > 0) df += 0.65f * d2;
        }
        flux[t] = df;
        prev_band[0] = b[0]; prev_band[1] = b[1]; prev_band[2] = b[2];
    }

    free(re);
    free(im);
    free(hann);

    /* Zero-padded O(N log N) FFT autocorrelation over N_acf = 4096 */
    const int N_acf = 4096;
    float *ac_re = (float *)calloc((size_t)N_acf, sizeof(float));
    float *ac_im = (float *)calloc((size_t)N_acf, sizeof(float));
    if (!ac_re || !ac_im) {
        free(flux); free(ac_re); free(ac_im);
        return res;
    }

    double odf_sq = 0.0;
    for (size_t t = 0; t < n_frames; t++) {
        size_t lo = t > 16 ? t - 16 : 0;
        size_t hi = (t + 16 < n_frames) ? (t + 16) : (n_frames - 1);
        double m = 0.0;
        for (size_t u = lo; u <= hi; u++) m += flux[u];
        m /= (double)(hi - lo + 1);
        float v = flux[t] - (float)m;
        if (v < 0.0f) v = 0.0f;
        ac_re[t] = v;
        odf_sq += (double)v * v;
    }
    free(flux);

    if (odf_sq < 1e-8) {
        free(ac_re); free(ac_im);
        return res;
    }

    /* Power spectrum -> inverse FFT via conjugate symmetry */
    fft_radix2(ac_re, ac_im, N_acf);
    for (int k = 0; k < N_acf; k++) {
        float p = ac_re[k] * ac_re[k] + ac_im[k] * ac_im[k];
        ac_re[k] = p;
        ac_im[k] = 0.0f;
    }
    fft_radix2(ac_re, ac_im, N_acf);
    double norm_denom = (double)N_acf * odf_sq;

    double fps = sr / (double)hop;
    int lag_min = (int)floor(fps * 60.0 / 185.0);
    int lag_max = (int)ceil(fps * 60.0 / 54.0);
    if (lag_min < 6) lag_min = 6;
    if ((size_t)(lag_max * 2) >= n_frames) lag_max = (int)(n_frames / 2) - 1;
    if (lag_max <= lag_min) {
        free(ac_re); free(ac_im);
        return res;
    }

    double *acf = (double *)calloc((size_t)(lag_max + 2), sizeof(double));
    if (!acf) {
        free(ac_re); free(ac_im);
        return res;
    }

    double best_score = -1.0;
    double mean_score = 0.0;
    int count_cand = 0;
    int best_lag = lag_min;

    for (int lag = lag_min; lag <= lag_max; lag++) {
        double r1 = (double)ac_re[lag] / norm_denom;
        double r2 = ((size_t)(2 * lag) < n_frames - 4) ? ((double)ac_re[2 * lag] / norm_denom) : 0.0;
        double cand_bpm = 60.0 * fps / (double)lag;
        double oct = log2(cand_bpm / 115.0);
        double prior = exp(-0.5 * (oct / 0.85) * (oct / 0.85));
        double score = (0.72 * r1 + 0.28 * r2) * (0.65 + 0.35 * prior);

        acf[lag] = score;
        mean_score += score;
        count_cand++;
        if (score > best_score) {
            best_score = score;
            best_lag = lag;
        }
    }

    double refined_lag = (double)best_lag;
    if (best_lag > lag_min && best_lag < lag_max) {
        double yl = acf[best_lag - 1], ym = acf[best_lag], yr = acf[best_lag + 1];
        double denom = yl - 2.0 * ym + yr;
        if (fabs(denom) > 1e-12) {
            double delta = 0.5 * (yl - yr) / denom;
            if (delta > -0.5 && delta < 0.5) refined_lag += delta;
        }
    }

    double raw_bpm = 60.0 * fps / refined_lag;
    while (raw_bpm < 62.0) raw_bpm *= 2.0;
    while (raw_bpm > 175.0) raw_bpm *= 0.5;

    mean_score /= (count_cand > 0 ? count_cand : 1);
    double ratio = (mean_score > 1e-9) ? (best_score / mean_score) : 1.0;
    double conf = (ratio - 1.0) / 2.5;
    if (conf > 0.99) conf = 0.99;
    if (conf < 0.1) conf = 0.1;

    free(acf);
    free(ac_re);
    free(ac_im);

    res.valid = true;
    res.bpm = round(raw_bpm * 10.0) / 10.0;
    res.confidence = round(conf * 1000.0) / 1000.0;
    return res;
}

/* -------------------------------------------------------------------------- */
/* Incremental Cache Hash Table                                               */
/* -------------------------------------------------------------------------- */

typedef struct {
    char *full_path;
    uint64_t size;
    int64_t mtime;
    char *json_line;
    bool pending_host;
} CacheEntry;

typedef struct {
    CacheEntry *slots;
    size_t cap;
    size_t count;
} CacheMap;

static uint64_t fnv1a_str(const char *s) {
    uint64_t h = 14695981039346656037ULL;
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        h ^= *p;
        h *= 1099511628211ULL;
    }
    return h;
}

static void cache_init(CacheMap *m, size_t init_cap) {
    size_t cap = 1024;
    while (cap < init_cap) cap <<= 1;
    m->slots = (CacheEntry *)calloc(cap, sizeof(CacheEntry));
    m->cap = cap;
    m->count = 0;
}

static void cache_rehash(CacheMap *m, size_t new_cap) {
    CacheEntry *ns = (CacheEntry *)calloc(new_cap, sizeof(CacheEntry));
    if (!ns) return;
    for (size_t i = 0; i < m->cap; i++) {
        if (!m->slots[i].full_path) continue;
        size_t idx = (size_t)(fnv1a_str(m->slots[i].full_path) & (new_cap - 1));
        while (ns[idx].full_path) idx = (idx + 1) & (new_cap - 1);
        ns[idx] = m->slots[i];
    }
    free(m->slots);
    m->slots = ns;
    m->cap = new_cap;
}

static CacheEntry *cache_find(CacheMap *m, const char *full_path) {
    if (!m->slots || m->cap == 0) return NULL;
    size_t idx = (size_t)(fnv1a_str(full_path) & (m->cap - 1));
    while (m->slots[idx].full_path) {
        if (strcmp(m->slots[idx].full_path, full_path) == 0) return &m->slots[idx];
        idx = (idx + 1) & (m->cap - 1);
    }
    return NULL;
}

static void cache_put(
    CacheMap *m,
    const char *full_path,
    uint64_t size,
    int64_t mtime,
    const char *json_line,
    bool pending_host
) {
    if ((m->count + 1) * 10 >= m->cap * 7) {
        cache_rehash(m, m->cap * 2);
    }
    size_t idx = (size_t)(fnv1a_str(full_path) & (m->cap - 1));
    while (m->slots[idx].full_path) {
        if (strcmp(m->slots[idx].full_path, full_path) == 0) {
            free(m->slots[idx].json_line);
            m->slots[idx].size = size;
            m->slots[idx].mtime = mtime;
            m->slots[idx].json_line = strdup(json_line);
            m->slots[idx].pending_host = pending_host;
            return;
        }
        idx = (idx + 1) & (m->cap - 1);
    }
    m->slots[idx].full_path = strdup(full_path);
    m->slots[idx].size = size;
    m->slots[idx].mtime = mtime;
    m->slots[idx].json_line = strdup(json_line);
    m->slots[idx].pending_host = pending_host;
    m->count++;
}

static void cache_free(CacheMap *m) {
    for (size_t i = 0; i < m->cap; i++) {
        free(m->slots[i].full_path);
        free(m->slots[i].json_line);
    }
    free(m->slots);
    m->slots = NULL;
    m->cap = 0;
    m->count = 0;
}

/* Lightweight JSON field extractor for previously written library.jsonl lines */
static bool json_get_string(const char *line, const char *key, char *out, size_t cap) {
    char pat[128];
    snprintf(pat, sizeof(pat), "\"%s\":\"", key);
    const char *p = strstr(line, pat);
    if (!p) return false;
    p += strlen(pat);
    size_t i = 0;
    while (*p && *p != '"' && i + 1 < cap) {
        if (*p == '\\' && p[1]) {
            p++;
            if (*p == 'n') out[i++] = '\n';
            else if (*p == 'r') out[i++] = '\r';
            else if (*p == 't') out[i++] = '\t';
            else out[i++] = *p;
            p++;
        } else {
            out[i++] = *p++;
        }
    }
    out[i] = '\0';
    return true;
}

static bool json_get_int64(const char *line, const char *key, int64_t *out) {
    char pat[128];
    snprintf(pat, sizeof(pat), "\"%s\":", key);
    const char *p = strstr(line, pat);
    if (!p) return false;
    p += strlen(pat);
    if (!isdigit((unsigned char)*p) && *p != '-') return false;
    *out = strtoll(p, NULL, 10);
    return true;
}

static size_t load_existing_cache(const char *jsonl_path, CacheMap *cache) {
    FILE *f = fopen(jsonl_path, "r");
    if (!f) return 0;
    char *line = NULL;
    size_t lcap = 0;
    ssize_t llen;
    size_t loaded = 0;
    while ((llen = getline(&line, &lcap, f)) > 0) {
        while (llen > 0 && (line[llen - 1] == '\n' || line[llen - 1] == '\r')) {
            line[--llen] = '\0';
        }
        if (llen < 10 || line[0] != '{') continue;
        char fpath[2048];
        int64_t sz = -1, mt = -1;
        if (!json_get_string(line, "full_path", fpath, sizeof(fpath))) continue;
        if (!json_get_int64(line, "size", &sz) || !json_get_int64(line, "mtime", &mt)) continue;
        char status[64] = "";
        json_get_string(line, "status", status, sizeof(status));
        bool pending = (strcmp(status, "pending_host_decode") == 0);
        cache_put(cache, fpath, (uint64_t)sz, mt, line, pending);
        loaded++;
    }
    free(line);
    fclose(f);
    return loaded;
}

/* -------------------------------------------------------------------------- */
/* Single File Analysis + Compact JSON Serialization                          */
/* -------------------------------------------------------------------------- */

static bool format_sample_json(
    const char *drive,
    const char *rel_path,
    const char *full_path,
    const char *ext,
    uint64_t size,
    int64_t mtime,
    double oneshot_max_s,
    char *out_json,
    size_t out_cap
) {
    FILE *mem = NULL;
    char *mbuf = NULL;
    size_t mlen = 0;
    mem = open_memstream(&mbuf, &mlen);
    if (!mem) return false;

    if (is_host_audio_ext(ext)) {
        bool skip_hint = path_has_oneshot_hint(full_path);
        fprintf(mem, "{\"drive\":");
        json_write_str(mem, drive);
        fprintf(mem, ",\"rel_path\":");
        json_write_str(mem, rel_path);
        fprintf(mem, ",\"full_path\":");
        json_write_str(mem, full_path);
        fprintf(mem, ",\"ext\":");
        json_write_str(mem, ext);
        fprintf(mem, ",\"size\":%llu,\"mtime\":%lld,\"category\":",
                (unsigned long long)size, (long long)mtime);
        json_write_str(mem, skip_hint ? "one_shot" : "unknown");
        fprintf(mem, ",\"duration_s\":null,\"sr\":null,\"ch\":null,\"bits\":null,"
                     "\"bpm\":null,\"bpm_conf\":null,\"key\":null,\"short_key\":null,"
                     "\"camelot\":null,\"key_conf\":null,\"atonal\":null,"
                     "\"status\":\"pending_host_decode\",\"analyzer\":\"mpc_c\"}");
        fclose(mem);
        if (mbuf) {
            snprintf(out_json, out_cap, "%s", mbuf);
            free(mbuf);
        }
        return true;
    }

    DecodedAudio da;
    memset(&da, 0, sizeof(da));
    bool ok = false;
    if (strcmp(ext, "wav") == 0) ok = decode_wav(full_path, &da);
    else if (strcmp(ext, "aif") == 0 || strcmp(ext, "aiff") == 0 || strcmp(ext, "aifc") == 0)
        ok = decode_aiff(full_path, &da);
    else if (strcmp(ext, "mp3") == 0)
        ok = decode_mp3(full_path, &da);

    if (!ok || !da.decode_ok) {
        free_decoded_audio(&da);
        fprintf(mem, "{\"drive\":");
        json_write_str(mem, drive);
        fprintf(mem, ",\"rel_path\":");
        json_write_str(mem, rel_path);
        fprintf(mem, ",\"full_path\":");
        json_write_str(mem, full_path);
        fprintf(mem, ",\"ext\":");
        json_write_str(mem, ext);
        fprintf(mem, ",\"size\":%llu,\"mtime\":%lld,\"category\":\"error\","
                     "\"duration_s\":null,\"sr\":null,\"ch\":null,\"bits\":null,"
                     "\"bpm\":null,\"bpm_conf\":null,\"key\":null,\"short_key\":null,"
                     "\"camelot\":null,\"key_conf\":null,\"atonal\":null,"
                     "\"status\":\"decode_error\",\"analyzer\":\"mpc_c\"}",
                (unsigned long long)size, (long long)mtime);
        fclose(mem);
        if (mbuf) {
            snprintf(out_json, out_cap, "%s", mbuf);
            free(mbuf);
        }
        return true;
    }

    bool skip_bpm = false;
    const char *cat = classify_sample(da.duration_s, full_path, oneshot_max_s, &skip_bpm);

    KeyResult kr;
    memset(&kr, 0, sizeof(kr));
    if (da.duration_s >= ULTRA_SHORT_KEY_MIN_S && da.samples && da.n_samples >= 512) {
        kr = detect_key_from_buffer(da.samples, da.n_samples, da.decimated_sr);
    }

    BpmResult br = {false, 0.0, 0.0};
    if (!skip_bpm && !kr.silence && da.samples && da.n_samples >= 4096) {
        br = detect_bpm_from_buffer(da.samples, da.n_samples, da.decimated_sr);
    }

    fprintf(mem, "{\"drive\":");
    json_write_str(mem, drive);
    fprintf(mem, ",\"rel_path\":");
    json_write_str(mem, rel_path);
    fprintf(mem, ",\"full_path\":");
    json_write_str(mem, full_path);
    fprintf(mem, ",\"ext\":");
    json_write_str(mem, ext);
    fprintf(mem, ",\"size\":%llu,\"mtime\":%lld,\"category\":",
            (unsigned long long)size, (long long)mtime);
    json_write_str(mem, cat);
    fprintf(mem, ",\"duration_s\":%.4f,\"sr\":%u,\"ch\":%u,\"bits\":%u",
            da.duration_s, da.orig_sr, da.orig_ch, da.orig_bits);

    if (br.valid && br.bpm > 0.0) {
        fprintf(mem, ",\"bpm\":%.1f,\"bpm_conf\":%.3f", br.bpm, br.confidence);
    } else {
        fprintf(mem, ",\"bpm\":null,\"bpm_conf\":null");
    }

    if (kr.valid && !kr.atonal && !kr.silence && kr.key[0]) {
        fprintf(mem, ",\"key\":"); json_write_str(mem, kr.key);
        fprintf(mem, ",\"short_key\":"); json_write_str(mem, kr.short_key);
        fprintf(mem, ",\"camelot\":"); json_write_str(mem, kr.camelot);
        fprintf(mem, ",\"key_conf\":%.3f,\"atonal\":false", kr.confidence);
    } else if (kr.silence) {
        fprintf(mem, ",\"key\":\"Silence\",\"short_key\":\"--\",\"camelot\":\"--\","
                     "\"key_conf\":0.0,\"atonal\":true");
    } else {
        fprintf(mem, ",\"key\":null,\"short_key\":null,\"camelot\":null,"
                     "\"key_conf\":%.3f,\"atonal\":%s",
                kr.confidence, (da.duration_s < ULTRA_SHORT_KEY_MIN_S ? "null" : "true"));
    }

    fprintf(mem, ",\"status\":\"ok\",\"analyzer\":\"mpc_c\"}");
    fclose(mem);
    free_decoded_audio(&da);
    if (mbuf) {
        snprintf(out_json, out_cap, "%s", mbuf);
        free(mbuf);
    }
    return true;
}

/* -------------------------------------------------------------------------- */
/* Drive Scanner + JSONL / CSV / Stats Persistence                            */
/* -------------------------------------------------------------------------- */

typedef struct {
    char **paths;
    char **drives;
    char **rels;
    char **exts;
    uint64_t *sizes;
    int64_t *mtimes;
    size_t count;
    size_t cap;
} FileQueue;

static void fq_push(
    FileQueue *q,
    const char *drive,
    const char *rel,
    const char *full_path,
    const char *ext,
    uint64_t size,
    int64_t mtime
) {
    if (q->count >= q->cap) {
        size_t nc = q->cap ? q->cap * 2 : 4096;
        q->paths = (char **)realloc(q->paths, nc * sizeof(char *));
        q->drives = (char **)realloc(q->drives, nc * sizeof(char *));
        q->rels = (char **)realloc(q->rels, nc * sizeof(char *));
        q->exts = (char **)realloc(q->exts, nc * sizeof(char *));
        q->sizes = (uint64_t *)realloc(q->sizes, nc * sizeof(uint64_t));
        q->mtimes = (int64_t *)realloc(q->mtimes, nc * sizeof(int64_t));
        q->cap = nc;
    }
    q->paths[q->count] = strdup(full_path);
    q->drives[q->count] = strdup(drive);
    q->rels[q->count] = strdup(rel);
    q->exts[q->count] = strdup(ext);
    q->sizes[q->count] = size;
    q->mtimes[q->count] = mtime;
    q->count++;
}

static void fq_free(FileQueue *q) {
    for (size_t i = 0; i < q->count; i++) {
        free(q->paths[i]);
        free(q->drives[i]);
        free(q->rels[i]);
        free(q->exts[i]);
    }
    free(q->paths); free(q->drives); free(q->rels); free(q->exts);
    free(q->sizes); free(q->mtimes);
    memset(q, 0, sizeof(*q));
}

static void walk_dir(
    const char *drive_name,
    const char *drive_root,
    const char *cur_dir,
    dev_t root_dev,
    FileQueue *q,
    CacheMap *dir_cache,
    bool fast_dir_prune
) {
    if (g_stop) return;
    struct stat cur_st;
    if (stat(cur_dir, &cur_st) != 0) return;

    bool scan_files_here = true;
    if (fast_dir_prune && dir_cache) {
        CacheEntry *dce = cache_find(dir_cache, cur_dir);
        if (dce && dce->mtime == (int64_t)cur_st.st_mtime) {
            scan_files_here = false;
        }
    }

    DIR *d = opendir(cur_dir);
    if (!d) return;

    struct dirent *de;
    char child[2048];
    size_t root_len = strlen(drive_root);

    while ((de = readdir(d)) != NULL && !g_stop) {
        const char *name = de->d_name;
        if (name[0] == '.') continue; /* skip ., .., ._* AppleDouble, .Trashes, .Spotlight */
        if (strcmp(name, "MPC_Sample_Library") == 0 ||
            strcmp(name, "mpc-control") == 0 ||
            strcasecmp(name, "[Previews]") == 0 ||
            strcasecmp(name, "Previews") == 0) {
            continue;
        }
#ifdef DT_LNK
        if (de->d_type == DT_LNK) continue;
        if (de->d_type == DT_REG) {
            if (!scan_files_here) continue;
            char ext[16];
            get_ext_lower(name, ext, sizeof(ext));
            if (!is_direct_audio_ext(ext) && !is_host_audio_ext(ext)) continue;
        }
#endif
        int n = snprintf(child, sizeof(child), "%s/%s", cur_dir, name);
        if (n <= 0 || n >= (int)sizeof(child)) continue;

#ifdef DT_DIR
        if (de->d_type != DT_DIR && !scan_files_here && de->d_type != DT_UNKNOWN) {
            continue;
        }
#endif
        struct stat st;
        if (lstat(child, &st) != 0) continue;
        if (S_ISLNK(st.st_mode)) continue;
        if (st.st_dev != root_dev) continue;

        if (S_ISDIR(st.st_mode)) {
            walk_dir(drive_name, drive_root, child, root_dev, q, dir_cache, fast_dir_prune);
        } else if (scan_files_here && S_ISREG(st.st_mode)) {
            char ext[16];
            get_ext_lower(name, ext, sizeof(ext));
            if (is_direct_audio_ext(ext) || is_host_audio_ext(ext)) {
                const char *rel = child + root_len;
                while (*rel == '/') rel++;
                fq_push(q, drive_name, rel, child, ext, (uint64_t)st.st_size, (int64_t)st.st_mtime);
            }
        }
    }
    closedir(d);

    if (dir_cache) {
        cache_put(dir_cache, cur_dir, 1, (int64_t)cur_st.st_mtime, "d", false);
    }
}

typedef struct {
    char path[512];
    char name[128];
    dev_t dev;
} ScanRoot;

static int discover_roots(const char *roots_spec, ScanRoot *out_roots, int max_roots) {
    int count = 0;
    char *copy = strdup(roots_spec);
    char *tok = strtok(copy, ",");
    while (tok && count < max_roots) {
        while (*tok == ' ') tok++;
        if (strcmp(tok, "/media") == 0) {
            DIR *d = opendir("/media");
            if (d) {
                struct dirent *de;
                while ((de = readdir(d)) != NULL && count < max_roots) {
                    if (de->d_name[0] == '.') continue;
                    if (strcmp(de->d_name, "acvs-content") == 0) continue;
                    char mpath[512];
                    snprintf(mpath, sizeof(mpath), "/media/%s", de->d_name);
                    struct stat st;
                    if (stat(mpath, &st) != 0 || !S_ISDIR(st.st_mode)) continue;
                    bool dup = false;
                    for (int k = 0; k < count; k++) {
                        if (out_roots[k].dev == st.st_dev) { dup = true; break; }
                    }
                    if (!dup) {
                        snprintf(out_roots[count].path, sizeof(out_roots[count].path), "%s", mpath);
                        snprintf(out_roots[count].name, sizeof(out_roots[count].name), "%s", de->d_name);
                        out_roots[count].dev = st.st_dev;
                        count++;
                    }
                }
                closedir(d);
            }
        } else {
            struct stat st;
            if (stat(tok, &st) == 0 && S_ISDIR(st.st_mode)) {
                bool dup = false;
                for (int k = 0; k < count; k++) {
                    if (out_roots[k].dev == st.st_dev && strcmp(out_roots[k].path, tok) == 0) {
                        dup = true;
                        break;
                    }
                }
                if (!dup) {
                    char tmp_b[512];
                    snprintf(tmp_b, sizeof(tmp_b), "%s", tok);
                    size_t bl = strlen(tmp_b);
                    while (bl > 1 && tmp_b[bl - 1] == '/') tmp_b[--bl] = '\0';
                    const char *bn = strrchr(tmp_b, '/');
                    bn = bn ? bn + 1 : tmp_b;
                    snprintf(out_roots[count].path, sizeof(out_roots[count].path), "%s", tmp_b);
                    snprintf(out_roots[count].name, sizeof(out_roots[count].name), "%s", bn);
                    out_roots[count].dev = st.st_dev;
                    count++;
                }
            }
        }
        tok = strtok(NULL, ",");
    }
    free(copy);
    return count;
}

static bool copy_file_atomic(const char *src, const char *dst) {
    char tmp[1024];
    snprintf(tmp, sizeof(tmp), "%s.tmp.%d", dst, (int)getpid());
    FILE *fi = fopen(src, "rb");
    if (!fi) return false;
    FILE *fo = fopen(tmp, "wb");
    if (!fo) {
        fclose(fi);
        return false;
    }
    char buf[65536];
    size_t nr;
    bool ok = true;
    while ((nr = fread(buf, 1, sizeof(buf), fi)) > 0) {
        if (fwrite(buf, 1, nr, fo) != nr) {
            ok = false;
            break;
        }
    }
    fclose(fi);
    if (fflush(fo) != 0) ok = false;
    fclose(fo);
    if (!ok || rename(tmp, dst) != 0) {
        unlink(tmp);
        return false;
    }
    return true;
}

static void write_progress_json(
    const char *out_dir,
    const char *mirror_dir,
    const char *phase,
    size_t total_found,
    size_t todo_count,
    size_t done_in_pass,
    size_t total_cached,
    double elapsed_s,
    const char *last_file
) {
    char ppath[1024], ptmp[1024];
    snprintf(ppath, sizeof(ppath), "%s/progress.json", out_dir);
    snprintf(ptmp, sizeof(ptmp), "%s/progress.json.tmp", out_dir);
    double pct = (todo_count > 0) ? (100.0 * (double)done_in_pass / (double)todo_count) : 100.0;
    double rate = (elapsed_s > 0.25 && done_in_pass > 0) ? ((double)done_in_pass / elapsed_s) : 0.0;
    double eta_s = (rate > 0.0 && todo_count > done_in_pass)
        ? ((double)(todo_count - done_in_pass) / rate)
        : 0.0;

    FILE *fp = fopen(ptmp, "w");
    if (!fp) return;
    fprintf(fp,
            "{\"updated_ts\":%lld,\"phase\":",
            (long long)time(NULL));
    json_write_str(fp, phase);
    fprintf(fp,
            ",\"total_found\":%zu,\"pending_work\":%zu,\"done_in_pass\":%zu,"
            "\"total_cached\":%zu,\"percent\":%.2f,\"elapsed_s\":%.1f,"
            "\"rate_per_s\":%.2f,\"eta_s\":%.1f,\"last_file\":",
            total_found, todo_count, done_in_pass, total_cached,
            pct, elapsed_s, rate, eta_s);
    json_write_str(fp, last_file ? last_file : "");
    fputs("}\n", fp);
    fclose(fp);
    if (rename(ptmp, ppath) == 0 && mirror_dir && mirror_dir[0] && strcmp(mirror_dir, out_dir) != 0) {
        if (mkdir_p(mirror_dir) == 0) {
            char mp[1024];
            snprintf(mp, sizeof(mp), "%s/progress.json", mirror_dir);
            copy_file_atomic(ppath, mp);
        }
    }
}

static void export_summary_and_csv(
    const char *out_dir,
    const char *mirror_dir,
    CacheMap *cache,
    const ScanRoot *roots,
    int n_roots,
    bool write_csv
) {
    mkdir_p(out_dir);

    char csv_path[1024], csv_tmp[1024];
    char stats_path[1024], stats_tmp[1024];
    char jsonl_path[1024];
    snprintf(jsonl_path, sizeof(jsonl_path), "%s/library.jsonl", out_dir);
    snprintf(csv_path, sizeof(csv_path), "%s/library.csv", out_dir);
    snprintf(csv_tmp, sizeof(csv_tmp), "%s/library.csv.tmp", out_dir);
    snprintf(stats_path, sizeof(stats_path), "%s/stats.json", out_dir);
    snprintf(stats_tmp, sizeof(stats_tmp), "%s/stats.json.tmp", out_dir);

    size_t total = 0, cat_one = 0, cat_ultra = 0, cat_loop = 0, cat_phrase = 0, cat_track = 0, cat_other = 0;
    size_t bpm_known = 0, key_known = 0, pending_host = 0, errors = 0;

    FILE *fc = write_csv ? fopen(csv_tmp, "w") : NULL;
    if (fc) {
        fputs("drive,rel_path,ext,category,duration_s,sr,ch,bits,bpm,bpm_conf,key,short_key,camelot,key_conf,status,full_path\n", fc);
    }

    for (size_t i = 0; i < cache->cap; i++) {
        if (!cache->slots[i].full_path || !cache->slots[i].json_line) continue;
        const char *ln = cache->slots[i].json_line;
        total++;
        char drive[64] = "", rel[1024] = "", ext[16] = "", cat[32] = "", key[32] = "", skey[16] = "", cam[8] = "", status[32] = "";
        json_get_string(ln, "drive", drive, sizeof(drive));
        json_get_string(ln, "rel_path", rel, sizeof(rel));
        json_get_string(ln, "ext", ext, sizeof(ext));
        json_get_string(ln, "category", cat, sizeof(cat));
        json_get_string(ln, "key", key, sizeof(key));
        json_get_string(ln, "short_key", skey, sizeof(skey));
        json_get_string(ln, "camelot", cam, sizeof(cam));
        json_get_string(ln, "status", status, sizeof(status));

        if (strcmp(status, "pending_host_decode") == 0) pending_host++;
        else if (strcmp(status, "decode_error") == 0) errors++;

        if (strcmp(cat, "one_shot") == 0) cat_one++;
        else if (strcmp(cat, "ultra_short") == 0) cat_ultra++;
        else if (strcmp(cat, "loop") == 0) cat_loop++;
        else if (strcmp(cat, "phrase") == 0) cat_phrase++;
        else if (strcmp(cat, "track") == 0) cat_track++;
        else cat_other++;

        if (key[0] && strcmp(key, "Silence") != 0) key_known++;
        bool has_bpm = (strstr(ln, "\"bpm\":null") == NULL && strstr(ln, "\"bpm\":") != NULL);
        if (has_bpm) bpm_known++;

        if (fc) {
            char dur_s[32] = "", sr_s[16] = "", ch_s[8] = "", bits_s[8] = "", bpm_s[16] = "", bconf_s[16] = "", kconf_s[16] = "";
            /* Extract raw numeric tokens concisely */
            const char *keys[7] = {"\"duration_s\":", "\"sr\":", "\"ch\":", "\"bits\":", "\"bpm\":", "\"bpm_conf\":", "\"key_conf\":"};
            char *dsts[7] = {dur_s, sr_s, ch_s, bits_s, bpm_s, bconf_s, kconf_s};
            for (int f = 0; f < 7; f++) {
                const char *p = strstr(ln, keys[f]);
                if (p) {
                    p += strlen(keys[f]);
                    if (strncmp(p, "null", 4) != 0) {
                        size_t j = 0;
                        while (p[j] && p[j] != ',' && p[j] != '}' && j < 15) {
                            dsts[f][j] = p[j];
                            j++;
                        }
                        dsts[f][j] = '\0';
                    }
                }
            }
            csv_write_str(fc, drive); fputc(',', fc);
            csv_write_str(fc, rel); fputc(',', fc);
            csv_write_str(fc, ext); fputc(',', fc);
            csv_write_str(fc, cat); fputc(',', fc);
            fprintf(fc, "%s,%s,%s,%s,%s,%s,", dur_s, sr_s, ch_s, bits_s, bpm_s, bconf_s);
            csv_write_str(fc, key); fputc(',', fc);
            csv_write_str(fc, skey); fputc(',', fc);
            csv_write_str(fc, cam); fputc(',', fc);
            fprintf(fc, "%s,", kconf_s);
            csv_write_str(fc, status); fputc(',', fc);
            csv_write_str(fc, cache->slots[i].full_path); fputc('\n', fc);
        }
    }
    if (fc) {
        fclose(fc);
        rename(csv_tmp, csv_path);
    }

    FILE *fs = fopen(stats_tmp, "w");
    if (fs) {
        time_t now = time(NULL);
        fprintf(fs,
                "{\n"
                "  \"updated_ts\": %lld,\n"
                "  \"total_samples\": %zu,\n"
                "  \"categories\": {\n"
                "    \"one_shot\": %zu,\n"
                "    \"ultra_short\": %zu,\n"
                "    \"loop\": %zu,\n"
                "    \"phrase\": %zu,\n"
                "    \"track\": %zu,\n"
                "    \"other\": %zu\n"
                "  },\n"
                "  \"bpm_detected\": %zu,\n"
                "  \"key_detected\": %zu,\n"
                "  \"pending_host_decode\": %zu,\n"
                "  \"errors\": %zu,\n"
                "  \"drives\": [",
                (long long)now, total,
                cat_one, cat_ultra, cat_loop, cat_phrase, cat_track, cat_other,
                bpm_known, key_known, pending_host, errors);
        for (int i = 0; i < n_roots; i++) {
            if (i > 0) fputc(',', fs);
            fprintf(fs, "{\"name\":");
            json_write_str(fs, roots[i].name);
            fprintf(fs, ",\"path\":");
            json_write_str(fs, roots[i].path);
            fputc('}', fs);
        }
        fputs("]\n}\n", fs);
        fclose(fs);
        rename(stats_tmp, stats_path);
    }

    if (mirror_dir && mirror_dir[0] && strcmp(mirror_dir, out_dir) != 0) {
        if (mkdir_p(mirror_dir) == 0) {
            char m_jsonl[1024], m_csv[1024], m_stats[1024];
            snprintf(m_jsonl, sizeof(m_jsonl), "%s/library.jsonl", mirror_dir);
            snprintf(m_csv, sizeof(m_csv), "%s/library.csv", mirror_dir);
            snprintf(m_stats, sizeof(m_stats), "%s/stats.json", mirror_dir);
            if (write_csv) {
                copy_file_atomic(jsonl_path, m_jsonl);
                copy_file_atomic(csv_path, m_csv);
            }
            copy_file_atomic(stats_path, m_stats);
        }
    }
}

typedef struct {
    const FileQueue *q;
    const size_t *todo_idx;
    size_t todo_count;
    size_t next_pos;
    double oneshot_max_s;
    bool emit_events;
    const char *out_dir;
    const char *mirror_dir;
    FILE *fa;
    FILE *fe;
    CacheMap *cache;
    const ScanRoot *roots;
    int n_roots;
    size_t newly_processed;
    time_t t_start;
    pthread_mutex_t mu;
} WorkCtx;

static void *scan_worker(void *arg) {
    WorkCtx *w = (WorkCtx *)arg;
    char json_buf[8192];
    while (!g_stop) {
        pthread_mutex_lock(&w->mu);
        size_t pos = w->next_pos++;
        pthread_mutex_unlock(&w->mu);
        if (pos >= w->todo_count || g_stop) break;

        size_t i = w->todo_idx[pos];
        if (!format_sample_json(
                w->q->drives[i], w->q->rels[i], w->q->paths[i], w->q->exts[i],
                w->q->sizes[i], w->q->mtimes[i], w->oneshot_max_s, json_buf, sizeof(json_buf))) {
            continue;
        }
        bool pending = (strstr(json_buf, "\"status\":\"pending_host_decode\"") != NULL);

        pthread_mutex_lock(&w->mu);
        fprintf(w->fa, "%s\n", json_buf);
        cache_put(w->cache, w->q->paths[i], w->q->sizes[i], w->q->mtimes[i], json_buf, pending);
        w->newly_processed++;

        if (w->emit_events) {
            printf("NEW %s\n", json_buf);
            fflush(stdout);
            if (w->fe) fprintf(w->fe, "%s\n", json_buf);
        }
        if ((w->newly_processed % 200) == 0) {
            fflush(w->fa);
            if (w->fe) fflush(w->fe);
            double el = difftime(time(NULL), w->t_start);
            write_progress_json(w->out_dir, w->mirror_dir, "scanning",
                                w->q->count, w->todo_count, w->newly_processed,
                                w->cache->count, el, w->q->rels[i]);
            if ((w->newly_processed % 2000) == 0) {
                export_summary_and_csv(w->out_dir, w->mirror_dir, w->cache, w->roots, w->n_roots, false);
            }
            fprintf(stderr, "[mpc_sample_indexer] progress analyzed=%zu/%zu (cached=%zu, %.1f/s)\n",
                    w->newly_processed, w->todo_count, w->cache->count,
                    el > 0.1 ? (double)w->newly_processed / el : 0.0);
            fflush(stderr);
        }
        pthread_mutex_unlock(&w->mu);
    }
    return NULL;
}

static size_t run_scan_pass(
    const char *roots_spec,
    const char *out_dir,
    const char *mirror_dir,
    double oneshot_max_s,
    size_t limit,
    bool force,
    int jobs,
    CacheMap *cache,
    CacheMap *dir_cache,
    bool fast_dir_prune,
    bool emit_events
) {
    ScanRoot roots[32];
    int n_roots = discover_roots(roots_spec, roots, 32);

    FileQueue q = {0};
    for (int i = 0; i < n_roots; i++) {
        walk_dir(roots[i].name, roots[i].path, roots[i].path, roots[i].dev, &q, dir_cache, fast_dir_prune);
    }

    size_t todo_cap = (limit > 0 && limit < q.count) ? limit : (q.count > 0 ? q.count : 1);
    size_t *todo_idx = (size_t *)malloc(todo_cap * sizeof(size_t));
    size_t todo_count = 0;
    if (todo_idx) {
        for (size_t i = 0; i < q.count; i++) {
            CacheEntry *ce = cache_find(cache, q.paths[i]);
            if (force || !ce || ce->size != q.sizes[i] || ce->mtime != q.mtimes[i]) {
                todo_idx[todo_count++] = i;
                if (limit > 0 && todo_count >= limit) break;
            }
        }
    }

    mkdir_p(out_dir);
    char jsonl_path[1024], events_path[1024];
    snprintf(jsonl_path, sizeof(jsonl_path), "%s/library.jsonl", out_dir);
    snprintf(events_path, sizeof(events_path), "%s/events.jsonl", out_dir);

    FILE *fa = fopen(jsonl_path, "a");
    if (!fa) {
        free(todo_idx);
        fq_free(&q);
        return 0;
    }
    FILE *fe = emit_events ? fopen(events_path, "a") : NULL;

    time_t t_start = time(NULL);
    if (todo_count > 0 && !emit_events) {
        write_progress_json(out_dir, mirror_dir, "scanning", q.count, todo_count, 0, cache->count, 0.0, "");
    }

    size_t newly_processed = 0;
    if (todo_count > 0) {
        /* Warm up mpg123 dlopen/init on main thread before parallel workers spawn */
        ensure_mpg123();
        WorkCtx ctx = {
            &q, todo_idx, todo_count, 0, oneshot_max_s, emit_events,
            out_dir, mirror_dir, fa, fe, cache, roots, n_roots, 0, t_start,
            PTHREAD_MUTEX_INITIALIZER
        };
        int n_threads = jobs;
        if (n_threads < 1) n_threads = 1;
        if (n_threads > 8) n_threads = 8;
        if (todo_count < 8 || n_threads == 1) {
            scan_worker(&ctx);
        } else {
            if ((size_t)n_threads > todo_count) n_threads = (int)todo_count;
            pthread_t tids[8];
            int spawned = 0;
            for (int t = 0; t < n_threads; t++) {
                if (pthread_create(&tids[t], NULL, scan_worker, &ctx) == 0) spawned++;
            }
            if (spawned == 0) scan_worker(&ctx);
            for (int t = 0; t < spawned; t++) pthread_join(tids[t], NULL);
        }
        newly_processed = ctx.newly_processed;
        pthread_mutex_destroy(&ctx.mu);
    }
    free(todo_idx);
    fclose(fa);
    if (fe) fclose(fe);

    char host_path[1024];
    snprintf(host_path, sizeof(host_path), "%s/host_resolved.jsonl", out_dir);
    struct stat st_host;
    static int64_t s_last_host_mtime = -1;
    bool host_updated = false;
    if (stat(host_path, &st_host) == 0 && (int64_t)st_host.st_mtime != s_last_host_mtime) {
        if (load_existing_cache(host_path, cache) > 0) {
            host_updated = true;
        }
        s_last_host_mtime = (int64_t)st_host.st_mtime;
    }

    if (newly_processed > 0 || host_updated) {
        char tmp_j[1024];
        snprintf(tmp_j, sizeof(tmp_j), "%s/library.jsonl.tmp", out_dir);
        FILE *fc = fopen(tmp_j, "w");
        if (fc) {
            for (size_t i = 0; i < cache->cap; i++) {
                if (cache->slots[i].full_path && cache->slots[i].json_line) {
                    fprintf(fc, "%s\n", cache->slots[i].json_line);
                }
            }
            fclose(fc);
            rename(tmp_j, jsonl_path);
        }
        export_summary_and_csv(out_dir, mirror_dir, cache, roots, n_roots, true);
    } else {
        char stats_chk[1024];
        snprintf(stats_chk, sizeof(stats_chk), "%s/stats.json", out_dir);
        struct stat st_chk;
        if (stat(stats_chk, &st_chk) != 0) {
            export_summary_and_csv(out_dir, mirror_dir, cache, roots, n_roots, true);
        }
    }

    write_progress_json(out_dir, mirror_dir, g_stop ? "interrupted" : "idle",
                        q.count, todo_count, newly_processed, cache->count,
                        difftime(time(NULL), t_start), "");

    if (!emit_events || newly_processed > 0) {
        fprintf(stderr,
                "[mpc_sample_indexer] scan done roots=%d found=%zu newly_analyzed=%zu total_cached=%zu\n",
                n_roots, q.count, newly_processed, cache->count);
        fflush(stderr);
    }
    fq_free(&q);
    return newly_processed;
}

static void print_usage(void) {
    fprintf(stderr,
            "Usage:\n"
            "  mpc_sample_indexer analyze-file <path> [--oneshot-max-s 3.0]\n"
            "  mpc_sample_indexer scan [--roots /media] [--out-dir DIR] [--mirror-dir DIR] [--oneshot-max-s 3.0] [--limit N] [--jobs 2] [--force]\n"
            "  mpc_sample_indexer watch [--roots /media] [--out-dir DIR] [--mirror-dir DIR] [--interval 8] [--oneshot-max-s 3.0] [--jobs 2]\n");
}

int main(int argc, char **argv) {
    if (argc < 2) {
        print_usage();
        return 2;
    }

    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);

    const char *mode = argv[1];
    const char *roots = DEFAULT_ROOTS;
    const char *out_dir = DEFAULT_OUT_DIR;
    const char *mirror_dir = DEFAULT_MIRROR_DIR;
    double oneshot_max_s = DEFAULT_ONESHOT_MAX_S;
    size_t limit = 0;
    int interval_s = 8;
    int jobs = DEFAULT_JOBS;
    bool force = false;

    for (int i = 2; i < argc; i++) {
        if (strcmp(argv[i], "--roots") == 0 && i + 1 < argc) roots = argv[++i];
        else if (strcmp(argv[i], "--out-dir") == 0 && i + 1 < argc) out_dir = argv[++i];
        else if (strcmp(argv[i], "--mirror-dir") == 0 && i + 1 < argc) mirror_dir = argv[++i];
        else if (strcmp(argv[i], "--oneshot-max-s") == 0 && i + 1 < argc) oneshot_max_s = atof(argv[++i]);
        else if (strcmp(argv[i], "--limit") == 0 && i + 1 < argc) limit = (size_t)atoll(argv[++i]);
        else if (strcmp(argv[i], "--interval") == 0 && i + 1 < argc) interval_s = atoi(argv[++i]);
        else if (strcmp(argv[i], "--jobs") == 0 && i + 1 < argc) jobs = atoi(argv[++i]);
        else if (strcmp(argv[i], "--force") == 0) force = true;
    }
    if (interval_s < 2) interval_s = 2;
    if (oneshot_max_s < 0.5) oneshot_max_s = 0.5;
    if (jobs < 1) jobs = 1;
    if (jobs > 8) jobs = 8;

    if (strcmp(mode, "analyze-file") == 0) {
        if (argc < 3) {
            print_usage();
            return 2;
        }
        const char *fpath = argv[2];
        struct stat st;
        if (stat(fpath, &st) != 0) {
            fprintf(stderr, "stat failed for %s: %s\n", fpath, strerror(errno));
            return 1;
        }
        char ext[16];
        get_ext_lower(fpath, ext, sizeof(ext));
        char copy[2048];
        snprintf(copy, sizeof(copy), "%s", fpath);
        const char *slash = strrchr(copy, '/');
        const char *rel = slash ? slash + 1 : copy;
        char json_buf[8192];
        if (!format_sample_json("local", rel, fpath, ext,
                                (uint64_t)st.st_size, (int64_t)st.st_mtime,
                                oneshot_max_s, json_buf, sizeof(json_buf))) {
            return 1;
        }
        printf("%s\n", json_buf);
        return 0;
    }

    CacheMap cache, dir_cache;
    cache_init(&cache, 16384);
    cache_init(&dir_cache, 8192);
    char jsonl_path[1024], host_path[1024];
    snprintf(jsonl_path, sizeof(jsonl_path), "%s/library.jsonl", out_dir);
    snprintf(host_path, sizeof(host_path), "%s/host_resolved.jsonl", out_dir);
    size_t preloaded = load_existing_cache(jsonl_path, &cache);
    size_t host_pre = load_existing_cache(host_path, &cache);
    fprintf(stderr, "[mpc_sample_indexer] preloaded %zu cached + %zu host entries\n", preloaded, host_pre);

    if (strcmp(mode, "scan") == 0) {
        run_scan_pass(roots, out_dir, mirror_dir, oneshot_max_s, limit, force, jobs, &cache, &dir_cache, false, false);
        cache_free(&dir_cache);
        cache_free(&cache);
        return 0;
    }

    if (strcmp(mode, "watch") == 0) {
        /* Initial baseline pass across all mounted drives */
        run_scan_pass(roots, out_dir, mirror_dir, oneshot_max_s, 0, force, jobs, &cache, &dir_cache, false, false);
        struct stat st_j;
        int64_t last_jsonl_mtime = (stat(jsonl_path, &st_j) == 0) ? (int64_t)st_j.st_mtime : -1;
        fprintf(stderr, "[mpc_sample_indexer] watching %s every %ds (jobs=%d)...\n", roots, interval_s, jobs);
        fflush(stderr);
        unsigned long tick = 0;
        while (!g_stop) {
            for (int s = 0; s < interval_s && !g_stop; s++) sleep(1);
            if (g_stop) break;
            tick++;
            /* Reload jsonl only when modified externally (avoids re-parsing 60MB+ every cycle) */
            if (stat(jsonl_path, &st_j) == 0 && (int64_t)st_j.st_mtime != last_jsonl_mtime) {
                load_existing_cache(jsonl_path, &cache);
                last_jsonl_mtime = (int64_t)st_j.st_mtime;
            }
            bool fast_prune = ((tick % 45) != 0);
            run_scan_pass(roots, out_dir, mirror_dir, oneshot_max_s, 0, false, jobs, &cache, &dir_cache, fast_prune, true);
            if (stat(jsonl_path, &st_j) == 0) last_jsonl_mtime = (int64_t)st_j.st_mtime;
        }
        cache_free(&dir_cache);
        cache_free(&cache);
        return 0;
    }

    cache_free(&dir_cache);
    cache_free(&cache);
    print_usage();
    return 2;
}

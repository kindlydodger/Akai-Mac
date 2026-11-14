/*
 * mpc_iac_midi.c — Lightweight native macOS CoreMIDI helper for IAC Driver & MPCUTER.
 *
 * Capabilities:
 *   --ensure-iac              Bring Apple IAC Driver online (enables Bus 1 / Yol 1)
 *   --list                    Print all CoreMIDI destinations & sources as JSON
 *   --dest <name>             Target destination/source substring (default: IAC Bus 1 / Yol 1)
 *   --send <hex...>           Send one MIDI message (e.g. --send 90 3C 64)
 *   --pipe                    Read newline-delimited hex MIDI messages from stdin -> MIDISend
 *   --listen-ms <ms> [--count N]  Capture incoming MIDI packets from matching source as JSON
 */

#include <CoreFoundation/CoreFoundation.h>
#include <CoreMIDI/CoreMIDI.h>
#include <ctype.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void print_json_str(const char *s) {
    putchar('"');
    if (s) {
        for (const unsigned char *p = (const unsigned char *)s; *p; ++p) {
            unsigned char c = *p;
            if (c == '"' || c == '\\') {
                putchar('\\');
                putchar(c);
            } else if (c == '\n') {
                fputs("\\n", stdout);
            } else if (c == '\r') {
                fputs("\\r", stdout);
            } else if (c == '\t') {
                fputs("\\t", stdout);
            } else if (c < 0x20) {
                printf("\\u%04x", c);
            } else {
                putchar(c);
            }
        }
    }
    putchar('"');
}

static bool cfstr_to_utf8(CFStringRef cf, char *buf, size_t cap) {
    if (!cf || cap == 0) {
        if (cap > 0) buf[0] = '\0';
        return false;
    }
    if (!CFStringGetCString(cf, buf, (CFIndex)cap, kCFStringEncodingUTF8)) {
        buf[0] = '\0';
        return false;
    }
    return true;
}

static bool get_string_prop(MIDIObjectRef obj, CFStringRef key, char *buf, size_t cap) {
    CFStringRef val = NULL;
    buf[0] = '\0';
    if (MIDIObjectGetStringProperty(obj, key, &val) == noErr && val) {
        bool ok = cfstr_to_utf8(val, buf, cap);
        CFRelease(val);
        return ok;
    }
    return false;
}

static int32_t get_int_prop(MIDIObjectRef obj, CFStringRef key, int32_t fallback) {
    SInt32 val = fallback;
    if (MIDIObjectGetIntegerProperty(obj, key, &val) == noErr) {
        return (int32_t)val;
    }
    return fallback;
}

static bool strcasestr_ascii(const char *hay, const char *needle) {
    if (!hay || !needle || !*needle) return false;
    size_t nlen = strlen(needle);
    for (const char *p = hay; *p; ++p) {
        size_t i = 0;
        while (i < nlen && p[i] && tolower((unsigned char)p[i]) == tolower((unsigned char)needle[i])) {
            ++i;
        }
        if (i == nlen) return true;
    }
    return false;
}

static bool is_iac_pattern(const char *pat) {
    if (!pat || !*pat) return true;
    return strcasestr_ascii(pat, "iac") || strcasestr_ascii(pat, "bus 1") || strcasestr_ascii(pat, "yol 1");
}

static int ensure_iac_online(void) {
    int changed = 0;
    ItemCount nd = MIDIGetNumberOfDevices();
    for (ItemCount i = 0; i < nd; ++i) {
        MIDIDeviceRef dev = MIDIGetDevice(i);
        char drv[256];
        get_string_prop(dev, kMIDIPropertyDriverOwner, drv, sizeof(drv));
        if (strcmp(drv, "com.apple.AppleMIDIIACDriver") == 0) {
            int32_t offline = get_int_prop(dev, kMIDIPropertyOffline, 1);
            if (offline != 0) {
                OSStatus st = MIDIObjectSetIntegerProperty(dev, kMIDIPropertyOffline, 0);
                if (st == noErr) changed = 1;
            }
        }
    }
    if (changed) {
        /* Let MIDIServer publish the online IAC Bus 1 endpoints. */
        usleep(250000);
    }
    return changed;
}

static MIDIEndpointRef find_iac_endpoint(bool want_source) {
    ItemCount nd = MIDIGetNumberOfDevices();
    for (ItemCount i = 0; i < nd; ++i) {
        MIDIDeviceRef dev = MIDIGetDevice(i);
        char drv[256];
        get_string_prop(dev, kMIDIPropertyDriverOwner, drv, sizeof(drv));
        if (strcmp(drv, "com.apple.AppleMIDIIACDriver") != 0) continue;
        ItemCount ne = MIDIDeviceGetNumberOfEntities(dev);
        for (ItemCount j = 0; j < ne; ++j) {
            MIDIEntityRef ent = MIDIDeviceGetEntity(dev, j);
            ItemCount cnt = want_source ? MIDIEntityGetNumberOfSources(ent) : MIDIEntityGetNumberOfDestinations(ent);
            if (cnt > 0) {
                return want_source ? MIDIEntityGetSource(ent, 0) : MIDIEntityGetDestination(ent, 0);
            }
        }
    }
    return 0;
}

static MIDIEndpointRef resolve_endpoint(const char *pattern, bool want_source, char *out_name, size_t cap) {
    if (is_iac_pattern(pattern)) {
        ensure_iac_online();
    }
    ItemCount count = want_source ? MIDIGetNumberOfSources() : MIDIGetNumberOfDestinations();
    /* First try exact/substring display-name match when an explicit non-default pattern is given. */
    if (pattern && *pattern) {
        for (ItemCount i = 0; i < count; ++i) {
            MIDIEndpointRef ep = want_source ? MIDIGetSource(i) : MIDIGetDestination(i);
            char name[256];
            get_string_prop(ep, kMIDIPropertyDisplayName, name, sizeof(name));
            if (strcasestr_ascii(name, pattern)) {
                snprintf(out_name, cap, "%s", name);
                return ep;
            }
        }
    }
    /* Locale-safe IAC lookup (matches "IAC Driver Bus 1" in EN, "IAC Sürücüsü Yol 1" in TR, etc.). */
    if (is_iac_pattern(pattern)) {
        MIDIEndpointRef iac_ep = find_iac_endpoint(want_source);
        if (iac_ep != 0) {
            get_string_prop(iac_ep, kMIDIPropertyDisplayName, out_name, cap);
            if (!out_name[0]) snprintf(out_name, cap, "IAC Bus 1");
            return iac_ep;
        }
        for (ItemCount i = 0; i < count; ++i) {
            MIDIEndpointRef ep = want_source ? MIDIGetSource(i) : MIDIGetDestination(i);
            char name[256];
            get_string_prop(ep, kMIDIPropertyDisplayName, name, sizeof(name));
            if (strcasestr_ascii(name, "IAC")) {
                snprintf(out_name, cap, "%s", name);
                return ep;
            }
        }
    }
    if (out_name && cap > 0) out_name[0] = '\0';
    return 0;
}

static void print_endpoint_array(bool want_source) {
    ItemCount count = want_source ? MIDIGetNumberOfSources() : MIDIGetNumberOfDestinations();
    putchar('[');
    for (ItemCount i = 0; i < count; ++i) {
        MIDIEndpointRef ep = want_source ? MIDIGetSource(i) : MIDIGetDestination(i);
        char name[256];
        get_string_prop(ep, kMIDIPropertyDisplayName, name, sizeof(name));
        int32_t uid = get_int_prop(ep, kMIDIPropertyUniqueID, 0);
        int32_t off = get_int_prop(ep, kMIDIPropertyOffline, 0);
        if (i > 0) fputs(", ", stdout);
        printf("{\"index\": %lu, \"name\": ", (unsigned long)i);
        print_json_str(name);
        printf(", \"unique_id\": %d, \"offline\": %s}", (int)uid, off ? "true" : "false");
    }
    putchar(']');
}

static int parse_hex_tokens(int argc, char **argv, int start_idx, uint8_t *out_bytes, size_t max_len) {
    size_t n = 0;
    for (int i = start_idx; i < argc; ++i) {
        const char *tok = argv[i];
        if (!tok || !*tok) continue;
        if (tok[0] == '-' && tok[1] == '-') break;
        const char *p = tok;
        while (*p) {
            while (*p && (isspace((unsigned char)*p) || *p == ',' || *p == ':')) ++p;
            if (!*p) break;
            if (p[0] == '0' && (p[1] == 'x' || p[1] == 'X')) p += 2;
            if (!isxdigit((unsigned char)p[0]) || !isxdigit((unsigned char)p[1])) return -1;
            char hex2[3] = {p[0], p[1], '\0'};
            if (n >= max_len) return -1;
            out_bytes[n++] = (uint8_t)strtoul(hex2, NULL, 16);
            p += 2;
        }
    }
    return (int)n;
}

static int parse_hex_line(const char *line, uint8_t *out_bytes, size_t max_len) {
    size_t n = 0;
    const char *p = line;
    while (*p) {
        while (*p && (isspace((unsigned char)*p) || *p == ',' || *p == ':')) ++p;
        if (!*p) break;
        if (p[0] == '0' && (p[1] == 'x' || p[1] == 'X')) p += 2;
        if (!isxdigit((unsigned char)p[0]) || !isxdigit((unsigned char)p[1])) return -1;
        char hex2[3] = {p[0], p[1], '\0'};
        if (n >= max_len) return -1;
        out_bytes[n++] = (uint8_t)strtoul(hex2, NULL, 16);
        p += 2;
    }
    return (int)n;
}

static OSStatus send_raw_midi(MIDIPortRef out_port, MIDIEndpointRef dest, const uint8_t *bytes, size_t len) {
    if (len == 0 || len > 4096) return -50;
    uint8_t pkt_buf[4352];
    MIDIPacketList *list = (MIDIPacketList *)pkt_buf;
    MIDIPacket *pkt = MIDIPacketListInit(list);
    pkt = MIDIPacketListAdd(list, sizeof(pkt_buf), pkt, 0, (ByteCount)len, bytes);
    if (!pkt) return -108;
    return MIDISend(out_port, dest, list);
}

static void format_hex(const uint8_t *bytes, size_t len, char *out, size_t cap) {
    size_t pos = 0;
    for (size_t i = 0; i < len && pos + 4 < cap; ++i) {
        if (i > 0) out[pos++] = ' ';
        pos += (size_t)snprintf(out + pos, cap - pos, "%02X", bytes[i]);
    }
    out[pos] = '\0';
}

typedef struct {
    int captured;
    int target_count;
    bool first;
} CaptureState;

static void midi_read_cb(const MIDIPacketList *pktlist, void *readProcRefCon, void *srcConnRefCon) {
    (void)srcConnRefCon;
    CaptureState *st = (CaptureState *)readProcRefCon;
    const MIDIPacket *pkt = &pktlist->packet[0];
    for (UInt32 i = 0; i < pktlist->numPackets; ++i) {
        if (pkt->length > 0) {
            char hex[1024];
            size_t cap = pkt->length < 250 ? pkt->length : 250;
            format_hex(pkt->data, cap, hex, sizeof(hex));
            if (!st->first) fputs(", ", stdout);
            st->first = false;
            printf("{\"hex\": ");
            print_json_str(hex);
            printf(", \"bytes\": [");
            for (UInt16 b = 0; b < pkt->length; ++b) {
                printf("%s%u", b ? ", " : "", (unsigned)pkt->data[b]);
            }
            printf("]}");
            fflush(stdout);
            st->captured += 1;
        }
        pkt = MIDIPacketNext(pkt);
    }
}

int main(int argc, char **argv) {
    const char *dest_pattern = NULL;
    bool mode_list = false;
    bool mode_ensure_iac = false;
    bool mode_pipe = false;
    bool mode_listen = false;
    int listen_ms = 1500;
    int listen_count = 0;
    int send_idx = -1;

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--dest") == 0 && i + 1 < argc) {
            dest_pattern = argv[++i];
        } else if (strcmp(argv[i], "--list") == 0) {
            mode_list = true;
        } else if (strcmp(argv[i], "--ensure-iac") == 0) {
            mode_ensure_iac = true;
        } else if (strcmp(argv[i], "--pipe") == 0) {
            mode_pipe = true;
        } else if (strcmp(argv[i], "--listen-ms") == 0 && i + 1 < argc) {
            mode_listen = true;
            listen_ms = atoi(argv[++i]);
            if (listen_ms < 50) listen_ms = 50;
        } else if (strcmp(argv[i], "--count") == 0 && i + 1 < argc) {
            listen_count = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--send") == 0) {
            send_idx = i + 1;
            break;
        }
    }

    if (mode_ensure_iac && !mode_list && !mode_pipe && !mode_listen && send_idx < 0) {
        int changed = ensure_iac_online();
        char name[256];
        MIDIEndpointRef ep = resolve_endpoint(dest_pattern, false, name, sizeof(name));
        printf("{\"ok\": %s, \"activated\": %s, \"iac_destination\": ", ep ? "true" : "false", changed ? "true" : "false");
        print_json_str(name);
        fputs(", \"destinations\": ", stdout);
        print_endpoint_array(false);
        fputs("}\n", stdout);
        return ep ? 0 : 1;
    }

    if (mode_list) {
        fputs("{\"destinations\": ", stdout);
        print_endpoint_array(false);
        fputs(", \"sources\": ", stdout);
        print_endpoint_array(true);
        fputs("}\n", stdout);
        return 0;
    }

    MIDIClientRef client = 0;
    OSStatus st = MIDIClientCreate(CFSTR("MPCUTER_IAC_MIDI"), NULL, NULL, &client);
    if (st != noErr) {
        fprintf(stderr, "{\"ok\": false, \"error\": \"MIDIClientCreate failed: %d\"}\n", (int)st);
        return 1;
    }

    if (mode_listen) {
        char src_name[256];
        MIDIEndpointRef src = resolve_endpoint(dest_pattern, true, src_name, sizeof(src_name));
        if (!src) {
            fprintf(stderr, "{\"ok\": false, \"error\": \"No matching MIDI source\"}\n");
            MIDIClientDispose(client);
            return 1;
        }
        CaptureState cap = {0, listen_count, true};
        MIDIPortRef in_port = 0;
        st = MIDIInputPortCreate(client, CFSTR("MPCUTER_IN"), midi_read_cb, &cap, &in_port);
        if (st != noErr) {
            fprintf(stderr, "{\"ok\": false, \"error\": \"MIDIInputPortCreate failed: %d\"}\n", (int)st);
            MIDIClientDispose(client);
            return 1;
        }
        MIDIPortConnectSource(in_port, src, NULL);
        fputs("{\"ok\": true, \"source\": ", stdout);
        print_json_str(src_name);
        fputs(", \"packets\": [", stdout);
        fflush(stdout);

        double step_s = 0.02;
        int steps = (int)((listen_ms / 1000.0) / step_s);
        if (steps < 1) steps = 1;
        for (int k = 0; k < steps; ++k) {
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, step_s, false);
            if (listen_count > 0 && cap.captured >= listen_count) break;
        }
        printf("], \"count\": %d}\n", cap.captured);
        MIDIPortDisconnectSource(in_port, src);
        MIDIPortDispose(in_port);
        MIDIClientDispose(client);
        return 0;
    }

    char dest_name[256];
    MIDIEndpointRef dest = resolve_endpoint(dest_pattern, false, dest_name, sizeof(dest_name));
    if (!dest) {
        fprintf(stderr, "{\"ok\": false, \"error\": \"No matching MIDI destination (e.g. IAC Driver Bus 1 / Yol 1)\"}\n");
        MIDIClientDispose(client);
        return 1;
    }

    MIDIPortRef out_port = 0;
    st = MIDIOutputPortCreate(client, CFSTR("MPCUTER_OUT"), &out_port);
    if (st != noErr) {
        fprintf(stderr, "{\"ok\": false, \"error\": \"MIDIOutputPortCreate failed: %d\"}\n", (int)st);
        MIDIClientDispose(client);
        return 1;
    }

    if (send_idx >= 0) {
        uint8_t bytes[4096];
        int n = parse_hex_tokens(argc, argv, send_idx, bytes, sizeof(bytes));
        if (n <= 0) {
            fprintf(stderr, "{\"ok\": false, \"error\": \"Invalid MIDI hex bytes\"}\n");
            MIDIPortDispose(out_port);
            MIDIClientDispose(client);
            return 2;
        }
        st = send_raw_midi(out_port, dest, bytes, (size_t)n);
        char hex[1024];
        format_hex(bytes, (size_t)n < 250 ? (size_t)n : 250, hex, sizeof(hex));
        printf("{\"ok\": %s, \"status\": %d, \"destination\": ", st == noErr ? "true" : "false", (int)st);
        print_json_str(dest_name);
        fputs(", \"bytes_hex\": ", stdout);
        print_json_str(hex);
        printf(", \"byte_count\": %d}\n", n);
        MIDIPortDispose(out_port);
        MIDIClientDispose(client);
        return st == noErr ? 0 : 1;
    }

    if (mode_pipe) {
        fputs("{\"ready\": true, \"destination\": ", stdout);
        print_json_str(dest_name);
        fputs("}\n", stdout);
        fflush(stdout);

        char line[8192];
        uint8_t bytes[4096];
        while (fgets(line, sizeof(line), stdin)) {
            char *p = line;
            while (*p && isspace((unsigned char)*p)) ++p;
            if (!*p || *p == '#') continue;
            if (strncmp(p, "QUIT", 4) == 0) break;
            int n = parse_hex_line(p, bytes, sizeof(bytes));
            if (n <= 0) {
                fputs("{\"ok\": false, \"error\": \"invalid_hex\"}\n", stdout);
                fflush(stdout);
                continue;
            }
            st = send_raw_midi(out_port, dest, bytes, (size_t)n);
            char hex[512];
            format_hex(bytes, (size_t)n < 128 ? (size_t)n : 128, hex, sizeof(hex));
            printf("{\"ok\": %s, \"status\": %d, \"bytes_hex\": ", st == noErr ? "true" : "false", (int)st);
            print_json_str(hex);
            printf(", \"byte_count\": %d}\n", n);
            fflush(stdout);
        }
        MIDIPortDispose(out_port);
        MIDIClientDispose(client);
        return 0;
    }

    fprintf(stderr, "Usage: mpc_iac_midi [--ensure-iac | --list | --send <hex...> | --pipe | --listen-ms <ms>]\n");
    MIDIPortDispose(out_port);
    MIDIClientDispose(client);
    return 2;
}

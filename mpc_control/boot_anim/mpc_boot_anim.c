// mpc_boot_anim.c — Early-boot /dev/fb0 ASCII grin animator, Frame-0 ACV8 exporter, and ANSI preview CLI.
#define _GNU_SOURCE
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <unistd.h>

#ifdef __linux__
#include <linux/fb.h>
#endif

#include "mpc_boot_splash_shared.h"

#define MPC_BOOT_PID_FILE "/dev/shm/.mpc_boot_anim.pid"

static volatile sig_atomic_t g_stop = 0;

static void handle_sig(int sig) {
    (void)sig;
    g_stop = 1;
}

static uint32_t g_canvas[BOOT_SCR_W * BOOT_SCR_H];
static BootMessageBank g_msg_bank;
static const char *g_messages_path = MPC_BOOT_MESSAGES_FILE;
static int g_msg_loaded = 0;

static void ensure_msg_bank(void) {
    if (!g_msg_loaded) {
        boot_load_message_bank(&g_msg_bank, g_messages_path);
        g_msg_loaded = 1;
    }
}

#ifdef __linux__
static void write_pid_file(void) {
    FILE *f = fopen(MPC_BOOT_PID_FILE, "w");
    if (f) {
        fprintf(f, "%d\n", (int)getpid());
        fclose(f);
    }
}

static void remove_pid_file(void) {
    unlink(MPC_BOOT_PID_FILE);
}
#endif

static int run_dump_frame0(const char *out_path, float progress, const char *stage) {
    ensure_msg_bank();
    char auto_stage[MPC_BOOT_MSG_LEN];
    char splash[MPC_BOOT_MSG_LEN];
    char tip[MPC_BOOT_MSG_LEN];
    boot_select_from_bank(&g_msg_bank, 1U, 0.0f, progress, NULL,
                          auto_stage, sizeof(auto_stage),
                          splash, sizeof(splash),
                          tip, sizeof(tip));
    const char *final_stage = (stage && stage[0]) ? stage : auto_stage;
    boot_render_frame_full(g_canvas, progress, 0.0f, 0.0f, final_stage, splash, tip, 1.0f, 1U);
    if (boot_write_acv8_bgra(out_path, g_canvas) != 0) {
        fprintf(stderr, "mpc_boot_anim: failed writing ACV8 BGRA to %s\n", out_path);
        return 1;
    }
    fprintf(stderr, "mpc_boot_anim: wrote ACV8 BGRA (%ux%u = %u bytes) to %s\n",
            MPC_PHYS_W, MPC_PHYS_H, (unsigned)(MPC_PHYS_W * MPC_PHYS_H * 4), out_path);
    return 0;
}

static void print_ansi_frame(float progress, float anim_t, float elapsed_s, const char *stage, int clear_screen) {
    ensure_msg_bank();
    if (clear_screen) {
        printf("\x1b[H");
    }
    int pct_int = (int)(progress * 100.0f + 0.5f);
    if (pct_int > 100) pct_int = 100;
    int wink_mode = (progress >= 0.99f) ? 1 : 0;

    // Compact 88-col centered slice of the 124-col grid so it fits nicely in terminals
    const int c_start = 18;
    const int c_end = 106;
    for (int row = 2; row < 34; row++) {
        // Every 2nd row gives true 1:1 square aspect ratio in terminals
        if ((row & 1) == 1) continue;
        for (int col = c_start; col < c_end; col++) {
            BootCell c = boot_eval_face_cell(col, row, progress, anim_t, wink_mode);
            if (c.ch <= ' ') {
                putchar(' ');
            } else {
                printf("\x1b[38;2;%u;%u;%um%c", c.r, c.g, c.b, c.ch);
            }
        }
        printf("\x1b[0m\n");
    }

    char bank_stage[MPC_BOOT_MSG_LEN];
    char splash[MPC_BOOT_MSG_LEN];
    char tip[MPC_BOOT_MSG_LEN];
    boot_select_from_bank(&g_msg_bank, 1U, anim_t, progress, NULL,
                          bank_stage, sizeof(bank_stage),
                          splash, sizeof(splash),
                          tip, sizeof(tip));
    const char *quote = wink_mode ? "BEAT MACHINE ARMED -- DROP THE NEEDLE!" : splash;
    const char *active_stage = (stage && stage[0]) ? stage : bank_stage;

    int qlen = (int)strlen(quote);
    int pad = (88 - qlen) / 2;
    if (pad < 2) pad = 2;
    for (int i = 0; i < pad; i++) putchar(' ');
    if (wink_mode) {
        printf("\x1b[38;2;130;255;160m%s\x1b[0m\n", quote);
    } else {
        printf("\x1b[38;2;255;224;64m%s\x1b[0m\n", quote);
    }
    printf("\x1b[38;2;130;210;230m  TIP: %s\x1b[0m\n", tip);

    int bar_w = 54;
    int filled = (int)(progress * (float)bar_w + 0.5f);
    printf("\x1b[38;2;200;210;225m  MPC LIVE // HAKAI OS   ELAPSED %4.1fs\x1b[0m\n  [", elapsed_s);
    for (int i = 0; i < bar_w; i++) {
        if (i < filled) {
            if (pct_int >= 100) printf("\x1b[38;2;80;240;140m@");
            else if (i == filled - 1) printf("\x1b[38;2;255;255;255m>");
            else printf("\x1b[38;2;225;36;76m@");
        } else {
            printf("\x1b[38;2;75;78;88m:");
        }
    }
    printf("\x1b[0m] [%3d%%]\n  > %s\n", pct_int, active_stage);
    fflush(stdout);
}

static int run_ansi_preview(float duration_s, int steps, int single_progress_flag, float single_progress) {
    ensure_msg_bank();
    if (single_progress_flag) {
        print_ansi_frame(single_progress, single_progress * 18.0f, single_progress * 23.0f,
                         single_progress >= 0.99f ? "MPC LIVE READY" : NULL, 0);
        return 0;
    }
    if (steps < 2) steps = 40;
    useconds_t delay_us = (useconds_t)((duration_s / (float)steps) * 1e6f);
    if (delay_us < 15000) delay_us = 15000;
    printf("\x1b[2J");
    for (int i = 0; i <= steps && !g_stop; i++) {
        float p = (float)i / (float)steps;
        float t = p * duration_s * 2.5f;
        print_ansi_frame(p, t, p * 23.0f, (p >= 0.99f) ? "MPC LIVE READY" : NULL, 1);
        if (i < steps) usleep(delay_us);
    }
    printf("\x1b[0m\n");
    return 0;
}

static int run_fb_daemon(int force_warm, int daemonize, float max_wait_s) {
#ifndef __linux__
    (void)force_warm; (void)daemonize; (void)max_wait_s;
    fprintf(stderr, "mpc_boot_anim: --fb is only supported on Linux /dev/fb0\n");
    return 1;
#else
    if (daemonize) {
        pid_t pid = fork();
        if (pid < 0) return 1;
        if (pid > 0) return 0; // Parent returns immediately
        setsid();
    }

    int fd = open("/dev/fb0", O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        fprintf(stderr, "mpc_boot_anim: cannot open /dev/fb0: %s\n", strerror(errno));
        return 1;
    }

    struct fb_fix_screeninfo finfo;
    struct fb_var_screeninfo vinfo;
    memset(&finfo, 0, sizeof(finfo));
    memset(&vinfo, 0, sizeof(vinfo));
    if (ioctl(fd, FBIOGET_FSCREENINFO, &finfo) != 0 || ioctl(fd, FBIOGET_VSCREENINFO, &vinfo) != 0) {
        fprintf(stderr, "mpc_boot_anim: fb ioctl failed: %s\n", strerror(errno));
        close(fd);
        return 1;
    }

    if (vinfo.xres != MPC_PHYS_W || vinfo.yres != MPC_PHYS_H || vinfo.bits_per_pixel != 32) {
        fprintf(stderr, "mpc_boot_anim: unexpected fb geometry %ux%u %ubpp\n",
                vinfo.xres, vinfo.yres, vinfo.bits_per_pixel);
        close(fd);
        return 1;
    }

    size_t fb_size = finfo.smem_len ? (size_t)finfo.smem_len : (size_t)finfo.line_length * vinfo.yres;
    uint32_t *fb_ptr = (uint32_t *)mmap(NULL, fb_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (fb_ptr == MAP_FAILED) {
        fprintf(stderr, "mpc_boot_anim: fb mmap failed: %s\n", strerror(errno));
        close(fd);
        return 1;
    }

    int stride_u32 = (int)(finfo.line_length / 4U);
    if (stride_u32 < MPC_PHYS_W) stride_u32 = MPC_PHYS_W;

    write_pid_file();
    if (force_warm) {
        unlink(MPC_BOOT_SHM_STATE);
        unlink(MPC_BOOT_SHM_DRM_ACTIVE);
    }

    BootTimingProfile prof;
    boot_load_timing_profile(&prof, MPC_BOOT_TIMING_FILE);

    BootShmState st;
    boot_init_or_resume_state(&st, MPC_BOOT_SHM_STATE, !force_warm);
    if (force_warm) {
        st.is_warm = 1;
        boot_write_shm_state(&st, MPC_BOOT_SHM_STATE);
    }

    ensure_msg_bank();
    double start_wall = boot_now_mono_s();
    double next_deadline = 0.0;

    while (!g_stop) {
        if (access(MPC_BOOT_SHM_DRM_ACTIVE, F_OK) == 0) {
            break; // DRM plane[38] splash overlay inside /usr/bin/MPC has seamlessly taken over!
        }

        double now_m = boot_now_mono_s();
        if ((float)(now_m - start_wall) >= max_wait_s) {
            break;
        }

        float elapsed_s = 0.0f;
        char fallback_stage[96];
        float progress = boot_eval_progress_and_stage(&st, &prof, 0, 0, &elapsed_s, fallback_stage, sizeof(fallback_stage));
        // Unified anim_t directly equals elapsed_s from BootShmState so DRM handoff has 0.0ms jump!
        float anim_t = elapsed_s;

        char stage[MPC_BOOT_MSG_LEN];
        char splash[MPC_BOOT_MSG_LEN];
        char tip[MPC_BOOT_MSG_LEN];
        boot_select_from_bank(&g_msg_bank, st.splash_seed, anim_t, progress, NULL,
                              stage, sizeof(stage), splash, sizeof(splash), tip, sizeof(tip));

        boot_render_frame_full(g_canvas, progress, anim_t, elapsed_s, stage, splash, tip, 1.0f, st.splash_seed);
        boot_rotate_canvas_to_phys(g_canvas, fb_ptr, stride_u32);

        boot_pace_frame(&next_deadline, 1.0 / 30.0); // Locked smooth 30 FPS
    }

    munmap(fb_ptr, fb_size);
    close(fd);
    remove_pid_file();
    return 0;
#endif
}

int main(int argc, char **argv) {
    signal(SIGTERM, handle_sig);
    signal(SIGINT, handle_sig);

    int mode_fb = 0;
    int mode_ansi = 0;
    const char *dump_path = NULL;
    float dump_progress = 0.0f;
    const char *dump_stage = NULL;
    int force_warm = 0;
    int daemonize = 0;
    float max_wait = 38.0f;
    float ansi_dur = 4.0f;
    int ansi_steps = 36;
    int single_prog_flag = 0;
    float single_prog = 0.65f;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--fb") == 0) {
            mode_fb = 1;
        } else if (strcmp(argv[i], "--bg") == 0) {
            daemonize = 1;
        } else if (strcmp(argv[i], "--warm") == 0) {
            force_warm = 1;
        } else if (strcmp(argv[i], "--max-wait") == 0 && i + 1 < argc) {
            max_wait = strtof(argv[++i], NULL);
        } else if (strcmp(argv[i], "--ansi") == 0) {
            mode_ansi = 1;
        } else if (strcmp(argv[i], "--duration") == 0 && i + 1 < argc) {
            ansi_dur = strtof(argv[++i], NULL);
        } else if (strcmp(argv[i], "--steps") == 0 && i + 1 < argc) {
            ansi_steps = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--progress") == 0 && i + 1 < argc) {
            single_prog = strtof(argv[++i], NULL);
            dump_progress = single_prog;
            single_prog_flag = 1;
        } else if (strcmp(argv[i], "--stage") == 0 && i + 1 < argc) {
            dump_stage = argv[++i];
        } else if (strcmp(argv[i], "--dump-frame0") == 0 && i + 1 < argc) {
            dump_path = argv[++i];
        } else if (strcmp(argv[i], "--messages") == 0 && i + 1 < argc) {
            g_messages_path = argv[++i];
        } else if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            printf("Usage:\n"
                   "  mpc_boot_anim --fb [--bg] [--warm] [--max-wait SEC] [--messages JSON]\n"
                   "  mpc_boot_anim --dump-frame0 OUT.bgra [--progress F] [--stage STR] [--messages JSON]\n"
                   "  mpc_boot_anim --ansi [--duration SEC] [--steps N] [--progress F] [--messages JSON]\n");
            return 0;
        }
    }

    if (dump_path) {
        return run_dump_frame0(dump_path, dump_progress, dump_stage);
    }
    if (mode_fb) {
        return run_fb_daemon(force_warm, daemonize, max_wait);
    }
    if (mode_ansi || argc == 1) {
        return run_ansi_preview(ansi_dur, ansi_steps, single_prog_flag, single_prog);
    }
    return 0;
}

#!/bin/sh
LOG_FILE="/media/HAKAI/boot_script.log"

BOOT_ANIM_BIN="/data/mpc-control/mpc_boot_anim"
if [ -x "$BOOT_ANIM_BIN" ]; then
    ANIM_RUNNING=0
    if [ -f "/dev/shm/.mpc_boot_anim.pid" ]; then
        ANIM_PID=$(cat /dev/shm/.mpc_boot_anim.pid 2>/dev/null || true)
        [ -n "$ANIM_PID" ] && kill -0 "$ANIM_PID" 2>/dev/null && ANIM_RUNNING=1
    fi
    if [ "$ANIM_RUNNING" -eq 0 ]; then
        rm -f /dev/shm/.mpc_boot_drm_active
        UP_INT=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 99)
        if [ "${UP_INT:-99}" -ge 16 ] 2>/dev/null; then
            "$BOOT_ANIM_BIN" --fb --bg --warm >/tmp/mpc_boot_anim.log 2>&1 || true
        else
            "$BOOT_ANIM_BIN" --fb --bg >/tmp/mpc_boot_anim.log 2>&1 || true
        fi
    fi
fi

DFUUTILINFO=$(dfu-util -l | grep "0x08000000")
[ "x$DFUUTILINFO" != "x" ] && sh /usr/share/Akai/SME0/Firmware/update.sh

/usr/share/Hakai/hakai-init

CURSOR_SO=""
if [ -f "/dev/shm/.mouseCursor" ]; then
    MOUSE_DEV=$(awk 'NR==1{print;exit}' /dev/shm/.mouseCursor)
    if [ -n "$MOUSE_DEV" ] && [ -e "$MOUSE_DEV" ]; then
        CURSOR_SO="/usr/lib/mpc_cursor.so"
    fi
fi

HW_BRIDGE_SO=""
if [ -f "/data/mpc-control/libmpc_hw_bridge.so" ]; then
    HW_BRIDGE_SO="/data/mpc-control/libmpc_hw_bridge.so"
fi

BOOT_SPLASH_SO=""
if [ -f "/data/mpc-control/libmpc_boot_splash.so" ]; then
    BOOT_SPLASH_SO="/data/mpc-control/libmpc_boot_splash.so"
fi

EXTRA_PRELOAD=$(echo "${LD_PRELOAD:-}" | tr ":" " ")

if [ -L "/dev/disk/by-label/HAKAI" ]; then
    BASE_PRELOAD="$HW_BRIDGE_SO $BOOT_SPLASH_SO /usr/lib/hakai_driver.so /usr/lib/customBufferSizeMPC.so $CURSOR_SO $EXTRA_PRELOAD"
else
    BASE_PRELOAD="$HW_BRIDGE_SO $BOOT_SPLASH_SO /usr/lib/customBufferSizeMPC.so $CURSOR_SO $EXTRA_PRELOAD"
fi

if type systemd-inhibit >/dev/null 2>&1; then
    export LD_PRELOAD="$BASE_PRELOAD"
    exec systemd-inhibit --what=handle-power-key /usr/bin/MPC "$@"
else
    ulimit -S -s 1024
    export LD_PRELOAD="$BASE_PRELOAD"
    export MALLOC_ARENA_MAX=1
    exec setarch -R -- /usr/bin/MPC "$@"
fi

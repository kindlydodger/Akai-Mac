#!/bin/sh
LOG_FILE="/media/HAKAI/boot_script.log"
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

EXTRA_PRELOAD=$(echo "${LD_PRELOAD:-}" | tr ":" " ")

if [ -L "/dev/disk/by-label/HAKAI" ]; then
    BASE_PRELOAD="$HW_BRIDGE_SO /usr/lib/hakai_driver.so /usr/lib/customBufferSizeMPC.so $CURSOR_SO $EXTRA_PRELOAD"
else
    BASE_PRELOAD="$HW_BRIDGE_SO /usr/lib/customBufferSizeMPC.so $CURSOR_SO $EXTRA_PRELOAD"
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

#!/bin/bash

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/data/logs/npu_monitor.log"
STARTUP_LOG="$SCRIPT_DIR/data/logs/monitor.out"
PID_FILE="$SCRIPT_DIR/data/npu_monitor.pid"

mkdir -p "$SCRIPT_DIR/data/logs" "$SCRIPT_DIR/data/daily"

inside_container() {
    [ -f /.dockerenv ] && return 0
    grep -qaE '(docker|containerd|kubepods|lxc)' /proc/1/cgroup 2>/dev/null
}

pid1_can_reap_children() {
    local pid1_comm pid1_args
    pid1_comm="$(cat /proc/1/comm 2>/dev/null || true)"
    pid1_args="$(tr '\0' ' ' </proc/1/cmdline 2>/dev/null || true)"
    case "$pid1_args" in
        *mini_init.py*) return 0 ;;
    esac
    case "$pid1_comm" in
        init|systemd|tini|docker-init|dumb-init|supervisord|s6-svscan|runsvdir) return 0 ;;
        *) return 1 ;;
    esac
}

if inside_container && ! pid1_can_reap_children; then
    if [ "${NPU_MONITOR_ALLOW_UNSAFE_BACKGROUND:-0}" != "1" ]; then
        echo "Refusing unsafe background start inside this container."
        echo "Container PID 1 ($(cat /proc/1/comm 2>/dev/null || echo unknown)) is not a known child reaper."
        echo "Starting with nohup here creates unreaped Python zombie processes."
        echo "Recreate the container with deploy/mini_init.py as PID 1 or run:"
        echo "  exec python3 -u $SCRIPT_DIR/npu_monitor.py"
        exit 2
    fi
    echo "WARNING: unsafe background mode explicitly enabled; zombie processes may remain."
fi

process_state() {
    ps -o stat= -p "$1" 2>/dev/null | awk '{print substr($1, 1, 1)}'
}

is_monitor_process() {
    local pid="$1" state args
    state="$(process_state "$pid")"
    [ -n "$state" ] && [ "$state" != "Z" ] || return 1
    args="$(ps -o args= -p "$pid" 2>/dev/null)"
    case "$args" in
        *npu_monitor.py*--pid-file*"$PID_FILE"*) return 0 ;;
        *) return 1 ;;
    esac
}

remove_owned_pid() {
    local owner
    owner="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ "$owner" = "$1" ]; then
        rm -f "$PID_FILE"
    fi
}

if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ "$PID" =~ ^[0-9]+$ ]] && is_monitor_process "$PID"; then
        echo "NPU Monitor is already running (PID: $PID)"
        exit 1
    fi
    echo "Removing stale PID file"
    rm -f "$PID_FILE"
fi

echo "Starting NPU Monitor..."
cd "$SCRIPT_DIR" || exit 1
nohup python3 -u npu_monitor.py --pid-file "$PID_FILE" >> "$STARTUP_LOG" 2>&1 &
PID=$!

for _ in $(seq 1 30); do
    if [ -f "$PID_FILE" ] && is_monitor_process "$PID"; then
        echo "NPU Monitor started successfully (PID: $PID)"
        echo "Log file: $LOG_FILE"
        echo "Data directory: $SCRIPT_DIR/data/daily"
        exit 0
    fi
    if ! kill -0 "$PID" 2>/dev/null; then
        break
    fi
    sleep 0.1
done

remove_owned_pid "$PID"
echo "Failed to start NPU Monitor"
echo "Check log: $LOG_FILE"
exit 1

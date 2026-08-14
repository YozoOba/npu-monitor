#!/bin/bash

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/data/npu_monitor.pid"

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

remove_stale_pid() {
    rm -f "$PID_FILE"
    echo "NPU Monitor is not running (stale PID file removed)"
}

warn_if_zombie() {
    local pid="$1" state parent
    state="$(ps -o stat= -p "$pid" 2>/dev/null | awk '{print substr($1, 1, 1)}')"
    parent="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
    if [ "$state" = "Z" ]; then
        echo "WARNING: PID $pid is a zombie owned by PPID ${parent:-unknown}."
        echo "Only its parent can reap it; if PPID is 1, recreate the container with --init."
    fi
}

if [ ! -f "$PID_FILE" ]; then
    echo "NPU Monitor is not running"
    exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if ! [[ "$PID" =~ ^[0-9]+$ ]]; then
    remove_stale_pid
    exit 0
fi
if ! is_monitor_process "$PID"; then
    warn_if_zombie "$PID"
    remove_stale_pid
    exit 0
fi

echo "Stopping NPU Monitor (PID: $PID)..."
kill -TERM "$PID" 2>/dev/null || true

# npu-smi has a 10-second timeout. Allow it to return before using SIGKILL.
for _ in $(seq 1 150); do
    if ! is_monitor_process "$PID"; then
        warn_if_zombie "$PID"
        rm -f "$PID_FILE"
        echo "NPU Monitor stopped"
        exit 0
    fi
    sleep 0.1
done

echo "Graceful shutdown timed out; force stopping..."
kill -KILL "$PID" 2>/dev/null || true
for _ in $(seq 1 30); do
    if ! is_monitor_process "$PID"; then
        warn_if_zombie "$PID"
        rm -f "$PID_FILE"
        python3 - "$SCRIPT_DIR/data/health.json" <<'PY' 2>/dev/null || true
import json, os, sys
path = sys.argv[1]
temp = path + '.stop.tmp'
with open(temp, 'w', encoding='utf-8') as handle:
    json.dump({'status': 'stopped', 'forced': True}, handle)
    handle.write('\n')
os.replace(temp, path)
PY
        echo "NPU Monitor stopped"
        exit 0
    fi
    sleep 0.1
done

echo "Failed to stop NPU Monitor (PID: $PID)"
exit 1

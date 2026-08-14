#!/bin/bash

set -eu

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_DIR="$(mktemp -d)"
SLEEP_PID=""

cleanup() {
    if [ -n "$SLEEP_PID" ]; then
        kill "$SLEEP_PID" 2>/dev/null || true
    fi
    if [ -f "$TEST_DIR/data/npu_monitor.pid" ]; then
        PID="$(cat "$TEST_DIR/data/npu_monitor.pid" 2>/dev/null || true)"
        if [[ "$PID" =~ ^[0-9]+$ ]]; then
            kill "$PID" 2>/dev/null || true
        fi
    fi
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT

mkdir -p "$TEST_DIR/bin"
cp "$SOURCE_DIR"/*.py "$TEST_DIR/"
cp "$SOURCE_DIR"/start.sh "$SOURCE_DIR"/stop.sh "$SOURCE_DIR"/status.sh "$TEST_DIR/"
cp "$SOURCE_DIR/tests/fake_npu_smi.sh" "$TEST_DIR/bin/npu-smi"
chmod +x "$TEST_DIR"/*.sh "$TEST_DIR/bin/npu-smi"

export PATH="$TEST_DIR/bin:$PATH"
export NPU_MONITOR_COLLECT_INTERVAL=1
export NPU_MONITOR_EXPECTED_NPU_COUNT=8
export NPU_MONITOR_COMMAND_TIMEOUT=2
export NPU_MONITOR_MIN_FREE_BYTES=0
export NPU_MONITOR_MIN_FREE_INODES=0

cd "$TEST_DIR"
./start.sh
sleep 2
./status.sh

if ./start.sh; then
    echo "FAILED: concurrent start was accepted" >&2
    exit 1
fi

./stop.sh
./status.sh
test ! -e data/npu_monitor.pid

# A stale PID pointing at an unrelated process must never be killed.
sleep 30 &
SLEEP_PID=$!
echo "$SLEEP_PID" > data/npu_monitor.pid
./stop.sh
kill -0 "$SLEEP_PID"
test ! -e data/npu_monitor.pid

echo "Lifecycle tests passed"

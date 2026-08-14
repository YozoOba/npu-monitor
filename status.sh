#!/bin/bash

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/data/npu_monitor.pid"
HEALTH_FILE="$SCRIPT_DIR/data/health.json"

show_health() {
    if [ ! -f "$HEALTH_FILE" ]; then
        echo "Collector: UNKNOWN (no health data)"
        return
    fi
    python3 - "$HEALTH_FILE" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding='utf-8') as handle:
        health = json.load(handle)
except (OSError, ValueError) as exc:
    print('Collector: UNKNOWN ({})'.format(exc))
else:
    print('Collector: {}'.format(str(health.get('status', 'unknown')).upper()))
    if 'collected_cards' in health:
        print('Cards: {}/{} ({}% coverage)'.format(
            health.get('collected_cards', 0),
            health.get('expected_cards', '?'),
            health.get('coverage_percent', 0),
        ))
    if health.get('missing_card_ids'):
        print('Missing cards: {}'.format(','.join(
            str(value) for value in health['missing_card_ids']
        )))
    if health.get('last_success'):
        print('Last success: {}'.format(health['last_success']))
    if 'consecutive_failures' in health:
        print('Consecutive failures: {}'.format(health['consecutive_failures']))
    if health.get('last_error'):
        print('Last error: {}'.format(health['last_error']))
PY
}

echo "NPU Monitor Status"
echo "=================="

show_zombie_warning() {
    local zombie_count
    zombie_count="$(ps -eo ppid=,stat=,comm= 2>/dev/null | awk '$1 == 1 && $2 ~ /^Z/ && $3 ~ /python/ {count++} END {print count+0}')"
    if [ "$zombie_count" -gt 0 ]; then
        echo "WARNING: $zombie_count Python zombie process(es) are owned by container PID 1."
        echo "They cannot be killed; recreate the container with docker run --init."
    fi
}

if [ ! -f "$PID_FILE" ]; then
    echo "Status: STOPPED"
    show_health
    show_zombie_warning
    exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
STATE=""
ARGS=""
if [[ "$PID" =~ ^[0-9]+$ ]]; then
    STATE="$(ps -o stat= -p "$PID" 2>/dev/null | awk '{print substr($1, 1, 1)}')"
    ARGS="$(ps -o args= -p "$PID" 2>/dev/null)"
fi

case "$ARGS" in
    *npu_monitor.py*--pid-file*"$PID_FILE"*) IS_MONITOR=1 ;;
    *) IS_MONITOR=0 ;;
esac

if [ -z "$STATE" ] || [ "$STATE" = "Z" ] || [ "$IS_MONITOR" -ne 1 ]; then
    echo "Status: STOPPED (stale PID file removed)"
    rm -f "$PID_FILE"
    show_health
    show_zombie_warning
    exit 0
fi

echo "Status: RUNNING"
echo "PID: $PID"
echo ""
echo "Process Info:"
ps -p "$PID" -o pid,ppid,stat,etime,cmd

echo ""
show_health
show_zombie_warning

echo ""
echo "Data Files:"
DAILY_DIR="$SCRIPT_DIR/data/daily"
FILE_COUNT="$(find "$DAILY_DIR" -maxdepth 1 -type f -name '*.csv' 2>/dev/null | wc -l)"
echo "  CSV files: $FILE_COUNT"
LATEST="$(ls -1t "$DAILY_DIR"/stats_*.csv 2>/dev/null | head -1)"
if [ -n "$LATEST" ]; then
    LINES="$(wc -l < "$LATEST")"
    SIZE="$(du -h "$LATEST" | awk '{print $1}')"
    echo "  Latest file: $(basename "$LATEST") ($LINES lines, $SIZE)"
fi

#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=container_common.sh
source "$SCRIPT_DIR/container_common.sh"

if [[ $# -ne 2 ]]; then
    echo "Usage: ./deploy/create_collector_container.sh IMAGE_ID_OR_NAME HOST_PROJECT_DIR" >&2
    exit 2
fi

IMAGE_REF="$1"
HOST_PROJECT_DIR="$(resolve_project_dir "$2")"
CONTAINER_NAME="${NPU_COLLECTOR_CONTAINER:-npu-monitor-collector}"
RESTART_POLICY="${NPU_MONITOR_RESTART_POLICY:-unless-stopped}"
STOP_TIMEOUT="${NPU_MONITOR_STOP_TIMEOUT:-30}"
LOG_MAX_SIZE="${NPU_MONITOR_LOG_MAX_SIZE:-20m}"
LOG_MAX_FILES="${NPU_MONITOR_LOG_MAX_FILES:-5}"
INIT_KILL_AFTER="${NPU_MONITOR_INIT_KILL_AFTER:-20}"
COLLECTOR_PORT="${NPU_COLLECTOR_PORT:-18080}"

require_docker
require_image "$IMAGE_REF"
require_new_container_name "$CONTAINER_NAME"
require_project_files "$HOST_PROJECT_DIR" \
    collector/app.py collector/healthcheck.py cluster_common/protocol.py deploy/mini_init.py
mkdir -p "$HOST_PROJECT_DIR/runtime-data/collector"

docker_args=(
    run -d
    --name "$CONTAINER_NAME"
    --restart "$RESTART_POLICY"
    --stop-timeout "$STOP_TIMEOUT"
    --log-driver json-file
    --log-opt "max-size=$LOG_MAX_SIZE"
    --log-opt "max-file=$LOG_MAX_FILES"
    --net=host
    --workdir /work/monitor
    --entrypoint python3
    --health-cmd "cd /work/monitor && python3 -m collector.healthcheck"
    --health-interval 30s
    --health-timeout 10s
    --health-retries 3
    -e PYTHONPATH=/work/monitor
    -e "NPU_MONITOR_INIT_KILL_AFTER=$INIT_KILL_AFTER"
    -e NPU_COLLECTOR_DATA_DIR=/work/monitor/runtime-data/collector
    -e "NPU_COLLECTOR_PORT=$COLLECTOR_PORT"
    -v "$HOST_PROJECT_DIR:/work/monitor"
)

optional_collector_variables=(
    NPU_COLLECTOR_HOST
    NPU_COLLECTOR_RETENTION_DAYS
    NPU_COLLECTOR_ARCHIVE_DIR
    NPU_COLLECTOR_BACKUP_DIR
    NPU_COLLECTOR_STALE_SECONDS
    NPU_COLLECTOR_OFFLINE_SECONDS
    NPU_COLLECTOR_MAX_FUTURE_SECONDS
    NPU_COLLECTOR_MAX_BODY_BYTES
    NPU_COLLECTOR_SNAPSHOT_INTERVAL
    NPU_COLLECTOR_CLOCK_SKEW_WARN_SECONDS
    NPU_COLLECTOR_BUSY_UTILIZATION
    NPU_COLLECTOR_IDLE_UTILIZATION
    NPU_COLLECTOR_MIN_FREE_BYTES
    NPU_COLLECTOR_MIN_FREE_INODES
)
for variable_name in "${optional_collector_variables[@]}"; do
    if [[ -n "${!variable_name:-}" ]]; then
        docker_args+=(-e "$variable_name=${!variable_name}")
    fi
done

container_id="$(docker "${docker_args[@]}" "$IMAGE_REF" \
    /work/monitor/deploy/mini_init.py python3 -u -m collector.app)"

print_created "Collector" "$CONTAINER_NAME" "$container_id" "$IMAGE_REF" "$HOST_PROJECT_DIR"
echo "  endpoint:   http://127.0.0.1:$COLLECTOR_PORT"
echo
echo "Checks:"
echo "  docker logs --tail 100 $CONTAINER_NAME"
echo "  curl -sS http://127.0.0.1:$COLLECTOR_PORT/health"

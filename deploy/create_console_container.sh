#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=container_common.sh
source "$SCRIPT_DIR/container_common.sh"

if [[ $# -ne 2 ]]; then
    echo "Usage: ./deploy/create_console_container.sh IMAGE_ID_OR_NAME HOST_PROJECT_DIR" >&2
    exit 2
fi

IMAGE_REF="$1"
HOST_PROJECT_DIR="$(resolve_project_dir "$2")"
CONTAINER_NAME="${NPU_CONSOLE_CONTAINER:-npu-monitor-console}"
RESTART_POLICY="${NPU_MONITOR_RESTART_POLICY:-unless-stopped}"
STOP_TIMEOUT="${NPU_MONITOR_STOP_TIMEOUT:-30}"
LOG_MAX_SIZE="${NPU_MONITOR_LOG_MAX_SIZE:-20m}"
LOG_MAX_FILES="${NPU_MONITOR_LOG_MAX_FILES:-5}"
INIT_KILL_AFTER="${NPU_MONITOR_INIT_KILL_AFTER:-20}"
CONSOLE_PORT="${NPU_CONSOLE_PORT:-18081}"
COLLECTOR_URL="${NPU_CONSOLE_COLLECTOR_URL:-http://127.0.0.1:18080}"

require_docker
require_image "$IMAGE_REF"
require_new_container_name "$CONTAINER_NAME"
require_project_files "$HOST_PROJECT_DIR" \
    console/web.py console/client.py deploy/mini_init.py

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
    --health-cmd "python3 -c \"from urllib.request import urlopen; urlopen('http://127.0.0.1:$CONSOLE_PORT/health', timeout=3).read()\""
    --health-interval 30s
    --health-timeout 10s
    --health-retries 3
    -e PYTHONPATH=/work/monitor
    -e "NPU_MONITOR_INIT_KILL_AFTER=$INIT_KILL_AFTER"
    -e "NPU_CONSOLE_COLLECTOR_URL=$COLLECTOR_URL"
    -e "NPU_CONSOLE_PORT=$CONSOLE_PORT"
    -e "NPU_CONSOLE_HTTP_TIMEOUT=${NPU_CONSOLE_HTTP_TIMEOUT:-10}"
    -v "$HOST_PROJECT_DIR:/work/monitor"
)

container_id="$(docker "${docker_args[@]}" "$IMAGE_REF" \
    /work/monitor/deploy/mini_init.py python3 -u -m console.web)"

print_created "Console" "$CONTAINER_NAME" "$container_id" "$IMAGE_REF" "$HOST_PROJECT_DIR"
echo "  endpoint:   http://127.0.0.1:$CONSOLE_PORT"
echo "  collector:  $COLLECTOR_URL"
echo
echo "Checks:"
echo "  docker logs --tail 100 $CONTAINER_NAME"
echo "  curl -sS http://127.0.0.1:$CONSOLE_PORT/health"

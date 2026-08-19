#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=container_common.sh
source "$SCRIPT_DIR/container_common.sh"

usage() {
    cat <<'EOF'
Usage:
  ./deploy/create_agent_container.sh IMAGE_ID_OR_NAME HOST_PROJECT_DIR

Required environment:
  NPU_AGENT_COLLECTOR_URL

Recommended environment:
  NPU_AGENT_NODE_ID       Stable node ID (default: host short hostname)
  NPU_AGENT_NODE_NAME     Display name (default: host hostname)

Optional container settings:
  NPU_AGENT_CONTAINER             default: npu-monitor-agent
  NPU_MONITOR_SHM_SIZE            default: 500g
  NPU_MONITOR_RESTART_POLICY      default: unless-stopped
  NPU_MONITOR_STOP_TIMEOUT        default: 30 seconds
  NPU_MONITOR_LOG_MAX_SIZE        default: 20m
  NPU_MONITOR_LOG_MAX_FILES       default: 5
  NPU_MONITOR_INIT_KILL_AFTER     default: 20 seconds
EOF
}

if [[ $# -ne 2 ]]; then
    usage >&2
    exit 2
fi

: "${NPU_AGENT_COLLECTOR_URL:?Set NPU_AGENT_COLLECTOR_URL, for example http://192.168.10.20:18080}"

IMAGE_REF="$1"
HOST_PROJECT_DIR="$(resolve_project_dir "$2")"
CONTAINER_NAME="${NPU_AGENT_CONTAINER:-npu-monitor-agent}"
SHM_SIZE="${NPU_MONITOR_SHM_SIZE:-500g}"
RESTART_POLICY="${NPU_MONITOR_RESTART_POLICY:-unless-stopped}"
STOP_TIMEOUT="${NPU_MONITOR_STOP_TIMEOUT:-30}"
LOG_MAX_SIZE="${NPU_MONITOR_LOG_MAX_SIZE:-20m}"
LOG_MAX_FILES="${NPU_MONITOR_LOG_MAX_FILES:-5}"
INIT_KILL_AFTER="${NPU_MONITOR_INIT_KILL_AFTER:-20}"
HOST_SHORT_NAME="$(hostname -s 2>/dev/null || hostname)"
HOST_FULL_NAME="$(hostname)"

require_docker
require_image "$IMAGE_REF"
require_new_container_name "$CONTAINER_NAME"
require_project_files "$HOST_PROJECT_DIR" \
    agent/app.py agent/healthcheck.py cluster_common/protocol.py deploy/mini_init.py

required_host_paths=(
    /dev/davinci0
    /dev/davinci1
    /dev/davinci2
    /dev/davinci3
    /dev/davinci4
    /dev/davinci5
    /dev/davinci6
    /dev/davinci7
    /dev/davinci_manager
    /dev/devmm_svm
    /dev/hisi_hdc
    /usr/local/dcmi
    /usr/local/Ascend/driver/tools/hccn_tool
    /usr/local/bin/npu-smi
    /usr/local/Ascend/driver/lib64
    /usr/local/Ascend/driver/version.info
    /etc/ascend_install.info
    /etc/hccn.conf
    /mnt
)
require_host_paths "${required_host_paths[@]}"
mkdir -p "$HOST_PROJECT_DIR/runtime-data/agent"

docker_args=(
    run -d
    --name "$CONTAINER_NAME"
    --restart "$RESTART_POLICY"
    --stop-timeout "$STOP_TIMEOUT"
    --log-driver json-file
    --log-opt "max-size=$LOG_MAX_SIZE"
    --log-opt "max-file=$LOG_MAX_FILES"
    --privileged=true
    --net=host
    --shm-size="$SHM_SIZE"
    --workdir /work/monitor
    --entrypoint python3
    --health-cmd "cd /work/monitor && python3 -m agent.healthcheck"
    --health-interval 60s
    --health-timeout 10s
    --health-retries 3
    -e PYTHONPATH=/work/monitor
    -e "NPU_MONITOR_INIT_KILL_AFTER=$INIT_KILL_AFTER"
    -e "NPU_AGENT_COLLECTOR_URL=$NPU_AGENT_COLLECTOR_URL"
    -e "NPU_AGENT_NODE_ID=${NPU_AGENT_NODE_ID:-$HOST_SHORT_NAME}"
    -e "NPU_AGENT_NODE_NAME=${NPU_AGENT_NODE_NAME:-$HOST_FULL_NAME}"
    -e NPU_AGENT_DATA_DIR=/work/monitor/runtime-data/agent
)

for card_index in {0..7}; do
    docker_args+=(--device "/dev/davinci${card_index}")
done

docker_args+=(
    --device /dev/davinci_manager
    --device /dev/devmm_svm
    --device /dev/hisi_hdc
    -v /usr/local/dcmi:/usr/local/dcmi
    -v /usr/local/Ascend/driver/tools/hccn_tool:/usr/local/Ascend/driver/tools/hccn_tool
    -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi
    -v /usr/local/Ascend/driver/lib64:/usr/local/Ascend/driver/lib64
    -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info
    -v /etc/ascend_install.info:/etc/ascend_install.info
    -v /etc/hccn.conf:/etc/hccn.conf
    -v /mnt:/mnt
    -v "$HOST_PROJECT_DIR:/work/monitor"
)

optional_agent_variables=(
    NPU_AGENT_EXPECTED_CARDS
    NPU_AGENT_COLLECT_INTERVAL
    NPU_AGENT_COMMAND_TIMEOUT
    NPU_AGENT_HTTP_TIMEOUT
    NPU_AGENT_RETENTION_DAYS
    NPU_AGENT_SPOOL_RETENTION_DAYS
    NPU_AGENT_SPOOL_MAX_FILES
    NPU_AGENT_SPOOL_MAX_BYTES
    NPU_AGENT_UPLOAD_BATCH_SIZE
    NPU_AGENT_MIN_FREE_BYTES
    NPU_AGENT_MIN_FREE_INODES
    NPU_AGENT_NPU_SMI_BIN
)
for variable_name in "${optional_agent_variables[@]}"; do
    if [[ -n "${!variable_name:-}" ]]; then
        docker_args+=(-e "$variable_name=${!variable_name}")
    fi
done

container_id="$(docker "${docker_args[@]}" "$IMAGE_REF" \
    /work/monitor/deploy/mini_init.py python3 -u -m agent.app)"

print_created "Agent" "$CONTAINER_NAME" "$container_id" "$IMAGE_REF" "$HOST_PROJECT_DIR"
echo "  node_id:    ${NPU_AGENT_NODE_ID:-$HOST_SHORT_NAME}"
echo "  collector:  $NPU_AGENT_COLLECTOR_URL"
echo
echo "Checks:"
echo "  docker logs --tail 100 $CONTAINER_NAME"
echo "  docker exec $CONTAINER_NAME python3 -m agent.healthcheck"
echo "  docker exec $CONTAINER_NAME ps -o pid,ppid,stat,args -p 1"

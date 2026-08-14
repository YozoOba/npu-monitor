#!/usr/bin/env bash

set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage:
  ./deploy/create_npu_monitor_container.sh IMAGE_ID_OR_NAME HOST_PROJECT_DIR

Example:
  ./deploy/create_npu_monitor_container.sh 13315b656180 /work/monitor

Optional environment variables:
  NPU_MONITOR_CONTAINER_NAME  Container name (default: npu-monitor)
  NPU_MONITOR_SHM_SIZE        Shared memory size (default: 500g)
  NPU_MONITOR_RESTART_POLICY  Docker restart policy (default: unless-stopped)
  NPU_AGENT_COLLECTOR_URL     If set, run Agent as the container main process
  NPU_AGENT_NODE_ID           Stable node ID (default: host short hostname)
  NPU_AGENT_NODE_NAME         Display name (default: host hostname)
EOF
}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

if [[ $# -ne 2 ]]; then
    usage >&2
    exit 2
fi

IMAGE_REF="$1"
HOST_PROJECT_DIR="$2"
CONTAINER_NAME="${NPU_MONITOR_CONTAINER_NAME:-npu-monitor}"
SHM_SIZE="${NPU_MONITOR_SHM_SIZE:-500g}"
RESTART_POLICY="${NPU_MONITOR_RESTART_POLICY:-unless-stopped}"
HOST_SHORT_NAME="$(hostname -s 2>/dev/null || hostname)"
HOST_FULL_NAME="$(hostname)"

command -v docker >/dev/null 2>&1 || fail "docker is not installed or is not in PATH"
docker image inspect "$IMAGE_REF" >/dev/null 2>&1 || fail "image does not exist locally: $IMAGE_REF"

[[ -d "$HOST_PROJECT_DIR" ]] || fail "project directory does not exist: $HOST_PROJECT_DIR"
HOST_PROJECT_DIR="$(cd "$HOST_PROJECT_DIR" && pwd -P)"

required_project_files=(
    "agent/app.py"
    "collector/app.py"
    "console/cli.py"
    "cluster_common/protocol.py"
)

for relative_path in "${required_project_files[@]}"; do
    [[ -f "$HOST_PROJECT_DIR/$relative_path" ]] || \
        fail "not an npu-monitor project directory; missing: $HOST_PROJECT_DIR/$relative_path"
done

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    fail "container already exists: $CONTAINER_NAME (the script will not delete or replace it)"
fi

required_host_paths=(
    "/dev/davinci0"
    "/dev/davinci1"
    "/dev/davinci2"
    "/dev/davinci3"
    "/dev/davinci4"
    "/dev/davinci5"
    "/dev/davinci6"
    "/dev/davinci7"
    "/dev/davinci_manager"
    "/dev/devmm_svm"
    "/dev/hisi_hdc"
    "/usr/local/dcmi"
    "/usr/local/Ascend/driver/tools/hccn_tool"
    "/usr/local/bin/npu-smi"
    "/usr/local/Ascend/driver/lib64"
    "/usr/local/Ascend/driver/version.info"
    "/etc/ascend_install.info"
    "/etc/hccn.conf"
    "/mnt"
)

missing_paths=()
for host_path in "${required_host_paths[@]}"; do
    [[ -e "$host_path" ]] || missing_paths+=("$host_path")
done

if (( ${#missing_paths[@]} > 0 )); then
    echo "ERROR: required NPU device or driver paths are missing:" >&2
    printf '  %s\n' "${missing_paths[@]}" >&2
    exit 1
fi

docker_args=(
    run
    -itd
    --init
    --privileged=true
    --name "$CONTAINER_NAME"
    --net=host
    --shm-size="$SHM_SIZE"
    --restart "$RESTART_POLICY"
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

container_command=(/bin/bash)
container_mode="inspection shell"

if [[ -n "${NPU_AGENT_COLLECTOR_URL:-}" ]]; then
    mkdir -p "$HOST_PROJECT_DIR/runtime-data/agent"
    docker_args+=(
        -e "PYTHONPATH=/work/monitor"
        -e "NPU_AGENT_COLLECTOR_URL=$NPU_AGENT_COLLECTOR_URL"
        -e "NPU_AGENT_NODE_ID=${NPU_AGENT_NODE_ID:-$HOST_SHORT_NAME}"
        -e "NPU_AGENT_NODE_NAME=${NPU_AGENT_NODE_NAME:-$HOST_FULL_NAME}"
        -e "NPU_AGENT_DATA_DIR=/work/monitor/runtime-data/agent"
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

    container_command=(
        /bin/bash
        -lc
        "cd /work/monitor && exec python3 -u -m agent.app"
    )
    container_mode="Agent service"
fi

container_id="$(docker "${docker_args[@]}" "$IMAGE_REF" "${container_command[@]}")"

echo "Container created successfully."
echo "  name:       $CONTAINER_NAME"
echo "  id:         $container_id"
echo "  image:      $IMAGE_REF"
echo "  project:    $HOST_PROJECT_DIR -> /work/monitor"
echo "  mode:       $container_mode"
echo
echo "Next checks:"
echo "  docker exec $CONTAINER_NAME npu-smi info"
if [[ -n "${NPU_AGENT_COLLECTOR_URL:-}" ]]; then
    echo "  docker logs --tail 100 $CONTAINER_NAME"
    echo "  docker exec $CONTAINER_NAME python3 -m agent.healthcheck"
else
    echo "  docker exec -it $CONTAINER_NAME bash"
    echo
    echo "NPU_AGENT_COLLECTOR_URL was not set, so Agent was not started."
fi

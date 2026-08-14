#!/bin/bash
set -euo pipefail

: "${NPU_AGENT_NODE_ID:?Set a stable NPU_AGENT_NODE_ID, for example npu-node-01}"
: "${NPU_AGENT_COLLECTOR_URL:?Set NPU_AGENT_COLLECTOR_URL, for example http://192.168.1.10:18080}"
: "${NPU_AGENT_DATA_DIR:?Set NPU_AGENT_DATA_DIR to an absolute host directory}"
mkdir -p "$NPU_AGENT_DATA_DIR"

# Put the device/driver mounts already used by the working 910B container in
# NPU_AGENT_DOCKER_ARGS. Shell splitting is intentional for Docker arguments.
# shellcheck disable=SC2086
docker run -d \
  --name "${NPU_AGENT_CONTAINER:-npu-monitor-agent}" \
  --init \
  --restart unless-stopped \
  ${NPU_AGENT_DOCKER_ARGS:-} \
  -e NPU_AGENT_NODE_ID="$NPU_AGENT_NODE_ID" \
  -e NPU_AGENT_NODE_NAME="${NPU_AGENT_NODE_NAME:-$NPU_AGENT_NODE_ID}" \
  -e NPU_AGENT_COLLECTOR_URL="$NPU_AGENT_COLLECTOR_URL" \
  -e NPU_AGENT_EXPECTED_CARDS="${NPU_AGENT_EXPECTED_CARDS:-8}" \
  -e NPU_AGENT_COLLECT_INTERVAL="${NPU_AGENT_COLLECT_INTERVAL:-60}" \
  -v "$NPU_AGENT_DATA_DIR:/app/data" \
  "${NPU_AGENT_IMAGE:-npu-monitor-agent:1.0}"


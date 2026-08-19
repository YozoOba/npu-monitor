#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

echo "NOTICE: create_npu_monitor_container.sh is kept for compatibility." >&2
echo "        Use create_agent_container.sh for new deployments." >&2

if [[ -n "${NPU_MONITOR_CONTAINER_NAME:-}" && -z "${NPU_AGENT_CONTAINER:-}" ]]; then
    export NPU_AGENT_CONTAINER="$NPU_MONITOR_CONTAINER_NAME"
fi

exec "$SCRIPT_DIR/create_agent_container.sh" "$@"

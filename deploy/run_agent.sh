#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="${NPU_MONITOR_PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
: "${NPU_AGENT_IMAGE:?Set NPU_AGENT_IMAGE to an existing local image ID or name}"

echo "NOTICE: run_agent.sh is a compatibility wrapper." >&2
exec "$SCRIPT_DIR/create_agent_container.sh" "$NPU_AGENT_IMAGE" "$PROJECT_DIR"

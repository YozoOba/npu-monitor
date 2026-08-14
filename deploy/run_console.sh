#!/bin/bash
set -euo pipefail

: "${NPU_CONSOLE_COLLECTOR_URL:?Set NPU_CONSOLE_COLLECTOR_URL, for example http://192.168.1.10:18080}"

docker run -d \
  --name "${NPU_CONSOLE_CONTAINER:-npu-monitor-console}" \
  --init \
  --restart unless-stopped \
  -p "${NPU_CONSOLE_PUBLIC_PORT:-18081}:18081" \
  -e NPU_CONSOLE_COLLECTOR_URL="$NPU_CONSOLE_COLLECTOR_URL" \
  "${NPU_CONSOLE_IMAGE:-npu-monitor-console:1.0}"


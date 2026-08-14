#!/bin/bash
set -euo pipefail

: "${NPU_COLLECTOR_DATA_DIR:?Set NPU_COLLECTOR_DATA_DIR to an absolute host directory}"
mkdir -p "$NPU_COLLECTOR_DATA_DIR"

docker run -d \
  --name "${NPU_COLLECTOR_CONTAINER:-npu-monitor-collector}" \
  --init \
  --restart unless-stopped \
  -p "${NPU_COLLECTOR_PUBLIC_PORT:-18080}:18080" \
  -e NPU_COLLECTOR_RETENTION_DAYS="${NPU_COLLECTOR_RETENTION_DAYS:-180}" \
  -e NPU_COLLECTOR_STALE_SECONDS="${NPU_COLLECTOR_STALE_SECONDS:-120}" \
  -e NPU_COLLECTOR_OFFLINE_SECONDS="${NPU_COLLECTOR_OFFLINE_SECONDS:-300}" \
  -v "$NPU_COLLECTOR_DATA_DIR:/app/data" \
  "${NPU_COLLECTOR_IMAGE:-npu-monitor-collector:1.0}"


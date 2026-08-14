#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${NPU_AGENT_BASE_IMAGE:?Set NPU_AGENT_BASE_IMAGE to the existing 910B runtime image}"

docker build \
  --build-arg "BASE_IMAGE=${NPU_AGENT_BASE_IMAGE}" \
  -f "$ROOT_DIR/agent/Dockerfile" \
  -t "${NPU_AGENT_IMAGE:-npu-monitor-agent:1.0}" \
  "$ROOT_DIR"

docker build \
  --build-arg "BASE_IMAGE=${NPU_SERVICE_BASE_IMAGE:-python:3.11-slim}" \
  -f "$ROOT_DIR/collector/Dockerfile" \
  -t "${NPU_COLLECTOR_IMAGE:-npu-monitor-collector:1.0}" \
  "$ROOT_DIR"

docker build \
  --build-arg "BASE_IMAGE=${NPU_SERVICE_BASE_IMAGE:-python:3.11-slim}" \
  -f "$ROOT_DIR/console/Dockerfile" \
  -t "${NPU_CONSOLE_IMAGE:-npu-monitor-console:1.0}" \
  "$ROOT_DIR"

ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

WORKDIR /app
COPY cluster_common /app/cluster_common
COPY agent /app/agent
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NPU_AGENT_DATA_DIR=/app/data

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD ["python3", "-m", "agent.healthcheck"]

CMD ["python3", "-u", "-m", "agent.app"]


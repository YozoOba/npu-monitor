ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

WORKDIR /app
COPY cluster_common /app/cluster_common
COPY agent /app/agent
COPY deploy/mini_init.py /app/mini_init.py
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NPU_AGENT_DATA_DIR=/app/data

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD ["python3", "-m", "agent.healthcheck"]

ENTRYPOINT ["python3", "/app/mini_init.py"]
CMD ["python3", "-u", "-m", "agent.app"]

ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

WORKDIR /app
COPY cluster_common /app/cluster_common
COPY collector /app/collector
COPY deploy/mini_init.py /app/mini_init.py
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NPU_COLLECTOR_DATA_DIR=/app/data

EXPOSE 18080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD ["python3", "-m", "collector.healthcheck"]

ENTRYPOINT ["python3", "/app/mini_init.py"]
CMD ["python3", "-u", "-m", "collector.app"]

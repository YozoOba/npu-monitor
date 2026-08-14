ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

WORKDIR /app
COPY console /app/console

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 18081
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18081/health', timeout=3).read()"]

CMD ["python3", "-u", "-m", "console.web"]

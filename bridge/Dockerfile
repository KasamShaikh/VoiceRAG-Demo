FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Speech SDK native deps
RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        build-essential libssl-dev libasound2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY bridge/requirements.txt /app/bridge/requirements.txt
RUN pip install -r /app/bridge/requirements.txt

COPY bridge /app/bridge
COPY web    /app/web

EXPOSE 8000
CMD ["uvicorn", "bridge.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

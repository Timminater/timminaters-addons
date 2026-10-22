# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

LABEL io.hass.version="0.2.9" \
      io.hass.type="app" \
      io.hass.arch="aarch64|amd64"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /opt/stroomvoorspeller
COPY app ./app
COPY icon.png ./app/static/icon.png
COPY run.sh ./run.sh
RUN chmod 0755 ./run.sh && mkdir -p /data
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data \
    HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_XET=1
EXPOSE 8099
CMD ["./run.sh"]

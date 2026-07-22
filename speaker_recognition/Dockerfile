FROM python:3.11.13-slim-bookworm

ARG BUILD_VERSION=1.0.0
ARG BUILD_ARCH=amd64

LABEL \
    io.hass.name="Speaker Recognition" \
    io.hass.description="Local speaker enrollment and recognition with an ingress web interface" \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.source="https://github.com/Timminater/timminaters-addons" \
    org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PORT=8099

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 \
    && pip install -r requirements.txt

COPY app ./app
COPY web ./web
COPY integration ./integration
COPY LICENSE.md ./LICENSE.md
COPY THIRD_PARTY_NOTICES.md ./THIRD_PARTY_NOTICES.md

EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=5).read()"

CMD ["python", "-m", "app"]

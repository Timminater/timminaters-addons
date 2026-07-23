FROM python:3.11.13-slim-bookworm@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1

ARG BUILD_VERSION=1.0.0
ARG BUILD_ARCH=amd64
ARG DEEPFILTERNET2_REVISION=d375b2d8309e0935d165700c91da9de862a99c31
ARG DEEPFILTERNET2_SHA256=850e695d1c27f2a7b4dcd89b16bc8d49367effc680b1369e995e31d46376cba7
ARG SPEX_REVISION=2b99a144297ac4d074bb9dcc4ce9734a7e8924fd
ARG SPEX_SHA256=5ffcf87a45f46ece3fa43db5b4d7f9779a73392933fed0b563f0ead9bd9b492f

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

COPY requirements.txt ./

# Both checkpoints are embedded and checksum-verified so inference never
# downloads executable model data at runtime. Build-only packages are removed
# in this same layer, keeping compilers and download tools out of the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates curl libsndfile1 unzip \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 torchaudio==2.5.1 \
    && pip install -r requirements.txt \
    && curl -fsSL \
      "https://raw.githubusercontent.com/Rikorose/DeepFilterNet/${DEEPFILTERNET2_REVISION}/models/DeepFilterNet2.zip" \
      -o /tmp/deepfilternet2.zip \
    && echo "${DEEPFILTERNET2_SHA256}  /tmp/deepfilternet2.zip" | sha256sum -c - \
    && unzip -q /tmp/deepfilternet2.zip -d /opt/models \
    && mkdir -p /opt/models/spex \
    && curl -fsSL \
      "https://huggingface.co/alibabasglab/log_wsj0-2mix_speech_SpEx-plus_2spk/resolve/${SPEX_REVISION}/checkpoints/log_wsj0-2mix_speech_SpEx-plus_2spk/last_best_checkpoint.pt?download=true" \
      -o /opt/models/spex/last_best_checkpoint.pt \
    && echo "${SPEX_SHA256}  /opt/models/spex/last_best_checkpoint.pt" | sha256sum -c - \
    && rm -f /tmp/deepfilternet2.zip \
    && apt-get purge -y --auto-remove build-essential curl unzip \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY web ./web
COPY integration ./integration
COPY LICENSE.md ./LICENSE.md
COPY THIRD_PARTY_NOTICES.md ./THIRD_PARTY_NOTICES.md

EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=5).read()"

CMD ["python", "-m", "app"]

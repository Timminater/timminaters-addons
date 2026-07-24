FROM python:3.11.15-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

ARG BUILD_VERSION=1.0.0
ARG BUILD_ARCH=amd64
ARG DEEPFILTERNET2_REVISION=d375b2d8309e0935d165700c91da9de862a99c31
ARG DEEPFILTERNET2_SHA256=850e695d1c27f2a7b4dcd89b16bc8d49367effc680b1369e995e31d46376cba7
ARG PIPECAT_DF3_REVISION=212c7f684d41159b897a986ddfbb7ad667405ccd
ARG PIPECAT_DF3_SHA256=2064706cf5488e723a3404de2f0c6a559dffbf35daf9ede0b382e2ab32ef5a60

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
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    RAYON_NUM_THREADS=1 \
    DATA_DIR=/data \
    PORT=8099

WORKDIR /app

COPY requirements.txt ./

# The DeepFilterNet2 checkpoint is embedded and checksum-verified so inference
# never downloads model data at runtime. Build-only packages are removed in
# this same layer, keeping compilers and download tools out of the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates curl libsndfile1 unzip \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 torchaudio==2.5.1 \
    && pip install -r requirements.txt \
    && curl -fsSL \
      "https://github.com/vahidkowsari/pipecat-deepfilternet-stream/archive/${PIPECAT_DF3_REVISION}.tar.gz" \
      -o /tmp/pipecat-deepfilternet-stream.tar.gz \
    && echo "${PIPECAT_DF3_SHA256}  /tmp/pipecat-deepfilternet-stream.tar.gz" | sha256sum -c - \
    && mkdir -p /tmp/pipecat-deepfilternet-stream \
    && tar -xzf /tmp/pipecat-deepfilternet-stream.tar.gz \
      --strip-components=1 -C /tmp/pipecat-deepfilternet-stream \
    && pip install --no-deps /tmp/pipecat-deepfilternet-stream \
    && curl -fsSL \
      "https://raw.githubusercontent.com/Rikorose/DeepFilterNet/${DEEPFILTERNET2_REVISION}/models/DeepFilterNet2.zip" \
      -o /tmp/deepfilternet2.zip \
    && echo "${DEEPFILTERNET2_SHA256}  /tmp/deepfilternet2.zip" | sha256sum -c - \
    && unzip -q /tmp/deepfilternet2.zip -d /opt/models \
    && rm -rf /tmp/deepfilternet2.zip \
      /tmp/pipecat-deepfilternet-stream \
      /tmp/pipecat-deepfilternet-stream.tar.gz \
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

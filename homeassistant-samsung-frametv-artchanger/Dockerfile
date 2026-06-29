FROM ghcr.io/home-assistant/base@sha256:94ff231402a5e7ad2a82e261ad5fa4ffae7d7bb095c3febb2edbdf309c9b6aca

RUN apk add --no-cache python3 py3-pip git

RUN python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    python-multipart \
    pillow \
    git+https://github.com/NickWaterton/samsung-tv-ws-api.git

COPY run.sh /
COPY app /app
COPY web /web

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

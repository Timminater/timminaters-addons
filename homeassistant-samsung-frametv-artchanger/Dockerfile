ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip

RUN pip3 install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    python-multipart \
    pillow \
    git+https://github.com/NickWaterton/samsung-tv-ws-api.git

COPY run.sh /
COPY app /app
COPY web /web

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

ARG BUILD_FROM
FROM $BUILD_FROM

RUN \
  apk add --no-cache \
    python3 git py3-pip
RUN \
  pip3 install Pillow && pip3 install python-resize-image && pip3 install aiohttp && pip3 install websockets && pip3 install git+https://github.com/NickWaterton/samsung-tv-ws-api.git

# Copy data for add-on
COPY run.sh /
COPY art.py /


RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

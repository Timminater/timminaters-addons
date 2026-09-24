FROM python:3.12-alpine

LABEL io.hass.version="0.1.6" \
      io.hass.type="app" \
      io.hass.arch="aarch64|amd64"

RUN apk add --no-cache tzdata
RUN pip install --no-cache-dir paho-mqtt==2.1.0
WORKDIR /opt/stroomvoorspeller
COPY app ./app
COPY icon.png ./app/static/icon.png
COPY run.sh ./run.sh
RUN chmod 0755 ./run.sh && mkdir -p /data
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
EXPOSE 8099
CMD ["./run.sh"]

FROM python:3.12-alpine

LABEL io.hass.version="0.1.3" \
      io.hass.type="app" \
      io.hass.arch="aarch64|amd64"

RUN apk add --no-cache tzdata
WORKDIR /opt/stroomvoorspeller
COPY app ./app
COPY run.sh ./run.sh
RUN chmod 0755 ./run.sh && mkdir -p /data
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
EXPOSE 8099
CMD ["./run.sh"]

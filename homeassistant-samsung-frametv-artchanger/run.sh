#!/usr/bin/with-contenv bashio

TVIP=$(bashio::config 'tv')
AUTOMATION_TOKEN=$(bashio::config 'automation_token')

mkdir -p /media/frame /data

echo "Starting Frame TV Art Changer Web UI"
echo "Configured TV IPs: ${TVIP}"

export TV_IPS="${TVIP}"
export AUTOMATION_TOKEN="${AUTOMATION_TOKEN}"
export MEDIA_DIR="/media/frame"
export DATA_DIR="/data"

python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8099

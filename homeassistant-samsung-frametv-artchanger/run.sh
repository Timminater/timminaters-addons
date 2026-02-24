#!/usr/bin/with-contenv bashio

TVIP=$(bashio::config 'tv')
AUTOMATION_TOKEN=$(bashio::config 'automation_token')
MEDIA_DIR="${MEDIA_DIR:-/media/frame}"
DATA_DIR="${DATA_DIR:-/data}"

mkdir -p "${MEDIA_DIR}" "${DATA_DIR}"

echo "Starting Frame TV Art Changer Web UI"
echo "Configured TV IPs: ${TVIP}"

export TV_IPS="${TVIP}"
export AUTOMATION_TOKEN="${AUTOMATION_TOKEN}"
export MEDIA_DIR="${MEDIA_DIR}"
export DATA_DIR="${DATA_DIR}"

python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8099

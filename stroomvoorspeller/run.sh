#!/bin/sh
set -eu
cd /opt/stroomvoorspeller
exec python -m app.backend

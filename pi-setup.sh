#!/bin/sh
set -e
cd ~/mistral-gateway
if [ ! -f .env ]; then
  KEY=$(python3 -c "import secrets; print(secrets.token_hex(24))")
  printf 'VIBE_KEY=\nSTUDIO_API_KEY=\nGATEWAY_KEY=%s\n' "$KEY" > .env
  chmod 600 .env
  echo "created .env with generated GATEWAY_KEY (VIBE_KEY still empty)"
else
  echo ".env already exists, leaving it alone"
fi
mkdir -p data
docker compose build 2>&1 | tail -5
echo BUILD_OK

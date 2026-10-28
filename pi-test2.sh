#!/bin/sh
cd ~/mistral-gateway
GK=$(grep '^GATEWAY_KEY=' .env | cut -d= -f2)
VK=$(grep '^VIBE_KEY=' .env | cut -d= -f2)
kill 800145 800140 2>/dev/null; sleep 1
echo "--- direct api.mistral.ai/v1/models with vibe key, 20s cap ---"
curl -s -m 20 -o /tmp/dm.json -w '%{http_code} %{time_total}s %{size_download}b\n' -H "Authorization: Bearer $VK" https://api.mistral.ai/v1/models
head -c 300 /tmp/dm.json; echo
echo "--- gateway /v1/models, 20s cap ---"
curl -s -m 20 -o /tmp/gm.json -w '%{http_code} %{time_total}s %{size_download}b\n' -H "Authorization: Bearer $GK" http://192.168.0.157:18788/v1/models
head -c 300 /tmp/gm.json; echo
docker logs mistral-gateway --tail=10

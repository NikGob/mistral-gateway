#!/bin/sh
cd ~/mistral-gateway
VK=$(grep '^VIBE_KEY=' .env | cut -d= -f2)
GK=$(grep '^GATEWAY_KEY=' .env | cut -d= -f2)
echo "--- direct models gzip 60s ---"
curl -s --compressed -m 60 -o /tmp/dm2.json -w '%{http_code} %{time_total}s %{size_download}b\n' -H "Authorization: Bearer $VK" https://api.mistral.ai/v1/models
python3 -c "import json; d=json.load(open('/tmp/dm2.json')); ids=[m['id'] for m in d['data']]; print(len(ids)); print([i for i in ids if 'glm' in i or 'deepseek' in i])" 2>&1 | head -3
echo "--- direct chat mistral-small 60s ---"
curl -s -m 60 -H "Authorization: Bearer $VK" -H 'Content-Type: application/json' -d '{"model":"mistral-small-latest","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":10}' https://api.mistral.ai/v1/chat/completions | head -c 400; echo
echo "--- gateway chat 60s ---"
curl -s -m 60 -H "Authorization: Bearer $GK" -H 'Content-Type: application/json' -d '{"model":"mistral-small-latest","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":10}' http://192.168.0.157:18788/v1/chat/completions | head -c 400; echo

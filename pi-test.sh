#!/bin/sh
set -e
cd ~/mistral-gateway
GK=$(grep '^GATEWAY_KEY=' .env | cut -d= -f2)
B=http://192.168.0.157:18788
echo "--- models ---"
curl -s -H "Authorization: Bearer $GK" $B/v1/models | python3 -c "import sys,json; d=json.load(sys.stdin); ids=[m['id'] for m in d.get('data',[])]; print(len(ids)); print([i for i in ids if 'glm' in i or 'deepseek' in i or 'large-4' in i])"
echo "--- chat non-stream (mistral-small) ---"
curl -s -H "Authorization: Bearer $GK" -H 'Content-Type: application/json' \
  -d '{"model":"mistral-small-latest","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":10}' \
  $B/v1/chat/completions | head -c 500; echo
echo "--- conversations web_search (mistral-small) ---"
curl -s -H "Authorization: Bearer $GK" -H 'Content-Type: application/json' \
  -d '{"model":"mistral-small-latest","inputs":"Use web search: what is the latest stable Python version? Answer briefly with the version.","tools":[{"type":"web_search"}],"stream":false}' \
  $B/v1/conversations | head -c 1200; echo
echo "--- ddg reachable ---"
curl -s -o /dev/null -w '%{http_code}\n' --max-time 15 -A 'Mozilla/5.0' 'https://html.duckduckgo.com/html/?q=test'
echo "--- stats ---"
curl -s -H "Authorization: Bearer $GK" "$B/api/stats?days=7" | head -c 300; echo

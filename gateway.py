#!/usr/bin/env python3
"""Local OpenAI-compatible gateway for a Mistral Vibe subscription.

Bridges OpenAI-style /v1/chat/completions and /v1/responses clients to
Mistral's endpoint of the same name using your Vibe plan key (from
`vibe --setup` or copied into VIBE_KEY from
https://chat.mistral.ai/code/extensions), with an optional Mistral AI
Studio API key as pay-as-you-go fallback.

Key logic:
  - 429  -> retry the same key every RETRY_429_INTERVAL (10s) for
            RETRY_429_SECONDS (60s); then cooldown COOLDOWN_429 (60s) and
            the fallback key serves meanwhile
  - 401/402 -> cooldown COOLDOWN_DEAD (6h): budget spent or key revoked
  - 404  -> no cooldown, this request falls through to the fallback key
  - cooldowns persist in pool_state.json so restarts keep the state

Auth: clients send the gateway's own key from GATEWAY_KEY, or the gateway
      runs keyless on loopback.
"""
import datetime
import hashlib
import hmac
import http.client
import json
import logging
import os
import pathlib
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("mistral-gateway")

MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_RESPONSES_URL = "https://api.mistral.ai/v1/responses"
MISTRAL_CONVERSATIONS_URL = "https://api.mistral.ai/v1/conversations"
MISTRAL_MODELS_URL = "https://api.mistral.ai/v1/models"
MISTRAL_WHOAMI_URL = "https://console.mistral.ai/api/vibe/whoami"

BASE_DIR = pathlib.Path(__file__).parent
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
LOOPBACK_BINDS = {"127.0.0.1", "localhost", "::1", "[::1]"}
RESPONSES_ROUTES = ("/v1/responses", "/v1/responses/", "/responses")
CONVERSATIONS_ROUTES = ("/v1/conversations", "/v1/conversations/", "/conversations")
MAX_BODY_BYTES_DEFAULT = 10 * 1024 * 1024

# Static catalogue for the panel. Prices are Mistral PAYG list prices in USD
# per 1M tokens (input / cached input / output) where published; None means
# "not published here - do not invent a number". Vibe-plan traffic itself is
# covered by the flat subscription, so the panel labels this as the
# *equivalent* PAYG value, never as money actually charged.
KNOWN_MODELS = [
    {"id": "zai-glm-5-3-flash", "label": "GLM 5.3 Flash", "context": 1000000, "images": True, "reasoning": ["low", "high", "max"], "price": {"in": 0.15, "cached": 0.03, "out": 0.50}, "note": "New 2026-10-22, multimodal, 128K max output"},
    {"id": "zai-glm-5-3", "label": "GLM 5.3", "context": 1000000, "images": False, "reasoning": ["low", "high", "max"], "price": None, "note": ""},
    {"id": "deepseek-v4-1-flash-1017", "label": "DeepSeek V4.1 Flash", "context": 1000000, "images": False, "reasoning": ["none", "low", "high", "xhigh", "max"], "price": None, "note": ""},
    {"id": "mistral-large-4", "label": "Mistral Large 4", "context": 1000000, "images": True, "reasoning": ["none", "high"], "price": None, "note": ""},
    {"id": "mistral-medium-latest", "label": "Mistral Medium 3.5", "context": 262144, "images": True, "reasoning": ["none", "high"], "price": None, "note": ""},
    {"id": "mistral-small-latest", "label": "Mistral Small", "context": 262144, "images": True, "reasoning": ["none", "high"], "price": None, "note": ""},
    {"id": "codestral-latest", "label": "Codestral", "context": 131072, "images": False, "reasoning": [], "price": None, "note": ""},
]
KNOWN_BY_ID = {m["id"]: m for m in KNOWN_MODELS}


def data_dir() -> pathlib.Path:
    """Writable state dir; DATA_DIR lets a container keep state on a volume."""
    return pathlib.Path(os.environ.get("DATA_DIR") or BASE_DIR)


def state_file() -> pathlib.Path:
    return data_dir() / "pool_state.json"
POLL_SLEEP = 0.3
REASONING_EFFORT_LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
# Rejection code -> cooldown seconds for the account that was rejected.
# Built in main() after env files load, so gateway.env overrides apply.
COOLDOWNS: dict[int, float] = {}


def build_cooldowns() -> None:
    COOLDOWNS.update({
        429: float(os.environ.get("COOLDOWN_429", "60")),
        401: float(os.environ.get("COOLDOWN_DEAD", str(6 * 3600))),
        402: float(os.environ.get("COOLDOWN_DEAD", str(6 * 3600))),
    })


# 429 retry window ("seconds") and sleep between attempts ("interval"),
# built in main() after env files load like COOLDOWNS.
RETRY_429: dict[str, float] = {}


def build_retries() -> None:
    RETRY_429.update({
        "seconds": float(os.environ.get("RETRY_429_SECONDS", "60")),
        "interval": float(os.environ.get("RETRY_429_INTERVAL", "10")),
    })


def read_env_file(path: pathlib.Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser so no python-dotenv dependency is needed."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip("\"'")
    return values


def load_state() -> dict:
    try:
        return json.loads(state_file().read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    target = state_file()
    temp_path = target.with_suffix(f".{os.getpid()}-{threading.get_ident()}.tmp")
    temp_path.write_text(json.dumps(state))
    temp_path.replace(target)


@dataclass(frozen=True)
class Account:
    name: str
    key: str


def load_accounts(vibe_env: dict[str, str]) -> list[Account]:
    """The Vibe plan key first, the optional Studio API key as fallback."""
    vibe_key = os.environ.get("VIBE_KEY") or vibe_env.get("MISTRAL_API_KEY") or ""
    studio_key = os.environ.get("STUDIO_API_KEY") or ""
    accounts = []
    if vibe_key:
        accounts.append(Account("vibe", vibe_key))
    if studio_key:
        accounts.append(Account("studio", studio_key))
    if not accounts:
        raise SystemExit(
            "no key found. Get your Vibe key one of two ways:\n"
            "  1. run `vibe --setup` (the gateway reads ~/.vibe/.env)\n"
            "  2. copy it from https://chat.mistral.ai/code/extensions (Advanced)"
            " into gateway.env as VIBE_KEY=...\n"
            "Or set STUDIO_API_KEY for a pay-as-you-go key."
        )
    return accounts


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


class KeyPool:
    def __init__(self, accounts: list[Account]) -> None:
        self.accounts = accounts
        self.state = load_state()  # {account name: {"down_until": epoch, "key": fingerprint}}
        self.lock = threading.Lock()

    def _down_until_locked(self, account: Account) -> float:
        entry = self.state.get(account.name, {})
        if entry.get("key") != key_fingerprint(account.key):
            return 0.0  # cooldown belonged to a key that has since been replaced
        return float(entry.get("down_until") or 0)

    def down_until(self, account: Account) -> float:
        with self.lock:
            return self._down_until_locked(account)

    def mark_down(self, account: Account, reason: str, cooldown: float) -> None:
        with self.lock:
            until = time.time() + cooldown
            if self._down_until_locked(account) >= until:
                return
            self.state[account.name] = {"down_until": until, "key": key_fingerprint(account.key)}
            save_state(self.state)
        LOG.warning("account %s cooling down (%s) for %.0fs", account.name, reason, cooldown)

    def accounts_for(self) -> list[Account]:
        """Serving keys in order. The Vibe key gets all traffic so its upstream
        prompt cache stays warm; the fallback only steps in while it cools down."""
        now = time.time()
        with self.lock:
            serving = [account for account in self.accounts if now >= self._down_until_locked(account)]
        if not serving:
            return [self.accounts[0]]  # all cooling: try the first anyway
        return serving


POOL: KeyPool | None = None  # set in main()
USAGE_DB_LOCK = threading.Lock()


def usage_db() -> sqlite3.Connection:
    """One row per (day, model, account) in usage.db; updated in place."""
    data_dir().mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(data_dir() / "usage.db", timeout=30)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS usage ("
        "day TEXT, model TEXT, account TEXT,"
        "requests INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0,"
        "cached_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,"
        "PRIMARY KEY (day, model, account))"
    )
    return connection


def record_usage(model: str, account: str, usage: dict) -> None:
    """Upsert one request's usage into today's row; errors never kill a reply."""
    day = time.strftime("%Y-%m-%d")
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    with USAGE_DB_LOCK:
        try:
            with usage_db() as connection:
                connection.execute(
                    "UPDATE usage SET requests = requests + 1,"
                    " prompt_tokens = prompt_tokens + ?,"
                    " cached_tokens = cached_tokens + ?,"
                    " completion_tokens = completion_tokens + ?"
                    " WHERE day = ? AND model = ? AND account = ?",
                    (prompt, cached, completion, day, model, account),
                )
                if connection.total_changes == 0:
                    connection.execute(
                        "INSERT INTO usage (day, model, account, requests,"
                        " prompt_tokens, cached_tokens, completion_tokens)"
                        " VALUES (?, ?, ?, 1, ?, ?, ?)",
                        (day, model, account, prompt, cached, completion),
                    )
        except sqlite3.Error as error:
            LOG.warning("usage write failed: %s", error)


def upstream_request(url: str, key: str, body: bytes | None, method: str = "POST"):
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    return urllib.request.urlopen(request, timeout=600)


def upstream_request_with_retry(
    url: str, key: str, name: str, body: bytes | None, method: str = "POST"
):
    """Like upstream_request, but a 429 retries the same key within the window."""
    deadline = time.time() + RETRY_429["seconds"]
    while True:
        try:
            return upstream_request(url, key, body, method)
        except urllib.error.HTTPError as error:
            if error.code != 429:
                raise
            remaining = deadline - time.time()
            if remaining <= 0:
                raise  # window spent
            try:
                detail = error.read()
            except OSError:
                detail = b""
            error.close()
            LOG.info("key=%s rejected: %s %s", name, error.code, detail[:200])
            time.sleep(min(RETRY_429["interval"], remaining))


def read_ratelimit_headers(response) -> dict:
    return {
        name.lower(): value
        for name, value in response.headers.items()
        if name.lower().startswith("x-ratelimit")
    }


def normalize_content_chunks(content):
    """Flatten Mistral GLM content chunks (thinking/text lists) to OpenAI shape.

    GLM replies put message.content as a list of typed parts; many clients
    do string ops on it and crash. Returns (plain_text, reasoning_text).
    """
    if not isinstance(content, list):
        return content, None
    text_parts, thinking_parts = [], []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "thinking":
            for item in part.get("thinking", []):
                if isinstance(item, dict):
                    thinking_parts.append(item.get("text", ""))
        elif part.get("type") == "text":
            if isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            else:
                for item in part.get("text", []):
                    if isinstance(item, dict):
                        text_parts.append(item.get("text", ""))
    return "".join(text_parts), ("".join(thinking_parts) or None)


def normalize_stream_event(event: bytes, saw_tool_call: bool = False) -> tuple[bytes, bool]:
    """Rewrite one SSE event: GLM chunk-lists become plain fields, and a
    terminal "stop" after tool-call deltas becomes "tool_calls".

    Returns (event_bytes, saw_tool_call); the flag persists across events so
    the finish_reason translation can apply at the stream's last chunk.
    """
    if not event.startswith(b"data: "):
        return event, saw_tool_call
    # The caller passes whole events including the blank separator line;
    # split it off before parsing so the rewritten event keeps its framing.
    data_line, separator, tail = event.partition(b"\n\n")
    if not separator:
        data_line = data_line.rstrip(b"\n")
        tail = b"\n" if event.endswith(b"\n") else b""
    try:
        parsed = json.loads(data_line[6:])
    except ValueError:
        return event, saw_tool_call
    for choice in parsed.get("choices", []):
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, list):
            text, reasoning = normalize_content_chunks(content)
            delta["content"] = text
            if reasoning:
                delta["reasoning_content"] = reasoning
        if delta.get("tool_calls"):
            saw_tool_call = True
        if saw_tool_call and choice.get("finish_reason") == "stop":
            choice["finish_reason"] = "tool_calls"
    return b"data: " + json.dumps(parsed).encode() + separator + tail, saw_tool_call


def parse_event(event: bytes) -> dict | None:
    try:
        parsed = json.loads(event.partition(b"data:")[2])  # Responses events start with an event: line
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def is_cut_event(event: bytes) -> bool:
    """A cut stream leaves a trailing event whose data line no longer parses."""
    if b"\r\n\r\n" in event:
        return False  # CRLF framing never splits above, so this holds whole events
    data_line = next((line for line in event.splitlines() if line.startswith(b"data:")), None)
    if data_line is None:
        return False
    payload = data_line[5:].strip()
    return payload != b"[DONE]" and parse_event(b"data: " + payload) is None


def normalize_completion(payload: bytes) -> bytes:
    """Rewrite one non-streaming completion to plain OpenAI message shape."""
    try:
        parsed = json.loads(payload)
    except ValueError:
        return payload
    for choice in parsed.get("choices", []):
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        if isinstance(message.get("content"), list):
            text, reasoning = normalize_content_chunks(message["content"])
            message["content"] = text
            if reasoning:
                message["reasoning_content"] = reasoning
        if message.get("tool_calls") and choice.get("finish_reason") == "stop":
            choice["finish_reason"] = "tool_calls"
    return json.dumps(parsed).encode()


def snap_reasoning_effort(body: bytes, detail: bytes) -> bytes | None:
    """Swap a rejected reasoning_effort for the nearest allowed level, or drop it if the model has none."""
    error_text = detail.decode(errors="replace")
    if "reasoning_effort" not in error_text:
        return None
    chat_request = json.loads(body)
    # Responses requests nest the level as reasoning.effort.
    reasoning = chat_request.get("reasoning")
    effort_owner, effort_key = (reasoning, "effort") if isinstance(reasoning, dict) else (chat_request, "reasoning_effort")
    requested = effort_owner.get(effort_key)
    if "reasoning_effort is not enabled" in error_text and requested is not None:
        LOG.info("reasoning_effort %s unsupported, retrying without it", requested)
        del effort_owner[effort_key]
        return json.dumps(chat_request).encode()
    if "is not supported" not in error_text:
        return None
    listed = error_text.split("is not supported", 1)[1]
    supported = [level for level in REASONING_EFFORT_LEVELS if f"'{level}'" in listed]
    if requested not in REASONING_EFFORT_LEVELS or requested in supported or not supported:
        return None
    rank = REASONING_EFFORT_LEVELS.index(requested)
    # On a tie, low and below lean toward less reasoning, medium and above toward more.
    candidates = supported if requested in ("none", "minimal", "low") else reversed(supported)
    closest = min(candidates, key=lambda level: abs(REASONING_EFFORT_LEVELS.index(level) - rank))
    LOG.info("reasoning_effort %s unsupported, retrying as %s", requested, closest)
    effort_owner[effort_key] = closest
    return json.dumps(chat_request).encode()


def estimate_cost_usd(model: str, prompt: int, cached: int, completion: int):
    """Equivalent PAYG value in USD, or None when no published price is known."""
    info = KNOWN_BY_ID.get(model)
    if not info or not info.get("price"):
        return None
    price = info["price"]
    uncached = max(0, prompt - cached)
    return round((uncached * price["in"] + cached * price["cached"] + completion * price["out"]) / 1_000_000, 6)


def collect_stats(window_days: int) -> dict:
    since = (datetime.date.today() - datetime.timedelta(days=window_days)).isoformat()
    with USAGE_DB_LOCK:
        with usage_db() as connection:
            rows = connection.execute(
                "SELECT day, model, account, requests, prompt_tokens, cached_tokens, completion_tokens"
                " FROM usage WHERE day >= ? ORDER BY day DESC, model, account", (since,)).fetchall()
    by_model: dict = {}
    by_day: dict = {}
    by_account: dict = {}
    totals = {"requests": 0, "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "est_cost_usd_known": 0.0}
    for day, model, account, req, prompt, cached, completion in rows:
        total = prompt + completion
        cost = estimate_cost_usd(model, prompt, cached, completion)
        for bucket, key in ((by_model, model), (by_day, day), (by_account, account)):
            entry = bucket.setdefault(key, {"requests": 0, "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "est_cost_usd": None})
            entry["requests"] += req; entry["prompt_tokens"] += prompt; entry["cached_tokens"] += cached
            entry["completion_tokens"] += completion; entry["total_tokens"] += total
            if cost is not None:
                entry["est_cost_usd"] = round((entry["est_cost_usd"] or 0) + cost, 6)
        totals["requests"] += req; totals["prompt_tokens"] += prompt; totals["cached_tokens"] += cached
        totals["completion_tokens"] += completion; totals["total_tokens"] += total
        if cost is not None:
            totals["est_cost_usd_known"] = round(totals["est_cost_usd_known"] + cost, 6)
    return {"days": window_days, "since": since, "totals": totals,
            "by_model": [{"model": k, **v, "known": KNOWN_BY_ID.get(k)} for k, v in sorted(by_model.items(), key=lambda kv: -kv[1]["total_tokens"])],
            "by_day": [{"day": k, **v} for k, v in sorted(by_day.items(), reverse=True)],
            "by_account": [{"account": k, **v} for k, v in sorted(by_account.items())],
            "known_models": KNOWN_MODELS}


PANEL_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Mistral Gateway</title>
<style>
:root{--p:#6750a4;--onp:#fff;--pc:#eaddff;--onpc:#21005d;--bg:#fef7ff;--sf:#fef7ff;--sfc:#f3edf7;--sfch:#ece6f0;--txt:#1d1b20;--mut:#49454f;--out:#79747e;--err:#b3261e;--ok:#146c2e;--radius:28px}
@media(prefers-color-scheme:dark){:root{--p:#d0bcff;--onp:#381e72;--pc:#4f378b;--onpc:#eaddff;--bg:#141218;--sf:#141218;--sfc:#211f26;--sfch:#2b2930;--txt:#e6e0e9;--mut:#cac4d0;--out:#938f99}}
*{box-sizing:border-box}body{margin:0;font-family:Roboto,system-ui,'Segoe UI',sans-serif;background:var(--bg);color:var(--txt)}
header{position:sticky;top:0;background:var(--sfc);padding:16px 20px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;z-index:2}
h1{font-size:22px;margin:0;font-weight:500}.chip{background:var(--pc);color:var(--onpc);border-radius:8px;padding:4px 10px;font-size:12px}
main{max-width:1100px;margin:0 auto;padding:20px;display:grid;gap:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
.card{background:var(--sfc);border-radius:var(--radius);padding:20px}.card h2{font-size:14px;color:var(--mut);margin:0 0 8px;font-weight:500}
.big{font-size:30px;font-weight:500}.sub{color:var(--mut);font-size:13px}
button,select{background:var(--p);color:var(--onp);border:0;border-radius:20px;padding:10px 18px;font-size:14px;cursor:pointer}
select{background:var(--sfch);color:var(--txt)}button.tonal{background:var(--pc);color:var(--onpc)}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:right;padding:8px 6px;border-bottom:1px solid var(--out)}th:first-child,td:first-child{text-align:left}
.bar{height:8px;background:var(--sfch);border-radius:4px;overflow:hidden}.bar>i{display:block;height:100%;background:var(--p)}
.err{color:var(--err)}.ok{color:var(--ok)}code{background:var(--sfch);padding:2px 6px;border-radius:6px}
</style></head><body>
<header><h1>🌫️ Mistral Gateway</h1><span class="chip" id="pool">pool: …</span>
<select id="days"><option value="7">7 дней</option><option value="30" selected>30 дней</option><option value="90">90 дней</option><option value="3650">Всё время</option></select>
<button class="tonal" id="keybtn">🔑 Ключ</button><button id="refresh">Обновить</button></header>
<main>
<div class="grid" id="totals"></div>
<section class="card"><h2>По моделям (токены и эквивалент PAYG, $)</h2><div id="models"></div>
<p class="sub">Эквивалент PAYG считается только для моделей с опубликованной ценой (GLM 5.3 Flash: $0.15 / $0.03 cached / $0.50 за 1M). Трафик Vibe-плана оплачен подпиской — это не списанные деньги, а «сколько бы это стоило по Studio-тарифу».</p></section>
<section class="card"><h2>По дням</h2><div id="byday"></div></section>
<section class="card"><h2>Известные модели CLI / API</h2><div id="known"></div>
<p class="sub">Поиск по вебу: через <code>POST /v1/conversations</code> с <code>tools:[{"type":"web_search"}]</code> (или <code>web_search_premium</code>). В обычном <code>/v1/chat/completions</code> Mistral веб-поиск не поддерживает.</p></section>
<p class="sub" id="msg"></p>
</main>
<script>
const fmt=n=>new Intl.NumberFormat('ru-RU').format(n||0);
const $=id=>document.getElementById(id);
function headers(){const k=sessionStorage.getItem('gwkey');return k?{Authorization:'Bearer '+k}:{}}
async function load(){
 $('msg').textContent='';
 try{
  const [s,p]=await Promise.all([fetch('/api/stats?days='+$('days').value,{headers:headers()}),fetch('/pool/status',{headers:headers()})]);
  if(s.status===401){$('msg').innerHTML='<span class="err">Нужен GATEWAY_KEY — нажми «Ключ».</span>';return}
  const st=await s.json(), pool=await p.json().catch(()=>null);
  if(pool&&pool.accounts) $('pool').textContent='pool: '+pool.accounts.map(a=>a.name+(a.cooling?' ⏸':' ✅')).join(' · ')+' → '+(pool.serving||[]).join(',');
  const t=st.totals;
  $('totals').innerHTML=[['Запросов',fmt(t.requests),''],['Токенов всего',fmt(t.total_tokens),'prompt + completion'],['Prompt',fmt(t.prompt_tokens),'из них cached: '+fmt(t.cached_tokens)],['Completion',fmt(t.completion_tokens),''],['Эквивалент PAYG (известные цены)',t.est_cost_usd_known?'$'+t.est_cost_usd_known.toFixed(4):'—','только модели с ценой']].map(x=>`<div class="card"><h2>${x[0]}</h2><div class="big">${x[1]}</div><div class="sub">${x[2]}</div></div>`).join('');
  const mx=Math.max(1,...st.by_model.map(m=>m.total_tokens));
  $('models').innerHTML=st.by_model.length?'<table><tr><th>Модель</th><th>Запр.</th><th>Prompt</th><th>Cached</th><th>Completion</th><th>Всего</th><th>~$</th></tr>'+st.by_model.map(m=>`<tr><td>${m.model}<div class="bar"><i style="width:${Math.round(m.total_tokens/mx*100)}%"></i></div></td><td>${fmt(m.requests)}</td><td>${fmt(m.prompt_tokens)}</td><td>${fmt(m.cached_tokens)}</td><td>${fmt(m.completion_tokens)}</td><td>${fmt(m.total_tokens)}</td><td>${m.est_cost_usd==null?'—':'$'+m.est_cost_usd.toFixed(4)}</td></tr>`).join('')+'</table>':'<p class="sub">Пока нет запросов.</p>';
  $('byday').innerHTML=st.by_day.length?'<table><tr><th>День</th><th>Запр.</th><th>Всего токенов</th></tr>'+st.by_day.map(d=>`<tr><td>${d.day}</td><td>${fmt(d.requests)}</td><td>${fmt(d.total_tokens)}</td></tr>`).join('')+'</table>':'<p class="sub">Пусто.</p>';
  $('known').innerHTML='<table><tr><th>Модель</th><th>Контекст</th><th>Картинки</th><th>Reasoning</th><th>Цена /1M</th></tr>'+st.known_models.map(m=>`<tr><td><code>${m.id}</code> ${m.label}</td><td>${fmt(m.context)}</td><td>${m.images?'да':'нет'}</td><td>${(m.reasoning||[]).join(', ')||'—'}</td><td>${m.price?'$'+m.price.in+' / $'+m.price.cached+' / $'+m.price.out:'—'}</td></tr>`).join('')+'</table>';
 }catch(e){$('msg').innerHTML='<span class="err">'+e+'</span>'}
}
$('refresh').onclick=load;$('days').onchange=load;
$('keybtn').onclick=()=>{const k=prompt('GATEWAY_KEY',sessionStorage.getItem('gwkey')||'');if(k!==null){sessionStorage.setItem('gwkey',k);load()}};
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # route stdlib noise through logging
        LOG.debug(fmt, *args)

    # --- helpers -------------------------------------------------------
    def _reply(self, code: int, payload, headers: dict | None = None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if code >= 400:
            self.send_header("Connection", "close")  # request body may be unread
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        expected = os.environ.get("GATEWAY_KEY", "")
        if not expected:
            return True  # keyless on loopback
        auth = self.headers.get("Authorization", "")
        return hmac.compare_digest(auth.removeprefix("Bearer "), expected)

    def _host_allowed(self) -> bool:
        """Keyless mode only answers loopback hostnames, which blocks DNS rebinding."""
        if os.environ.get("GATEWAY_KEY"):
            return True
        host = self.headers.get("Host", "")
        if not host.endswith("]") and ":" in host:
            host = host.rpartition(":")[0]  # strip the port
        if host in LOOPBACK_HOSTS:
            return True
        LOG.warning("rejected Host %r: set GATEWAY_KEY to allow other hostnames", host)
        return False

    def _proxy_body(self, body: bytes, stream: bool, _snap_depth: int = 0) -> None:
        """Try accounts in pool order; stream the winning response downstream."""
        last_error = None
        route = self.path.partition("?")[0]
        if route in RESPONSES_ROUTES:
            url = MISTRAL_RESPONSES_URL
        elif route in CONVERSATIONS_ROUTES:
            url = MISTRAL_CONVERSATIONS_URL
        else:
            url = MISTRAL_CHAT_URL
        for account in POOL.accounts_for():
            try:
                upstream = upstream_request_with_retry(
                    url, account.key, account.name, body
                )
            except urllib.error.HTTPError as error:
                try:
                    detail = error.read()
                except OSError:
                    detail = b""
                error.close()
                last_error = (error.code, detail)
                LOG.info("key=%s rejected: %s %s", account.name, error.code, detail[:200])
                if error.code in (400, 422):
                    snapped_body = snap_reasoning_effort(body, detail) if _snap_depth < 2 else None
                    if snapped_body:
                        return self._proxy_body(snapped_body, stream, _snap_depth + 1)
                    break
                if error.code in COOLDOWNS:
                    POOL.mark_down(account, f"HTTP {error.code}", COOLDOWNS[error.code])
                continue
            except OSError as error:
                LOG.warning("key=%s network error: %s", account.name, error)
                last_error = (502, str(error).encode())
                continue

            if not stream:
                # Read the full payload first: headers stay unsent, so a mid-read
                # upstream failure can still fall through to the next account.
                try:
                    payload = normalize_completion(upstream.read())
                except (OSError, http.client.HTTPException) as error:
                    LOG.warning("key=%s died mid-read: %s", account.name, error)
                    last_error = (502, str(error).encode())
                    continue
                finally:
                    upstream.close()
                completion = parse_event(b"data: " + payload) or {}
                record_usage(completion.get("model") or self._request_model, account.name,
                             completion.get("usage") or {})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Pool-Key", account.name)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                LOG.info("served by key=%s", account.name)
                return

            sent = 0
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                for name, value in read_ratelimit_headers(upstream).items():
                    self.send_header(f"X-Pool-{name}", f"{account.name}: {value}")
                self.send_header("X-Pool-Key", account.name)
                self.end_headers()
                pending = b""
                saw_tool_call = False
                stream_usage = None
                stream_model = None
                while True:
                    chunk = upstream.read1(8192)  # read() would hold events until 8 KB arrive
                    if not chunk:
                        break
                    pending += chunk
                    # SSE events end with a blank line; only complete lines
                    # are rewritten so a split JSON payload is never mangled.
                    while b"\n\n" in pending:
                        raw_event, pending = pending.split(b"\n\n", 1)
                        event, saw_tool_call = normalize_stream_event(raw_event + b"\n\n", saw_tool_call)
                        self.wfile.write(event)
                        sent += len(event)
                        self.wfile.flush()
                        event_json = parse_event(event) or {}
                        event_json = event_json.get("response") or event_json
                        stream_model = event_json.get("model") or stream_model
                        stream_usage = event_json.get("usage") or stream_usage
                if pending and is_cut_event(pending):
                    LOG.warning("stream cut mid-event: key=%s dropped=%dB", account.name, len(pending))
                    pending = b""
                if pending:
                    tail_event, saw_tool_call = normalize_stream_event(pending, saw_tool_call)
                    self.wfile.write(tail_event)
                    sent += len(tail_event)
                    self.wfile.flush()
                    event_json = parse_event(tail_event) or {}
                    event_json = event_json.get("response") or event_json
                    stream_model = event_json.get("model") or stream_model
                    stream_usage = event_json.get("usage") or stream_usage
                LOG.info("stream complete: key=%s forwarded=%dB tool_calls=%s", account.name, sent, saw_tool_call)
                if stream_usage:
                    record_usage(stream_model or self._request_model, account.name, stream_usage)
            except (OSError, http.client.HTTPException) as error:
                LOG.warning("stream cut: key=%s forwarded=%dB error=%s", account.name, sent, error)
            else:
                LOG.info("served by key=%s", account.name)
            finally:
                upstream.close()
            return

        code, detail = last_error if last_error else (500, b"no keys available")
        self._reply(code, {"error": {"message": detail.decode(errors="replace")[:500], "type": "upstream_error", "code": code}})

    # --- routes --------------------------------------------------------
    def do_GET(self):
        if not self._host_allowed():
            return self._reply(403, {"error": {"message": "forbidden host"}})
        route, _, query = self.path.partition("?")
        if route in ("/", "/panel"):
            data = PANEL_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(data)
            return
        if route == "/health":
            if os.environ.get("GATEWAY_KEY") and not self._authorized():
                return self._reply(200, {"ok": True})
            return self._reply(200, {
                "ok": True,
                "accounts": len(POOL.accounts),
                "serving": len(POOL.accounts_for()),
            })
        if not self._authorized():
            return self._reply(401, {"error": {"message": "unauthorized"}})
        if route in ("/v1/models", "/models"):
            for account in POOL.accounts_for():
                try:
                    upstream = upstream_request_with_retry(
                        MISTRAL_MODELS_URL, account.key, account.name, None, method="GET"
                    )
                except urllib.error.HTTPError as error:
                    error.close()
                    LOG.info("models via key=%s failed: %s", account.name, error)
                    if error.code in COOLDOWNS:
                        POOL.mark_down(account, f"HTTP {error.code}", COOLDOWNS[error.code])
                    continue
                except OSError as error:
                    LOG.info("models via key=%s failed: %s", account.name, error)
                    continue
                try:
                    models = json.loads(upstream.read())
                except (OSError, http.client.HTTPException, ValueError) as error:
                    LOG.info("models read via key=%s failed: %s", account.name, error)
                    continue
                finally:
                    upstream.close()
                return self._reply(200, models)
            return self._reply(502, {"error": {"message": "all keys failed"}})
        if route == "/pool/status":
            now = time.time()
            accounts = []
            for account in POOL.accounts:
                down_until = POOL.down_until(account)
                accounts.append({
                    "name": account.name,
                    "cooling": now < down_until,
                    "down_remaining_s": max(0, int(down_until - now)),
                })
            return self._reply(200, {
                "accounts": accounts,
                "serving": [account.name for account in POOL.accounts_for()],
            })
        if route == "/api/stats":
            params = urllib.parse.parse_qs(query)
            try:
                window_days = min(max(int(params["days"][0]), 1), 3650) if "days" in params else 30
            except (ValueError, OverflowError):
                window_days = 30
            try:
                return self._reply(200, collect_stats(window_days))
            except sqlite3.Error as error:
                return self._reply(500, {"error": {"message": f"usage db: {error}"}})
        if route == "/usage":
            # Optional ?days=N limits the window; default is everything logged.
            params = urllib.parse.parse_qs(query)
            try:
                window_days = int(params["days"][0]) if "days" in params else 3650
                since = (datetime.date.today() - datetime.timedelta(days=window_days)).isoformat()
            except (ValueError, OverflowError):
                since = "0000-01-01"
            try:
                with USAGE_DB_LOCK:
                    with usage_db() as connection:
                        rows = connection.execute(
                            "SELECT day, model, account, requests, prompt_tokens,"
                            " cached_tokens, completion_tokens FROM usage"
                            " WHERE day >= ? ORDER BY day DESC, model, account",
                            (since,),
                        ).fetchall()
            except sqlite3.Error as error:
                return self._reply(500, {"error": {"message": f"usage db: {error}"}})
            columns = ("day", "model", "account", "requests", "prompt_tokens", "cached_tokens", "completion_tokens")
            return self._reply(200, {"usage": [dict(zip(columns, row)) for row in rows]})
        return self._reply(404, {"error": {"message": f"unknown route {self.path}"}})

    def do_POST(self):
        if not self._host_allowed():
            return self._reply(403, {"error": {"message": "forbidden host"}})
        if not self._authorized():
            return self._reply(401, {"error": {"message": "unauthorized"}})
        route = self.path.partition("?")[0]
        if route not in ("/v1/chat/completions", "/v1/chat/completions/", "/chat/completions") + RESPONSES_ROUTES + CONVERSATIONS_ROUTES:
            return self._reply(404, {"error": {"message": f"unknown route {self.path}"}})
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return self._reply(411, {"error": {"message": "Content-Length required"}})
        # application/json forces a CORS preflight, which this server never answers
        if self.headers.get_content_type() != "application/json":
            return self._reply(415, {"error": {"message": "Content-Type must be application/json"}})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._reply(400, {"error": {"message": "invalid Content-Length"}})
        try:
            max_body = int(os.environ.get("MAX_REQUEST_BYTES", str(MAX_BODY_BYTES_DEFAULT)))
        except ValueError:
            max_body = MAX_BODY_BYTES_DEFAULT
        if length > max_body:
            return self._reply(413, {"error": {"message": f"request body over {max_body} bytes"}})
        body = self.rfile.read(length)
        try:
            chat_request = json.loads(body)
        except ValueError:
            chat_request = None
        if not isinstance(chat_request, dict):
            return self._reply(400, {"error": {"message": "invalid JSON body"}})
        stream = bool(chat_request.get("stream"))
        if route in RESPONSES_ROUTES + CONVERSATIONS_ROUTES:
            # Conversations is passed through untouched: it is the only route
            # where Mistral's built-in web_search / web_search_premium /
            # code_interpreter tools work.
            self._request_model = chat_request.get("model") or "unknown"
            return self._proxy_body(body, stream)
        changed = False
        if stream and "stream_options" not in chat_request:
            # Without include_usage the final usage chunk never arrives, and
            # clients that bill tokens show zero for every response.
            chat_request["stream_options"] = {"include_usage": True}
            changed = True
        # Mistral answers these OpenAI fields with 422.
        if "max_completion_tokens" in chat_request:
            max_completion_tokens = chat_request.pop("max_completion_tokens")
            if max_completion_tokens is not None:
                chat_request["max_tokens"] = max_completion_tokens
            changed = True
        if "seed" in chat_request:
            chat_request["random_seed"] = chat_request.pop("seed")
            changed = True
        if "user" in chat_request:
            del chat_request["user"]
            changed = True
        if "max_tokens" not in chat_request:
            # Mistral caps GLM completions at 4096 tokens when max_tokens is
            # absent; thinking alone eats that, so long-composition turns end
            # mid-reasoning with no visible text. GLM can think past 32768
            # too, so it gets its full 128K output limit.
            chat_request["max_tokens"] = 131072 if "glm" in str(chat_request.get("model")) else 32768
            changed = True
        messages = chat_request.get("messages")
        if isinstance(messages, list):
            for message in messages:
                # Replies carry these fields, and Mistral 422s when a client sends a reply back as it came.
                for field in ("reasoning_content", "index"):
                    if isinstance(message, dict) and field in message:
                        del message[field]
                        changed = True
        if (
            "reasoning_effort" not in chat_request
            and "glm" in str(chat_request.get("model"))
            and chat_request.get("tools")
            and isinstance(messages, list)
            and messages
            and isinstance(messages[-1], dict)
            and messages[-1].get("role") == "tool"
        ):
            # GLM sometimes terminates tool-result composition turns with zero
            # visible text ("stop", completion_tokens 0) after a long reasoning
            # phase. Raising the effort to high counterintuitively avoids the
            # premature stop: the model finishes deliberating and composes.
            chat_request["reasoning_effort"] = "high"
            changed = True
        if changed:
            body = json.dumps(chat_request).encode()
        self._request_model = chat_request.get("model") or "unknown"
        self._proxy_body(body, stream)


def main() -> int:
    os.environ.update(read_env_file(BASE_DIR / "gateway.env"))
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    vibe_env = read_env_file(pathlib.Path(
        os.environ.get("VIBE_ENV_FILE", str(pathlib.Path.home() / ".vibe/.env"))))
    build_cooldowns()
    build_retries()

    global POOL
    POOL = KeyPool(load_accounts(vibe_env))
    for account in POOL.accounts:
        if account.name != "vibe":
            continue
        try:
            with upstream_request(MISTRAL_WHOAMI_URL, account.key, None, method="GET") as response:
                whoami = json.loads(response.read())
            LOG.info("vibe plan: %s (%s)", whoami.get("plan_name"), whoami.get("plan_type"))
        except (urllib.error.HTTPError, OSError, ValueError) as error:
            LOG.warning("vibe whoami failed (continuing anyway): %s", error)

    port = int(os.environ.get("PORT", "8788"))
    bind_address = os.environ.get("BIND", "127.0.0.1")
    if bind_address not in LOOPBACK_BINDS and not os.environ.get("GATEWAY_KEY"):
        # The keyless Host-header check is not authentication: anyone who can
        # reach the port can send "Host: localhost". Fail closed instead.
        raise SystemExit(
            f"refusing to bind {bind_address} without GATEWAY_KEY: "
            "set GATEWAY_KEY or keep BIND on loopback"
        )
    data_dir().mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((bind_address, port), Handler)
    LOG.info("mistral gateway on %s:%d (keys: %s)", bind_address, port,
             ", ".join(account.name for account in POOL.accounts))

    def shutdown(signum, frame):
        LOG.info("signal %s, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever(poll_interval=POLL_SLEEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())

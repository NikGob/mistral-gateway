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
MODELS_CACHE: dict = {"at": 0.0, "payload": None}
MODELS_CACHE_TTL = 600.0

# --- anti-abuse guards (protect the single subscription account) --------
_rate_lock = threading.Lock()
_rate_hits: list[float] = []
_concurrency = threading.BoundedSemaphore(6)
MAX_CONCURRENT = 6


def rate_limit_per_min() -> int:
    try:
        return max(0, int(os.environ.get("RATE_LIMIT_PER_MIN", "0")))
    except ValueError:
        return 0


def _rate_ok() -> bool:
    limit = rate_limit_per_min()
    if not limit:
        return True
    now = time.monotonic()
    with _rate_lock:
        while _rate_hits and now - _rate_hits[0] > 60:
            _rate_hits.pop(0)
        if len(_rate_hits) >= limit:
            return False
        _rate_hits.append(now)
        return True


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


def upstream_request(url: str, key: str, body: bytes | None, method: str = "POST", gzip_response: bool = False):
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if gzip_response:
        # Without this, api.mistral.ai drips large JSON (e.g. /v1/models)
        # uncompressed so slowly from some networks that clients time out;
        # gzipped the same list arrives in <0.5 s (19 KB -> 2.8 KB).
        headers["Accept-Encoding"] = "gzip"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    return urllib.request.urlopen(request, timeout=600)


def read_upstream_bytes(response) -> bytes:
    raw = response.read()
    if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
        import gzip as _gzip
        try:
            return _gzip.decompress(raw)
        except OSError:
            return raw
    return raw


def upstream_request_with_retry(
    url: str, key: str, name: str, body: bytes | None, method: str = "POST", gzip_response: bool = False
):
    """Like upstream_request, but a 429 retries the same key within the window."""
    deadline = time.time() + RETRY_429["seconds"]
    while True:
        try:
            return upstream_request(url, key, body, method, gzip_response)
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


WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the public web for current or external information. "
            "Use this when the answer may need up-to-date facts, news, "
            "prices, versions, or sources beyond your training data."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search query"}},
            "required": ["query"],
        },
    },
}


def search_enabled() -> bool:
    return os.environ.get("WEB_SEARCH", "").strip().lower() in ("1", "true", "yes", "on")


def _strip_tags(fragment: str) -> str:
    import html as _html
    import re as _re
    return _html.unescape(_re.sub(r"<[^>]+>", " ", fragment)).strip()


def _search_ddg(query: str, limit: int = 5) -> list[dict]:
    """DuckDuckGo HTML endpoint, stdlib only, no key needed."""
    import re as _re
    import urllib.parse as _parse
    url = "https://html.duckduckgo.com/html/?" + _parse.urlencode({"q": query})
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (mistral-gateway)"})
    with urllib.request.urlopen(request, timeout=20) as response:
        page = response.read().decode("utf-8", errors="replace")
    results = []
    for match in _re.finditer(
        r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
        page, _re.S,
    ):
        href, title, snippet = match.groups()
        parsed = _parse.urlparse(href)
        real = _parse.parse_qs(parsed.query).get("uddg", [href])[0]
        results.append({"title": _strip_tags(title), "url": real, "snippet": _strip_tags(snippet)})
        if len(results) >= limit:
            break
    return results


def _search_tavily(query: str, limit: int = 5) -> list[dict]:
    body = json.dumps({"query": query, "max_results": limit, "search_depth": "basic"}).encode()
    request = urllib.request.Request(
        "https://api.tavily.com/search", data=body, method="POST",
        headers={"Authorization": f"Bearer {os.environ['SEARCH_API_KEY']}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read())
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": (r.get("content") or "")[:600]} for r in data.get("results", [])]


def _search_serper(query: str, limit: int = 5) -> list[dict]:
    body = json.dumps({"q": query, "num": limit}).encode()
    request = urllib.request.Request(
        "https://google.serper.dev/search", data=body, method="POST",
        headers={"X-API-KEY": os.environ["SEARCH_API_KEY"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read())
    return [{"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", "")} for r in data.get("organic", [])]


def do_web_search(query: str) -> str:
    """Run the configured provider and format results as tool content."""
    provider = os.environ.get("SEARCH_PROVIDER", "").strip().lower()
    if not provider:
        provider = "tavily" if os.environ.get("SEARCH_API_KEY") else "ddg"
    try:
        if provider == "tavily":
            results = _search_tavily(query)
        elif provider == "serper":
            results = _search_serper(query)
        else:
            results = _search_ddg(query)
    except Exception as error:  # search failure must not kill the chat reply
        LOG.warning("web_search (%s) failed: %s", provider, error)
        return f"web_search failed ({provider}): {error}. Answer without search results and say so."
    if not results:
        return f"web_search ({provider}) found no results for: {query}"
    lines = [f"Web search results ({provider}) for: {query}"]
    for i, r in enumerate(results, 1):
        lines.append(f"\n[{i}] {r['title']}\n{r['url']}\n{r['snippet']}")
    return "\n".join(lines)[:8000]


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


PANEL_HTML = (pathlib.Path(__file__).parent / "panel.html").read_text(encoding="utf-8")


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

    def _proxy_body(self, body: bytes, stream: bool, _snap_depth: int = 0, _search_round: int = 0) -> None:
        if not _concurrency.acquire(blocking=False):
            return self._reply(429, {"error": {"message": "gateway busy: too many concurrent requests", "type": "rate_limit"}})
        try:
            return self._proxy_body_inner(body, stream, _snap_depth, _search_round)
        finally:
            _concurrency.release()

    def _proxy_body_inner(self, body: bytes, stream: bool, _snap_depth: int = 0, _search_round: int = 0) -> None:
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
                    url, account.key, account.name, body, gzip_response=not stream
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
                        return self._proxy_body(snapped_body, stream, _snap_depth + 1, _search_round)
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
                    payload = normalize_completion(read_upstream_bytes(upstream))
                except (OSError, http.client.HTTPException) as error:
                    LOG.warning("key=%s died mid-read: %s", account.name, error)
                    last_error = (502, str(error).encode())
                    continue
                finally:
                    upstream.close()
                completion = parse_event(b"data: " + payload) or {}
                record_usage(completion.get("model") or self._request_model, account.name,
                             completion.get("usage") or {})
                # Server-side web_search: the gateway injected the tool, so it
                # must also execute it - the client never saw this tool call.
                if getattr(self, "_search_injected", False) and _search_round < 2:
                    followup = self._search_followup(payload)
                    if followup is not None:
                        LOG.info("web_search round %d via key=%s", _search_round + 1, account.name)
                        return self._proxy_body(followup, False, _snap_depth, _search_round + 1)
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

    def _search_followup(self, payload: bytes) -> bytes | None:
        """If the reply calls our injected web_search, run it and build round 2."""
        try:
            parsed = json.loads(payload)
            message = parsed["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
        except (ValueError, KeyError, IndexError, TypeError):
            return None
        searches = []
        for call in tool_calls:
            fn = (call.get("function") or {}) if isinstance(call, dict) else {}
            if fn.get("name") != "web_search":
                return None  # a client-visible tool call: leave the reply alone
            try:
                query = (json.loads(fn.get("arguments") or "{}") or {}).get("query", "")
            except ValueError:
                query = ""
            searches.append((call, str(query)))
        if not searches:
            return None
        original = getattr(self, "_chat_request_orig", None)
        if not isinstance(original, dict) or not isinstance(original.get("messages"), list):
            return None
        assistant = {k: v for k, v in message.items() if k not in ("reasoning_content", "index")}
        followup_messages = list(original["messages"]) + [assistant]
        for call, query in searches:
            LOG.info("web_search query: %s", query[:200])
            followup_messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or "web_search",
                "name": "web_search",
                "content": do_web_search(query) if query else "web_search called with an empty query.",
            })
        followup = dict(original)
        followup["messages"] = followup_messages
        return json.dumps(followup).encode()

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
            if MODELS_CACHE["payload"] is not None and time.time() - MODELS_CACHE["at"] < MODELS_CACHE_TTL:
                return self._reply(200, MODELS_CACHE["payload"])
            for account in POOL.accounts_for():
                try:
                    upstream = upstream_request_with_retry(
                        MISTRAL_MODELS_URL, account.key, account.name, None, method="GET", gzip_response=True
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
                    models = json.loads(read_upstream_bytes(upstream))
                except (OSError, http.client.HTTPException, ValueError) as error:
                    LOG.info("models read via key=%s failed: %s", account.name, error)
                    continue
                finally:
                    upstream.close()
                MODELS_CACHE.update({"at": time.time(), "payload": models})
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
                stats = collect_stats(window_days)
            except sqlite3.Error as error:
                return self._reply(500, {"error": {"message": f"usage db: {error}"}})
            stats["safety"] = {
                "gateway_key_set": bool(os.environ.get("GATEWAY_KEY")),
                "bind": os.environ.get("BIND", "127.0.0.1"),
                "single_account": True,
                "accounts": [a.name for a in POOL.accounts] if POOL else [],
                "rate_limit_per_min": rate_limit_per_min(),
                "max_concurrent": MAX_CONCURRENT,
                "web_search": search_enabled(),
                "search_provider": (os.environ.get("SEARCH_PROVIDER") or ("tavily" if os.environ.get("SEARCH_API_KEY") else "ddg")),
                "models_cached_s": MODELS_CACHE_TTL,
            }
            return self._reply(200, stats)
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
        if not _rate_ok():
            return self._reply(429, {"error": {"message": "gateway rate limit (protects the subscription account)", "type": "rate_limit"}})
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
        # Variant B web search: only for plain chat requests that bring no
        # tools of their own (agent clients execute tool calls themselves)
        # and only non-streaming, where the gateway can run the search and
        # feed the results back in a second round.
        self._search_injected = False
        self._chat_request_orig = None
        if search_enabled() and not stream and not chat_request.get("tools"):
            import copy as _copy
            injected = _copy.deepcopy(chat_request)
            injected["tools"] = [WEB_SEARCH_TOOL]
            self._chat_request_orig = injected
            self._search_injected = True
            body = json.dumps(injected).encode()
            self._request_model = chat_request.get("model") or "unknown"
            return self._proxy_body(body, stream)
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
    global _concurrency, MAX_CONCURRENT
    try:
        MAX_CONCURRENT = max(1, int(os.environ.get("MAX_CONCURRENT", "6")))
    except ValueError:
        MAX_CONCURRENT = 6
    _concurrency = threading.BoundedSemaphore(MAX_CONCURRENT)
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

# mistral-gateway

A small local proxy that lets any OpenAI-compatible chat client (Open WebUI, editors, scripts) use the models included in your Mistral Vibe subscription. It is a single Python file with no dependencies beyond the standard library.

It uses the same key the official [Mistral Vibe CLI](https://github.com/mistralai/mistral-vibe) creates when you log in, sends requests to Mistral's public API and returns the answers in the exact shape OpenAI clients expect.

> **Unofficial project.** Not affiliated with, endorsed by or supported by Mistral AI. Read the "Terms of service" section below before using it.

## What it does

- Exposes `/v1/chat/completions`, `/v1/responses` and `/v1/models` on `127.0.0.1:8788`, so a client only needs a base URL. Chat Completions requests go to Mistral's Chat Completions API and Responses requests to Mistral's Responses API.
- Fixes wire-format differences that break OpenAI clients:
  - reasoning models return `content` as a list of typed parts; the gateway flattens it to plain text and moves the thinking into `reasoning_content`, in both streaming and non-streaming replies
  - a final `finish_reason: "stop"` after tool-call deltas is rewritten to `tool_calls`
  - `stream_options.include_usage` is added to streaming requests so clients get token counts
  - `max_tokens` defaults to 32768 when the client sends none, and to 131072 (the 128K maximum) for GLM (Mistral's default is 4096, which reasoning models can use up before writing any visible text)
  - `reasoning_effort: "high"` is added to GLM turns that answer a tool result, because GLM sometimes ends those turns with empty output otherwise
  - `max_completion_tokens` and `seed` are passed on as Mistral's `max_tokens` and `random_seed`, and `user` is left out, because Mistral rejects all three
  - `reasoning_content` and `index` are left out of earlier assistant messages, because replies carry them but Mistral rejects them when a client sends a reply back as it came (the OpenAI SDK does this when you append the reply message, and DeepSeek's own API asks for the thinking back)
  - a `reasoning_effort` (or `reasoning.effort` on Responses) the model rejects is retried with the closest level it accepts, or without it for models that have no reasoning (see [Models](#models))
- Responses requests are passed on unchanged apart from that reasoning level, because Mistral's Responses API already answers in the shape OpenAI clients expect. It keeps no history, so `store: true` and `previous_response_id` are rejected and the client has to send the whole conversation each time.
- Handles rate limits politely: on HTTP 429 it waits and retries the same key every 10 s for up to 60 s.
- Optional fallback to a Mistral AI Studio API key when the Vibe key is rate limited or its budget is spent.
- Records daily token usage per model in a local SQLite file (`usage.db`), readable at `/usage`.

## Models

The chat models below were tested through the gateway on 2026-10-06 (GLM 5.3 Flash on 2026-10-23, DeepSeek V4.1 Flash on 2026-10-28), including tool calls and every `reasoning_effort` level. Every model in the table was also tested with tool calls over `/v1/responses` on 2026-10-28, and Mistral Large 4, GLM 5.3, GLM 5.3 Flash and DeepSeek V4.1 Flash with streaming and every reasoning level too. Mistral changes the line-up from time to time; `/v1/models` always lists what your key can use.

| Model | Also available as | Context | Images | Reasoning effort |
|---|---|---|---|---|
| `mistral-large-4` | `mistral-large-4-0` | 1M | yes | `none`, `high` |
| `mistral-medium-latest` | `mistral-medium-3-5`, `mistral-medium-2604`, `mistral-vibe-cli-latest` | 256K | yes | `none`, `high` |
| `mistral-small-latest` | `mistral-small-2603`, `mistral-vibe-cli-fast` | 256K | yes | `none`, `high` |
| `zai-glm-5-3` | `zai-glm-latest`, `zai-glm-5` | 1M | no | `low`, `high`, `max` |
| `zai-glm-5-3-flash` | none | 1M | yes | `low`, `high`, `max` |
| `deepseek-v4-1-flash-1017` | none | 1M | no | `none`, `low`, `high`, `xhigh`, `max` |
| `mistral-large-latest` | `mistral-large-2512` | 256K | yes | no reasoning |
| `codestral-latest` | `codestral-2508` | 128K | no | no reasoning |
| `ministral-14b-latest` | `ministral-14b-2512` | 256K | yes | no reasoning |
| `ministral-8b-latest` | `ministral-8b-2512` | 256K | yes | no reasoning |
| `ministral-3b-latest` | `ministral-3b-2512` | 256K | yes | no reasoning |
| `voxtral-small-latest` | `voxtral-small-2507` | 32K | no | no reasoning |

All of them handle tool calls. Clients can send any standard `reasoning_effort` level to any model. When a model does not offer the requested level, the gateway uses the closest one it does offer, and on a tie `minimal` and `low` go down while everything else goes up: on Mistral Large 4, Medium and Small, `minimal` and `low` become `none` and the rest become `high`. Models without reasoning get the request without it. DeepSeek V4.1 Flash only thinks when the client sends a reasoning level. Mistral's docs list it as `deepseek-v4-flash-1017`, but the API only accepts `deepseek-v4-1-flash-1017`. Labs models such as `labs-leanstral-1-5` only answer once an admin enables Labs models in the organization settings. Embedding, OCR and transcription models are listed too, but the gateway only serves chat.

## This fork (NikGob)

Fork of `Classic298/mistral-gateway` 1.0.4 with a security audit (see
[`AUDIT.md`](AUDIT.md), including the Russia/RF-IP account-risk section) and:

- **Web panel (Material Design 3)** at `/` or `/panel`: token totals, per-model
  and per-day usage, key-pool status, and the equivalent pay-as-you-go value
  for models with a published price (e.g. GLM 5.3 Flash at $0.15/$0.03/$0.50
  per 1M input/cached/output). Vibe-plan traffic is covered by the flat
  subscription — the dollar figure is "what this would cost on Studio",
  not money charged. `GLM 5.3 Flash` (`zai-glm-5-3-flash`, released
  2026-10-22) is highlighted in the model catalogue; the gateway itself
  serves every model your key can use via `/v1/models`.
- **`POST /v1/conversations` passthrough**, the only route where Mistral's
  built-in `web_search` / `web_search_premium` / `code_interpreter` tools work.
- **Server-side web search (Variant B)** with `WEB_SEARCH=1`: for plain
  non-streaming chat requests that bring no tools of their own, the gateway
  injects a `web_search(query)` function, executes the model's call itself
  (DuckDuckGo by default — no key; or Tavily/Serper with `SEARCH_API_KEY`)
  and feeds the results back in a second round (max 2 rounds). Agent clients
  that execute tool calls themselves are never intercepted.
- **Anti-ban guards:** `RATE_LIMIT_PER_MIN` and `MAX_CONCURRENT` caps so a
  runaway client cannot hammer the single subscription account, on top of
  the upstream polite 429 wait-and-retry. The panel has a "subscription
  protection" card showing these live.
- **Hardening:** refuses to start on a non-loopback bind without
  `GATEWAY_KEY` (the old keyless `Host`-header check was spoofable),
  `MAX_REQUEST_BYTES` cap (default 10 MiB), POST routes tolerate query
  strings, reasoning-effort retry capped, `DATA_DIR` for container state.
- **Docker:** `Dockerfile` + `docker-compose.yml` (LAN-IP-only port publish,
  non-root, read-only FS, state in `./data`). On the Pi:

  ```sh
  git clone https://github.com/NikGob/mistral-gateway.git ~/mistral-gateway
  cd ~/mistral-gateway && cp .env.example .env  # fill VIBE_KEY + GATEWAY_KEY
  docker compose up -d --build
  # panel: http://192.168.0.157:18788/   API: http://192.168.0.157:18788/v1
  ```

## Setup

Requirements: Python 3.10+ and a Mistral Vibe plan with coding included (Pro, Team or Enterprise).

1. Install the official Vibe CLI (see the [Mistral Vibe repository](https://github.com/mistralai/mistral-vibe) for other install methods):

   ```sh
   curl -LsSf https://mistral.ai/vibe/install.sh | bash
   ```

2. Log in with your Mistral account. Pick the browser login in the setup wizard:

   ```sh
   vibe --setup
   ```

   The CLI stores your plan key in `~/.vibe/.env`. The gateway reads it from there, so you never copy the key by hand.

   Without the CLI: open [Code › Extensions](https://chat.mistral.ai/code/extensions) in Vibe, expand **Advanced** on the Vibe CLI card and copy the **Vibe API Key**. Put it in `gateway.env` as `VIBE_KEY=...`. The same panel shows your monthly usage and lets you rotate the key.

3. Get the gateway and start it:

   ```sh
   git clone https://github.com/Classic298/mistral-gateway.git
   cd mistral-gateway
   python3 gateway.py
   ```

   The log shows your plan name when the key works:

   ```
   INFO vibe plan: ... (...)
   INFO mistral gateway on 127.0.0.1:8788 (keys: vibe)
   ```

4. Point your client at it:

   - Base URL: `http://127.0.0.1:8788/v1`
   - API key: anything (or the value of `GATEWAY_KEY` if you set one)
   - Model: any ID from `curl http://127.0.0.1:8788/v1/models`

   Requests must be sent as `Content-Type: application/json`. Browsers cannot call the gateway directly; it is meant for server-side and desktop clients.

   Quick test:

   ```sh
   curl http://127.0.0.1:8788/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model": "mistral-small-latest", "messages": [{"role": "user", "content": "Hello"}]}'
   ```

### Run it as a service (Linux, optional)

```sh
mkdir -p ~/.config/systemd/user
cp mistral-gateway.service.example ~/.config/systemd/user/mistral-gateway.service
# the example assumes the clone is at ~/mistral-gateway; otherwise edit WorkingDirectory and ExecStart
systemctl --user daemon-reload
systemctl --user enable --now mistral-gateway
journalctl --user -u mistral-gateway -f
```

## Configuration

Settings come from environment variables or from a `gateway.env` file next to `gateway.py` (see `gateway.env.example`; `gateway.env` wins over the environment).

| Variable | Default | Meaning |
|---|---|---|
| `VIBE_KEY` | `MISTRAL_API_KEY` from `~/.vibe/.env` | Your Vibe plan key. Set it if you copied the key from Code › Extensions, or to override the CLI's file. |
| `VIBE_ENV_FILE` | `~/.vibe/.env` | Where to look for the Vibe CLI's key. |
| `STUDIO_API_KEY` | unset | Optional pay-as-you-go fallback key from Mistral AI Studio. |
| `GATEWAY_KEY` | unset | If set, clients must send it as `Authorization: Bearer <key>`. Without it the gateway only answers requests addressed to `localhost`, `127.0.0.1` or `[::1]`, so set it if a client reaches the gateway under another hostname (e.g. from a Docker container). |
| `BIND` | `127.0.0.1` | Listen address. Keep it on loopback (see below). |
| `PORT` | `8788` | Listen port. |
| `RETRY_429_SECONDS` / `RETRY_429_INTERVAL` | `60` / `10` | How long and how often to retry a rate-limited key. |
| `COOLDOWN_429` / `COOLDOWN_DEAD` | `60` / `21600` | Seconds a key is skipped after rate limiting / after 401 or 402. |
| `LOG_LEVEL` | `INFO` | Python logging level. |

Endpoints: `/v1/chat/completions`, `/v1/responses`, `/v1/models`, `/health`, `/pool/status` (which key is serving, cooldowns) and `/usage?days=N`.

## Terms of service

Checked against the [Mistral AI Terms of Service for EU consumers](https://legal.mistral.ai/terms/eu-consumers-terms-of-service) (effective August 7, 2026) and the [Mistral AI Commercial Terms of Service](https://legal.mistral.ai/terms/commercial-terms-of-service) (effective August 5, 2026). Consumers outside the EU have [separate terms](https://legal.mistral.ai/terms). This section is the author's reading of those documents and is **not legal advice**.

**Why the gateway is designed to stay within those terms:**

- **Official key, official API.** The key comes from Mistral's own login flow in the official Vibe CLI. The gateway calls the same public API endpoint with the same standard `Authorization: Bearer` header the CLI uses. It does not scrape, impersonate the CLI, reverse engineer anything or touch any security mechanism. Mistral's [API key documentation](https://docs.mistral.ai/admin/identity-access/api-keys) lists Vibe keys as "Keys used by Vibe Code" and states that "Vibe-only users usually do not need API keys unless they also use Studio, the API, Vibe Code, or another developer tool."
- **Your plan's limits stay in force.** Every request counts against your plan exactly like a Vibe CLI request. A rate limit makes the gateway wait and retry the same key; it never switches to another Vibe account to get around a limit. The only fallback is your own optional Studio key.
- **One person, one account.** The EU consumer terms state: "The creation or use of multiple Mistral AI accounts by a single individual is strictly prohibited, including to bypass rate limits or any other restrictions." The gateway therefore supports exactly one Vibe key.
- **Personal use only.** The consumer terms also state: "Your account is intended for your individual use only, and you may not share your account with any other person. [...] You may not make your account credentials available to third parties, [...] or resell or lease access to your account." The commercial terms (section 2.2) forbid customers to "buy, sell, or transfer API keys" and to "grant any third party access to the Mistral AI Products without our prior written authorization". The gateway therefore listens on `127.0.0.1` only. **Do not expose it to the internet or share it with other people**; doing so would hand them access to your account.

**Mistral has confirmed third-party use.** In a [stickied comment on the official r/MistralAI subreddit](https://www.reddit.com/r/MistralAI/comments/1ta8itv/comment/olccfnn/), a moderator with the Mistral team badge wrote: "you are currently able to freely use the Vibe key with any software, including Hermes and any other packages or third-party services. This has always been the case, meaning if you used the Vibe API key via third parties, the Vibe plan budget would be used first before any PAYG." In the same thread, the user who asked reports that Mistral support confirmed this and refunded the API charges they had been billed.

**Your responsibility.** Mistral can change its terms at any time. Before you use this gateway, and again whenever Mistral announces a change, read the current terms yourself and stop using the gateway if they no longer allow it. Mistral decides how to enforce its own terms, and nobody can promise that an account will not be restricted or suspended.

## Disclaimer

This software is provided "as is" under the [PolyForm Noncommercial License 1.0.0](LICENSE), without warranty of any kind. The author accepts no liability for any damage or loss arising from its use, including suspension, restriction or termination of your Mistral account, lost subscription fees or lost data.

## License

[PolyForm Noncommercial 1.0.0](LICENSE): free for personal, hobby, research and other noncommercial use. Commercial use is not permitted.

It is noncommercial because a Mistral Vibe subscription is a consumer plan: Mistral's consumer terms only cover "your personal use as a consumer", and business use falls under their separate [Commercial Terms of Service](https://legal.mistral.ai/terms/commercial-terms-of-service).

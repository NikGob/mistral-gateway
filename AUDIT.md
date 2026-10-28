# Audit of upstream 1.0.4 (24cf304) — 2026-10-28

Single stdlib-only Python file. No dependency supply chain, no telemetry.

## Malicious code / "cheats": none found

- The only outbound hosts are hardcoded `api.mistral.ai` and
  `console.mistral.ai` (whoami at startup). No `eval`/`exec`/`subprocess`/
  `os.system`, no pickle, no dynamic downloads, no data exfiltration paths.
- SQL is parameterized; keys are only ever sent as `Authorization: Bearer`
  to Mistral; logs print key *names* and upstream error bodies, never keys.
- It is not a limit bypass: one Vibe key, 429 = wait-and-retry the same key,
  no multi-account rotation. The only fallback is your own Studio PAYG key.
  The residual risk is Mistral's ToS interpretation of third-party clients
  (see README "Terms of service") — an account-policy risk, not a code trick.

## Exploits / security bugs

1. **[fixed in this fork] Keyless mode is not authenticated.** With no
   `GATEWAY_KEY`, access control was the client-supplied `Host` header.
   Anyone able to reach the port (e.g. after setting `BIND=0.0.0.0` or via a
   Docker port publish) can send `Host: localhost` and use your Vibe plan.
   Fix: the gateway now refuses to start on a non-loopback bind without
   `GATEWAY_KEY`, and the Docker setup always sets one and publishes the
   port on the LAN address only.
2. **[fixed] Unbounded request body.** `rfile.read(Content-Length)` with no
   cap = one request can OOM the process. Fix: `MAX_REQUEST_BYTES`
   (default 10 MiB) → 413.
3. **[mitigated, inherent] Unbounded threads / 600 s upstream timeout.**
   `ThreadingHTTPServer` spawns a thread per connection; a slow client can
   pin threads for up to 10 minutes. Fine for a LAN-only personal gateway;
   do not expose it beyond the LAN.
4. **Upstream error bodies are echoed to the client** (≤500 chars) and logged
   (≤200 chars). No key material in them, but they can reveal model/plan
   details to anyone holding `GATEWAY_KEY`.
5. `GATEWAY_KEY` comparison accepts the bare key without the `Bearer `
   prefix (it strips the prefix if present). Harmless, but clients should
   send the standard header.

## Plain bugs

1. **[fixed] POST routes broke on query strings** (`/v1/responses?x=1` → 404,
   and URL selection compared the raw `self.path`). Routes are now matched
   on the part before `?`.
2. **[fixed] `snap_reasoning_effort` could recurse** through `_proxy_body`
   once per rejection; now capped at 2 retries.
3. **Conversations/Agents were not proxied at all**, so Mistral's built-in
   `web_search` was unreachable through the gateway. **[added]**:
   `POST /v1/conversations` is passed through untouched.
4. Streaming event parsing splits on `\n\n`; CRLF-framed streams are only
   handled by the cut-event heuristic. Carried over from upstream.
5. `usage.db` / `pool_state.json` were written next to the script, which
   breaks in a read-only container. **[fixed]** with `DATA_DIR`.

## Russia / RF-IP ban risk (checked 2026-10-28)

Facts from the actual Pi (external IP in Novosibirsk, RU):

- `GET https://api.mistral.ai/v1/models` from the Pi returns **401
  "Invalid API Key"** in ~0.2 s — the API is *reachable* from the RU IP,
  there is no network-level geo-block (a geo-block would be 403).
  `chat.mistral.ai` returns 403 (Cloudflare bot wall for curl, not proof of
  a country ban); `console.mistral.ai` returns a normal 302 login redirect.
- Mistral's Commercial ToS §14.13 and the consumer terms list
  comprehensively-sanctioned territories: Cuba, Iran, North Korea, Syria,
  Crimea/Donetsk/Luhansk. **Russia (proper) is not listed.** There is no
  official supported-countries list naming Russia as prohibited; the
  third-party "AI blocked" lists that name Russia are undated and
  unverified, and contradict the live 401 test above.

Residual risks, in order of likelihood:

1. **Payment/registration, not inference.** An account registered and paid
   from a non-RU profile that suddenly only calls from RU IPs is a fraud/
   geo signal every provider watches. Mistral may ask for verification or
   restrict the *account* under the catch-all "comply with applicable law /
   risk of harm" suspension clause (§11.3 commercial, §7/§9 consumer),
   which allows immediate suspension with appeal to support@mistral.ai.
2. **Policy change.** The EU can expand sanctions and Mistral can add a
   geo-block at any time; then the Pi gets 403 and the gateway fails safe
   (errors, no retry storm beyond the 429 logic).
3. **The gateway itself is the bigger ToS surface** than the IP: consumer
   terms say one person, no sharing. Keep it LAN-only, one account, do not
   publish the port, do not let other people use it.

Verdict: hosting on the RU-IP Pi is **not an automatic ban** and works
today at network level, but it is not zero-risk. The safe posture is:
personal use only, LAN-only bind, strong `GATEWAY_KEY`, and accept that a
future geo-policy change can cut access without notice.

## Web search through this gateway

Mistral docs: `web_search` / `web_search_premium` and `code_interpreter`
work **only** on the Conversations/Agents API, never on Chat Completions:

```sh
curl http://192.168.0.157:18788/v1/conversations \
  -H "Authorization: Bearer $GATEWAY_KEY" -H "Content-Type: application/json" \
  -d '{"model":"zai-glm-5-3-flash","inputs":"Latest news about Mistral",
       "tools":[{"type":"web_search"}]}'
```

In a normal chat client (Open WebUI etc.) the practical equivalent is the
client's own function/MCP search tool, which works over
`/v1/chat/completions` because the model only *calls* the function — the
gateway already passes tool calls through.

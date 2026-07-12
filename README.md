# fetch-url-raw

Short description: a **stateless MCP server** that exposes one tool, `fetch_url_raw` — a lightweight programmable HTTP client for LLM agents. Send arbitrary methods, headers, and bodies; control timeouts, redirects, response size, TLS verification, and DNS overrides. No cookies or session state between calls.

## Requirements

- Python 3.12+
- Network access from the host that runs the server

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

For tests:

```bash
pip install -e '.[dev]'
```

## Deployment

The server speaks **MCP over stdio**. Deploy it as a local process that your MCP client launches; it does not open a public HTTP port by default.

### 1. Install into a venv (recommended)

```bash
cd /path/to/fetch-url-raw
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Confirm the entry point:

```bash
fetch-url-raw
# process waits on stdin (MCP). Stop with Ctrl+C.
```

Or:

```bash
python -m fetch_url_raw
```

### 2. Wire into an MCP client

Point the client at the venv interpreter (or the `fetch-url-raw` script) so dependencies resolve correctly.

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "fetch-url-raw": {
      "command": "/path/to/fetch-url-raw/.venv/bin/python",
      "args": ["-m", "fetch_url_raw"],
      "cwd": "/path/to/fetch-url-raw"
    }
  }
}
```

**Generic MCP host / Cursor-style config:**

```json
{
  "mcpServers": {
    "fetch-url-raw": {
      "command": "/path/to/fetch-url-raw/.venv/bin/fetch-url-raw"
    }
  }
}
```

**Codex / other stdio MCP runners:** use the same `command` + `args` pattern; no extra env is required for basic use.

### 3. Operational notes

| Topic | Guidance |
|-------|----------|
| State | Stateless — safe to restart anytime; no DB or disk cache |
| Network | Outbound HTTP/HTTPS only; needs reachability to targets you fetch |
| Security | Tool can hit arbitrary URLs — run only for trusted clients; consider host firewall / network policy |
| Resources | Response bodies are capped (`max_response_bytes`, default 1 MiB) to limit memory |
| Proxies | System proxy env is ignored (`trust_env=False`) for predictable behavior |
| Logs | Server logs go to stderr; keep stdin/stdout for MCP framing |

### 4. Optional: install from wheel

```bash
pip install dist/fetch_url_raw-0.1.0-py3-none-any.whl
fetch-url-raw
```

## Usage

Once the MCP server is connected, call the `fetch_url_raw` tool from the client.

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | string | **required** | Absolute `http` or `https` URL |
| `method` | string | `GET` | HTTP method (normalized to uppercase) |
| `headers` | object | — | Request headers (`string` → `string`) |
| `body` | string | — | Raw body (no automatic JSON encoding) |
| `timeout` | number | `30` | Timeout in seconds |
| `follow_redirect` | bool | `true` | Follow redirects |
| `max_response_bytes` | int | `1048576` | Stop reading after this many body bytes |
| `dns_override` | object | — | Hostname → IP map (like `curl --resolve`) |
| `verify_tls` | bool | `true` | Verify TLS certificates |

### Example tool calls

**GET**

```json
{
  "url": "https://example.com/"
}
```

**POST JSON**

```json
{
  "url": "https://httpbin.org/post",
  "method": "POST",
  "headers": {
    "Content-Type": "application/json",
    "Authorization": "Bearer token"
  },
  "body": "{\"hello\":\"world\"}",
  "timeout": 15
}
```

**No redirects + small body cap**

```json
{
  "url": "https://example.com/redirect",
  "follow_redirect": false,
  "max_response_bytes": 4096
}
```

**DNS override** (connect to `1.2.3.4` while keeping Host/SNI as `api.example.com`)

```json
{
  "url": "https://api.example.com/health",
  "dns_override": {
    "api.example.com": "1.2.3.4"
  },
  "verify_tls": true
}
```

### Success response

```json
{
  "success": true,
  "status": 200,
  "reason": "OK",
  "http_version": "HTTP/1.1",
  "headers": { "...": "..." },
  "body": "...",
  "body_base64": null,
  "content_type": "text/html",
  "encoding": "utf-8",
  "elapsed_ms": 123,
  "redirected": false,
  "final_url": "https://example.com",
  "truncated": false,
  "received_bytes": 12345
}
```

- Text-like content types (`text/*`, `application/json`, etc.) fill `body`.
- Other types set `body` to `null` and put Base64 data in `body_base64`.
- If the body hits `max_response_bytes`, `truncated` is `true` and only the first N bytes are returned.

### Error response

Failures return a structured object instead of raising:

```json
{
  "success": false,
  "error": {
    "type": "TIMEOUT",
    "message": "Operation timed out"
  }
}
```

| `error.type` | Meaning |
|--------------|---------|
| `INVALID_URL` | Missing/unsupported scheme or host |
| `INVALID_PARAMETER` | Bad method, headers, timeout, etc. |
| `DNS_ERROR` | Hostname could not be resolved |
| `TIMEOUT` | Connect/read/write timed out |
| `TLS_ERROR` | Certificate or TLS failure |
| `CONNECT_ERROR` | TCP/connect failure |
| `PROTOCOL_ERROR` | HTTP protocol / too many redirects |
| `HTTP_ERROR` / `ERROR` | Other HTTP or unexpected failure |

## Features

- Methods: `GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`, `OPTIONS`, `TRACE`
- Custom headers and raw body
- Timeout, redirect control, response size limit
- DNS override (SNI and Host header preserved)
- TLS verification toggle
- Stateless: no cookies, session cache, or filesystem writes
- Structured errors suitable for LLM tool loops

## Development

```bash
pip install -e '.[dev]'
pytest
FETCH_URL_RAW_LIVE=1 pytest tests/test_live.py   # optional live network tests
```

## Design

See [design.md](design.md) for architecture, DNS override details, body decoding rules, and security considerations.

## License

MIT

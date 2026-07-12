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

Confirm the entry point (stdio by default; waits on stdin for MCP):

```bash
fetch-url-raw
# Stop with Ctrl+C.
```

Or:

```bash
python -m fetch_url_raw
```

HTTP mode is off by default. To enable it, see section 3 below.

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

### 3. Optional HTTP server mode (not enabled by default)

Default transport is **stdio**. To expose MCP over HTTP instead, pass `--transport` explicitly and set listen address/port:

```bash
# Streamable HTTP (recommended HTTP transport)
fetch-url-raw --transport streamable-http --host 127.0.0.1 --port 8000

# SSE transport
fetch-url-raw --transport sse --host 127.0.0.1 --port 8000

# Bind all interfaces (trusted networks only)
fetch-url-raw --transport streamable-http --host 0.0.0.0 --port 9000 --allow-remote
```

| Flag | Default | Description |
|------|---------|-------------|
| `--transport` | `stdio` | `stdio`, `streamable-http`, or `sse` |
| `--host` | `127.0.0.1` | Listen IP (HTTP transports only) |
| `--port` | `8000` | Listen port (HTTP transports only) |
| `--path` | `/mcp` or `/sse` | Endpoint path (`streamable-http` → `/mcp`, `sse` → `/sse`) |
| `--stateless-http` | off | FastMCP stateless HTTP mode (`streamable-http` only) |
| `--allow-remote` | off | Relax Host/Origin DNS-rebinding checks for non-local clients |
| `--allow-private-network` | off | Allow resolved private/local destination IPs (see below) |
| `--log-level` | `INFO` | Uvicorn/server log level |

Endpoints:

- `streamable-http`: `http://<host>:<port>/mcp` (or custom `--path`)
- `sse`: `http://<host>:<port>/sse` (messages under the default FastMCP message path)

Example MCP client config against a local HTTP server (client-specific; streamable HTTP):

```json
{
  "mcpServers": {
    "fetch-url-raw": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

> HTTP mode is optional. Prefer stdio for desktop/local agent integrations. Only bind `0.0.0.0` or enable `--allow-remote` on trusted networks; the tool can initiate arbitrary outbound HTTP.

### Private / local destination blocking

By default the server **blocks connections by resolved destination IP** (not by DNS name):

- DNS lookup is always allowed
- After resolve (or `dns_override` / literal IP URL), the TCP destination must not fall in private/local ranges
- Blocked by default: RFC1918 (`10/8`, `172.16/12`, `192.168/16`), loopback, link-local (`169.254/16`, `fe80::/10`), CGNAT (`100.64/10`), IPv6 ULA (`fc00::/7`)
- Always blocked: multicast / unspecified special-use ranges
- IPv4-mapped IPv6 addresses are checked as their IPv4 form

Opt in when you intentionally need LAN/metadata/loopback targets:

```bash
fetch-url-raw --allow-private-network
# with HTTP mode:
fetch-url-raw --transport streamable-http --host 127.0.0.1 --port 8000 --allow-private-network
```

Blocked attempts return:

```json
{
  "success": false,
  "error": {
    "type": "DESTINATION_BLOCKED",
    "message": "destination IP 192.168.1.1 is private/local (...)"
  }
}
```

### 4. Operational notes

| Topic | Guidance |
|-------|----------|
| State | Stateless — safe to restart anytime; no DB or disk cache |
| Network | Outbound HTTP/HTTPS only; needs reachability to targets you fetch |
| Security | Tool can hit arbitrary URLs — run only for trusted clients; consider host firewall / network policy |
| Resources | Returned body is capped (`max_response_bytes`, default 1 MiB); up to 16 MiB may be buffered to decode large text |
| Proxies | System proxy env is ignored (`trust_env=False`) for predictable behavior |
| Logs | Server logs go to stderr; keep stdin/stdout for MCP framing in stdio mode |
| HTTP listen | Not started unless `--transport streamable-http` or `--transport sse` is set |

### 5. Optional: install from wheel

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
| `body` | string \| object \| array \| number \| bool | — | Request body: raw string as-is, or JSON value (object/array/number/bool) which is serialized and gets `Content-Type: application/json` when unset |
| `timeout` | number | `30` | Timeout in seconds |
| `follow_redirect` | bool | `true` | Follow redirects |
| `max_response_bytes` | int | `1048576` | Max body bytes returned to the LLM. Internally buffers up to 16 MiB so large text can still be decoded, then truncates the returned `body`/`body_base64` |
| `dns_override` | object | — | Hostname → IP map (like `curl --resolve`) |
| `verify_tls` | bool | `true` | Verify TLS certificates |
| `include_tls` | bool | `false` | Include TLS metadata + peer cert(s) in the result (HTTPS) |
| `tls_only` | bool | `false` | Handshake only (no HTTP). Requires `include_tls=true` and `https` |

### Example tool calls

**GET**

```json
{
  "url": "https://example.com/"
}
```

**POST JSON** (LLM-friendly: pass a JSON object directly; string body still works)

```json
{
  "url": "https://httpbin.org/post",
  "method": "POST",
  "headers": {
    "Authorization": "Bearer token"
  },
  "body": {"hello": "world"},
  "timeout": 15
}
```

Raw string body (no auto Content-Type):

```json
{
  "url": "https://httpbin.org/post",
  "method": "POST",
  "headers": {
    "Content-Type": "application/x-www-form-urlencoded"
  },
  "body": "hello=world&a=1"
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

**Inspect TLS cert only** (no HTTP request)

```json
{
  "url": "https://example.com/",
  "include_tls": true,
  "tls_only": true
}
```

**Fetch and include TLS info**

```json
{
  "url": "https://example.com/",
  "include_tls": true,
  "max_response_bytes": 4096
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
  "body_json": null,
  "body_base64": null,
  "content_type": "text/html",
  "encoding": "utf-8",
  "elapsed_ms": 123,
  "redirected": false,
  "final_url": "https://example.com",
  "truncated": false,
  "received_bytes": 12345,
  "content_length": 12345
}
```

- Text-like content types (`text/*`, `application/json`, etc.) fill `body` (string still kept).
- `body_json` is set **only** when the body is valid JSON (object/array/etc.); otherwise `null`.
- Other non-text types set `body` to `null` and put Base64 data in `body_base64`.
- Internally the client may buffer up to **16 MiB** so large text (e.g. JS bundles) can be decoded as text even when `max_response_bytes` is smaller. The tool result is then truncated to `max_response_bytes`.
- If the returned body is truncated, `truncated` is `true` and only the first N bytes (or text whose UTF-8 size is N) are returned.
- `received_bytes` is how many body bytes were actually returned to the LLM (after truncation).
- Optional `tls` object (when `include_tls` or on many `TLS_ERROR`s): version, cipher, ALPN, SNI, peer IP, leaf cert PEM/fingerprint/SAN/dates, and chain.
- `content_length` is the full response body size when known: from the `Content-Length` header if present, otherwise the full size if the body fit in the 16 MiB prefetch buffer, otherwise `null` if the stream was cut at the prefetch ceiling without a header. Use this so agents know the real size (e.g. 1 MiB JS) while reading only the first 64 KiB of text.

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
| `TIMEOUT` | Connect/read/write/pool timed out (message says which phase) |
| `TLS_ERROR` | Certificate or TLS failure (message distinguishes expired, self-signed/untrusted, hostname/SAN mismatch, incomplete chain, outdated/unsupported protocol, malformed TLS) |
| `CONNECT_ERROR` | TCP/connect failure (message distinguishes refused, reset/RST, network/host unreachable, broken pipe, etc.) |
| `DESTINATION_BLOCKED` | Resolved destination IP denied by private-network policy |
| `PROTOCOL_ERROR` | HTTP protocol / too many redirects |
| `HTTP_ERROR` / `ERROR` | Other HTTP or unexpected failure |

Example `message` values:

- `TIMEOUT`: `Connection timed out while establishing TCP/TLS`, `Timed out while reading the response`
- `CONNECT_ERROR`: `Connection refused (no service listening or port closed)`, `Connection reset by peer (TCP RST during connect or request)`, `Network is unreachable (no route to destination network)`, `No route to host (destination host unreachable)`
- `TLS_ERROR`: `TLS certificate has expired`, `TLS certificate is self-signed and not trusted (...)`, `TLS certificate hostname mismatch: ... (wrong CNAME/SAN)`, `TLS certificate chain incomplete or untrusted: unable to get local issuer certificate (...)`, `TLS protocol version mismatch or unsupported/outdated TLS (...)`, `TLS handshake failed: malformed or unexpected TLS data (...)`

## Features

- Methods: `GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`, `OPTIONS`, `TRACE`
- Custom headers and body (raw string or JSON value for LLM-friendly POSTs)
- Timeout, redirect control, response size limit
- DNS override (SNI and Host header preserved)
- TLS verification toggle
- Stateless: no cookies, session cache, or filesystem writes
- Default block of private/local destination IPs (post-resolve); opt-in with `--allow-private-network`
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

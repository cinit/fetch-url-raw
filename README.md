# fetch-url-raw

Stateless [MCP](https://modelcontextprotocol.io/) server that exposes a single tool, `fetch_url_raw`: a lightweight programmable HTTP client for LLM tool use.

## Features

- Arbitrary HTTP methods (`GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`, `OPTIONS`, `TRACE`)
- Optional raw request body (no automatic JSON encoding)
- Custom headers
- Timeout control
- Redirect follow/disable
- Maximum response size with truncation
- DNS resolution override (like `curl --resolve`; SNI/Host preserved)
- TLS verification toggle
- Fully stateless: no cookies, session cache, or filesystem writes
- Structured error objects instead of tracebacks

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
# or with test extras:
pip install -e '.[dev]'
```

## Run

Stdio MCP server:

```bash
fetch-url-raw
# or
python -m fetch_url_raw
```

### Claude Desktop / MCP client config example

```json
{
  "mcpServers": {
    "fetch-url-raw": {
      "command": "python",
      "args": ["-m", "fetch_url_raw"],
      "cwd": "/path/to/fetch-url-raw"
    }
  }
}
```

## Tool: `fetch_url_raw`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | string | required | Absolute `http`/`https` URL |
| `method` | string | `GET` | HTTP method |
| `headers` | object | — | Request headers |
| `body` | string | — | Raw body |
| `timeout` | number | `30` | Seconds |
| `follow_redirect` | bool | `true` | Follow redirects |
| `max_response_bytes` | int | `1048576` | Response body cap |
| `dns_override` | object | — | `{ "host": "1.2.3.4" }` |
| `verify_tls` | bool | `true` | Verify TLS certificates |

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

Binary (non-text) bodies set `body` to `null` and populate `body_base64`.

### Error response

```json
{
  "success": false,
  "error": {
    "type": "TIMEOUT",
    "message": "Operation timed out"
  }
}
```

Error types include: `INVALID_URL`, `INVALID_PARAMETER`, `DNS_ERROR`, `TIMEOUT`, `TLS_ERROR`, `CONNECT_ERROR`, `PROTOCOL_ERROR`, `HTTP_ERROR`, `ERROR`.

## Development

```bash
pip install -e '.[dev]'
pytest
```

## Design

See [design.md](design.md) for architecture, DNS override approach, body decoding rules, and security notes.

## License

MIT

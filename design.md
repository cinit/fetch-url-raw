# Design Plan: `fetch_url_raw` MCP Server (Python)

## 1. Goals

Implement a **stateless** MCP server exposing one tool:

* `fetch_url_raw`

The tool behaves like a lightweight programmable HTTP client.

Characteristics:

* Supports arbitrary HTTP methods
* Supports optional request body
* Supports custom headers
* Supports timeout
* Supports DNS resolution override
* Supports redirect control
* Supports maximum response size limit
* Stateless
* No cookies/session persistence
* No WebSocket
* No SSE
* No browser emulation
* Suitable for LLM tool use

---

# 2. High-level Architecture

```
+----------------------+
| MCP Client           |
| (Claude/ChatGPT...)  |
+----------+-----------+
           |
           | Tool Call
           |
+----------v-----------+
| MCP Server           |
|                      |
| Parameter Validation |
| URL Parsing          |
| Request Builder      |
| HTTP Executor        |
| Response Limiter     |
| Result Formatter     |
+----------+-----------+
           |
           |
+----------v-----------+
| httpx/httpcore       |
|                      |
| custom transport     |
| optional DNS         |
+----------+-----------+
           |
        Internet
```

---

# 3. Technology Choice

## Language

Python 3.12+

---

## MCP SDK

Official MCP Python SDK

---

## HTTP Library

Prefer:

```
httpx
```

Reasons:

* HTTP/1.1
* HTTP/2
* sync/async
* timeout support
* redirect support
* custom transports
* clean API

---

## DNS Override

Implement using

```
httpcore.AsyncHTTPTransport
```

or custom transport that overrides hostname resolution.

If finer control is needed:

```
httpx
 ↓
httpcore
 ↓
custom socket.create_connection()
```

---

# 4. Tool Interface

## Tool name

```
fetch_url_raw
```

---

## Parameters

### url

```
type: string
required: yes
```

Example

```
https://example.com/
```

---

### method

```
string
optional
default = GET
```

Examples

```
GET
POST
PUT
DELETE
PATCH
HEAD
OPTIONS
TRACE
```

Normalize to uppercase.

---

### headers

```
object<string,string>
optional
```

Example

```json
{
  "Authorization":"Bearer ...",
  "User-Agent":"MyClient/1.0",
  "Accept":"application/json"
}
```

---

### body

```
string | object | array | number | boolean
optional
```

Request payload. Two input styles are accepted so both raw HTTP and LLM tool calls are convenient:

1. **Raw string** — sent as UTF-8 bytes unchanged. Caller sets `Content-Type` if needed (form, XML, already-serialized JSON string, etc.).
2. **JSON value** (object / array / number / boolean) — JSON-encoded automatically. If `Content-Type` is missing, it is set to `application/json; charset=utf-8`.

Examples

Raw string:

```
hello=world&a=1
```

JSON object (preferred for LLM tool arguments):

```json
{"a": 1, "name": "x"}
```

---

### timeout

```
number
optional
default = 30
```

Seconds.

Can be float.

---

### follow_redirect

```
boolean
optional
default = true
```

Equivalent to

```
httpx.follow_redirects
```

---

### max_response_bytes

```
integer
optional
default = 1048576
```

Example

```
1 MiB
```

Once exceeded:

* stop downloading
* mark truncated

---

### dns_override

```
optional
```

Structure:

```json
{
  "example.com":"1.2.3.4",
  "api.example.com":"8.8.8.8"
}
```

Allows multiple overrides.

---

### verify_tls

```
bool
optional
default=true
```

Useful for testing.

---

# 5. Execution Flow

```
Validate Parameters

↓

Normalize Method

↓

Parse URL

↓

Build HTTP Client

↓

Apply DNS Override

↓

Send Request

↓

Stream Response

↓

Enforce Response Limit

↓

Return Result
```

---

# 6. DNS Override Design

## Why

LLMs sometimes need

* testing
* debugging
* virtual hosts
* CDN verification
* reverse proxy validation

without modifying system DNS.

---

## Approach

Maintain map

```
hostname

↓

IP address
```

During connection

Instead of

```
example.com

↓

DNS

↓

93.184.216.34
```

Use

```
example.com

↓

lookup override

↓

1.2.3.4
```

TLS SNI remains

```
example.com
```

Host header remains

```
example.com
```

Only TCP destination changes.

Equivalent to

```
curl --resolve
```

---

# 7. Request Execution

Pseudo flow

```
Client

↓

build request

↓

open stream

↓

send headers

↓

send body

↓

receive response

↓

stream body

↓

count bytes

↓

limit reached?

 ├── no
 │     continue
 │
 └── yes
       abort read
```

---

# 8. Response Object

Suggested JSON

```json
{
  "success": true,

  "status": 200,

  "reason": "OK",

  "http_version": "HTTP/1.1",

  "headers": {
    "...":"..."
  },

  "body": "...",

  "body_base64": null,

  "content_type":"text/html",

  "encoding":"utf-8",

  "elapsed_ms":123,

  "redirected":false,

  "final_url":"https://example.com",

  "truncated":false,

  "received_bytes":12345
}
```

---

# 9. Body Decoding

Decision tree

```
Content-Type

↓

text/*
↓

decode

↓

return body
```

Otherwise

```
application/octet-stream

↓

base64
```

Return

```
body_base64
```

instead.

Avoid corrupt UTF-8.

---

# 10. Response Size Limit

Never call

```
response.text
```

Instead

```
async for chunk in response.aiter_bytes():
```

Maintain

```
received += len(chunk)
```

If

```
received > limit
```

then

```
stop
```

Mark

```
truncated=true
```

This avoids downloading a 10 GB file into memory.

---

# 11. Redirect Handling

If

```
follow_redirect=false
```

Return

```
302

Location
```

unchanged.

If enabled

```
302

↓

GET

↓

301

↓

GET

↓

200
```

Return

```
redirected=true
```

and

```
final_url
```

---

# 12. Error Model

All failures return structured errors instead of Python tracebacks.

Examples:

### DNS failure

```json
{
  "success": false,
  "error": {
    "type": "DNS_ERROR",
    "message": "Cannot resolve hostname"
  }
}
```

---

### Timeout

```json
{
  "success": false,
  "error": {
    "type":"TIMEOUT",
    "message":"Operation timed out"
  }
}
```

---

### TLS

```json
{
  "success": false,
  "error": {
    "type":"TLS_ERROR",
    "message":"TLS certificate hostname mismatch: Hostname mismatch, certificate is not valid for 'wrong.example'. The certificate may be valid but was issued for a different name (wrong CNAME/SAN)."
  }
}
```

TLS messages distinguish common cases: expired, self-signed/untrusted CA, hostname/SAN mismatch, incomplete chain, outdated/unsupported protocol, and malformed TLS (e.g. HTTP on an HTTPS port).

---

### Connection refused

```json
{
  "success": false,
  "error": {
    "type":"CONNECT_ERROR",
    "message":"Connection refused (no service listening or port closed)"
  }
}
```

Connect messages also distinguish TCP reset, network/host unreachable, and similar OS-level failures. Timeouts name the phase (connect vs read vs write).

---

### Invalid URL

```json
{
  "success": false,
  "error": {
    "type":"INVALID_URL"
  }
}
```

---

# 13. Statelessness

The server stores **no persistent state** between calls:

* No cookies
* No authentication cache
* No DNS cache (beyond library/runtime behavior)
* No connection pool reuse requirement
* No request history
* No filesystem writes

Each invocation constructs a fresh HTTP client, performs the request, returns the result, and releases all resources.

---

# 14. Security Considerations

The server intentionally exposes raw HTTP capabilities, so deployment should consider policy controls. Recommended configurable safeguards include:

* Optional allowlist/blocklist for URL schemes (`http`, `https` only by default)
* Optional allowlist/blocklist for destination hosts or CIDR ranges (to mitigate SSRF against internal services)
* Maximum request body size
* Maximum response size
* Maximum redirect count
* Timeout limits (minimum/maximum)
* Header count and total header size limits
* Reject malformed or ambiguous URLs

These can be enabled or disabled by the deployer depending on the trust model.

---

# 15. Future Extensions (Out of Scope)

Potential enhancements that can be added without changing the core API:

* Multipart/form-data uploads
* Streaming request bodies
* Streaming responses back through MCP
* Proxy support (HTTP/SOCKS5)
* Client certificate (mTLS) authentication
* Unix domain socket HTTP
* HTTP/3 (QUIC)
* Brotli/Zstd decompression controls
* Fine-grained connect/read/write timeout settings
* Per-request source IP or network interface binding
* Cookie jar support
* Incremental response streaming to the MCP client
* HAR-format export for debugging

This design keeps the initial implementation compact while providing a flexible, stateless HTTP fetch tool suitable for use by MCP clients.


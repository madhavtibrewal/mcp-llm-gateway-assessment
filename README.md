# MCP & LLM gateway assessment

My solutions to the four tasks. Python 3.12, managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest      # 111 tests, ~30s
```

No API keys needed - everything runs against small mock servers that are part of the repo. A few of the tests start the real servers as separate processes and talk to them over HTTP.

- `task1_mcp_server/` - MCP server with strict input validation (stdio)
- `task2_mcp_gateway/` - security gateway that sits in front of an MCP server
- `task3_pii_guardrail/` - PII redaction on a streaming LLM response
- `task4_model_router/` - token rate limiter + primary/secondary model fallback

## SDK version

I used the current Python SDK (`mcp==2.3.0`, protocol 2026-07-28), which is newer than what the task text assumes. Two changes affect the solutions:

1. The SDK's own validation returns bad tool arguments as a normal result with `isError: true`. That's what [SEP-1303](https://modelcontextprotocol.io/seps/1303-input-validation-errors-as-tool-execution-errors) recommends, so the model can see what it got wrong and retry. The task asks for JSON-RPC error codes though, so in Task 1 I validate the arguments myself and return `-32602`.
2. Newer clients send the method and tool name in `Mcp-Method` / `Mcp-Name` headers as well as in the body. The gateway rejects requests where the two don't match. Older clients don't send these headers at all, and I tested both kinds.

## Task 1 - MCP server

```bash
uv run python -m task1_mcp_server
npx @modelcontextprotocol/inspector uv run python -m task1_mcp_server   # if you want to try it in a UI
```

Two tools, `get_customer_record` and `trigger_refund`, over some fake customers: CUST-00001 (active), CUST-00002 (active, only 50.00 refundable) and CUST-00003 (closed).

The argument rules are Pydantic models in `models.py`: the `CUST-XXXXX` pattern, amount > 0 (finite, max 10,000), reason at least 10 characters after trimming, no extra fields, and no type coercion, so `"10"` isn't accepted as a number. `tools/list` advertises the same models, so clients see the real schema.

To get proper JSON-RPC errors I subclassed `MCPServer` and validate in `call_tool`, before the SDK sees the arguments:
- unknown tool or bad arguments -> `-32602`, with the failing fields in `data`
- valid request that a business rule rejects (unknown customer, refund too big) -> tool result with `isError: true`, because the model should be able to read that
- anything unexpected -> `-32603 Internal error`, with the traceback only in the logs

On stdout: logging goes to stderr, and while the server runs, the SDK points stdout at stderr and writes the protocol to its own copy of the file descriptor. One thing I ran into is that anything `print()` leaves in Python's buffer gets flushed at exit, after the SDK has restored stdout, so it ends up in the client's stream anyway. `main()` points fd 1 at stderr again in a `finally` to fix that. There's a test that prints from inside a tool and checks that every line on stdout is still valid JSON-RPC.

## Task 2 - MCP gateway

```bash
uv run python -m task2_mcp_gateway.mock_downstream --port 9001   # MCP server with an admin_ tool
uv run python -m task2_mcp_gateway --port 8080                   # forwards to :9001

VIEWER=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ2aWV3ZXItdXNlciIsInJvbGUiOiJ2aWV3ZXIiLCJpYXQiOjE3OTM5OTAxNjAsImV4cCI6MTg1NzA2MjE2MH0.zYpWYbuXjspgcsRu1Q7ETkVAJaWChoPJhg0Q9QYC91A
curl -s http://127.0.0.1:8080/mcp -H "Authorization: Bearer $VIEWER" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"key_id":"k1"}}}'
# {"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"Unauthorized Tool Call"}}
curl -s http://127.0.0.1:9001/_stats    # shows the downstream never got the call
```

An admin token, if you want to try the allowed path (both are signed with the dev secret and valid for 2 years; `python -m task2_mcp_gateway.mint_token --role admin` makes new ones):

```
ADMIN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbi11c2VyIiwicm9sZSI6ImFkbWluIiwiaWF0IjoxNzkzOTkwMTYxLCJleHAiOjE4NTcwNjIxNjF9.XVxrP8eR3FW5i9yhCrrGl9YvuEOzjchnNAdGwINHhTE
```

How a `POST /mcp` goes through `gateway.py`:

1. Check the bearer token (HS256 JWT with `sub`, `role`, `exp`). The algorithm is pinned, so `alg: none` tokens don't work. No token or a bad one gets a 401. If the role is something I don't recognise, it's treated as `viewer`.
2. Parse the body. I reject duplicate keys (with `{"name": "echo", "name": "admin_x"}` Python takes the last value and some other parsers take the first) and batches.
3. If the `Mcp-Method` / `Mcp-Name` headers are there, they have to match the body.
4. A `tools/call` for anything starting with `admin_` (case-insensitive) needs the `admin` role. If the caller doesn't have it, the gateway answers `-32001 Unauthorized Tool Call` with the same `id` and doesn't call the downstream server at all. I send that with HTTP 200 rather than 403, because MCP clients treat non-2xx responses as a transport error and the agent would never see the -32001.
5. Everything else gets forwarded: the parsed message (so the server gets exactly what I checked), only the MCP-related headers (not `Authorization`), and the response is streamed back so SSE works.

The end-to-end test runs the official SDK client through the gateway, once with the older session-based protocol and once with the new stateless one.

## Task 3 - streaming PII redaction

```bash
uv run python -m task3_pii_guardrail.mock_llm --port 9201    # streams PII in random 1-6 char pieces
uv run python -m task3_pii_guardrail --port 8081

curl -N http://127.0.0.1:8081/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"mock","stream":true,"messages":[{"role":"user","content":"hi"}]}'
```

It redacts emails, US SSNs and card numbers and replaces them with `[REDACTED]`. Card numbers have to pass the Luhn check, so things like order numbers are left alone.

The tricky part is not holding back too much text. Emails don't contain spaces, and card numbers/SSNs only have spaces between two digits. So any whitespace that isn't digit-space-digit can't be in the middle of a match, and everything before the last one is safe to redact and send. In practice only the word currently coming in is held back, which keeps the time to first token low and the buffer small. There's also a cap for long runs with no whitespace at all.

`stream.py` handles the SSE side: buffer bytes until a whole event has arrived, keep one redactor per choice, and flush at `finish_reason` / `[DONE]`. If the upstream dies mid-stream, it drops whatever is buffered instead of sending it, since that could be half an SSN.

The tests split PII at every possible position, re-chop the byte stream at random points, and check that the first words reach the client before the upstream has finished.

## Task 4 - rate limiter and fallback

```bash
uv run python -m task4_model_router.mock_provider --name primary --port 9301
uv run python -m task4_model_router.mock_provider --name secondary --port 9302
uv run python -m task4_model_router --port 8082                  # SQLite file: ./router.db

curl -i http://127.0.0.1:8082/v1/chat/completions -H "Authorization: Bearer tenant-a-key" \
  -H "Content-Type: application/json" -d '{"messages":[{"role":"user","content":"hi"}],"mock":{"primary":"slow"}}'
# ~3s later it's answered by the secondary, with X-Gateway-Fallback: true
```

The limiter (`limiter.py`) keeps one SQLite row per request (tenant, timestamp, tokens), and usage is the sum over the last 60 seconds, so it's a proper sliding window rather than a per-minute bucket. The check and the insert happen inside `BEGIN IMMEDIATE`, which takes the write lock straight away. Without it, concurrent requests can all read "49k used" and all get in. One test fires 100 requests at once and checks that exactly 50 x 1000 tokens get admitted.

Before calling the model I reserve an estimate (prompt length / 4 + `max_tokens`), and afterwards I replace it with the usage the provider reports. If the call fails, the reservation is released. Tenants are stored as a sha256 of their API key, not the key itself.

For the fallback (`router.py`), the primary call runs under `asyncio.timeout(3.0)`, which covers the whole call including reading the body. httpx's own timeouts are per read, so a server that trickles bytes wouldn't trip them (there's a test for that). On a timeout, 429, 5xx or connection error it tries the secondary. Other 4xx errors aren't retried, since they'd just fail again.

All errors from `app.py` have the same shape, including FastAPI's validation errors and unexpected exceptions:

```json
{"error": {"type": "rate_limit_error", "code": "rate_limit_exceeded", "message": "Token rate limit exceeded. Retry after 42 seconds.", "request_id": "req_..."}}
```

The real upstream error or traceback is logged under the same `request_id`, but it isn't sent back to the client.

## If I had more time

- Task 1: an idempotency key on `trigger_refund`, so a retry can't refund twice
- Task 2: check tokens against the identity provider's JWKS instead of a shared secret, and maybe hide `admin_` tools from viewers in `tools/list`
- Task 3: the regexes only cover these three formats. Names and addresses would need an NER model, and tool call arguments in the stream aren't scanned yet.
- Task 4: a circuit breaker, so requests don't each wait 3s while the primary is down, plus streaming support

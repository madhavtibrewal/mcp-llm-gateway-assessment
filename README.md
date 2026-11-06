# MCP & LLM Gateway Assessment

All four tasks in one Python 3.12 project, managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest        # 111 tests, about 30 seconds
```

Everything runs locally against mock servers in this repo, so no API keys are needed. Some tests start the real servers as separate processes and talk to them over the network.

| Task | Folder |
|---|---|
| 1. MCP server with strict validation | [task1_mcp_server/](task1_mcp_server/) |
| 2. MCP security gateway | [task2_mcp_gateway/](task2_mcp_gateway/) |
| 3. Streaming PII redaction | [task3_pii_guardrail/](task3_pii_guardrail/) |
| 4. Rate limiter and model fallback | [task4_model_router/](task4_model_router/) |

## A note on versions

I used the current Python MCP SDK (`mcp==2.3.0`, protocol `2026-07-28`). It differs from what the assessment describes in two ways that matter here:

- **Invalid tool arguments.** On its own, the SDK returns them as a tool result with `isError: true`. [SEP-1303](https://modelcontextprotocol.io/seps/1303-input-validation-errors-as-tool-execution-errors) recommends this, so the model can see what it got wrong and fix its call. The assessment asks for JSON-RPC error codes, so Task 1 validates arguments itself and returns `-32602`.
- **Routing headers.** Newer clients repeat the method and tool name in `Mcp-Method` / `Mcp-Name` headers. The Task 2 gateway rejects a request whose headers disagree with its body. Older clients don't send these headers, and the tests cover both kinds of client.

## Task 1: MCP server

```bash
uv run python -m task1_mcp_server
npx @modelcontextprotocol/inspector uv run python -m task1_mcp_server    # optional: try it in MCP Inspector
```

It has two tools, `get_customer_record` and `trigger_refund`, over mock customers `CUST-00001` (active), `CUST-00002` (active, 50.00 refundable) and `CUST-00003` (closed).

- **Validation** lives in Pydantic models ([models.py](task1_mcp_server/models.py)): `^CUST-[0-9]{5}$`, `amount > 0` (finite, at most 10,000), `reason` at least 10 characters after trimming, no extra fields, and no type coercion (`"10"` is not a number). The same models are what `tools/list` advertises.
- **Errors:** an unknown tool or bad arguments gives JSON-RPC `-32602`, with the failing fields in `data`. A valid call that a business rule refuses (unknown customer, refund over the balance) gives a tool result with `isError: true`, which the model can read. A crash gives `-32603` "Internal error", and the stack trace goes to the log only.
- **stdout carries only JSON-RPC.** Logging goes to stderr. While serving, the SDK points stdout at stderr and writes the protocol to a private copy of it. That left one gap, which a test found: `print()` output that Python hasn't flushed yet gets written at exit, after the SDK has put stdout back. `main()` closes that gap. The tests check that every stdout line is JSON-RPC, even with a backend that deliberately prints.

## Task 2: MCP security gateway

```bash
uv run python -m task2_mcp_gateway.mock_downstream --port 9001    # a real MCP server with an admin_ tool
uv run python -m task2_mcp_gateway --port 8080                    # forwards to :9001

VIEWER=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ2aWV3ZXItdXNlciIsInJvbGUiOiJ2aWV3ZXIiLCJpYXQiOjE3OTM5OTAxNjAsImV4cCI6MTg1NzA2MjE2MH0.zYpWYbuXjspgcsRu1Q7ETkVAJaWChoPJhg0Q9QYC91A
curl -s http://127.0.0.1:8080/mcp -H "Authorization: Bearer $VIEWER" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"key_id":"k1"}}}'
# {"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"Unauthorized Tool Call"}}
curl -s http://127.0.0.1:9001/_stats    # the downstream never saw the call
```

The demo tokens above (viewer) and below (admin) are signed with the built-in dev secret and are valid for two years. Make more with `uv run python -m task2_mcp_gateway.mint_token --role admin`.

```
ADMIN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbi11c2VyIiwicm9sZSI6ImFkbWluIiwiaWF0IjoxNzkzOTkwMTYxLCJleHAiOjE4NTcwNjIxNjF9.XVxrP8eR3FW5i9yhCrrGl9YvuEOzjchnNAdGwINHhTE
```

What happens to each `POST /mcp` ([gateway.py](task2_mcp_gateway/gateway.py)):
1. **Authenticate:** HS256 JWT with `sub`, `role` and `exp`. The algorithm is pinned, so `alg: none` tokens fail. A missing or bad token gets HTTP 401. An unknown role is treated as `viewer`, so it never gets admin.
2. **Parse:** duplicate JSON keys and batches are rejected. With `{"name": "echo", "name": "admin_x"}`, Python keeps the last value but other parsers keep the first, so the gateway and the server could disagree about which tool is being called.
3. **Check headers:** if `Mcp-Method` / `Mcp-Name` are present, they must match the body.
4. **Authorize:** a `tools/call` whose name starts with `admin_` (any case) needs role `admin`. Otherwise the gateway answers `-32001` with the request's `id` and never contacts the downstream. That answer is sent with **HTTP 200**, because MCP clients treat non-2xx responses as transport failures and the agent would never see the `-32001`. `tools/list` and all other methods pass through unchanged.
5. **Forward** the parsed message (exactly what was checked), drop `Authorization` and other non-MCP headers, and stream the response back without buffering, so SSE works.

The end-to-end test runs the official SDK client through the gateway, with both the older session-based protocol and the newer stateless one.

## Task 3: Streaming PII redaction

```bash
uv run python -m task3_pii_guardrail.mock_llm --port 9201    # streams PII in random 1-6 character pieces
uv run python -m task3_pii_guardrail --port 8081

curl -N http://127.0.0.1:8081/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"mock","stream":true,"messages":[{"role":"user","content":"hi"}]}'
```

It redacts emails, US SSNs and card numbers. Card numbers must pass the Luhn check, so order numbers aren't redacted. Each match is replaced with `[REDACTED]`.

**Holding back as little as possible** ([redactor.py](task3_pii_guardrail/redactor.py)): emails contain no spaces, and card numbers and SSNs only contain spaces *between two digits*. So any other whitespace is a point that no match can cross. Each time a chunk arrives, everything up to the last such point is redacted and sent, and only the text after it (usually the current word) is held. That keeps time to first token low. Memory stays small, and a hard cap covers long runs with no whitespace at all.

The SSE side ([stream.py](task3_pii_guardrail/stream.py)) buffers bytes until a whole event has arrived, keeps one redactor per choice, releases held text when a choice finishes or at `[DONE]`, and drops held text if the upstream fails mid-stream, since it might be half an SSN. The tests split PII at every possible position, re-cut the byte stream at random points, and check that the first words reach the client before the upstream sends the rest.

## Task 4: Rate limiter and fallback router

```bash
uv run python -m task4_model_router.mock_provider --name primary --port 9301
uv run python -m task4_model_router.mock_provider --name secondary --port 9302
uv run python -m task4_model_router --port 8082                   # SQLite file: ./router.db

curl -i http://127.0.0.1:8082/v1/chat/completions -H "Authorization: Bearer tenant-a-key" \
  -H "Content-Type: application/json" -d '{"messages":[{"role":"user","content":"hi"}],"mock":{"primary":"slow"}}'
# about 3 s later: answered by the secondary, with X-Gateway-Fallback: true
```

**Rate limiting** ([limiter.py](task4_model_router/limiter.py)): every admitted request is a row `(tenant, timestamp, tokens)` in SQLite. A tenant's usage is the sum of its rows from the last 60 seconds, so the window slides instead of resetting each minute. The check-and-insert runs inside one `BEGIN IMMEDIATE` transaction, which takes the write lock up front. Without that, concurrent requests could all read "49,000 used" and all be admitted; a test fires 100 at once and exactly 50 × 1,000 tokens get in. Before the call, the router reserves an estimate (prompt length ÷ 4 + `max_tokens`). Afterwards it corrects the row to the provider's reported usage, and releases it if the call failed. Tenants are stored as a SHA-256 of their API key.

**Fallback** ([router.py](task4_model_router/router.py)): the primary call runs under `asyncio.timeout(3.0)`, a deadline on the whole call including the response body. (httpx's own timeouts apply per read, so a server trickling bytes would never trip them; there's a test for that.) On timeout, 429, 5xx or a connection error it moves to the secondary. It doesn't fall back on other 4xx errors, since a bad request would just fail again.

**Errors** ([app.py](task4_model_router/app.py)): every error, including FastAPI's own validation errors and unexpected exceptions, uses one shape:

```json
{"error": {"type": "rate_limit_error", "code": "rate_limit_exceeded", "message": "Token rate limit exceeded. Retry after 42 seconds.", "request_id": "req_..."}}
```

Upstream bodies and stack traces are logged under the same `request_id`, but never sent to the client.

## What I'd do next

- **Task 1:** add an idempotency key to `trigger_refund`, so a client retry can't refund twice.
- **Task 2:** verify tokens against the identity provider's public keys (RS256/JWKS) instead of a shared secret, and optionally hide `admin_` tools from viewers in `tools/list`.
- **Task 3:** regexes cover only these three formats. Names and addresses would need an NER model, and tool-call arguments in the stream aren't scanned yet.
- **Task 4:** add a circuit breaker, so that while the primary is down each request doesn't spend 3 seconds finding out; support streaming responses.

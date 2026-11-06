"""Task 2: MCP security gateway.

A reverse proxy between an MCP client and a downstream MCP server (Streamable HTTP).
For every POST /mcp:
  1. Bearer JWT -> role                     HTTP 401 if missing or invalid
  2. parse the JSON-RPC message             HTTP 400 if malformed
  3. tools/call on admin_* needs "admin"    JSON-RPC -32001; downstream is never called
  4. anything else is forwarded, and the response is streamed back
"""

import json
import logging
import os

import httpx
import jwt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

JWT_SECRET = os.environ.get("GATEWAY_JWT_SECRET", "dev-only-secret-change-me-before-deploying")
MAX_BODY_BYTES = 1_000_000

# Tool-name prefix -> roles allowed to call those tools.
TOOL_RULES = {"admin_": {"admin"}}

# Only these headers reach the downstream server. The client's Authorization header stays here.
FORWARD_HEADERS = {"content-type", "accept", "mcp-session-id", "mcp-protocol-version", "mcp-method", "mcp-name", "last-event-id"}


class AuthError(Exception):
    pass


def authenticate(header: str | None) -> tuple[str, str]:
    """Returns (subject, role) from a 'Bearer <jwt>' header."""
    scheme, _, token = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("Missing bearer token")
    try:
        # algorithms is pinned, so a token with alg "none" is rejected.
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], options={"require": ["exp", "sub", "role"]})
    except jwt.ExpiredSignatureError:
        raise AuthError("Token expired") from None
    except jwt.InvalidTokenError:
        raise AuthError("Invalid token") from None
    # Unknown roles get the least privilege, never admin.
    role = claims["role"] if claims["role"] in ("admin", "viewer") else "viewer"
    return claims["sub"], role


def is_allowed(tool_name: str, role: str) -> bool:
    for prefix, roles in TOOL_RULES.items():
        # lower(): "ADMIN_reset_key" must not slip past the check.
        if tool_name.lower().startswith(prefix) and role not in roles:
            return False
    return True


def no_duplicate_keys(pairs):
    # {"name": "get_x", "name": "admin_x"}: Python keeps the last value and other
    # parsers keep the first, so the gateway and the server could disagree.
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def rpc_error(msg_id, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}, status_code=status)


async def mcp_endpoint(request: Request):
    try:
        subject, role = authenticate(request.headers.get("authorization"))
    except AuthError as e:
        return rpc_error(None, -32000, str(e), status=401)

    if request.method != "POST":
        # GET opens the server-to-client event stream, DELETE ends a session: nothing to inspect.
        return await forward(request, body=None)

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return rpc_error(None, -32600, "Request too large", status=413)
    try:
        message = json.loads(raw, object_pairs_hook=no_duplicate_keys)
    except ValueError:
        return rpc_error(None, -32700, "Parse error", status=400)
    if not isinstance(message, dict):
        # MCP doesn't use JSON-RPC batches, and a batch could hide a second call behind the first.
        return rpc_error(None, -32600, "Invalid Request: batches are not supported", status=400)

    msg_id = message.get("id")
    method = message.get("method")

    # Newer clients repeat the method and tool name in headers. If a header
    # disagrees with the body, something downstream might act on the wrong one.
    if request.headers.get("mcp-method", method) != method:
        return rpc_error(msg_id, -32600, "Mcp-Method header does not match the body", status=400)

    if method == "tools/call":
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str):
            return rpc_error(msg_id, -32602, "tools/call requires params.name", status=400)
        if request.headers.get("mcp-name", name) != name:
            return rpc_error(msg_id, -32600, "Mcp-Name header does not match the body", status=400)
        if not is_allowed(name, role):
            logger.warning("Denied tools/call %s for %s (role %s)", name, subject, role)
            # HTTP 200 on purpose: MCP clients treat non-2xx as a transport failure
            # and would never show the agent the -32001.
            return rpc_error(msg_id, -32001, "Unauthorized Tool Call")

    # Forward the message we checked, not the original bytes.
    return await forward(request, body=json.dumps(message).encode())


async def forward(request: Request, body: bytes | None):
    client: httpx.AsyncClient = request.app.state.client
    headers = {k: v for k, v in request.headers.items() if k in FORWARD_HEADERS}
    upstream_request = client.build_request(request.method, request.app.state.downstream_url, headers=headers, content=body)
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as e:
        logger.warning("Downstream MCP server unavailable: %r", e)  # details in our logs only
        return rpc_error(None, -32000, "Downstream MCP server unavailable", status=502)

    async def relay():
        # Pass chunks through as they arrive, so SSE responses aren't buffered.
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    headers = {k: v for k, v in upstream.headers.items() if k in ("content-type", "mcp-session-id")}
    return StreamingResponse(relay(), status_code=upstream.status_code, headers=headers)


def create_app(downstream_url: str | None = None, client: httpx.AsyncClient | None = None) -> Starlette:
    app = Starlette(routes=[
        Route("/mcp", mcp_endpoint, methods=["POST", "GET", "DELETE"]),
        Route("/healthz", lambda _: JSONResponse({"status": "ok"})),
    ])
    app.state.downstream_url = downstream_url or os.environ.get("GATEWAY_DOWNSTREAM_URL", "http://127.0.0.1:9001/mcp")
    app.state.client = client or httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5))
    return app

"""Task 2 - security gateway in front of an MCP server (Streamable HTTP).

Checks the bearer token, parses the JSON-RPC body, and blocks tools/call on
admin_* tools unless the caller's role is admin (-32001). Everything else is
proxied through to the downstream server.
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

# prefix -> roles that can call tools with that prefix
TOOL_RULES = {"admin_": {"admin"}}

# headers we pass on to the MCP server (Authorization isn't one of them)
FORWARD_HEADERS = {"content-type", "accept", "mcp-session-id", "mcp-protocol-version", "mcp-method", "mcp-name", "last-event-id"}


class AuthError(Exception):
    pass


def authenticate(header: str | None) -> tuple[str, str]:
    """Returns (subject, role) from a 'Bearer <jwt>' header."""
    scheme, _, token = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("Missing bearer token")
    try:
        # pin the algorithm, otherwise a token with alg=none would get through
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], options={"require": ["exp", "sub", "role"]})
    except jwt.ExpiredSignatureError:
        raise AuthError("Token expired") from None
    except jwt.InvalidTokenError:
        raise AuthError("Invalid token") from None
    # anything we don't recognise is treated as a viewer
    role = claims["role"] if claims["role"] in ("admin", "viewer") else "viewer"
    return claims["sub"], role


def is_allowed(tool_name: str, role: str) -> bool:
    for prefix, roles in TOOL_RULES.items():
        # case-insensitive so ADMIN_reset_key is caught too
        if tool_name.lower().startswith(prefix) and role not in roles:
            return False
    return True


def no_duplicate_keys(pairs):
    # {"name": "get_x", "name": "admin_x"} - python keeps the last one and some other
    # parsers keep the first, so we could check one tool while the server runs another
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
        # GET = SSE stream from the server, DELETE = end session. No body to check.
        return await forward(request, body=None)

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return rpc_error(None, -32600, "Request too large", status=413)
    try:
        message = json.loads(raw, object_pairs_hook=no_duplicate_keys)
    except ValueError:
        return rpc_error(None, -32700, "Parse error", status=400)
    if not isinstance(message, dict):
        # a list is a batch. MCP dropped batching, and it'd let a 2nd call hide behind the 1st
        return rpc_error(None, -32600, "Invalid Request: batches are not supported", status=400)

    msg_id = message.get("id")
    method = message.get("method")

    # 2026-07-28 clients also send the method/tool name as headers. If they're
    # present they have to match the body (older clients don't send them).
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
            # 200, not 403: MCP clients treat non-2xx as a transport error, so the
            # agent would never get to see the -32001
            return rpc_error(msg_id, -32001, "Unauthorized Tool Call")

    # send what we actually parsed and checked, not the raw bytes
    return await forward(request, body=json.dumps(message).encode())


async def forward(request: Request, body: bytes | None):
    client: httpx.AsyncClient = request.app.state.client
    headers = {k: v for k, v in request.headers.items() if k in FORWARD_HEADERS}
    upstream_request = client.build_request(request.method, request.app.state.downstream_url, headers=headers, content=body)
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as e:
        logger.warning("Downstream MCP server unavailable: %r", e)  # real error in the log, generic one to the client
        return rpc_error(None, -32000, "Downstream MCP server unavailable", status=502)

    async def relay():
        # stream it straight through so SSE works
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

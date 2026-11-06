"""Gateway tests against a fake downstream server that records what reaches it."""

import json
import time

import httpx
import jwt
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from task2_mcp_gateway.gateway import JWT_SECRET, create_app
from task2_mcp_gateway.mint_token import mint


class FakeDownstream:
    """Echoes each JSON-RPC request back and remembers it."""

    def __init__(self):
        self.requests = []

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        body = await request.body()
        self.requests.append({"headers": dict(request.headers), "body": body})
        message = json.loads(body)
        if "id" not in message:
            response = Response(status_code=202)  # notification
        else:
            response = JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": {"received": message}},
                                    headers={"mcp-session-id": "s1", "x-internal": "pod-7"})
        await response(scope, receive, send)


@pytest.fixture
def downstream():
    return FakeDownstream()


@pytest.fixture
async def gateway(downstream):
    app = create_app("http://downstream/mcp", httpx.AsyncClient(transport=httpx.ASGITransport(app=downstream)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        yield client


def bearer(role="viewer"):
    return {"Authorization": f"Bearer {mint(role)}"}


def tool_call(name, msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": {}}}


async def test_tools_list_is_forwarded(gateway, downstream):
    message = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
    response = await gateway.post("/mcp", json=message, headers=bearer())
    assert response.json() == {"jsonrpc": "2.0", "id": 7, "result": {"received": message}}
    assert len(downstream.requests) == 1


async def test_viewer_can_call_normal_tools(gateway, downstream):
    response = await gateway.post("/mcp", json=tool_call("echo"), headers=bearer("viewer"))
    assert "result" in response.json()


@pytest.mark.parametrize("name", ["admin_reset_key", "ADMIN_reset_key"])
async def test_viewer_cannot_call_admin_tools(gateway, downstream, name):
    response = await gateway.post("/mcp", json=tool_call(name, msg_id="abc"), headers=bearer("viewer"))
    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": "abc", "error": {"code": -32001, "message": "Unauthorized Tool Call"}}
    assert downstream.requests == []  # never forwarded


async def test_admin_can_call_admin_tools(gateway, downstream):
    response = await gateway.post("/mcp", json=tool_call("admin_reset_key"), headers=bearer("admin"))
    assert "result" in response.json()


async def test_unknown_role_is_treated_as_viewer(gateway, downstream):
    response = await gateway.post("/mcp", json=tool_call("admin_reset_key"), headers=bearer("superuser"))
    assert response.json()["error"]["code"] == -32001


def unsigned_token():
    # alg "none": no signature at all.
    return jwt.encode({"sub": "x", "role": "admin", "exp": time.time() + 60}, key=None, algorithm="none")


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Basic dXNlcjpwYXNz",
        "Bearer not-a-jwt",
        "Bearer " + jwt.encode({"sub": "x", "role": "admin", "exp": time.time() + 60}, "some-other-secret-0123456789abcdef", algorithm="HS256"),
        "Bearer " + jwt.encode({"sub": "x", "role": "admin", "exp": time.time() - 1}, JWT_SECRET, algorithm="HS256"),
        "Bearer " + unsigned_token(),
    ],
    ids=["missing", "basic", "garbage", "some-other-secret-0123456789abcdef", "expired", "alg-none"],
)
async def test_bad_tokens_get_401(gateway, downstream, authorization):
    headers = {"Authorization": authorization} if authorization else {}
    response = await gateway.post("/mcp", json=tool_call("echo"), headers=headers)
    assert response.status_code == 401
    assert downstream.requests == []


@pytest.mark.parametrize(
    "body, expected_code",
    [
        ('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"echo","name":"admin_reset_key"}}', -32700),
        ('[{"jsonrpc":"2.0","id":1,"method":"tools/list"}]', -32600),
        ("not json", -32700),
        ('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":42}}', -32602),
    ],
    ids=["duplicate-keys", "batch", "not-json", "name-not-a-string"],
)
async def test_malformed_requests_are_rejected(gateway, downstream, body, expected_code):
    response = await gateway.post("/mcp", content=body, headers={**bearer("admin"), "content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == expected_code
    assert downstream.requests == []


async def test_header_that_disagrees_with_the_body_is_rejected(gateway, downstream):
    headers = {**bearer("admin"), "Mcp-Name": "echo"}  # header says echo, body says admin_reset_key
    response = await gateway.post("/mcp", json=tool_call("admin_reset_key"), headers=headers)
    assert response.status_code == 400
    assert downstream.requests == []


async def test_forwarding_drops_credentials_and_keeps_mcp_headers(gateway, downstream):
    headers = {**bearer(), "Cookie": "session=1", "Mcp-Session-Id": "s1"}
    response = await gateway.post("/mcp", json=tool_call("echo"), headers=headers)
    sent = downstream.requests[0]["headers"]
    assert "authorization" not in sent and "cookie" not in sent
    assert sent["mcp-session-id"] == "s1"
    assert response.headers["mcp-session-id"] == "s1"
    assert "x-internal" not in response.headers


async def test_downstream_down_is_502_without_details():
    def refuse(request):
        raise httpx.ConnectError("connection refused to 10.0.0.5:9001")

    app = create_app("http://downstream/mcp", httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post("/mcp", json=tool_call("echo"), headers=bearer())
    assert response.status_code == 502
    assert "10.0.0.5" not in response.text

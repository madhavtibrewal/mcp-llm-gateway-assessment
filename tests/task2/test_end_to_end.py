"""The official MCP SDK client -> gateway process -> SDK server process."""

import httpx
import httpx2
import pytest
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from task2_mcp_gateway.mint_token import mint
from tests.servers import run_server


@pytest.fixture(scope="module")
def urls():
    with run_server("task2_mcp_gateway.mock_downstream", health_path="/_stats") as downstream:
        with run_server("task2_mcp_gateway", env={"GATEWAY_DOWNSTREAM_URL": f"{downstream}/mcp"}) as gateway:
            yield gateway, downstream


def admin_calls(downstream_url) -> int:
    return httpx.get(f"{downstream_url}/_stats").json().get("admin_reset_key", 0)


# "legacy": initialize handshake + session id (older clients).
# "2026-07-28": stateless requests with Mcp-Method / Mcp-Name headers.
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_viewer_is_blocked_and_admin_is_allowed(urls, mode):
    gateway, downstream = urls
    before = admin_calls(downstream)

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {mint('viewer')}"}) as http:
        async with Client(streamable_http_client(f"{gateway}/mcp", http_client=http), mode=mode) as viewer:
            assert {t.name for t in (await viewer.list_tools()).tools} == {"echo", "admin_reset_key"}
            assert (await viewer.call_tool("echo", {"text": "hi"})).content[0].text == "hi"
            with pytest.raises(MCPError) as denied:
                await viewer.call_tool("admin_reset_key", {"key_id": "k1"})
            assert denied.value.code == -32001
    assert admin_calls(downstream) == before

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {mint('admin')}"}) as http:
        async with Client(streamable_http_client(f"{gateway}/mcp", http_client=http), mode=mode) as admin:
            assert (await admin.call_tool("admin_reset_key", {"key_id": "k1"})).content[0].text == "key k1 rotated"
    assert admin_calls(downstream) == before + 1

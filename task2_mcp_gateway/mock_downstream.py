"""Small MCP server to put behind the gateway. GET /_stats counts the tool calls
that actually ran, which is how the tests check blocked calls didn't get through.

    uv run python -m task2_mcp_gateway.mock_downstream --port 9001
"""

import argparse
from collections import Counter

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse

calls = Counter()
server = MCPServer("mock-downstream")


@server.tool(description="Echo text back.")
def echo(text: str) -> str:
    calls["echo"] += 1
    return text


@server.tool(description="ADMIN: rotate an API key.")
def admin_reset_key(key_id: str) -> str:
    calls["admin_reset_key"] += 1
    return f"key {key_id} rotated"


def app_with_stats(mcp_app):
    async def app(scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/_stats":
            await JSONResponse(dict(calls))(scope, receive, send)
        else:
            await mcp_app(scope, receive, send)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9001)
    args = parser.parse_args()
    uvicorn.run(app_with_stats(server.streamable_http_app()), host="127.0.0.1", port=args.port, log_level="warning")

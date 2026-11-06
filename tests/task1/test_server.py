"""The real server process, driven with raw JSON-RPC over stdio."""

import json

import pytest

from tests.stdio_client import StdioServer

INVALID_PARAMS = -32602


@pytest.fixture(scope="module")
def server():
    # one server for the whole module, starting it takes ~2s
    s = StdioServer("task1_mcp_server")
    s.initialize()
    yield s
    s.close()
    s.assert_stdout_is_only_jsonrpc()


def tool_output(response: dict) -> dict:
    return json.loads(response["result"]["content"][0]["text"])


def test_tools_list_advertises_the_strict_schemas(server):
    tools = {t["name"]: t["inputSchema"] for t in server.request("tools/list")["result"]["tools"]}
    assert tools["get_customer_record"]["properties"]["customer_id"]["pattern"] == "^CUST-[0-9]{5}$"
    assert tools["trigger_refund"]["properties"]["amount"]["exclusiveMinimum"] == 0
    assert tools["trigger_refund"]["additionalProperties"] is False


def test_get_customer_record(server):
    response = server.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
    assert tool_output(response)["name"] == "Ada Lovelace"


@pytest.mark.parametrize(
    "tool, arguments, bad_field",
    [
        ("get_customer_record", {"customer_id": "CUST-1"}, "customer_id"),
        ("get_customer_record", {}, "customer_id"),
        ("get_customer_record", {"customer_id": "CUST-00001", "admin": True}, "admin"),
        ("trigger_refund", {"customer_id": "CUST-00001", "amount": 0, "reason": "Item arrived damaged"}, "amount"),
        ("trigger_refund", {"customer_id": "CUST-00001", "amount": "10", "reason": "Item arrived damaged"}, "amount"),
        ("trigger_refund", {"customer_id": "CUST-00001", "amount": 10, "reason": "too short"}, "reason"),
    ],
)
def test_invalid_input_is_32602_with_field_details(server, tool, arguments, bad_field):
    response = server.call_tool(tool, arguments)
    assert response["error"]["code"] == INVALID_PARAMS
    assert bad_field in [e["field"] for e in response["error"]["data"]]


def test_unknown_tool_is_32602(server):
    assert server.call_tool("delete_everything", {})["error"]["code"] == INVALID_PARAMS


def test_refund_succeeds(server):
    response = server.call_tool("trigger_refund", {"customer_id": "CUST-00001", "amount": 100.0, "reason": "  Item arrived damaged "})
    receipt = tool_output(response)
    assert receipt["status"] == "accepted"
    assert receipt["reason"] == "Item arrived damaged"
    assert receipt["remaining_refundable"] == 1100.0


@pytest.mark.parametrize(
    "customer_id, amount, expected",
    [("CUST-99999", 5.0, "not found"), ("CUST-00002", 60.0, "exceeds"), ("CUST-00003", 5.0, "closed")],
)
def test_refused_refunds_are_tool_errors(server, customer_id, amount, expected):
    # valid input but a business rule says no -> isError result, not a protocol error
    response = server.call_tool("trigger_refund", {"customer_id": customer_id, "amount": amount, "reason": "Item arrived damaged"})
    assert response["result"]["isError"] is True
    assert expected in response["result"]["content"][0]["text"]


@pytest.mark.parametrize("version", ["2024-11-05", "2025-06-18"])
def test_older_clients_can_connect(version):
    s = StdioServer("task1_mcp_server")
    try:
        assert s.initialize(version)["result"]["protocolVersion"] == version
    finally:
        s.close()


def test_stray_stdout_writes_go_to_stderr():
    s = StdioServer("tests.task1.misbehaving_server", "noisy")
    try:
        s.initialize()
        assert s.call_tool("get_customer_record", {"customer_id": "CUST-00001"})["result"]["isError"] is False
    finally:
        s.close()
    s.assert_stdout_is_only_jsonrpc()
    assert b"STRAY PRINT" in s.stderr and b"RAW FD WRITE" in s.stderr


def test_crash_is_32603_without_details():
    s = StdioServer("tests.task1.misbehaving_server", "crash")
    try:
        s.initialize()
        response = s.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
    finally:
        s.close()
    assert response["error"] == {"code": -32603, "message": "Internal error"}
    assert b"hunter2" in s.stderr  # logged for us, not sent to the client

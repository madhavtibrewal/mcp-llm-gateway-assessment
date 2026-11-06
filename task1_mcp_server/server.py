"""Task 1 - customer service MCP server (stdio).

Bad arguments or an unknown tool give a -32602 JSON-RPC error. A valid call that
a business rule rejects gives a tool result with isError: true. Crashes give -32603.
"""

import logging
import os
import sys
import uuid

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from .models import TOOL_ARGS

logger = logging.getLogger(__name__)

INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# fake data instead of a real DB
CUSTOMERS = {
    "CUST-00001": {"name": "Ada Lovelace", "status": "active", "refundable": 1200.00},
    "CUST-00002": {"name": "Alan Turing", "status": "active", "refundable": 50.00},
    "CUST-00003": {"name": "Grace Hopper", "status": "closed", "refundable": 80.00},
}


class StrictMCPServer(MCPServer):
    """MCPServer, but tool arguments are checked against our Pydantic models first.

    The SDK's own validation returns bad input as isError: true. The task wants a
    JSON-RPC error (-32602) instead, so I validate here and raise MCPError.
    """

    async def list_tools(self):
        tools = await super().list_tools()
        # the SDK builds schemas from the type hints, which don't have the pattern/limits
        return [t.model_copy(update={"input_schema": TOOL_ARGS[t.name].model_json_schema()}) for t in tools]

    async def call_tool(self, name, arguments, context=None):
        model = TOOL_ARGS.get(name)
        if model is None:
            raise MCPError(INVALID_PARAMS, f"Unknown tool: {name}")

        try:
            args = model.model_validate(arguments)
        except ValidationError as e:
            # say which field failed, but don't echo the value back
            errors = [{"field": ".".join(map(str, err["loc"])), "message": err["msg"]} for err in e.errors()]
            raise MCPError(INVALID_PARAMS, f"Invalid params for {name}", data=errors) from None

        try:
            return await super().call_tool(name, args.model_dump(), context)
        except UnexpectedToolError:
            logger.exception("Tool %s crashed", name)
            raise MCPError(INTERNAL_ERROR, "Internal error") from None


server = StrictMCPServer("customer-service")


@server.tool(description="Look up a customer by ID (format CUST-XXXXX).")
def get_customer_record(customer_id: str) -> dict:
    customer = CUSTOMERS.get(customer_id)
    if customer is None:
        raise ToolError(f"Customer {customer_id} not found")
    return {"customer_id": customer_id, **customer}


@server.tool(description="Refund a customer. Amount must be positive; reason at least 10 characters.")
def trigger_refund(customer_id: str, amount: float, reason: str) -> dict:
    customer = CUSTOMERS.get(customer_id)
    if customer is None:
        raise ToolError(f"Customer {customer_id} not found")
    if customer["status"] != "active":
        raise ToolError(f"Refunds are not allowed: {customer_id} is {customer['status']}")
    if amount > customer["refundable"]:
        raise ToolError(f"Refund of {amount:.2f} exceeds the refundable balance of {customer['refundable']:.2f}")

    customer["refundable"] = round(customer["refundable"] - amount, 2)
    logger.info("Refunded %.2f to %s", amount, customer_id)
    return {
        "refund_id": f"RF-{uuid.uuid4().hex[:10].upper()}",
        "customer_id": customer_id,
        "amount": amount,
        "reason": reason,
        "status": "accepted",
        "remaining_refundable": customer["refundable"],
    }


def main():
    # stdout is only for JSON-RPC messages, logs go to stderr
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        # while it's serving, the SDK writes JSON-RPC to its own copy of stdout
        # and points fd 1 at stderr
        server.run(transport="stdio")
    finally:
        # ...but it puts fd 1 back when it stops, and anything print() left in the
        # buffer gets flushed after that, i.e. into the client's stream. Point fd 1
        # at stderr again. (test_stray_stdout_writes_go_to_stderr caught this)
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

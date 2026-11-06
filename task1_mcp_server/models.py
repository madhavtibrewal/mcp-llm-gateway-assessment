"""Input schemas for the two tools.

These models validate every tool call, and they are also what tools/list
advertises, so clients see the same rules the server enforces.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# [0-9], not \d: \d also matches non-ASCII digits like "CUST-١٢٣٤٥".
CustomerId = Annotated[str, Field(pattern=r"^CUST-[0-9]{5}$", description="Customer ID, e.g. CUST-00001")]


class StrictArgs(BaseModel):
    # extra="forbid": unknown fields are an error. strict=True: no coercion, so "10" is not a number.
    model_config = ConfigDict(extra="forbid", strict=True)


class GetCustomerRecordArgs(StrictArgs):
    customer_id: CustomerId


class TriggerRefundArgs(StrictArgs):
    customer_id: CustomerId
    # allow_inf_nan=False: Python's JSON parser accepts NaN and Infinity, and inf > 0 is True.
    amount: Annotated[float, Field(gt=0, le=10_000, allow_inf_nan=False)]
    # Stripped before the length check, so ten spaces is not a valid reason.
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=500)]


TOOL_ARGS: dict[str, type[StrictArgs]] = {
    "get_customer_record": GetCustomerRecordArgs,
    "trigger_refund": TriggerRefundArgs,
}

"""Argument models for the two tools. tools/list uses these as the schemas too."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# [0-9] instead of \d, because \d also matches Arabic, Devanagari etc. digits
CustomerId = Annotated[str, Field(pattern=r"^CUST-[0-9]{5}$", description="Customer ID, e.g. CUST-00001")]


class StrictArgs(BaseModel):
    # reject extra fields, and don't coerce types ("10" shouldn't turn into 10.0)
    model_config = ConfigDict(extra="forbid", strict=True)


class GetCustomerRecordArgs(StrictArgs):
    customer_id: CustomerId


class TriggerRefundArgs(StrictArgs):
    customer_id: CustomerId
    # json.loads accepts NaN/Infinity and inf > 0 is True, so block them explicitly
    amount: Annotated[float, Field(gt=0, le=10_000, allow_inf_nan=False)]
    # strip first, otherwise "          " would count as 10 characters
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=500)]


TOOL_ARGS: dict[str, type[StrictArgs]] = {
    "get_customer_record": GetCustomerRecordArgs,
    "trigger_refund": TriggerRefundArgs,
}

import math

import pytest
from pydantic import ValidationError

from task1_mcp_server.models import GetCustomerRecordArgs, TriggerRefundArgs

REFUND = {"customer_id": "CUST-00001", "amount": 25.5, "reason": "Item arrived damaged"}


@pytest.mark.parametrize(
    "customer_id",
    [
        "CUST-1234",  # too short
        "cust-12345",  # wrong case
        "CUST-12345\n",  # trailing newline
        " CUST-12345",  # IDs are not trimmed
        "CUST-١٢٣٤٥",  # Arabic-Indic digits
        12345,  # not a string
    ],
)
def test_bad_customer_ids(customer_id):
    with pytest.raises(ValidationError):
        GetCustomerRecordArgs.model_validate({"customer_id": customer_id})


@pytest.mark.parametrize("amount", [0, -1, "10", True, math.nan, math.inf, 10_000.01])
def test_bad_amounts(amount):
    with pytest.raises(ValidationError):
        TriggerRefundArgs.model_validate({**REFUND, "amount": amount})


def test_reason_is_stripped_before_the_length_check():
    assert TriggerRefundArgs.model_validate({**REFUND, "reason": "  1234567890  "}).reason == "1234567890"
    with pytest.raises(ValidationError):
        TriggerRefundArgs.model_validate({**REFUND, "reason": "          "})


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        GetCustomerRecordArgs.model_validate({"customer_id": "CUST-00001", "admin": True})

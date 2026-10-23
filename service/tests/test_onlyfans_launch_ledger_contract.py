"""Strict money and identity contract; all records and dates are synthetic."""
from datetime import datetime
import json

import pytest

from onlyfans_runtime.ledger import parse_page
from onlyfans_runtime.launch_ledger.validation import LedgerValidationError


START, END = datetime(2026, 10, 1), datetime(2026, 10, 23)


def row(**changes):
    return dict(dict(id="synthetic-purchase", ownerId=7001, user={"id": 101},
        amount="10.01", currency="USD", status="done", createdAt="2026-10-18T10:00:00Z",
        description="Payment for message from Synthetic Fan", messageId=9001), **changes)


def parse(item):
    return parse_page(json.dumps({"list": [item], "hasMore": False}).encode(),
        owner_id=7001, start=START, end=END)[0][0]


@pytest.mark.parametrize("status,expected", [("loading", "pending"), ("done", "cleared"),
    ("pending_return", "refund_pending"), ("undo", "chargedback"),
    ("refunded", "refunded"), ("failed", "failed")])
def test_explicit_statuses_preserve_exact_minor_units(status, expected):
    parsed = parse(row(status=status, net="8.01", fee="2.00", vatAmount="0.00"))
    assert (parsed["amount_cents"], parsed["net_cents"], parsed["fee_cents"], parsed["vat_cents"]) == (1001, 801, 200, 0)
    assert parsed["status"] == expected
    assert (parsed["fan_id"], parsed["message_id"]) == (101, 9001)


@pytest.mark.parametrize("amount", ["0", "-10.01", "1.001", "1e-3", "NaN", "Infinity", "1000000.01", True, None])
def test_uncertain_or_unrepresentable_amount_never_becomes_paid(amount):
    with pytest.raises(LedgerValidationError) as caught:
        parse(row(amount=amount))
    assert caught.value.path == "amount" and caught.value.row_index == 0


def test_fractional_json_number_uses_exact_decimal_without_binary_float():
    raw = json.dumps({"list": [row()], "hasMore": False}).replace('"10.01"', '10.01').encode()
    assert parse_page(raw, owner_id=7001, start=START, end=END)[0][0]["amount_cents"] == 1001
    with pytest.raises(LedgerValidationError, match="unsupported native transaction page"):
        parse_page(raw.replace(b'10.01,', b'10.01000000000000001,'), owner_id=7001, start=START, end=END)


@pytest.mark.parametrize("changes,code,path", [
    ({"ownerId": 7002}, "owner_mismatch", "ownerId"),
    ({"ownerId": "7001"}, "invalid_owner_id", "ownerId"),
    ({"user": {"id": 7001}}, "fan_is_owner", "user.id"),
    ({"user": {"id": "101"}}, "invalid_fan_id", "user.id"),
    ({"user": None, "messageId": None}, "missing_fan", "user.id"),
    ({"messageId": True}, "invalid_message_id", "messageId"),
    ({"status": "paid"}, "invalid_status", "status"),
    ({"createdAt": "2026-10-24T00:00:00Z"}, "outside_window", "createdAt"),
    ({"createdAt": "2026-10-18T10:00:00"}, "invalid_created_at", "createdAt"),
    ({"clearedAt": "2026-10-17T10:00:00Z"}, "cleared_before_created", "clearedAt"),
    ({"net": "8.001"}, "invalid_net", "net"),
    ({"fee": "-2"}, "invalid_fee", "fee"),
    ({"vatAmount": True}, "invalid_vat", "vatAmount"),
])
def test_validation_diagnostics_do_not_coerce_ambiguous_transactions(changes, code, path):
    with pytest.raises(LedgerValidationError) as caught:
        parse(row(**changes))
    assert (caught.value.code, caught.value.path, caught.value.row_index) == (code, path, 0)


def test_missing_message_identity_stays_unlinked():
    assert parse(row(messageId=None))["message_id"] is None

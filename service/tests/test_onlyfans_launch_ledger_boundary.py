"""Synthetic reproduction of Claude's redacted offset-40 window overrun."""
from datetime import datetime, timedelta
import json

import pytest

from onlyfans_runtime.ledger import parse_page
from onlyfans_runtime.launch_ledger.diagnostics import inspect_page
from onlyfans_runtime.launch_ledger.validation import LedgerValidationError


END = datetime(2026, 10, 23, 5, 46)
START = END - timedelta(days=90)
OWNER = 7001


def boundary_rows(start=START, end=END):
    rows = []
    for index in range(20):
        stamp = end - timedelta(hours=index + 1) if index < 14 else start - timedelta(hours=index - 13)
        kind = "Payment for message from " if index < 8 else "Post from " if index < 10 else "Tip from "
        rows.append(dict(id="synthetic-boundary-" + str(index), user={"id": 101},
            amount="10.01", net="8.00", fee="2.01", vatAmount="0.00",
            mediaTaxAmount="0.00", taxAmount="0.00", currency="USD", status="done",
            description=kind + "Synthetic Fan", descriptionDetails={},
            createdAt=stamp.isoformat() + "Z"))
    return rows


def boundary_payload(rows, more=True):
    return json.dumps(dict(list=rows, hasMore=more, marker="synthetic-marker",
                           nextMarker="synthetic-next-marker")).encode()


def parse(rows, more=True):
    return parse_page(boundary_payload(rows, more), owner_id=OWNER, start=START, end=END)


@pytest.mark.parametrize("provider_more", [True, False])
def test_reported_fourteen_valid_plus_six_older_shape_preserves_provider_continuation(provider_more):
    source = boundary_rows()
    assert set(source[0]) == {"amount", "createdAt", "currency", "description", "descriptionDetails",
        "fee", "id", "mediaTaxAmount", "net", "status", "taxAmount", "user", "vatAmount"}
    rows, more = parse(source, provider_more)
    assert len(rows) == 14 and more is provider_more
    assert [r["kind"] for r in rows] == ["ppv_message"] * 8 + ["ppv_post"] * 2 + ["tip"] * 4
    assert all(r["amount_cents"] == 1001 and r["message_id"] is None for r in rows)
    assert {r["provider_transaction_id"] for r in rows}.isdisjoint({r["id"] for r in source[14:]})
    report = inspect_page(boundary_payload(source, provider_more), owner_id=OWNER, start=START, end=END)
    assert report["valid"] is True and report["applied"] is False and report["issues"] == []


def test_an_entire_older_page_validates_but_applies_nothing():
    rows, more = parse(boundary_rows()[14:])
    assert rows == [] and more is True


def test_both_window_boundaries_are_inclusive_and_equal_timestamps_allowed():
    source = boundary_rows()[:3]
    for row, stamp in zip(source, (END, START, START)):
        row["createdAt"] = stamp.isoformat() + "Z"
    rows, more = parse(source)
    assert len(rows) == 3 and more is True


def test_rows_newer_than_frozen_end_are_validated_but_not_applied():
    source = boundary_rows()
    source[0]["createdAt"] = (END + timedelta(seconds=1)).isoformat() + "Z"
    rows,more=parse(source)
    assert source[0]["id"] not in {row["provider_transaction_id"] for row in rows}
    assert len(rows)==13 and more is True


@pytest.mark.parametrize("left,right", [(0, 1), (13, 14), (14, 15), (0, 19)])
def test_row_order_never_substitutes_for_provider_exhaustion(left, right):
    source = boundary_rows()
    source[left], source[right] = source[right], source[left]
    rows,more=parse(source)
    assert len(rows)==14 and more is True
    assert all(START<=datetime.fromisoformat(row["occurred_at"])<=END for row in rows)


@pytest.mark.parametrize("change", [
    {"amount": "1.001"}, {"amount": "-10.01"}, {"net": True}, {"currency": "AUD"},
    {"status": "paid"}, {"user": {"id": True}}, {"ownerId": OWNER + 1},
    {"description": "Unknown event"}, {"messageId": "9001"},
    {"createdAt": "invalid"}, {"payoutPendingDays": -1}, {"fee": "NaN"},
])
def test_older_tail_never_bypasses_transaction_validation(change):
    source = boundary_rows()
    source[19].update(change)
    with pytest.raises(LedgerValidationError) as caught:
        parse(source)
    assert caught.value.row_index == 19


def test_duplicate_identity_in_older_tail_refuses_whole_page():
    source = boundary_rows()
    source[19]["id"] = source[0]["id"]
    with pytest.raises(LedgerValidationError) as caught:
        parse(source)
    assert (caught.value.code, caught.value.row_index) == ("duplicate_provider_id", 19)

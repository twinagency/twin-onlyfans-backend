"""Provider labels must not hold an entire financial import page."""
import json
from datetime import datetime
import pytest
from transaction_contract import classify_kind
from onlyfans_runtime.ledger import parse_page
from onlyfans_runtime.launch_ledger.validation import LedgerValidationError

@pytest.mark.parametrize("description,kind", [
    ('Stream purchase by <a href="/fan">fan</a>', "ppv_stream"),
    ("Stream from fan", "ppv_stream"),
    ("Payment for message from fan", "ppv_message"),
    ("Unrecognised purchase by fan", "unknown"),
])
def test_stream_purchase_classification(description, kind):
    assert classify_kind(description) == kind

def test_stream_purchase_page_keeps_money_and_no_media_ownership():
    row = dict(id="stream-1", user={"id":123},
        description='Stream purchase by <a href="/fan">fan</a>',
        status="done", currency="USD", amount=10,
        createdAt="2026-10-01T00:00:00Z")
    def parse():
        return parse_page(json.dumps({"list":[row],"hasMore":False}).encode(),
            owner_id=456,start=datetime(2026,9,1),end=datetime(2026,11,1))
    rows, more = parse()
    assert not more
    assert rows[0]["kind"] == "ppv_stream"
    assert rows[0]["amount_cents"] == 1000
    assert rows[0]["message_id"] is None
    row["description"] = "Unrecognised purchase by fan"
    with pytest.raises(LedgerValidationError) as exc:
        parse()
    assert exc.value.code == "unknown_kind"

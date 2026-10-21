"""Strict message interpretation and exact money; no HTTP/DB/model access."""
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from onlyfans_native_protocol_fixture import OWNER, native_message
from onlyfans_runtime.native_protocol import NativeProtocolError, acceptance, cents, load, message, plain_text, request
from onlyfans_runtime.text_delivery import Dispatch


def grant(**kwargs):
    return replace(Dispatch("acc-a", 101, "intent", "attempt", "private-token", "Hello & hi\n👩🏽‍🚀", "0"*64,
                            "manual", datetime.now(), 1, 1), **kwargs)


@pytest.mark.parametrize("value,expected", [(0,0),("0.00",0),("9.99",999),(10.07,1007),
    (Decimal("200.00"),20000),("1000000",100000000),("1e-2",1),(Decimal("1.0000000000000000000000000000000000000"),100)])
def test_exact_native_money(value, expected):
    assert cents(value) == expected


@pytest.mark.parametrize("value", [None,True,False,{},[],"", "NaN", "Infinity", -1, "-0", "0.001", "1000000.01",
    "1.000000000000000000000000000000001", "1e999999", "1e-999", "0."+"0"*100, float("inf"), " 1 "])
def test_native_money_never_silently_becomes_free_or_rounds_fractional_cents(value):
    with pytest.raises(NativeProtocolError): cents(value)


@pytest.mark.parametrize("raw", [b'[]',b'{"id":1,"id":2}',b'{"x":NaN}',b'{"x":Infinity}',b'\xff',b'{'*1001,b' '*65537],
    ids=['array','duplicate-key','nan','infinity','invalid-utf8','too-deep','too-large'])
def test_bounded_native_json_rejects_ambiguous_input(raw):
    with pytest.raises(NativeProtocolError): load(raw)


@pytest.mark.parametrize("patch", [
    {"id":True},{"id":1.0},{"id":"1"},{"fromUser":{"id":999}},{"toUser":{"id":999}},
    {"mediaCount":True},{"mediaCount":1},{"media":[{"id":1},{"id":1}],"mediaCount":2},
    {"media":[{"id":"1"}],"mediaCount":1},{"price":None},{"price":"0.001"},
    {"responseType":"tip"},{"isTip":True},{"isTip":0},{"isFromQueue":True},
    {"isUnsent":True},{"isDeleted":True},{"price":10,"isOpened":True},{"price":10,"isOpened":0},
    {"createdAt":"2026-10-18T00:00:00"},{"text":"<p><strong>unsupported</strong></p>"},
])
def test_unsupported_native_message_cannot_become_canonical_evidence(patch):
    value = {**native_message(), **patch}
    with pytest.raises(NativeProtocolError): message(value, owner_id=OWNER, fan_id=101)


def test_directions_html_entities_and_bound_recipient_are_explicit():
    row = native_message(text="<p>  café &amp; &lt;literal&gt;<br />next &amp;lt;  </p>")
    parsed = message(row, owner_id=OWNER, fan_id=101)
    assert parsed.direction == "in" and parsed.body == "  café & <literal>\nnext &lt;  "
    assert message(native_message(outgoing=True), owner_id=OWNER, fan_id=101).direction == "out"
    with pytest.raises(NativeProtocolError): message(row, owner_id=101, fan_id=101)


def test_native_send_freezes_exact_text_files_previews_price_and_receipt_hashes():
    dispatch = grant(media_ids=(11,12), preview_ids=(11,), price_cents=1007)
    raw = request(dispatch); value = json.loads(raw)
    assert value == {"text":"<p>Hello &amp; hi<br />👩🏽‍🚀</p>","lockedText":False,"mediaFiles":[11,12],
                     "price":10.07,"previews":[11],"rfTag":[],"rfGuest":[],"rfPartner":[],"isForward":False}
    reply = native_message(9001, outgoing=True, text=value["text"], media=(11,12), price="10.07")
    reply["previews"] = [11]
    proof = acceptance(json.dumps(reply).encode(), dispatch, owner_id=OWNER, request_bytes=raw)
    assert (proof.account_id,proof.fan_id,proof.message_id,proof.payload_hash) == ("acc-a",101,9001,"0"*64)
    assert proof.evidence_ref.startswith("native-local:") and len(proof.evidence_ref) < 256
    with pytest.raises(NativeProtocolError):
        acceptance(json.dumps(reply).encode(), dispatch, owner_id=OWNER, request_bytes=b'{}')


@pytest.mark.parametrize("body", [" leading", "trailing ", "<p>already HTML</p>", "&"*20000],
    ids=["leading-space","trailing-space","already-html","too-large"])
def test_native_formatting_that_changes_frozen_text_or_exceeds_wire_bound_is_refused(body):
    with pytest.raises(NativeProtocolError): request(grant(body=body))


def test_every_supported_cent_value_survives_existing_native_float_serialization():
    for price in range(20001):
        dispatch = grant(body="Hello", price_cents=price)
        assert cents(load(request(dispatch))["price"]) == price


@pytest.mark.parametrize("tag", ["strong", "b", "em", "i", "u", "s", "strike", "del", "span", "code", "sub", "sup"])
def test_inline_history_preserves_visible_text_without_affecting_send_evidence(tag):
    value = native_message(text=f'<p><{tag} class="ignored" onclick="ignored()">Hello &amp; <a href="https://synthetic.invalid">welcome</a><br/>back</{tag}></p>')
    event = message(value, owner_id=OWNER, fan_id=101, history=True)
    assert event.body == "Hello & welcome\nback"
    with pytest.raises(NativeProtocolError):
        message(value, owner_id=OWNER, fan_id=101)

def test_nested_history_formatting_keeps_words_and_paragraphs():
    value = native_message(text='<p><strong>Hello <em>there</em></strong></p><p><a href="x"><span>Next</span></a></p>')
    assert message(value, owner_id=OWNER, fan_id=101, history=True).body == "Hello there\nNext"

@pytest.mark.parametrize("html", [
    "<p><strong>unfinished</p>",
    "<p><a href='x'><a href='y'>nested links</a></a></p>",
    "<p><style>hidden</style></p>",
    "<p><iframe>hidden</iframe></p>",
    "<p>" + "<span>"*65 + "deep" + "</span>"*65 + "</p>",
])
def test_history_inline_support_keeps_structure_and_active_content_checks(html):
    with pytest.raises(NativeProtocolError):
        message(native_message(text=html), owner_id=OWNER, fan_id=101, history=True)

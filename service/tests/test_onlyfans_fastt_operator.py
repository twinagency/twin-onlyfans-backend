"""The runner's manual adapter preserves frozen payloads without real network I/O."""
from dataclasses import replace
from datetime import datetime, timedelta
import pytest
from onlyfans_runtime.fastt_manual import NotSent, post_operator_once
from onlyfans_runtime.native_protocol import NativeProtocolError
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import Dispatch


def grant(*, media=(11, 12, 13), body="Selected saved content"):
    return Dispatch("of-7001", 101, "intent", "attempt", "token", body, "a"*64,
        "manual", datetime.utcnow()+timedelta(minutes=1), media_ids=media,
        preview_ids=(11,) if media else (), price_cents=1000 if media else 0)


class Client:
    def __init__(self, change=None, timeout=False):
        self.calls=[]; self.change=change; self.timeout=timeout
    def response(self, body):
        if self.timeout: raise TimeoutError("synthetic lost acknowledgement")
        result={"id":9001, "fromUser":{"id":7001}, "toUser":{"id":101},
            "createdAt":"2026-10-26T05:00:00Z", "text":body["text"],
            "media":[{"id":m} for m in body["mediaFiles"]], "mediaCount":len(body["mediaFiles"]),
            "price":body["price"], "previews":body["previews"],
            "lockedText":False, "responseType":"message"}
        if self.change: self.change(result)
        return result
    def post_json(self, path, **kwargs):
        self.calls.append((path,kwargs))
        return self.response(kwargs["json_body"])
    def send_text_once_body(self, fan, text):
        self.calls.append((fan,text))
        return self.response({"text":text,"mediaFiles":[],"price":0,"previews":[]})


@pytest.mark.parametrize("media",[(11,), (12,), (11,12,13)])
def test_paid_payload_stays_paid_and_has_one_physical_attempt(media):
    item=grant(media=media)
    if 11 not in media: item=replace(item,preview_ids=())
    client=Client()
    assert post_operator_once(client,item,owner_id=7001)["id"]==9001
    assert len(client.calls)==1
    path,kw=client.calls[0]
    assert path=="/api2/v2/chats/101/messages"
    assert kw["proxy_tries"]==1 and kw["allow_redirects"] is False
    assert kw["json_body"]["mediaFiles"]==list(item.media_ids)
    assert kw["json_body"]["previews"]==list(item.preview_ids)
    assert kw["json_body"]["price"]==10


@pytest.mark.parametrize("body",["Ordinary reply", "  <p>literal</p> & emoji 🙂\nnext  "])
def test_manual_text_keeps_literal_markup_and_whitespace(body):
    client=Client()
    result=post_operator_once(client,grant(media=(),body=body),owner_id=7001)
    assert result["id"]==9001 and len(client.calls)==1


@pytest.mark.parametrize("field,value",[("price",0),("text","different"),
    ("fromUser",{"id":7002}),("toUser",{"id":102}),
    ("media",[{"id":99}]),("previews",[]),("lockedText",True),
    ("id",None),("isFromMe",False),("isUnsent",True)])
def test_contradictory_paid_receipt_remains_unknown_after_one_post(field,value):
    client=Client(change=lambda row:row.update({field:value}))
    with pytest.raises((ValueError,RuntimeError)):
        post_operator_once(client,grant(),owner_id=7001)
    assert len(client.calls)==1


@pytest.mark.parametrize("media",[(),(11,12,13)])
def test_lost_response_is_never_reported_not_sent_or_retried(media):
    client=Client(timeout=True)
    with pytest.raises(TimeoutError):
        post_operator_once(client,grant(media=media),owner_id=7001)
    assert len(client.calls)==1


@pytest.mark.parametrize("field,value",[("price",10),("text","different"),
    ("toUser",{"id":102}),("previews",[11]),("lockedText",True)])
def test_contradictory_text_receipt_is_not_delivery(field,value):
    client=Client(change=lambda row:row.update({field:value}))
    with pytest.raises((ValueError,RuntimeError)):
        post_operator_once(client,grant(media=()),owner_id=7001)
    assert len(client.calls)==1


@pytest.mark.parametrize("change",[{"price_cents":1000},{"preview_ids":(11,)},{"origin":"campaign"}])
def test_unsupported_payload_is_refused_before_any_provider_call(change):
    client=Client()
    with pytest.raises(NotSent):
        post_operator_once(client,replace(grant(media=()),**change),owner_id=7001)
    assert client.calls==[]

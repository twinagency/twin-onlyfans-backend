"""Strict native-client message dialect, with no transport or credentials.

This deliberately accepts a bounded subset of the existing client's message
shape. Unsupported/ambiguous history refuses; a POST status alone never proves
delivery, non-delivery, purchase or permission to retry. Real platform acceptance
of this subset is a separate release gate.
"""
from decimal import Decimal,InvalidOperation
import hashlib
import html
from html.parser import HTMLParser
import json
import re

from of_message_contract import message_body
from .store import Event,Refused,_id
from .text_delivery import Accepted,Dispatch,_stamp

API_PATH="/api2/v2"
MAX_BYTES=65536
MAX_VAULT_BYTES=1024*1024
MAX_HISTORY_BYTES=64*1024*1024


class NativeProtocolError(ValueError):pass


def load(raw, *, max_bytes=MAX_BYTES):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise NativeProtocolError("duplicate native response key")
            result[key]=value
        return result
    def nonfinite(_):raise NativeProtocolError("nonfinite native response")
    try:
        if (type(max_bytes) is not int or not 1<=max_bytes<=max(MAX_VAULT_BYTES,MAX_HISTORY_BYTES)
            or type(raw) is not bytes or len(raw)>max_bytes):raise NativeProtocolError("invalid native response size")
        value=json.loads(raw.decode("utf-8"),object_pairs_hook=unique,parse_float=Decimal,parse_constant=nonfinite)
    except (ValueError,UnicodeError,RecursionError) as exc:raise NativeProtocolError("invalid native JSON response") from exc
    if type(value) is not dict:raise NativeProtocolError("native response must be an object")
    return value


def cents(value):
    if type(value) not in {str,int,float,Decimal}:raise NativeProtocolError("explicit native money required")
    try:
        representation=str(value)
        if len(representation)>40 or not re.fullmatch(r"\d+(?:\.\d+)?(?:[eE][+-]?\d{1,3})?",representation):raise ValueError()
        money=Decimal(representation)
        if not money.is_finite() or money.is_signed() or money>1000000:raise ValueError()
        parts=money.as_tuple()
        coefficient=int("".join(map(str,parts.digits)))
        exponent=parts.exponent+2
        if exponent>=0:return coefficient*10**exponent
        minor,remainder=divmod(coefficient,10**(-exponent))
        if remainder:raise ValueError()
        return minor
    except (ValueError,InvalidOperation,OverflowError):raise NativeProtocolError("invalid native integer money") from None


def plain_text(value):
    if type(value) is not str or len(value.encode())>20000:raise NativeProtocolError("invalid native text")
    if value.startswith("<p>") and value.endswith("</p>"):
        value=value[3:-4].replace("<br />","\n").replace("<br/>","\n").replace("<br>","\n")
        if "<" in value or ">" in value:raise NativeProtocolError("unsupported native rich text")
        return html.unescape(value)
    if "<" in value or ">" in value:raise NativeProtocolError("unsupported native rich text")
    return value


class _HistoryText(HTMLParser):
    """Keep visible history formatting; attributes never become CRM authority."""
    INLINE = frozenset({"strong", "b", "em", "i", "u", "s", "strike", "del", "span", "code", "sub", "sup"})
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        if len(self.stack) >= 64:
            raise NativeProtocolError("native history formatting is too deep")
        if tag == "br" and self.stack:
            self.parts.append("\n")
        elif tag == "p" and not self.stack:
            if self.parts and not self.parts[-1].endswith("\n"):
                self.parts.append("\n")
            self.stack.append(tag)
        elif tag in self.INLINE and self.stack:
            self.stack.append(tag)
        elif tag == "a" and self.stack and "a" not in self.stack:
            self.stack.append(tag)
        else:
            raise NativeProtocolError("unsupported native history formatting")

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise NativeProtocolError("unbalanced native history formatting")

    def handle_startendtag(self, tag, attrs):
        if tag != "br":
            raise NativeProtocolError("unsupported native history formatting")
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if not self.stack and data.strip():
            raise NativeProtocolError("unsupported native history formatting")
        if self.stack:
            self.parts.append(data)

    def handle_comment(self, data):
        raise NativeProtocolError("unsupported native history formatting")

    def handle_decl(self, decl):
        raise NativeProtocolError("unsupported native history formatting")

    def handle_pi(self, data):
        raise NativeProtocolError("unsupported native history formatting")


def history_text(value):
    try:
        return plain_text(value)
    except NativeProtocolError:
        if type(value) is not str or len(value.encode()) > 20000:
            raise NativeProtocolError("unsupported native history formatting") from None
        # Older archive bodies contain bare text separated by BR tags, without
        # a paragraph wrapper. Accept only those same three known break forms;
        # other markup still requires the bounded, balanced history parser.
        unwrapped = value.replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n")
        if unwrapped != value and "<" not in unwrapped and ">" not in unwrapped:
            return html.unescape(unwrapped)
        if not value.startswith("<p>") or not value.endswith("</p>"):
            raise NativeProtocolError("unsupported native history formatting") from None
    parser = _HistoryText()
    parser.feed(value)
    parser.close()
    if parser.stack:
        raise NativeProtocolError("unbalanced native history formatting")
    return "".join(parser.parts)


def message(value,*,owner_id,fan_id,history=False):
    try:
        _id(owner_id);_id(fan_id)
        if type(history) is not bool:raise ValueError()
        if owner_id==fan_id:raise ValueError()
        if type(value) is not dict or not {"id","fromUser","createdAt","text","media","mediaCount","price","responseType"}<=set(value):raise ValueError()
        if value["responseType"]!="message" or type(value["fromUser"]) is not dict:raise ValueError()
        sender=_id(value["fromUser"].get("id"));identity=_id(value["id"])
        if sender not in {owner_id,fan_id}:raise ValueError()
        if "toUser" in value:
            if type(value["toUser"]) is not dict or _id(value["toUser"].get("id"))!=(fan_id if sender==owner_id else owner_id):raise ValueError()
        if any(value.get(key) is not None and value.get(key) is not False
               for key in ("isTip","isUnsent","isDeleted")):raise ValueError()
        if value.get("isFromQueue") is not None and type(value["isFromQueue"]) is not bool:raise ValueError()
        if value.get("isFromQueue") is True and not history:raise ValueError()
        media=value["media"]
        if type(media) is not list or len(media)>100 or any(type(row) is not dict for row in media):raise ValueError()
        ids=tuple(_id(row.get("id")) for row in media)
        if len(set(ids))!=len(ids) or type(value["mediaCount"]) is not int or value["mediaCount"]!=len(ids):raise ValueError()
        price=cents(value["price"])
        # A history flag is not an authoritative, source-revisioned payment event.
        if value.get("isOpened") is not None and type(value["isOpened"]) is not bool:raise ValueError()
        if price and value.get("isOpened") is True and not history:raise ValueError()
        # History is a message observation only. Queued/opened flags grant no
        # payment, ownership or dispatch acceptance; ledger evidence owns those.
        stamp=value["createdAt"]
        if type(stamp) is not str:raise ValueError()
        _stamp(stamp)
        text = history_text(value["text"]) if history else plain_text(value["text"])
        return Event(fan_id,identity,stamp,"out" if sender==owner_id else "in",text,ids,price).normalized()
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError) as exc:raise NativeProtocolError("native message scope or evidence is unsupported") from exc


def request(dispatch):
    if type(dispatch) is not Dispatch:raise NativeProtocolError("recorded native dispatch required")
    # Preserve the canonical text exactly after the native HTML representation.
    body=message_body(dispatch.body,media_files=list(dispatch.media_ids),previews=list(dispatch.preview_ids),price=dispatch.price_cents/100)
    if plain_text(body["text"])!=dispatch.body or cents(body["price"])!=dispatch.price_cents:
        raise NativeProtocolError("native formatting would alter the frozen payload")
    raw=json.dumps(body,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()
    if len(raw)>MAX_BYTES:raise NativeProtocolError("native request exceeds byte budget")
    return raw


def acceptance(raw,dispatch,*,owner_id,request_bytes):
    if request_bytes!=request(dispatch):raise NativeProtocolError("native request evidence differs")
    value=load(raw);event=message(value,owner_id=owner_id,fan_id=dispatch.fan_id)
    if (event.direction!="out" or event.body!=dispatch.body or event.media_ids!=dispatch.media_ids
            or event.price_cents!=dispatch.price_cents):raise NativeProtocolError("native response does not match the frozen request")
    expected=json.loads(request_bytes)
    if value["text"]!=expected["text"]:raise NativeProtocolError("native text representation changed")
    if dispatch.media_ids:
        previews=value.get("previews")
        if type(previews) is not list or any(type(item) is not int for item in previews) or previews!=list(dispatch.preview_ids):
            raise NativeProtocolError("native preview evidence is unavailable")
    if value.get("lockedText") is not False:raise NativeProtocolError("native lock evidence differs")
    evidence="native-local:"+hashlib.sha256(request_bytes).hexdigest()+":"+hashlib.sha256(raw).hexdigest()
    return Accepted(dispatch.account_id,dispatch.fan_id,dispatch.intent_id,dispatch.attempt_id,
        dispatch.payload_hash,event.message_id,event.created_at,evidence)


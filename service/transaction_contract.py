"""Provider-free vocabulary shared by the existing and dedicated ledger readers."""
import re

KIND_CATALOG = (
    ("Recurring subscription from ", "rebill"),
    ("Subscription from ", "subscription"),
    ("Tip in chat from ", "tip"),
    ("Tip from ", "tip"),
    ("Tip on post from ", "tip_post"),
    ("Tip on stream from ", "tip_stream"),
    ("Payment for message from ", "ppv_message"),
    ("Message from ", "ppv_message"),
    ("Post from ", "ppv_post"),
    ("Post purchase by ", "ppv_post"),
    ("Stream from ", "ppv_stream"),
    ("Custom request from ", "custom"),
    ("Custom from ", "custom"),
)
STATUS_MAP = {"done": "cleared", "loading": "pending", "undo": "chargedback",
    "pending_return": "refund_pending"}
# Canonical native ledger states. A failed payment is known non-revenue;
# unrecognised states must still refuse complete financial coverage.
PAID = frozenset({"pending", "cleared", "refund_pending"})
TERMINAL = frozenset({"refunded", "chargedback", "failed"})
STATUSES = PAID | TERMINAL
_HTML_TAG = re.compile(r"<[^>]+>")


def strip_html(value):
    return _HTML_TAG.sub("", value).strip() if value else ""


def classify_kind(description):
    value = strip_html(description) if description and "<" in description else (description or "").strip()
    return next((kind for prefix, kind in KIND_CATALOG if value.startswith(prefix)), "unknown")

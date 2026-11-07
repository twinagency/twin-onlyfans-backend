"""Authenticate raw webhook bytes before parsing or persisting an event.

Provider docs: https://docs.onlyfansapi.com/webhooks/protecting-your-webhooks
Persistence, deduplication, account binding and acknowledgement are separate.
"""
import hashlib
import hmac
import re


def verify_signature(raw_body: bytes, signature: str, secret: bytes) -> bool:
    if (type(raw_body) is not bytes or type(secret) is not bytes or not secret or
            type(signature) is not str or not re.fullmatch(r"[0-9a-fA-F]{64}", signature)):
        return False
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())

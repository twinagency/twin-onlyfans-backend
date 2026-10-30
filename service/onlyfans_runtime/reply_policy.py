"""Default-off native conversational features. No timing or model work here."""
import logging
import json

PACING_KEY = "native_reply_pacing_enabled"
FORMAT_KEY = "native_reply_format_enabled"
SWITCHES = {"nativeReplyPacingEnabled": PACING_KEY, "nativeReplyFormatEnabled": FORMAT_KEY}

FORMAT_INSTRUCTION = (
    "Answer the latest complete message in the creator's approved voice. "
    "Use the amount of detail the reply needs. A brief acknowledgment or one short sentence "
    "is sufficient when it answers the turn. Expand when the question needs explanation. "
    "Ask a follow-up only when it helps the conversation. Do not pad the reply or split it "
    "merely to meet a format."
)


def saved_object(raw):
    """Small stdlib-only read at the admission boundary; never import the API."""
    def unique(pairs):
        result = {}
        for key,value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    if not raw:
        return {}
    if type(raw) is not str or len(raw.encode()) > 65536:
        raise ValueError("configuration too large")
    value = json.loads(raw,object_pairs_hook=unique)
    if type(value) is not dict:
        raise ValueError("configuration object required")
    return value


def enabled(raw, key):
    value = raw.get(key, False)
    if type(value) is not bool:
        logging.getLogger(__name__).warning("native reply switch disabled: invalid %s", key)
        return False
    return value

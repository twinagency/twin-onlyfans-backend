"""Literal opt-ins shared by the audited editor and the native runner."""
import logging

SWITCHES = {"nativeReplyPacingEnabled": "native_reply_pacing_enabled"}


def enabled(settings, key):
    value = settings.get(key, False)
    if type(value) is not bool:
        logging.getLogger(__name__).warning("invalid-native-reply-switch: %s", key)
        return False
    return value

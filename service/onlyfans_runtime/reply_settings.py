"""Literal opt-ins shared by the audited editor and the native runner."""
import logging

SWITCHES = {"nativeReplyPacingEnabled": "native_reply_pacing_enabled",
            "nativeReplyFormatEnabled": "native_reply_format_enabled"}


def enabled(settings, key):
    value = settings.get(key, False)
    if type(value) is not bool:
        logging.getLogger(__name__).warning("invalid-native-reply-switch: %s", key)
        return False
    return value

DELAY_MAX_FIELD = "nativeReplyDelayMaxSeconds"
DELAY_MAX_KEY = "native_reply_delay_max_seconds"


def delay_max(settings):
    value = settings.get(DELAY_MAX_KEY, 90)
    if type(value) is not int or value not in (90, 180):
        raise ValueError("Reply delay maximum must be 90 or 180 seconds.")
    return value

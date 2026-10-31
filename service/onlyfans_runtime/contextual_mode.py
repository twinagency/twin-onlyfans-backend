"""Contextual selling permission is independent of every legacy sales switch."""
import logging

KEY = "fastt_contextual_sales_mode"
FIELD = "fasttContextualSalesMode"
MODES = ("off", "shadow", "live")
log = logging.getLogger(__name__)


def mode(settings):
    value = settings.get(KEY, "off") if type(settings) is dict else "off"
    if type(value) is str and value in MODES:
        return value
    log.warning("invalid_contextual_sales_mode: treating saved value as off")
    return "off"

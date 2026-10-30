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

def new_account_settings():
    """Saved offer defaults; provisioning and new-fan automation stay disabled."""
    return {KEY: "live", "pack_send_enabled": True, "pack_on_ask_enabled": True,
            "qualification_gate_enabled": True, "fastt_vault_mode": True,
            "fastt_text_only": False}

"""Per-creator mode for the new conversation-safety detectors: off | shadow | live (default shadow).

Same pattern and same saved JSON blob (``account_ai_config.ai_chatter_config_json``) as the contextual-offer mode
(``contextual_mode``), edited through the same audited configuration route. ``shadow`` records each verdict and
enforces nothing; ``live`` writes operator cases and conversation controls. The identity hold is the pre-existing
policy and is enforced in every mode. Holds already written (cases, controls) are honoured whatever the mode.
"""
import logging

KEY = "conversation_safety_mode"
FIELD = "conversationSafetyMode"
MODES = ("off", "shadow", "live")
DEFAULT = "shadow"
log = logging.getLogger(__name__)


def mode(settings):
    if type(settings) is not dict or KEY not in settings:
        return DEFAULT
    value = settings[KEY]
    if type(value) is str and value in MODES:
        return value
    log.warning("invalid_conversation_safety_mode: treating saved value as shadow")
    return DEFAULT

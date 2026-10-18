"""Small configuration boundary shared by the command gate and read capability."""
import hmac
import os
import re

SCHEMA = "of-controls.v1"
HEADER = "X-OnlyFans-Control-Version"
PREFIX = "/onlyfans/control/v1"
ACTIONS = {"pause_automation": "paused", "resume_automation": "automated",
           "request_takeover": "human_controlled", "release_takeover": "automated",
           "mark_no_contact": "no_contact"}


def configured():
    token = (os.environ.get("ONLYFANS_CONTROL_TOKEN") or "").strip()
    tenant = (os.environ.get("ONLYFANS_CONTROL_TENANT") or "").strip()
    others = [(os.environ.get(name) or "").strip() for name in ("ONLYFANS_API_TOKEN","ONLYFANS_CONFIGURATION_TOKEN","ONLYFANS_CAMPAIGN_TOKEN","ONLYFANS_CONTENT_TOKEN","ONLYFANS_CONNECTION_TOKEN")]
    if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "api" or not token or
            not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", tenant) or tenant == "deployment" or
            any(other and hmac.compare_digest(token.encode(),other.encode()) for other in others)):
        return None
    return token, tenant


def manual_configured():
    """Explicit operator capability; queued messages still pass every delivery gate."""
    return configured() if os.environ.get("ONLYFANS_MANUAL_ENABLED") == "1" else None

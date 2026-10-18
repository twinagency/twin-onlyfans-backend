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
    read_token = (os.environ.get("ONLYFANS_API_TOKEN") or "").strip()
    if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "api" or not token or
            not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", tenant) or tenant == "deployment" or
            (read_token and hmac.compare_digest(token.encode(), read_token.encode()))):
        return None
    return token, tenant

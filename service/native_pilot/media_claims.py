"""A text-only reply must never say it SENT media (a pic, video, voice note, "something") when nothing was sent.

Pure and conservative: it only matches a first-person past/progressive statement that a media item is on its
way or has been delivered. Asking, offering ("want me to send you a pic?"), refusing and talking about a pic the
fan sent are not matches. The runner calls it for every plain-text bubble of a turn that sent no media; a match
holds the bubble (``MEDIA_CLAIM_HELD``) and the turn is regenerated once.
"""
from __future__ import annotations

import re

_MEDIA = (r"(?:pic|pics|picture|pictures|photo|photos|selfie|selfies|video|videos|vid|vids|clip|clips|"
          r"voice\s*(?:note|message|memo)|nude|nudes|content|vault|gallery|something\s+(?:special|naughty|spicy|hot|fun))")
_DET = r"(?:(?:a|an|the|my|some|that|those|these|this|another|ur|your|new)\s+)?(?:\w+\s+){0,2}?"
_RULES = (
    # "i just sent you a pic", "sent u the video", "i've sent you something special"
    re.compile(rf"\b(?:i\s*'?\s*ve|ive|i\s+have|i|just|already|jus)\s+(?:just\s+|already\s+)?sent\s+(?:you\s+|u\s+|ya\s+)?(?:over\s+)?{_DET}{_MEDIA}\b", re.I),
    # "sending you a pic now", "i'm sending u the video"
    re.compile(rf"\b(?:(?:i\s*'?\s*m|im|i\s+am)\s+)?(?:just\s+)?sending\s+(?:you\s+|u\s+|ya\s+){_DET}{_MEDIA}\b", re.I),
    re.compile(rf"\bsent\s+(?:you|u|ya)\s+(?:over\s+)?{_DET}{_MEDIA}\b", re.I),
    # "check your dms/inbox for the pic", "it's in your inbox", "just dropped it in your dms"
    re.compile(r"\b(?:check|look\s+in|open)\s+(?:your|ur)\s+(?:dms?|inbox|messages)\b", re.I),
    re.compile(r"\bdropped\s+(?:it|them|one|something)\s+in\s+(?:your|ur)\s+(?:dms?|inbox|messages)\b", re.I),
    # "here's the pic", "here u go", "here you go" immediately followed by nothing else means a missing attachment
    re.compile(rf"\bhere(?:'?s|\s+is|\s+are)\s+{_DET}{_MEDIA}\b", re.I),
    re.compile(r"^\W*here\s+(?:you\s+go|u\s+go|ya\s+go)\W*$", re.I),
)

MEDIA_CLAIM_HELD = "media-claim-unbacked"


def claims_media_sent(text: str | None) -> bool:
    """True when ``text`` says media was sent / is being sent."""
    if not text:
        return False
    return any(rule.search(text) for rule in _RULES)

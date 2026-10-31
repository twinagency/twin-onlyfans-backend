"""Lightweight inbox liveness check; never import application or database code."""
import json
from pathlib import Path
import time

HEALTH_PATH = "/tmp/onlyfans-inbox-health.json"


def healthy(path=HEALTH_PATH):
    try:
        value = json.loads(Path(path).read_text())
        now = time.time()
        return (now - value["observedAt"] < 30
                and value.get("rosterUpdatedAt") is not None
                and now - value["rosterUpdatedAt"] < 60
                and all(not row.get("stale", True) for row in value["accounts"].values()))
    except (OSError, ValueError, KeyError, TypeError):
        return False

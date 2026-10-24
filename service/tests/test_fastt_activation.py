"""Synthetic deployment-scope validation; never connects to any provider or database."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from onlyfans_runtime.activation import deployment_scope, validate_selection
from onlyfans_runtime.configuration import validate_patch, InvalidConfiguration

checks = 0
def check(value):
    global checks
    assert value
    checks += 1

def rejected(account, value):
    try: validate_selection(account, value)
    except ValueError: check(True)
    else: raise AssertionError("Unauthorized automation selection was accepted")

with patch.dict(os.environ, {"ONLYFANS_FASTT_SCOPE": "db:of-900001,of-900002:123,of-900002:456"}):
    check(deployment_scope("of-900001")["allFansAllowed"])
    check(deployment_scope("of-900002")["allowedFans"] == [123,456])
    check(not deployment_scope("of-900003")["deploymentAllowed"])
    check(validate_selection("of-900001", {"mode":"all","fans":[]}) == {"mode":"all","fans":[]})
    check(validate_selection("of-900002", {"mode":"selected","fans":[456,123,123]})["fans"] == [123,456])
    rejected("of-900002", {"mode":"all","fans":[]})
    rejected("of-900003", {"mode":"selected","fans":[123]})
    rejected("of-900002", {"mode":"selected","fans":[999]})
    rejected("of-900001", {"mode":"selected","fans":[True]})
    rejected("of-900001", {"mode":"selected","fans":[2**63]})
    rejected("of-900001", {"mode":"selected","fans":[]})
    rejected("of-900001", {"mode":"all","fans":[],"activate":True})
    check(validate_patch({"fasttScope":{"mode":"single","fans":[123]}}) == {"fasttScope":{"mode":"single","fans":[123]}})
    for value in (None, True, {"mode":"selected","fans":[]}):
        try: validate_patch({"fasttScope":value})
        except InvalidConfiguration: check(True)
        else: raise AssertionError("Malformed patch accepted")

print(f"FASTT activation validation: {checks} checks passed")

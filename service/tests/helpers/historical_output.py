"""Replay captured model output against the current output contract.

Raw historical outputs are immutable evidence. They predate the mandatory
`unanswered_input_ids` field, so a replay must add it explicitly and visibly;
the original text is never edited and the adapter is applied at the call site.
"""
import json


def adapt_historical_output(raw, unanswered_input_ids=()):
    value = json.loads(raw)
    value.setdefault("unanswered_input_ids", list(unanswered_input_ids))
    return json.dumps(value)

"""Pure shared carrier-intent predicate, usable by the API without provider imports."""


def intent_unresolved(item):
    if item.get("carrier_write_pending"):
        return True
    return bool(item.get("carrier_intent") and not item.get("carrier_post_id")
                and not item.get("carrier_scan_clear"))

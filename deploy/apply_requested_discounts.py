"""Apply the requested-discount policy to an exact deployed source snapshot.

Only named functions/constants are changed; unrelated live patches are preserved.
Run offline against a candidate tree, never against a running container.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import sys
import textwrap


def function_edit(source, name, edit):
    matches = [n for n in ast.walk(ast.parse(source))
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert len(matches) == 1, (name, len(matches))
    n = matches[0]
    lines = source.splitlines(keepends=True)
    old = ''.join(lines[n.lineno - 1:n.end_lineno])
    new = edit(old)
    assert new != old, ('no change', name)
    return ''.join(lines[:n.lineno - 1]) + new + ''.join(lines[n.end_lineno:])


def replace_once(source, old, new):
    assert source.count(old) == 1, (old[:100], source.count(old))
    return source.replace(old, new, 1)


def apply(root):
    changes = {}
    def change(name, edit):
        p = root / name
        before = p.read_text()
        after = edit(before)
        ast.parse(after)
        changes[name] = (p, before, after)

    def upsell(source):
        source = re.sub(r'^DISCOUNT_BAND = .*$',
                        'DISCOUNT_BAND = (0.80, 0.85)    # Requested: 15% default, 20% maximum.', source, count=1, flags=re.M)
        source = re.sub(r'^RESEND_AFTER_M = .*$',
                        'RESEND_AFTER_M = 0               # One explicit request is enough; no age gate.', source, count=1, flags=re.M)
        source = function_edit(source, 'discount_price', lambda _: textwrap.dedent('''\
def discount_price(last_sent_cents: int, rng: Random, account_id: str,
                   *, discount_pct: float = 0.15, min_cents: int = OF_PRICE_FLOOR_CENTS) -> int:
    """15% by default, at most 20% of the price actually shown; never below the floor."""
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
    try:
        pct = Decimal(str(discount_pct))
        if not pct.is_finite():
            raise InvalidOperation()
    except (InvalidOperation, ValueError):
        pct = Decimal("0.15")
    pct = max(Decimal("0.15"), min(Decimal("0.20"), pct))
    price = int((Decimal(last_sent_cents) * (1 - pct)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    # Round the 20% ceiling towards the original price, never beyond it.
    return max(OF_PRICE_FLOOR_CENTS, int(min_cents), (last_sent_cents * 80 + 99) // 100, price)
'''))
        source = function_edit(source, 'may_discount', lambda _: textwrap.dedent('''\
def may_discount(ctx: DiscountCtx) -> tuple[bool, str]:
    """A single explicit request qualifies; silence, distress and repeat cuts do not."""
    from ._price_ask import PRICE_ASK
    if ctx.spend_regret:
        return False, "distress"
    if not (ctx.objection_at is not None or detect_stated_cap(ctx.last_inbound_text)
            or PRICE_ASK.search(ctx.last_inbound_text or "")):
        return False, "unprompted"
    if ctx.discount_count_this_item >= 1:
        return False, "already_cut"
    return True, "ok"
'''))
        return source
    change('automations/upsell.py', upsell)

    change('automations/_price_ask.py', lambda s: function_edit(s, 'beat_after', lambda _: textwrap.dedent('''\
def beat_after(inbound: list[tuple[datetime, str]], objection_at: datetime | None) -> bool:
    """Compatibility name: the request itself qualifies; no second fan message is required."""
    return objection_at is not None
''')))

    def chatter(source):
        source = replace_once(source, '"haggle_discount_pct": 0.10,', '"haggle_discount_pct": 0.15,')
        source = replace_once(source,
            'haggle_pct = float(cfg.get("haggle_discount_pct") or _HAGGLE_DISCOUNT_PCT)',
            'haggle_pct = max(0.15, min(0.20, float(cfg.get("haggle_discount_pct") or 0.15)))')
        def edit(s):
            s = replace_once(s,
                'if not offer.offered_at or offer.offered_at > now - timedelta(minutes=upsell.RESEND_AFTER_M):\n        return _skip("too-early")',
                'if not offer.offered_at or offer.offered_at > now:\n        return _skip("invalid-offer-time")')
            s = replace_once(s, '    cuts30, asks30 = await _cuts_asks_last_30d(account_id, fan_id, now)',
                '    from . import _price_ask\n'
                '    # The request must follow THIS offer, not an earlier card.\n'
                '    if last_in_at is None or last_in_at < offer.offered_at:\n'
                '        return _skip("unprompted")\n'
                '    if (lad.objection_at is None or lad.objection_at < offer.offered_at) and not (\n'
                '            _price_ask.PRICE_ASK.search(last_text or "") or upsell.detect_stated_cap(last_text or "")):\n'
                '        return _skip("unprompted")\n'
                '    cuts30, asks30 = await _cuts_asks_last_30d(account_id, fan_id, now)')
            s = replace_once(s, 'upsell.discount_price(seen_cents, rng, str(account_id))',
                'upsell.discount_price(seen_cents, rng, str(account_id),\n'
                '                                       discount_pct=cfg.get("haggle_discount_pct", 0.15),\n'
                '                                       min_cents=int(cfg.get("discount_min_price_cents") or upsell.OF_PRICE_FLOOR_CENTS))')
            s = replace_once(s, 'if seen_cents <= upsell.OF_PRICE_FLOOR_CENTS:',
                'if seen_cents <= max(upsell.OF_PRICE_FLOOR_CENTS, int(cfg.get("discount_min_price_cents") or 0)):')
            return s
        source = function_edit(source, '_maybe_discount_resend', edit)
        source = function_edit(source, '_run_discount', lambda s: replace_once(s,
            '    if objection_at is not None:\n',
            '    if objection_at is None:\n        return _skipped("unprompted")\n    if objection_at is not None:\n'))
        return source
    change('automations/ai_chatter.py', chatter)

    def postsale(source):
        def edit(s):
            s = replace_once(s, 'AND NOT COALESCE(m.is_paid, FALSE) ORDER BY o.id LIMIT 200',
                             'AND NOT COALESCE(m.is_paid, FALSE) ORDER BY o.id DESC LIMIT 200')
            s = replace_once(s, '    for oid, fan, offered, price, _msg in rows:\n',
                '    seen_fans = set()\n    for oid, fan, offered, price, _msg in rows:\n'
                '        # One request concerns the latest open card, not every older unpaid card.\n'
                '        if int(fan) in seen_fans:\n            continue\n        seen_fans.add(int(fan))\n')
            if 'Silence never authorizes' in s:
                s = replace_once(s,
                    '            # Silence never authorizes a discounted replacement for a contextual card.\n            continue',
                    '            # A contextual card is eligible only after a voiced request.\n'
                    '            from automations import _price_ask\n'
                    '            objection, blocker = _price_ask.objection_state(\n'
                    '                inbound_since(con, account, int(fan), _naive(offered)))\n'
                    '            if objection is None or blocker:\n                continue')
            return s
        return function_edit(source, 'unpaid_cards', edit)
    change('native_pilot/postsale.py', postsale)

    # Older and current sources call the same qualification helper. Keep queue and sender aligned.
    change('native_pilot/service.py', lambda s: function_edit(s, 'scan_postsale', lambda f: replace_once(f,
        'postsale.unpaid_cards(con, self.db, now, upsell.RESEND_AFTER_M)',
        'postsale.unpaid_cards(con, self.db, now, 0)')))

    engine = root / 'native_pilot/engine.py'
    if 'and ignored_contextual(con, self.account_id, self.fan_id)' in engine.read_text():
        change('native_pilot/engine.py', lambda s: replace_once(s,
            'and ignored_contextual(con, self.account_id, self.fan_id):',
            'and self._job == NUDGE_KIND and ignored_contextual(con, self.account_id, self.fan_id):'))

    # All compatibility assertions run before any file is written.
    report = {}
    for name, (p, before, after) in changes.items():
        p.write_text(after)
        report[name] = {'before': hashlib.sha256(before.encode()).hexdigest(),
                        'after': hashlib.sha256(after.encode()).hexdigest()}
    return report


if __name__ == '__main__':
    print(json.dumps(apply(Path(sys.argv[1])), sort_keys=True))

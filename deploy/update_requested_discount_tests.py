"""Update existing integration expectations for the approved discount policy."""
from pathlib import Path
import sys


def apply(root):
    p = root / 'tests/test_native_pilot_postsale_engine.py'
    s = p.read_text()
    edits = {
        '(lo, hi) == (0.80, 0.90)': '(lo, hi) == (0.80, 0.85)',
        "    await refused('B objection and no beat', 'same_turn', setup=lambda: objection(then=None))": """    # A single request shortly after a fresh card qualifies immediately.
    await reset()
    oid = await put_card(price=670, minutes_ago=0.5)
    await put_message(FAN, 'in', 'Can you please make it cheaper baby and I will buy it', 0)
    stats, out, gw = await run_job({'discount_offer': oid})
    check(stats.get('discount_resends') == 1 and len(out) == 1, ('immediate single request', stats))
    check(out[0][1]['price'] == 5.70, ('15 percent rounded to cents', out))""",
        "await refused('B too early', 'too-early', setup=objection, minutes_ago=30)":
            "await refused('B an older request cannot discount a new card', 'unprompted', setup=objection, minutes_ago=30)",
        "    check(stats.get('discount_skip') == 'governor', ('C: governor', stats))\n    untouched('C governor')":
            "    check(stats.get('discount_resends') == 1, ('C: explicit request is not blocked by earlier unrelated discounts', stats))",
    }
    for old, new in edits.items():
        assert s.count(old) == 1, ('test compatibility', old[:60])
        s = s.replace(old, new, 1)
    p.write_text(s)


if __name__ == '__main__':
    apply(Path(sys.argv[1]))

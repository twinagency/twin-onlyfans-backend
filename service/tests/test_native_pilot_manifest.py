"""Manifest built from the approved catalogue: exact rows only, blockers never clamped."""
import json, sqlite3, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_pilot.ledger import Refused
from native_pilot.manifest import build, main, verify

ACCOUNT, OTHER, FAN = 'of-900001', 'of-900009', 900002
checks = 0


def check(cond, label):
    global checks
    assert cond, label
    checks += 1


def make_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE catalog_items (id INTEGER PRIMARY KEY, account_id TEXT, kind TEXT, media_ids TEXT,
            preview_media_ids TEXT, price_cents INTEGER, is_free_teaser INTEGER, enabled INTEGER, pricing_mode TEXT);
        CREATE TABLE vault_items (account_id TEXT, media_id INTEGER, kind TEXT, PRIMARY KEY (account_id, media_id));
    """)
    con.executemany("INSERT INTO vault_items VALUES (?,?,?)",
                    [(ACCOUNT, 1001, 'photo'), (ACCOUNT, 1002, 'video'), (ACCOUNT, 1003, 'video'),
                     (OTHER, 1004, 'photo')])
    return con


def row(con, id_, media, previews, price, *, account=ACCOUNT, enabled=1, teaser=0, mode='fixed', kind='video'):
    con.execute("INSERT INTO catalog_items VALUES (?,?,?,?,?,?,?,?,?)",
                (id_, account, kind, json.dumps(media), json.dumps(previews), price, teaser, enabled, mode))
    con.commit()


with tempfile.TemporaryDirectory() as tmp:
    db = str(Path(tmp) / 'c.db')
    con = make_db(db)

    manifest, blockers = build(db, ACCOUNT, FAN)
    check(manifest is None and blockers == [{'catalog_id': None, 'reason': 'no-approved-items'}], 'empty catalogue')

    # One $3 item: the legacy shape, no price range, and it loads as the runner loads it.
    row(con, 1, [1001, 1002], [1001], 300)
    row(con, 9, [1004], [], 500, account=OTHER)          # another account's row is ignored
    row(con, 5, [1003], [], 900, enabled=0)               # a disabled draft is not listed
    manifest, blockers = build(db, ACCOUNT, FAN)
    check(blockers == [] and [i['catalog_id'] for i in manifest['items']] == [1], 'one approved item')
    check('price_range_cents' not in manifest, 'legacy fixed $3 has no range')
    check(manifest['items'][0]['media'] == [{'id': 1001, 'type': 'photo'}, {'id': 1002, 'type': 'video'}],
          'media types from the vault')
    plan = verify(db, manifest, FAN)
    check(plan.catalog_ids == {1}, 'runner loads the manifest and the catalogue matches')

    # A second approved item at its own exact price: listed with a range, never clamped to $3.
    con.execute("UPDATE catalog_items SET enabled=1, price_cents=1499 WHERE id=5"); con.commit()
    manifest, blockers = build(db, ACCOUNT, '*')
    check(blockers == [] and manifest['fan_id'] == '*', 'all-fans manifest')
    check(manifest['price_range_cents'] == [300, 1499], 'range spans the exact approved prices')
    check(verify(db, manifest, '*').item_for(5)['price_cents'] == 1499, 'second item keeps its price')

    # Every unsellable enabled row is a blocker (the runner would refuse it unlisted).
    for id_, kw, reason in ((20, dict(media=[1001], previews=[], price=10001), 'price-out-of-range'),
                            (21, dict(media=[1001], previews=[], price=299), 'price-out-of-range'),
                            (22, dict(media=[7777], previews=[], price=500), 'media-not-in-vault'),
                            (23, dict(media=[1004], previews=[], price=500), 'media-not-in-vault'),
                            (24, dict(media=[1001], previews=[1002], price=500), 'invalid-media'),
                            (25, dict(media=[], previews=[], price=500), 'invalid-media'),
                            (26, dict(media=[1001], previews=[], price=500, teaser=1), 'not-fixed-paid'),
                            (27, dict(media=[1001], previews=[], price=500, mode='band'), 'not-fixed-paid')):
        row(con, id_, kw.pop('media'), kw.pop('previews'), kw.pop('price'), **kw)
        manifest, blockers = build(db, ACCOUNT, FAN)
        check(manifest is None and blockers == [{'catalog_id': id_, 'reason': reason}], f'blocker {reason}')
        con.execute("DELETE FROM catalog_items WHERE id=?", (id_,)); con.commit()

    for account, fan in (('nope', FAN), (ACCOUNT, 0), (ACCOUNT, True), (ACCOUNT, 'all')):
        try:
            build(db, account, fan)
        except Refused as exc:
            check(exc.reason == 'manifest-scope', 'bad scope refused')
        else:
            raise AssertionError('bad scope accepted')

    out = Path(tmp) / 'out.json'
    check(main(['--db', db, '--account', ACCOUNT, '--fan', str(FAN), '--out', str(out)]) == 0, 'cli writes')
    check(json.loads(out.read_text())['account_id'] == ACCOUNT, 'cli output')
    row(con, 30, [7777], [], 500)
    check(main(['--db', db, '--account', ACCOUNT, '--all-fans', '--out', str(Path(tmp) / 'x.json')]) == 2
          and not (Path(tmp) / 'x.json').exists(), 'cli refuses with blockers and writes nothing')
    con.close()

print(f"native pilot manifest: {checks} checks; all cases passed")

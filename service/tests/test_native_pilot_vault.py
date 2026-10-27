"""FASTT stock vault mode (``--vault-mode``): the send boundary, config keys, defaults. Synthetic data only."""
import asyncio, json, logging, os, socket, sqlite3, sys, tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ACCOUNT, OTHER, FAN = 'of-900001', 'of-900009', 900002
COUNT = 0


def check(condition, label):
    global COUNT
    assert condition, label
    COUNT += 1


def refuses(code, fn):
    from native_pilot.ledger import Refused
    try:
        fn()
    except Refused as exc:
        check(exc.reason == code, (code, exc.reason))
    else:
        raise AssertionError('accepted, expected ' + code)


def shape(**over):
    base = {'price': 12.5, 'locked_text': False, 'media_files': [2001, 2002, 2003]}
    base.update(over)
    return base


def vault_db(path):
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE vault_items(account_id TEXT,media_id INTEGER,kind TEXT,removed_at TEXT)')
        con.execute('CREATE TABLE catalog_items(id INTEGER,account_id TEXT,tags TEXT,enabled INTEGER)')
        con.execute('CREATE TABLE account_ai_config(account_id TEXT,ai_chatter_config_json TEXT)')
        con.executemany('INSERT INTO vault_items VALUES(?,?,?,?)', [
            (ACCOUNT, 2001, 'photo', None), (ACCOUNT, 2002, 'video', None), (ACCOUNT, 2003, 'photo', None),
            (ACCOUNT, 2004, 'photo', '2026-01-01'), (ACCOUNT, 2005, 'audio', None), (OTHER, 2006, 'photo', None)])
        con.executemany('INSERT INTO catalog_items VALUES(?,?,?,?)', [
            (1, ACCOUNT, '["rung:vault-ask"]', 0), (2, ACCOUNT, '["hook_upsell"]', 0),
            (3, ACCOUNT, '["rung:feet-nude"]', 0), (4, OTHER, '["rung:vault-ask"]', 0), (5, ACCOUNT, '[]', 1)])


def rows(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def boundary(tmp):
    from native_pilot.content import VaultPlan, ContentPlan
    db = Path(tmp) / 'vault.db'
    vault_db(db)
    plan = VaultPlan(ACCOUNT, FAN, str(db))
    check(plan.vault_mode is True and plan.enables_catalog is False, 'vault plan flags')
    check(ContentPlan.vault_mode is False and ContentPlan.enables_catalog is True, 'catalogue plan flags unchanged')
    check(plan.terms(None) is None and plan.terms({}) is None, 'plain text has no terms')
    lo, hi = plan.bounds()
    check((lo, hi) == (300, 20000), 'FASTT own floor and ceiling')
    # accepted: a vault card at a valid price, with and without previews (FASTT's ask lane sends none)
    t = plan.terms(shape())
    check(t['price_cents'] == 1250 and t['media_ids'] == [2001, 2002, 2003] and t['preview_ids'] == [], 'valid card terms')
    check(plan.terms(shape(price=3))['price_cents'] == 300 and plan.terms(shape(price=200))['price_cents'] == 20000, 'bounds inclusive')
    check(plan.terms(shape(previews=[2001]))['preview_ids'] == [2001], 'previews subset of media accepted')
    # refused shapes, each before any provider call
    for code, bad in {
        'content-send-fields': [shape(is_forward=True)],
        'content-send-media': [shape(media_files=[]), shape(media_files=None), shape(media_files=['2001']), shape(media_files=[2001, 2001]),
                               shape(media_files=[0])],
        'content-send-price': [shape(price=2.99), shape(price=0), shape(price=200.01), shape(price=True), shape(price='12'), shape(price=12.005)],
        'content-send-lock': [shape(locked_text=True), shape(locked_text=1)],
        'content-send-previews': [shape(previews=[9999]), shape(previews=[2001, 2001]), shape(previews='2001')],
    }.items():
        for variant in bad:
            refuses(code, lambda: plan.terms(variant))
    # per-send store check: media must be live vault rows of THIS account
    con = rows(db)
    checked = plan.check_send(con, ACCOUNT, t)
    check(checked['media_types'] == {'2001': 'photo', '2002': 'video', '2003': 'photo'}, 'kinds come from vault_items')
    for ids in ([2001, 2999], [2001, 2004], [2001, 2006], [2001, 2005]):   # unknown, removed, other account, audio
        refuses('content-send-media', lambda: plan.check_send(con, ACCOUNT, plan.terms(shape(media_files=ids))))
    refuses('content-send-media', lambda: plan.check_send(con, OTHER, t))
    check(plan.check_catalog(con, ACCOUNT) is None, 'no manifest to drift')
    con.close()
    # provider receipt must match what was sent
    valid = {'id': 7, 'price': 12.5, 'mediaCount': 3,
             'media': [{'id': 2001, 'type': 'photo'}, {'id': 2002, 'type': 'video'}, {'id': 2003, 'type': 'photo'}]}
    ev = plan.evidence(valid, checked)
    check(ev['price_cents'] == 1250 and ev['media_ids'] == [2001, 2002, 2003] and ev['previews_confirmed'] is False, 'receipt accepted')
    for code, change in [('content-receipt-price', {'price': 3}),
                         ('content-receipt-media', {'media': valid['media'][:2], 'mediaCount': 2}),
                         ('content-receipt-media', {'media': [{'id': 2001, 'type': 'video'}] + valid['media'][1:]}),
                         ('content-receipt-media', {'lockedText': True}),
                         ('content-receipt-previews', {'previews': [2001]})]:
        refuses(code, lambda: plan.evidence({**valid, **change}, checked))
    # FASTT's own lane rows plus approved (enabled) catalogue items, this account only, may have an
    # open PPV; a disabled non-lane row (3) and another account's row (4) are not exempt
    check(plan.catalog_ids == frozenset({1, 2, 5}), ('lane ids', plan.catalog_ids))
    check(VaultPlan(ACCOUNT, FAN).catalog_ids == frozenset(), 'no store, no exemption')
    refuses('content-plan-scope', lambda: VaultPlan('abc', FAN))
    # catalogue mode is unchanged: a manifest plan still refuses a non-manifest card and a vault-style price
    manifest = Path(tmp) / 'm.json'
    manifest.write_text(json.dumps({'account_id': ACCOUNT, 'fan_id': FAN, 'items': [
        {'catalog_id': 1, 'kind': 'video', 'price_cents': 300, 'media': [{'id': 2001, 'type': 'photo'}, {'id': 2002, 'type': 'video'}],
         'preview_ids': [2001]}]}))
    cat = ContentPlan(manifest, ACCOUNT, FAN)
    refuses('content-send-media', lambda: cat.terms(shape()))
    refuses('content-send-price', lambda: cat.terms({'price': 12.5, 'locked_text': False, 'media_files': [2001, 2002], 'previews': [2001]}))
    check(cat.terms({'price': 3, 'locked_text': False, 'media_files': [2001, 2002], 'previews': [2001]})['catalog_id'] == 1, 'manifest card still accepted')
    return db


def attribution(tmp):
    from native_pilot import runner
    from native_pilot.content import VaultPlan, ContentPlan
    from native_pilot.ledger import Refused
    db = Path(tmp) / 'attr.db'
    with sqlite3.connect(db) as con:
        con.execute('CREATE TABLE messages(account_id TEXT,fan_id INTEGER,message_id INTEGER,direction TEXT,automation_kind TEXT,price_cents INTEGER,media_count INTEGER)')
        con.execute('CREATE TABLE native_pilot_receipts(attempt_id INTEGER,response_json TEXT)')
        con.execute("INSERT INTO messages VALUES(?,?,7,'out','pack_send',1250,3)", (ACCOUNT, FAN))
        con.execute('INSERT INTO native_pilot_receipts VALUES(1,?)', (json.dumps({'price': 12.5}),))
    receipts = [{'state': 'sent', 'provider_message_id': '7', 'attempt_id': 1}]
    runner.verify_attribution(str(db), ACCOUNT, FAN, receipts, content=VaultPlan(ACCOUNT, FAN))
    manifest = Path(tmp) / 'a.json'
    manifest.write_text(json.dumps({'account_id': ACCOUNT, 'fan_id': FAN, 'price_range_cents': [300, 2000], 'items': [
        {'catalog_id': 1, 'kind': 'photo', 'price_cents': 1250, 'media': [{'id': 2001, 'type': 'photo'}], 'preview_ids': []}]}))
    try:
        runner.verify_attribution(str(db), ACCOUNT, FAN, receipts, content=ContentPlan(manifest, ACCOUNT, FAN))
    except Refused as exc:
        check(exc.reason == 'native-attribution-missing', 'catalogue mode still refuses a pack_send row')
    else:
        raise AssertionError('catalogue accepted pack_send')


async def config():
    from native_pilot import runner, service
    from native_pilot.content import VaultPlan, ContentPlan
    from native_pilot.engine import NativeEngine
    # default off, everywhere
    parser = service.build_parser()
    check(parser.parse_args(['--account', ACCOUNT, '--fan', str(FAN), '--handle', 'x']).vault_mode is False, 'service flag default off')
    check(parser.parse_args(['--account', ACCOUNT, '--all-fans', '--handle', 'x', '--vault-mode']).vault_mode is True, 'service flag parses')
    stock = {'pack_send_enabled': True, 'pack_on_ask_enabled': True, 'prompt_sell_catalog': False,
             '_native_on_request_sales_enabled': True}
    def engine(content):
        return NativeEngine(ACCOUNT, FAN, chat=None, send=None, content=content)
    text = await engine(None).load_config(ACCOUNT, dict(stock))
    check(text['pack_send_enabled'] is False and text['pack_on_ask_enabled'] is False, 'text-only: ask lane off')
    cat_content = ContentPlan.__new__(ContentPlan)
    cat_content.items = []
    cat = await engine(cat_content).load_config(ACCOUNT, dict(stock))
    check(cat['pack_send_enabled'] is False and cat['prompt_sell_catalog'] is True, 'catalogue mode unchanged')
    vault = await engine(VaultPlan(ACCOUNT, FAN)).load_config(ACCOUNT, dict(stock, prompt_sell_catalog=True))
    check(vault['pack_send_enabled'] is True and vault['pack_on_ask_enabled'] is True and vault['prompt_sell_catalog'] is False, 'vault keys')
    check(vault['pack_on_ask_enabled'] and vault['gift_enabled'] is False and vault['pack_on_ask_enabled'] and vault['post_buy_rung_enabled'] is False
          and vault['nudge_enabled'] is False and vault['cadence_enabled'] is False, 'other media writers stay off')
    for supplied in ({}, dict(stock, _native_on_request_sales_enabled=False)):
        off = await engine(VaultPlan(ACCOUNT, FAN)).load_config(ACCOUNT, supplied)
        check(off['pack_send_enabled'] is False and off['pack_on_ask_enabled'] is False and off['enabled'] is True,
              'sales off leaves ordinary replies enabled')
    check(vault['qualification_gate_enabled'] is False, 'sales permission never arms the legacy ladder')


def supervisor(tmp):
    from native_pilot.supervisor import vault_mode_enabled
    db = Path(tmp) / 'sup.db'
    vault_db(db)
    check(vault_mode_enabled(str(db), ACCOUNT) is False, 'no config row: off')
    with sqlite3.connect(db) as con:
        con.execute('INSERT INTO account_ai_config VALUES(?,?)', (ACCOUNT, json.dumps({'fastt_vault_mode': 'true'})))
        con.execute('INSERT INTO account_ai_config VALUES(?,?)', (OTHER, json.dumps({'fastt_vault_mode': True})))
    check(vault_mode_enabled(str(db), ACCOUNT) is False, 'only boolean true enables')
    check(vault_mode_enabled(str(db), OTHER) is True, 'explicit true enables that account only')


def main():
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix='native-vault-fixture-') as tmp:
        with patch.dict(os.environ, {'DATABASE_URL': 'sqlite+aiosqlite:///' + (Path(tmp) / 'fixture.db').as_posix(), 'PYTHON_DOTENV_DISABLED': '1'}, clear=True):
            def deny(*a, **kw): raise AssertionError('network forbidden')
            with patch.object(socket.socket, 'connect', deny), patch.object(socket.socket, 'connect_ex', deny), patch.object(socket, 'create_connection', deny):
                boundary(tmp)
                attribution(tmp)
                supervisor(tmp)
                asyncio.run(config())
    print(json.dumps({'native_vault_mode_checks': COUNT}))
if __name__ == '__main__': main()

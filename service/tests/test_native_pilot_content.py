"""Run the real native catalogue path with synthetic data and blocked sockets.

The pilot may sell exactly ONE reviewed card: fixed $3, two media, one free
preview. The account is Twin-keyed (``of-<owner>``) so the real FASTT engine is
exercised on the DB-side id while the provider side stays decimal.
"""
import asyncio, json, logging, os, socket, sqlite3, sys, tempfile
from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ACCOUNT, FAN = 'of-900001', 900002
MEDIA, PREVIEWS = [1001, 1002], [1001]
REAL_SHAPE = {'price': 3.0, 'locked_text': False, 'media_files': MEDIA, 'previews': PREVIEWS}


def manifest_spec(**item_over):
    item = {'catalog_id': 1, 'kind': 'video', 'price_cents': 300,
            'media': [{'id': 1001, 'type': 'photo'}, {'id': 1002, 'type': 'video'}], 'preview_ids': PREVIEWS}
    item.update(item_over)
    return {'account_id': ACCOUNT, 'fan_id': FAN, 'items': [item]}


def refuses(code, fn):
    from native_pilot.ledger import Refused
    try:
        fn()
    except Refused as exc:
        assert exc.reason == code, (code, exc.reason)
    else:
        raise AssertionError('accepted, expected ' + code)


def plan_for(tmp, spec, name='m.json', account=ACCOUNT):
    from native_pilot.content import ContentPlan
    path = Path(tmp) / name
    path.write_text(json.dumps(spec))
    return ContentPlan(path, account, FAN)


def plan_checks(tmp, db_path):
    from native_pilot.content import ContentPlan
    from native_pilot.ledger import Refused
    plan = plan_for(tmp, manifest_spec())
    # the exact real shape is accepted and returns the frozen terms
    terms = plan.terms(dict(REAL_SHAPE))
    assert terms['price_cents'] == 300 and terms['media_ids'] == MEDIA and terms['preview_ids'] == PREVIEWS
    assert plan.terms({}) is None and plan.terms(None) is None
    # int price 3 and a bare decimal account in the manifest are the same item
    assert plan.terms({**REAL_SHAPE, 'price': 3})['price_cents'] == 300
    plan_for(tmp, {**manifest_spec(), 'account_id': '900001'}, 'bare.json', account='900001')
    bad_send = {
        'content-send-fields': [{**REAL_SHAPE, 'is_forward': True}, {**REAL_SHAPE, 'tagged_users': [1]}],
        'content-send-media': [{**REAL_SHAPE, 'media_files': [1001]}, {**REAL_SHAPE, 'media_files': [1002, 1001]},
                               {**REAL_SHAPE, 'media_files': MEDIA + [1003]}, {**REAL_SHAPE, 'media_files': [1003, 1002]},
                               {**REAL_SHAPE, 'media_files': ['1001', 1002]}, {**REAL_SHAPE, 'media_files': None}],
        'content-send-price': [{**REAL_SHAPE, 'price': 4}, {**REAL_SHAPE, 'price': 0}, {**REAL_SHAPE, 'price': True},
                               {**REAL_SHAPE, 'price': 2.99}, {**REAL_SHAPE, 'price': '3'}],
        'content-send-lock': [{**REAL_SHAPE, 'locked_text': True}, {**REAL_SHAPE, 'locked_text': 0}],
        'content-send-previews': [{**REAL_SHAPE, 'previews': []}, {**REAL_SHAPE, 'previews': [1002]},
                                  {**REAL_SHAPE, 'previews': MEDIA}, {**REAL_SHAPE, 'previews': [1001, 1001]},
                                  {k: v for k, v in REAL_SHAPE.items() if k != 'previews'}],
    }
    for code, variants in bad_send.items():
        for variant in variants:
            refuses(code, lambda: plan.terms(variant))
    # the manifest itself may name exactly one $3 item
    free = {'catalog_id': 2, 'kind': 'photo', 'price_cents': 0, 'media': [{'id': 1003, 'type': 'photo'}], 'preview_ids': []}
    paid = manifest_spec()['items'][0]
    bad_manifest = {
        'content-plan-shape': [{**manifest_spec(), 'items': []}, {**manifest_spec(), 'items': [paid, dict(paid)]}, {**manifest_spec(), 'items': paid}],
        'content-plan-scope': [{**manifest_spec(), 'fan_id': FAN + 1}, {**manifest_spec(), 'account_id': 'of-900009'},
                               {**manifest_spec(), 'account_id': 'abc'}],
        'content-plan-terms': [{**manifest_spec(), 'items': [paid, free]}, {**manifest_spec(), 'price_range_cents': [300, 20000]}, {**manifest_spec(), 'price_range_cents': [100, 500]},
                               {**manifest_spec(), 'price_range_cents': [500, 300]}, manifest_spec(price_cents=0), manifest_spec(price_cents=500), manifest_spec(price_cents=301)],
        'content-plan-item': [manifest_spec(catalog_id=True), manifest_spec(kind='audio'), manifest_spec(extra=1),
                              manifest_spec(price_cents='300')],
        'content-plan-media': [manifest_spec(media=[]), manifest_spec(media=[{'id': 0, 'type': 'photo'}]),
                               manifest_spec(media=[{'id': 1001, 'type': 'gif'}]),
                               manifest_spec(media=[{'id': 1001}])],
        'content-plan-previews': [manifest_spec(preview_ids=[9999]), manifest_spec(preview_ids=[1001, 1001]),
                                  manifest_spec(media=[{'id': 1001, 'type': 'photo'}, {'id': 1001, 'type': 'photo'}])],
    }
    for code, spec in bad_manifest.items():
        for i, one in enumerate(spec if isinstance(spec, list) else [spec]):
            refuses(code, lambda: plan_for(tmp, one, f'bad-{code}-{i}.json'))
    # provider receipt: only the exact card is evidence
    valid = {'id': 500, 'price': 3.0, 'media': [{'id': 1002, 'type': 'video'}, {'id': 1001, 'type': 'photo'}],
             'previews': [1001], 'mediaCount': 2}
    ev = plan.evidence(valid, terms)
    assert ev['price_cents'] == 300 and ev['previews_confirmed'] is True and ev['media_ids'] == MEDIA
    assert plan.evidence({k: v for k, v in valid.items() if k != 'previews'}, terms)['previews_confirmed'] is False
    for code, change in [('content-receipt-price', {'price': 4}), ('content-receipt-media', {'media': [{'id': 1002, 'type': 'video'}]}),
                         ('content-receipt-media', {'media': [{'id': 1001, 'type': 'video'}, {'id': 1002, 'type': 'video'}]}),
                         ('content-receipt-media', {'media': [{'id': 1001, 'type': 'photo'}, {'id': 1003, 'type': 'video'}]}),
                         ('content-receipt-media', {'mediaCount': 1}), ('content-receipt-media', {'lockedText': True}),
                         ('content-receipt-media', {'isUnsent': True}), ('content-receipt-media', {'isTip': True}),
                         ('content-receipt-previews', {'previews': [1002]}), ('content-receipt-previews', {'previews': []}),
                         ('content-receipt-previews', {'previews': [1001, 1002]})]:
        refuses(code, lambda: plan.evidence({**valid, **change}, terms))
    # catalogue: the one reviewed row must match exactly; nothing else may be sellable
    def catalog(mutate=None):
        with sqlite3.connect(db_path) as con:
            con.execute('DELETE FROM catalog_items')
            con.execute("INSERT INTO catalog_items(id,account_id,label,kind,media_ids,preview_media_ids,price_cents,"
                        "is_free_teaser,enabled,pricing_mode,tags,tip_unlock_cents) VALUES(1,?,'v','video','[1001, 1002]','[1001]',300,0,0,'fixed','[]',0)", (ACCOUNT,))
            if mutate:
                con.execute(mutate)
        con = sqlite3.connect(db_path); con.row_factory = sqlite3.Row
        try:
            plan.check_catalog(con, ACCOUNT)
        finally:
            con.close()
    catalog()
    catalog("INSERT INTO catalog_items(id,account_id,label,kind,media_ids,preview_media_ids,price_cents,is_free_teaser,enabled,pricing_mode,tags,tip_unlock_cents) "
            "VALUES(2,'of-900001','other','photo','[1003]','[]',0,1,0,'dynamic','[]',0)")  # disabled extras are not sellable
    for code, sql in [('content-catalog-scope', "INSERT INTO catalog_items(id,account_id,label,kind,media_ids,preview_media_ids,price_cents,is_free_teaser,enabled,pricing_mode,tags,tip_unlock_cents) "
                                                  "VALUES(2,'of-900001','other','photo','[1003]','[]',0,1,1,'dynamic','[]',0)"),
                      ('content-catalog-scope', "DELETE FROM catalog_items WHERE id=1"),
                      ('content-catalog-drift', "UPDATE catalog_items SET price_cents=500"),
                      ('content-catalog-drift', "UPDATE catalog_items SET preview_media_ids='[]'"),
                      ('content-catalog-drift', "UPDATE catalog_items SET media_ids='[1002, 1001]'"),
                      ('content-catalog-drift', "UPDATE catalog_items SET media_ids='[1001]'"),
                      ('content-catalog-drift', "UPDATE catalog_items SET pricing_mode='dynamic'"),
                      ('content-catalog-drift', "UPDATE catalog_items SET is_free_teaser=1"),
                      ('content-catalog-drift', "UPDATE catalog_items SET kind='photo'")]:
        refuses(code, lambda: catalog(sql))
    return plan


async def exercise(tmp):
    from db.engine import engine, get_session
    from db.models import Base, Account, Fan, Employee, Message, CatalogItem, ContentOffer
    from sqlalchemy import delete, select
    from native_pilot.engine import NativeEngine
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    db_path = Path(tmp) / 'fixture.db'
    plan = plan_checks(tmp, db_path)
    async with get_session() as s:
        s.add(Account(id=ACCOUNT, nickname='Fixture'))
        s.add(Employee(id=1, display_name='Automation', is_active=True))
        s.add_all([Fan(account_id=ACCOUNT, fan_id=FAN), Fan(account_id=ACCOUNT, fan_id=FAN + 1)])
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=200, direction='in', body='Send the three dollar video please',
                      created_at=datetime.utcnow() - timedelta(seconds=30)))
        await s.execute(delete(CatalogItem))
        s.add(CatalogItem(id=1, account_id=ACCOUNT, label='Video', kind='video',
                          description_for_ai='One video, 5 minutes, with a free teaser pic. No other content details are verified.',
                          media_ids=json.dumps(MEDIA), preview_media_ids=json.dumps(PREVIEWS), duration_sec=345,
                          price_cents=300, is_free_teaser=False, enabled=True, pricing_mode='fixed'))
    sent, calls = [], []
    async def chat(**kw):
        calls.append(kw)
        assert kw['account_id'] == ACCOUNT and kw['fan_id'] == FAN
        if kw.get('response_format'):
            return SimpleNamespace(content='{}', parsed={})
        return SimpleNamespace(content='This video is $3 to unlock.\n>>OFFER 1', parsed=None)
    def send(fid, text, kw):
        assert fid == FAN
        term = plan.terms(kw)
        sent.append((fid, text, kw))
        result = {'id': 10000 + len(sent), 'text': text, 'createdAt': datetime.utcnow().isoformat(), 'fromUser': {'id': 900001}}
        if term:
            result.update(media=[{'id': 1001, 'type': 'photo'}, {'id': 1002, 'type': 'video'}], mediaCount=2,
                          previews=[1001], price=term['price_cents'] / 100, lockedText=False)
            plan.evidence(result, term)
        return result
    with NativeEngine(ACCOUNT, FAN, chat=chat, send=send, content=plan) as native:
        a = await native.run(1)
        assert not a.get('errors'), a
    cards = [k for _, _, k in sent if k]
    assert len(cards) == 1 and cards[0] == REAL_SHAPE, ('real-card-shape', sent)
    async with get_session() as s:
        offers = (await s.execute(select(ContentOffer))).scalars().all()
        assert [(x.mode, x.status) for x in offers] == [('ppv', 'open')], [(x.mode, x.status) for x in offers]
    print(json.dumps({'native_single_card_real_shape': True, 'provider_calls': len(sent), 'model_calls': len(calls)}))
    await engine.dispose()


def main():
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix='native-content-fixture-') as tmp:
        with patch.dict(os.environ, {'DATABASE_URL': 'sqlite+aiosqlite:///' + (Path(tmp) / 'fixture.db').as_posix(), 'PYTHON_DOTENV_DISABLED': '1'}, clear=True):
            async def run():
                def deny(*a, **kw): raise AssertionError('network forbidden')
                with patch.object(socket.socket, 'connect', deny), patch.object(socket.socket, 'connect_ex', deny), patch.object(socket, 'create_connection', deny):
                    await exercise(tmp)
            asyncio.run(run())
if __name__ == '__main__': main()

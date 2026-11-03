"""Descriptions -> verified selection -> caption -> existing send path, through the real
Moxy runtime on disposable PostgreSQL. Synthetic model and transport only: no provider,
no model API, no customer send."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401  (fixture)
from test_moxy_integrated_pg import setup, run, decision, model, status
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from db.models import CatalogItem, VaultItem
from db.onlyfans_moxy_models import MoxyEvent
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from moxy_replacement.configuration import digest
from moxy_replacement import media_descriptions as md


def rebind(engine, service, settings, revision):
    """Re-approve asset facts from the CURRENT effective descriptions (what a catalogue
    re-bind does); nothing else about the products changes."""
    with Session(engine) as s:
        for p in settings['products']:
            for a in p['assets']:
                v = s.get(VaultItem, (ACCOUNT, a['id']))
                f = md.effective_description(v)
                a['facts'] = [f]
                a['evidence_ref'] = 'asset:' + digest({'media_id': v.media_id, 'content_hash': v.content_hash,
                    'description': f, 'updated_at': v.updated_at.isoformat()})
    service.configure(TENANT, ACCOUNT, expected_revision=revision, mode='on', settings=settings, actor='fixture-operator')


def edit(engine, mid, **fields):
    with Session(engine) as s, s.begin():
        v = s.get(VaultItem, (ACCOUNT, mid))
        for k, val in fields.items():
            setattr(v, k, val)


def prompt_of(m):
    return json.dumps(m.calls[0])


def offer(service, engine, key, text='Here is that set.'):
    return run(service, engine, decision('offer', text, 'first', sequence='fixture_level'), key=key)


def test_changed_description_leaves_selection_until_rebound_then_reaches_caption(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    new = 'Re-analysed: red dress on a balcony, single still.'
    edit(engine, 7101, description=new, content_hash='fixture-first-v2',
         ai_fields_json=json.dumps({'description': new, '_content_stamp': ':fixture-first-v2'}))
    # The stale approved facts can no longer select the product.
    identity, m = offer(service, engine, 'stale')
    with Session(engine) as s:
        e = s.get(MoxyEvent, (ACCOUNT, identity))
        assert e.status == 'held' and e.reason == 'real_inventory_missing_or_stale'
    rebind(engine, service, settings, 1)
    identity, m = offer(service, engine, 'after', 'The red dress one, just for you.')
    snap = json.loads(m.calls[0][1]['content'])
    first = snap['products']['first']
    assert first['assets'][0]['facts'] == [new]
    with Session(engine) as session:
        durable=json.loads(session.get(MoxyEvent,(ACCOUNT,identity)).snapshot_json)
        assert durable['inventory']['first']['item']['description_for_ai']==new
    intent = service.queue(TENANT, ACCOUNT, identity)
    transport = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'accepted'
    assert transport.calls[0].media_ids == (7101,) and transport.calls[0].body == 'The red dress one, just for you.'
    store.close()


def test_operator_correction_beats_model_description_in_prompt(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    ai = 'AI said: sunglasses and a hat'
    op = 'Operator: black lace set, taken indoors'
    edit(engine, 7101, description=op, content_hash='fixture-first-v2',   # content changed AND corrected
         ai_fields_json=json.dumps({'description': ai, '_content_stamp': ':fixture-first'}),
         operator_overrides_json=json.dumps({'description': op}), locked_fields_json='["description"]')
    with Session(engine) as s:
        v = s.get(VaultItem, (ACCOUNT, 7101))
        assert md.item_state(v) == 'described' and md.effective_description(v) == op   # correction survives content change
    rebind(engine, service, settings, 1)
    identity, m = offer(service, engine, 'op')
    p = prompt_of(m)
    assert op in p and ai not in p
    store.close()


def test_video_sampling_limits_reach_the_caption_prompt(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    note = '9 frames sampled in order from a 74s clip; audio and unsampled moments unseen'
    edit(engine, 7101, kind='video', video_description='A woman changes outfits in front of a mirror.',
         description=None, ai_fields_json=json.dumps({'_frame_source': 'storyboard', '_frames': 9,
         '_sampling_note': note, '_content_stamp': ':fixture-first'}))
    rebind(engine, service, settings, 1)
    identity, m = offer(service, engine, 'video')
    p = prompt_of(m)
    assert 'audio and unsampled moments unseen' in p and 'description_basis' in p
    assert 'never describe a different item or claim audio' in p
    store.close()


def test_multi_attachment_preview_and_price_reach_existing_send_path(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    ids = [7101, 7102]
    with Session(engine) as s, s.begin():
        item = s.get(CatalogItem, settings['catalogue_bindings']['first'])
        item.media_ids = json.dumps(ids); item.preview_media_ids = json.dumps([7101]); item.price_cents = 9000
    p0 = settings['products'][0]
    p0['price_cents'] = 9000
    p0['assets'] = [p0['assets'][0], dict(settings['products'][1]['assets'][0])]
    rebind(engine, service, settings, 1)
    identity, m = offer(service, engine, 'multi', 'Two from the same shoot.')
    intent = service.queue(TENANT, ACCOUNT, identity)
    transport = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'accepted'
    sent = transport.calls[0]
    assert sent.media_ids == (7101, 7102) and sent.preview_ids == (7101,) and sent.price_cents == 9000
    assert sent.body == 'Two from the same shoot.'          # no count/price forced into the caption
    store.close()


def test_unavailable_or_undescribed_asset_is_never_offered(database, monkeypatch):
    from moxy_replacement.runtime import account_readiness
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    edit(engine, 7101, describe_status='fetch_failed')
    edit(engine, 7102, removed_at=utcnow())
    with Session(engine) as s:
        r = account_readiness(s, ACCOUNT, settings)
    assert r['excluded_products']['first'] == 'media_description_failed:7101'
    assert r['excluded_products']['second'] == 'media_description_missing:7102'
    identity, m = offer(service, engine, 'gone')
    with Session(engine) as s:
        assert s.get(MoxyEvent, (ACCOUNT, identity)).status == 'held'
    store.close()


# ---- pure state rules (no database) ---------------------------------------------------
def item(**kw):
    base = dict(describe_model=None, describe_call_id=None, frames_sampled=None, describe_generated_at=None,
                updated_at=None, kind='photo', description=None, video_description=None, describe_status=None,
                content_hash=None, content_hash_kind=None, ai_fields_json=None, operator_overrides_json=None,
                locked_fields_json=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_state_rules_and_effective_description():
    ok = item(description='x', describe_status='described', content_hash='h', content_hash_kind='sha',
              ai_fields_json=json.dumps({'_content_stamp': 'sha:h'}))
    assert md.item_state(ok) == 'described'
    changed = item(description='x', describe_status='described', content_hash='h2', content_hash_kind='sha',
                   ai_fields_json=json.dumps({'_content_stamp': 'sha:h'}))
    assert md.item_state(changed) == 'pending'
    legacy = item(description='x', describe_status='described', content_hash='h2')   # no stamp: never re-paid
    assert md.item_state(legacy) == 'described'
    poster = item(kind='video', describe_status='poster_only')
    assert md.item_state(poster) == 'unsupported'
    # operator text on a video's `description` beats the AI `video_description`
    vid = item(kind='video', video_description='AI clip text', description='Operator clip text',
               describe_status='described', locked_fields_json='["description"]',
               operator_overrides_json=json.dumps({'description': 'Operator clip text'}))
    assert md.effective_description(vid) == 'Operator clip text' and md.operator_corrected(vid)
    # failure information is surfaced, not hidden
    failed = item(describe_status='fetch_failed', ai_fields_json=json.dumps({'_describe_failure': {'category': 'fetching', 'attempts': 2}}))
    assert md.item_state(failed) == 'failed'
    assert md._view(1, failed)['evidence']['failure']['category'] == 'fetching'

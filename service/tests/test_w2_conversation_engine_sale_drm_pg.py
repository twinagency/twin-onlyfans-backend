"""W2-conversation-engine-sale W2S-05: media its own provider metadata marks unusable is never offered.

Adapted from sweep 22fbbe79 to the head API (no prepare_commercial_offer/catalogue_select at head): every offer
and send path goes through `media_delivery.catalog_terms`, so the item must be refused there, whatever removed_at
says. OQ-3 (owner 2026-11-13): DRM media is sellable on both transports (sent by vault media id, no file URL).
Declared bypass: the readiness flag is seeded straight into vault_items.raw_json (SQL), as an import would.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from onlyfans_runtime.media_delivery import catalog_terms
from onlyfans_runtime.store import Refused
from test_engine_catalogue_pg import setup as engine_setup
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT


@pytest.mark.parametrize('raw', [
    pytest.param('{"isReady": false}', id='not_ready'), pytest.param('{"hasError": true}', id='error'),
    pytest.param('{"canView": false}', id='cannot_view'), pytest.param('{"isProcessing": true}', id='processing'),
    pytest.param('{"isHidden": true}', id='hidden'), pytest.param('{"id":', id='unreadable')])
def test_w2s05_an_unusable_paid_video_is_never_offered(database, raw):
    """Blue set's paid video (media 22) is unusable per its own provider metadata.
    Expected: no catalogue terms (every offer and send path), whatever removed_at says."""
    store, authority, engine, model, red, blue = engine_setup(database)
    with Session(database[1]) as s:
        assert catalog_terms(s, ACCOUNT, blue, lock=False)['media_ids'] == [21, 22]   # sellable before the flag
    with database[1].begin() as c:
        c.execute(text("UPDATE vault_items SET raw_json=:r WHERE account_id=:a AND media_id=22"), {'r': raw, 'a': ACCOUNT})
    for lock in (False, True):
        with Session(database[1]) as s, s.begin():
            with pytest.raises(Refused, match='catalogue media unavailable'):
                catalog_terms(s, ACCOUNT, blue, lock=lock)
    store.close()


DRM = ['{"id": 22, "type": "video", "isReady": true, "files": {"full": {"url": null}}}',
       '{"id": 22, "type": "video", "isReady": true}',
       '{"id": 22, "type": "video", "isReady": true, "drm": {"manifest": "synthetic"},'
       ' "files": {"full": {"url": "https://cdn.example/v.mp4"}}}']
# Not isReady true: sync stores the item anyway, so a FansAPI sale must refuse it (classify's rule).
FANSAPI_NOT_READY = ['{"id": 22, "type": "video", "files": {"full": {"url": "https://cdn.example/v.mp4"}}}',
                     '{"id": 22, "type": "video", "isReady": 1, "files": {"full": {"url": "https://cdn.example/v.mp4"}}}',
                     '{}']


def _seed(database, raw):
    with database[1].begin() as c:
        c.execute(text("UPDATE vault_items SET raw_json=:r WHERE account_id=:a AND media_id=22"), {'r': raw, 'a': ACCOUNT})


@pytest.mark.parametrize('raw', DRM, ids=['no_full_url', 'no_files', 'drm_marker'])
def test_w2s05_ready_drm_video_sells_on_native_and_fansapi(database, raw):
    """OQ-3: a catalogue row over a ready DRM video sells on a native account and on a FansAPI account."""
    from test_real_transport_pg import real_policy
    store, authority, engine, model, red, blue = engine_setup(database)
    _seed(database, raw)
    with Session(database[1]) as s:
        assert catalog_terms(s, ACCOUNT, blue, lock=False)['media_ids'] == [21, 22]   # native
    real_policy(database, authority)
    for lock in (False, True):
        with Session(database[1]) as s, s.begin():
            assert catalog_terms(s, ACCOUNT, blue, lock=lock)['media_ids'] == [21, 22]   # FansAPI
    store.close()


@pytest.mark.parametrize('raw', FANSAPI_NOT_READY, ids=['ready_missing', 'ready_not_bool', 'empty'])
def test_w2s05_fansapi_not_ready_video_is_never_sold_native_still_sells(database, raw):
    """A catalogue row inserted directly (as scripts_api.put_singles does, no curation gate) over a video not
    isReady true. FansAPI account: catalog_terms refuses (classify's isReady rule). Native: still sells.
    Media 21 keeps no metadata, so the FansAPI refusal is media 22's."""
    from test_real_transport_pg import real_policy
    store, authority, engine, model, red, blue = engine_setup(database)
    _seed(database, raw)
    with Session(database[1]) as s:
        assert catalog_terms(s, ACCOUNT, blue, lock=False)['media_ids'] == [21, 22]
    real_policy(database, authority)
    for lock in (False, True):
        with Session(database[1]) as s, s.begin():
            with pytest.raises(Refused, match='catalogue media unavailable'):
                catalog_terms(s, ACCOUNT, blue, lock=lock)
    store.close()


def test_w2s05_fansapi_ready_progressive_video_still_sells(database):
    """Control: a FansAPI isReady-true progressive video, and media with no metadata, sell."""
    from test_real_transport_pg import real_policy
    store, authority, engine, model, red, blue = engine_setup(database)
    with database[1].begin() as c:
        c.execute(text("UPDATE vault_items SET raw_json=:r WHERE account_id=:a AND media_id=22"),
                  {'r': '{"id": 22, "type": "video", "isReady": true,'
                        ' "files": {"full": {"url": "https://cdn.example/v.mp4"}}}', 'a': ACCOUNT})
    real_policy(database, authority)
    for lock in (False, True):
        with Session(database[1]) as s, s.begin():
            assert catalog_terms(s, ACCOUNT, blue, lock=lock)['media_ids'] == [21, 22]
    store.close()

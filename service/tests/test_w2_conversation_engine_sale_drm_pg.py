"""W2-conversation-engine-sale W2S-05: media its own provider metadata marks unusable is never offered.

Adapted from sweep 22fbbe79 to the head API (no prepare_commercial_offer/catalogue_select at head): every offer
and send path goes through `media_delivery.catalog_terms`, so the item must be refused there, whatever removed_at
says. Native DRM cases (OQ-3) live off-PR: native DRM stays offerable per the native design (owner decision queued);
FansAPI DRM is refused at curation (test_w1_fans_api_adapter_pg L3-02).
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


FANSAPI_DRM = ['{"id": 22, "type": "video", "isReady": true, "files": {"full": {"url": null}}}',
               '{"id": 22, "type": "video", "isReady": true}',
               '{"id": 22, "type": "video", "isReady": true, "drm": {"manifest": "synthetic"},'
               ' "files": {"full": {"url": "https://cdn.example/v.mp4"}}}',
               # Not isReady true: sync stores the item anyway, so the sale must refuse it (classify's rule).
               '{"id": 22, "type": "video", "files": {"full": {"url": "https://cdn.example/v.mp4"}}}',
               '{"id": 22, "type": "video", "isReady": 1, "files": {"full": {"url": "https://cdn.example/v.mp4"}}}',
               '{}']


@pytest.mark.parametrize('raw', FANSAPI_DRM, ids=['no_full_url', 'no_files', 'drm_marker',
                                                  'ready_missing', 'ready_not_bool', 'empty'])
def test_w2s05_fansapi_drm_video_is_never_sold_native_drm_still_sells(database, raw):
    """A catalogue row inserted directly (as scripts_api.put_singles does, no curation gate) over a DRM or
    not-ready video. FansAPI account: catalog_terms refuses (OQ-3, classify's isReady rule). Native account:
    the same row still sells (native design). Media 21 keeps no metadata, so the FansAPI refusal is media 22's."""
    from test_real_transport_pg import real_policy
    store, authority, engine, model, red, blue = engine_setup(database)
    with database[1].begin() as c:
        c.execute(text("UPDATE vault_items SET raw_json=:r WHERE account_id=:a AND media_id=22"), {'r': raw, 'a': ACCOUNT})
    with Session(database[1]) as s:
        assert catalog_terms(s, ACCOUNT, blue, lock=False)['media_ids'] == [21, 22]   # native: DRM offerable
    real_policy(database, authority)
    for lock in (False, True):
        with Session(database[1]) as s, s.begin():
            with pytest.raises(Refused, match='catalogue media unavailable'):
                catalog_terms(s, ACCOUNT, blue, lock=lock)
    store.close()


def test_w2s05_fansapi_ready_progressive_video_still_sells(database):
    """Control for the refusals above: a FansAPI isReady-true progressive video, and media with no metadata, sell."""
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

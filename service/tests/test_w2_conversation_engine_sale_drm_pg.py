"""W2-conversation-engine-sale red test W2S-05 (DRM). Asserts the FIXED behaviour.

OQ-3 default: DRM media is never offered. The DRM marker exists in vault raw metadata
(onlyfans_runtime/vault_video.protected_source: raw['drm'] or raw['files']['drm']), but no sale gate read it.
Adapted from sweep 22fbbe79 to the head API (no prepare_commercial_offer/catalogue_select at head): every offer
and send path goes through `media_delivery.catalog_terms`, so the DRM item must be refused there.
Declared bypass: the DRM/readiness flag is seeded straight into vault_items.raw_json (SQL), as an import would.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from onlyfans_runtime.media_delivery import catalog_terms
from onlyfans_runtime.store import Refused
from test_engine_catalogue_pg import setup as engine_setup
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT


# Native DRM offerability is a pinned, documented design (test_onlyfans_protected_vault_pg,
# test_onlyfans_vault_offer_availability) that OQ-3 contradicts: owner decision, not this PR (G06 covers FansAPI DRM).
NATIVE_DRM = pytest.mark.xfail(strict=True, reason="W2S-05 native DRM at catalog_terms: OQ-3 needs owner decision")


@pytest.mark.parametrize('raw', [
    pytest.param('{"drm": {"manifest": "synthetic"}}', id='drm', marks=NATIVE_DRM),
    pytest.param('{"files": {"drm": {"manifest": "synthetic"}}}', id='files_drm', marks=NATIVE_DRM),
    pytest.param('{"isReady": false}', id='not_ready'), pytest.param('{"hasError": true}', id='error'),
    pytest.param('{"canView": false}', id='cannot_view'), pytest.param('{"isProcessing": true}', id='processing'),
    pytest.param('{"isHidden": true}', id='hidden'), pytest.param('{"id":', id='unreadable')])
def test_w2s05_a_drm_or_unusable_paid_video_is_never_offered(database, raw):
    """Blue set's paid video (media 22) is DRM-protected or unusable per its own provider metadata.
    Expected: no catalogue terms (every offer and send path), whatever removed_at says."""
    store, authority, engine, model, red, blue = engine_setup(database)
    with Session(database[1]) as s:
        assert catalog_terms(s, ACCOUNT, blue, lock=False)['media_ids'] == [21, 22]   # sellable before DRM
    with database[1].begin() as c:
        c.execute(text("UPDATE vault_items SET raw_json=:r WHERE account_id=:a AND media_id=22"), {'r': raw, 'a': ACCOUNT})
    for lock in (False, True):
        with Session(database[1]) as s, s.begin():
            with pytest.raises(Refused, match='catalogue media unavailable'):
                catalog_terms(s, ACCOUNT, blue, lock=lock)
    store.close()

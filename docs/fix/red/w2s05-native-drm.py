"""W2S-05 native-DRM red cases (W2-conversation-engine-sale FINDINGS.md; OQ-3 "DRM never offered").

Deferred from G06 (PR branch fix/G06-vault-sync-catalogue): native DRM stays offerable per the native design,
pinned by test_onlyfans_protected_vault_pg::test_protected_video_product_and_explicit_playback_status and
test_onlyfans_vault_offer_availability (4 tests); owner decision queued. FansAPI DRM refusal is in G06 (_ready).
Red at 500a2b66 and at G06 head 4be4e46c (measured). Removed from the promoted file because premerge counts
xfail as skip. Synthetic ids only; DRM flag seeded into vault_items.raw_json (SQL), as an import would.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from onlyfans_runtime.media_delivery import catalog_terms
from onlyfans_runtime.store import Refused
from test_engine_catalogue_pg import setup as engine_setup
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT


@pytest.mark.parametrize('raw', ['{"drm": {"manifest": "synthetic"}}', '{"files": {"drm": {"manifest": "synthetic"}}}'],
                         ids=['drm', 'files_drm'])
def test_w2s05_a_drm_protected_paid_video_is_never_offered(database, raw):
    """Blue set's paid video (media 22) is DRM-protected. Expected: no catalogue terms (no offer, no send)."""
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

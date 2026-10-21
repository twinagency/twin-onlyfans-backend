"""Protected vault identities remain usable without pretending playback works."""
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_vault_pg import sync, media, create
from test_onlyfans_vault_media_pg import storage, api
from test_onlyfans_connections_pg import TENANT, ACTOR
from db.onlyfans_media_models import OfVaultMediaJob
from onlyfans_runtime.vault_management import list_vault
from onlyfans_runtime.vault_video import ProtectedVideoUnavailable, request_video, video_status


def test_protected_video_product_and_explicit_playback_status(database, monkeypatch):
    row = media(13, 'video', files={'full': {'url': None}, 'drm': {'manifest': {'dash': 'synthetic'}}})
    store, account, _, _ = sync(database, monkeypatch, [row])
    assert list_vault(store, TENANT, account)['items'][0]['ready'] is True
    product = create(store, account, mediaIds=['13'], previewMediaIds=[])
    assert product['item']['kind'] == 'video'
    with pytest.raises(ProtectedVideoUnavailable):
        request_video(store, TENANT, account, '13', command_key='e'*64, actor=ACTOR)
    assert video_status(store, TENANT, account, '13') == {
        'mediaId': '13', 'ready': False, 'status': 'unprepared', 'bytes': None,
        'lastError': 'protected_playback_not_supported'}
    client, headers = api(store, monkeypatch)
    url = f'/onlyfans/content/v1/accounts/{account}/vault/videos/13'
    result = client.post(url, headers=headers, json={'commandKey': 'e'*64, 'actor': ACTOR})
    assert result.status_code == 200, result.text
    assert result.json()['video']['lastError'] == 'protected_playback_not_supported'
    assert client.get(url + '/stream', headers=headers).status_code == 404
    with Session(store.engine) as session:
        assert session.scalar(select(func.count()).select_from(OfVaultMediaJob)) == 0
    store.close()

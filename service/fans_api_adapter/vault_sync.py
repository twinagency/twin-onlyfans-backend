"""Vault sync: Fans API vault -> canonical vault_items records (and a catalogue helper).

Reading the vault sends nothing. Items that are not ready, cannot be viewed, carry an
error, or are DRM videos (files.full.url is null) are never made sellable; if one was
sellable before, it is marked removed so catalogue terms refuse it. A pass that reaches
hasMore=false also marks items it did not see as removed.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CatalogItem, VaultItem
from onlyfans_runtime.store import Refused, utcnow

SELLABLE_KINDS = {'photo', 'video'}


def _url(files, name):
    node = files.get(name) if type(files) is dict else None
    url = node.get('url') if type(node) is dict else None
    return url if type(url) is str and url.startswith('https://') and len(url) <= 8192 else None


def classify(item):
    """Return (row dict, None) for a sellable item or (None, reason) for a skipped one."""
    try:
        media_id = int(item['id'])
    except (KeyError, TypeError, ValueError):
        return None, 'bad_id'
    if not 0 < media_id < 2**63:
        return None, 'bad_id'
    kind = item.get('type')
    files = item.get('files')
    if item.get('isReady') is not True:
        return None, 'not_ready'
    if item.get('canView') is False:
        return None, 'cannot_view'
    if item.get('hasError'):
        return None, 'error'
    if kind == 'video' and _url(files, 'full') is None:
        return None, 'drm'
    if kind not in SELLABLE_KINDS:
        return None, 'unsupported_kind'
    lists = sorted(int(s['id']) for s in item.get('listStates') or []
                   if type(s) is dict and s.get('type') == 'custom' and s.get('hasMedia') is True
                   and str(s.get('id', '')).isdecimal())
    # Metadata identity, not a byte hash: honest kind so nothing mistakes it for content proof.
    identity = {'id': media_id, 'type': kind, 'createdAt': item.get('createdAt'), 'duration': item.get('duration') or 0}
    return {'media_id': media_id, 'kind': kind, 'thumb_url': _url(files, 'thumb'), 'full_url': _url(files, 'full'),
            'duration_seconds': int(item.get('duration') or 0) or None, 'folders': lists,
            'content_hash': sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
            'content_hash_kind': 'vault-metadata-v1'}, None


def sync_vault(store, tenant, account, client, *, page_size=40, max_pages=200):
    """One pass. Returns counts; resumable (idempotent upserts)."""
    seen, skipped, upserted, pages, offset, complete = set(), {}, 0, 0, 0, False
    now = utcnow()
    while pages < max_pages:
        result = client.vault(limit=page_size, offset=offset)
        if result.state != 'observed':
            return {'complete': False, 'pages': pages, 'upserted': upserted, 'skipped': skipped, 'error': result.http_status}
        rows = []
        for item in result.data:
            row, reason = classify(item)
            if row is None:
                skipped[reason] = skipped.get(reason, 0) + 1
                try:
                    seen.add(('skip', int(item['id'])))
                except (KeyError, TypeError, ValueError):
                    pass
            else:
                rows.append(row)
        with Session(store.engine) as s, s.begin():
            store._runtime(s, tenant, account)
            for r in rows:
                seen.add(('ok', r['media_id']))
                v = s.get(VaultItem, (account, r['media_id']))
                if v is None:
                    v = VaultItem(account_id=account, media_id=r['media_id'], kind=r['kind'], created_at=now)
                    s.add(v)
                v.kind, v.thumb_url, v.full_url = r['kind'], r['thumb_url'], r['full_url']
                v.duration_seconds = r['duration_seconds']
                v.of_folder_ids = json.dumps(r['folders'])
                v.removed_at, v.updated_at = None, now
                if v.content_hash_kind != r['content_hash_kind'] or not v.content_hash:
                    v.content_hash, v.content_hash_kind = r['content_hash'], r['content_hash_kind']
            # skipped items that exist locally are no longer sellable
            skipped_ids = [i for tag, i in seen if tag == 'skip']
            if skipped_ids:
                for v in s.scalars(select(VaultItem).where(VaultItem.account_id == account,
                                                           VaultItem.media_id.in_(skipped_ids))):
                    v.removed_at = v.removed_at or now
        upserted += len(rows)
        pages += 1
        offset += len(result.data)
        if result.next_page is None or not result.data:
            complete = True
            break
    if complete:
        ok_ids = {i for tag, i in seen if tag == 'ok'}
        with Session(store.engine) as s, s.begin():
            for v in s.scalars(select(VaultItem).where(VaultItem.account_id == account, VaultItem.removed_at.is_(None))):
                if v.media_id not in ok_ids:
                    v.removed_at = now
    return {'complete': complete, 'pages': pages, 'upserted': upserted, 'skipped': skipped, 'error': None}


def sync_vault_lists(client):
    """Custom list names/ids and counts (read-only; membership is carried per item)."""
    result = client.vault_lists()
    if result.state != 'observed':
        return None
    return [{'id': row.get('id'), 'type': row.get('type'), 'name': row.get('name'),
             'photos': row.get('photosCount'), 'videos': row.get('videosCount')} for row in result.data]


def create_catalogue_item(store, tenant, account, media_ids, *, price_cents, label, description_for_ai,
                          preview_ids=(), kind=None):
    """Operator-approved catalogue entry over synced, still-sellable vault media."""
    media_ids = [int(v) for v in media_ids]
    if not media_ids or len(set(media_ids)) != len(media_ids) or not 300 <= price_cents <= 20000:
        raise Refused('invalid catalogue item')
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant, account)
        rows = s.scalars(select(VaultItem).where(VaultItem.account_id == account, VaultItem.media_id.in_(media_ids))).all()
        if len(rows) != len(media_ids) or any(r.removed_at is not None for r in rows):
            raise Refused('media is not a synced sellable vault item')
        item = CatalogItem(account_id=account, kind=kind or ('video' if any(r.kind == 'video' for r in rows) else 'image'),
                           label=label, description_for_ai=description_for_ai, media_ids=json.dumps(media_ids),
                           preview_media_ids=json.dumps([int(v) for v in preview_ids]), price_cents=price_cents,
                           pricing_mode='fixed', enabled=True)
        s.add(item)
        s.flush()
        return item.id

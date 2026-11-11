"""Shared builders for the commercial catalogue-terms suites (Step 3).

Terms rows are written through the production writer (`insert_revision`) or, for scale, by one set-based INSERT that
fills exactly the columns that writer fills. Nothing here grants a sale, an offer or an ownership fact.
"""
from datetime import timedelta
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from db.models import CatalogItem, VaultItem
from onlyfans_runtime.store import utcnow
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT  # noqa: F401

ACTOR = 'op-session:' + 'a' * 64


def runtime_of(store, tenant=TENANT, account=ACCOUNT):
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        return runtime.binding_id


def add_vault(engine, media, *, account=ACCOUNT, hash_kind='synthetic'):
    """media: {media_id: kind}. Real-looking vault rows; `synthetic` hash kind matches the synthetic harness."""
    with Session(engine) as s, s.begin():
        for mid, kind in media.items():
            if s.get(VaultItem, (account, mid)) is None:
                s.add(VaultItem(account_id=account, media_id=mid, kind=kind, created_at=utcnow(),
                                content_hash=f'h-{mid}', content_hash_kind=hash_kind))


def add_item(engine, *, label, media, previews=(), price=1200, description=None, tags='[]', enabled=True,
             account=ACCOUNT, mode='fixed'):
    with Session(engine) as s, s.begin():
        item = CatalogItem(account_id=account, kind='image', label=label,
                           description_for_ai=description or f'{label}, approved description.',
                           media_ids=json.dumps(list(media)), preview_media_ids=json.dumps(list(previews)),
                           price_cents=price, enabled=enabled, pricing_mode=mode, tags=tags)
        s.add(item)
        s.flush()
        return item.id


def approve(store, item_id, *, standard=1200, discount=None, tenant=TENANT, account=ACCOUNT, folder=7, folder_revision=1,
            source=None, revision_of=None):
    """Write an approved revision with the production writer (no agency audit chain; those are tested elsewhere)."""
    from onlyfans_runtime.catalogue_terms import insert_revision
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        item = s.get(CatalogItem, item_id)
        item.price_cents, item.pricing_mode = standard, 'fixed'
        bundle = len(json.loads(item.media_ids)) - len(json.loads(item.preview_media_ids)) > 1
        row = insert_revision(s, runtime, item, standard_cents=standard, discount_cents=discount,
            price_source=source or ('agency_bundle_total' if bundle else 'agency_category'),
            category_folder_id=None if bundle else folder, category_price_revision=None if bundle else folder_revision,
            actor=ACTOR, command_key=f'{item_id:08x}{(row_revision(s, item_id) + 1):08x}'.ljust(64, '0'),
            request_hash='f' * 64)
        return row.revision


def row_revision(s, item_id):
    return s.scalar(text('SELECT COALESCE(MAX(revision),0) FROM of_catalogue_terms WHERE item_id=:i'), {'i': item_id})


def bulk_commercial(engine, *, count, binding_id, account=ACCOUNT, first_media=100000, special=None, hold_at=None, with_terms=True):
    """`count` enabled fixed single-photo products with live approved terms, inserted set-based.

    `special` = {index: label} renames chosen products; `hold_at` = set of indexes whose media are marked removed.
    Returns the list of item ids in index order.
    """
    removed = set(hold_at or ())
    with engine.begin() as c:
        c.execute(text("""INSERT INTO vault_items(account_id,media_id,kind,created_at,content_hash,content_hash_kind,tags,send_count)
            SELECT :a, :m + g, 'image', now(), 'h-'||(:m+g), 'synthetic', '[]', 0 FROM generate_series(0,:n-1) g"""),
            {'a': account, 'm': first_media, 'n': count})
        if removed:
            c.execute(text('UPDATE vault_items SET removed_at=now() WHERE account_id=:a AND media_id = ANY(:ids)'),
                      {'a': account, 'ids': [first_media + i for i in removed]})
        c.execute(text("""INSERT INTO catalog_items(account_id,kind,label,description_for_ai,media_ids,preview_media_ids,price_cents,
                tip_unlock_cents,is_free_teaser,tags,enabled,pricing_mode,created_at,updated_at)
            SELECT :a,'image','Studio set '||g,'Studio photograph number '||g||'.','['||(:m+g)||']','[]',1200,0,false,'[]',true,'fixed',now(),now()
            FROM generate_series(0,:n-1) g"""), {'a': account, 'm': first_media, 'n': count})
        ids = [r[0] for r in c.execute(text("""SELECT id FROM catalog_items WHERE account_id=:a AND
            CASE WHEN media_ids ~ '^\\[[0-9]+\\]$' THEN substring(media_ids from 2 for length(media_ids)-2)::bigint END
            BETWEEN :lo AND :hi ORDER BY substring(media_ids from 2 for length(media_ids)-2)::bigint"""),
            {'a': account, 'lo': first_media, 'hi': first_media + count - 1})]
        for index, label in (special or {}).items():
            c.execute(text('UPDATE catalog_items SET label=:l, description_for_ai=:d WHERE id=:i'),
                      {'l': label, 'd': f'{label}, a distinctive approved photograph.', 'i': ids[index]})
        if not with_terms:
            return ids
        c.execute(text("""INSERT INTO of_catalogue_terms(account_id,item_id,revision,status,product_kind,standard_price_cents,
                price_source,category_folder_id,category_price_revision,media_count,paid_photo_count,paid_video_count,
                preview_photo_count,preview_video_count,approved_by,approved_at,binding_id,command_key,request_hash,snapshot_json,snapshot_sha256)
            SELECT :a,id,1,'approved','single_photo',1200,'agency_category',7,1,1,1,0,0,0,:actor,now(),:b,
                   'bulk'||id,repeat('e',64),'{}',repeat('d',64) FROM unnest(:ids) AS id"""),
            {'a': account, 'actor': ACTOR, 'b': binding_id, 'ids': ids})
        c.execute(text("""INSERT INTO of_catalogue_terms_member(account_id,item_id,revision,media_id,role,kind)
            SELECT :a, id, 1, :m + (ord-1), 'paid', 'photo' FROM unnest(:ids) WITH ORDINALITY AS t(id, ord)"""),
            {'a': account, 'm': first_media, 'ids': ids})
    return ids


def valid_for():
    return utcnow() + timedelta(minutes=5)

"""Approved commercial terms for catalogue items: the one writer and the one read model (Step 3).

CatalogItem stays the only catalogue owner; this module adds immutable, revisioned terms beside it. A price here is
always an agency's manual decision: a single costs its category's single price (revision-checked), a bundle costs the
total the agency approved for exactly that bundle, and an optional discount is an amount the agency typed below the
standard. No history, model or arithmetic price can enter: the writer has no parameter that could carry one.

`media_delivery.catalog_terms()` stays THE read entry point; it calls `live_terms`/`block` so that one `commercial` block
reaches the prompt, validators, claims, grant hash, quote and final-wire comparison identically.
"""
import hashlib
import json
import re

from sqlalchemy import select, update

from db.models import CatalogItem, VaultFolder, VaultItem
from db.onlyfans_catalogue_terms_models import OfCatalogueTerms, OfCatalogueTermsMember
from onlyfans_read.agency_scope import actor_allowed
from .native_context import canonical
from .store import Refused, _id, utcnow

VERSION = 'commercial-terms.v1'
FLOOR, CEILING = 300, 20000
_PHOTO = {'image', 'photo'}


def _digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _cents(value, *, low=FLOOR, high=CEILING):
    # bool is an int subclass; a float, string or percentage text is never an amount
    if type(value) is not int or not low <= value <= high:
        raise Refused('terms need a whole-cent amount between the floor and the ceiling')
    return value


def _identity(command_key, actor):
    if type(command_key) is not str or not re.fullmatch(r'[a-f0-9]{64}', command_key):
        raise Refused('the terms command identity is invalid')
    if type(actor) is str and actor.startswith('worker-import:'):
        raise Refused('agency pricing permission required')
    if not actor_allowed(actor, capability='manage'):
        raise Refused('agency pricing permission required')


def _ids(raw, *, allow_empty=False):
    try:
        values = json.loads(raw)
    except (ValueError, TypeError):
        raise Refused('invalid media identities') from None
    if (type(values) is not list or (not values and not allow_empty) or len(values) > 35
            or any(type(v) is not int or not 0 < v < 2**63 for v in values) or len(set(values)) != len(values)):
        raise Refused('invalid media identities')
    return values


def derive_members(s, account, item):
    """[(media_id, role, kind)] in media order, the product kind and the media counts for the item as it stands now."""
    ids, previews = _ids(item.media_ids), _ids(item.preview_media_ids, allow_empty=True)
    if not set(previews) < set(ids):
        raise Refused('invalid preview identities')
    kinds = dict(s.execute(select(VaultItem.media_id, VaultItem.kind).where(
        VaultItem.account_id == account, VaultItem.media_id.in_(ids))).all())
    members = []
    for mid in ids:
        raw = kinds.get(mid)
        kind = 'photo' if raw in _PHOTO else 'video' if raw == 'video' else None
        if kind is None:
            raise Refused('catalogue media unavailable')
        members.append((mid, 'preview' if mid in previews else 'paid', kind))
    pp = sum(1 for _, r, k in members if r == 'paid' and k == 'photo')
    pv = sum(1 for _, r, k in members if r == 'paid' and k == 'video')
    vp = sum(1 for _, r, k in members if r == 'preview' and k == 'photo')
    vv = sum(1 for _, r, k in members if r == 'preview' and k == 'video')
    if pp + pv == 0:
        raise Refused('a product needs at least one paid asset')
    if pp + pv == 1:
        kind = 'single_photo' if pp else 'single_video'
    else:
        kind = 'bundle_mixed' if pp and pv else 'bundle_photo' if pp else 'bundle_video'
    return members, kind, {'paid_photo_count': pp, 'paid_video_count': pv, 'preview_photo_count': vp, 'preview_video_count': vv,
                           'media_count': len(members)}


def live_terms(s, account, item_id, *, lock=False):
    query = select(OfCatalogueTerms).where(OfCatalogueTerms.account_id == account, OfCatalogueTerms.item_id == item_id,
                                           OfCatalogueTerms.status == 'approved')
    return s.scalar(query.with_for_update(read=True) if lock else query)


def latest_terms(s, account, item_id, *, lock=False):
    """The newest revision of any status. A closed (withdrawn) newest revision means the agency took the product off sale."""
    query = select(OfCatalogueTerms).where(OfCatalogueTerms.account_id == account, OfCatalogueTerms.item_id == item_id
                                           ).order_by(OfCatalogueTerms.revision.desc()).limit(1)
    return s.scalar(query.with_for_update(read=True) if lock else query)


def members_of(s, row):
    return {(m, r) for m, r in s.execute(select(OfCatalogueTermsMember.media_id, OfCatalogueTermsMember.role).where(
        OfCatalogueTermsMember.account_id == row.account_id, OfCatalogueTermsMember.item_id == row.item_id,
        OfCatalogueTermsMember.revision == row.revision)).all()}


def block(row, price):
    """The `commercial` block frozen into catalog_terms(): approved facts plus the variant this price selects."""
    variant = 'discount' if row.discount_price_cents is not None and price == row.discount_price_cents else 'standard'
    return {'v': VERSION, 'revision': row.revision, 'snapshotSha256': row.snapshot_sha256, 'kind': row.product_kind,
            'variant': variant, 'standardCents': row.standard_price_cents, 'discountCents': row.discount_price_cents,
            'priceSource': row.price_source, 'productCount': 1,
            'counts': {'media': row.media_count, 'pictures': row.paid_photo_count + row.preview_photo_count,
                       'videos': row.paid_video_count + row.preview_video_count,
                       'previews': row.preview_photo_count + row.preview_video_count}}


def commercial_facts(terms):
    """Display/validation facts of one frozen terms dict, or None for a legacy (no terms row) item. The only reader prompts,
    validators and claims use."""
    c = terms.get('commercial') if isinstance(terms, dict) else None
    if not isinstance(c, dict) or c.get('v') != VERSION:
        return None
    return {'variant': c['variant'], 'priceCents': terms['price_cents'], 'standardCents': c['standardCents'],
            'discountCents': c['discountCents'], 'discountApproved': c['variant'] == 'discount',
            'kind': c['kind'], 'isBundle': c['kind'].startswith('bundle'), 'productCount': c.get('productCount', 1),
            'counts': dict(c['counts']), 'revision': c['revision'], 'priceSource': c['priceSource']}


def _snapshot(item, kind, counts, members, standard, discount, source, folder, folder_revision):
    return {'v': VERSION, 'itemId': item.id, 'kind': kind, 'standardCents': standard, 'discountCents': discount,
            'priceSource': source, 'categoryFolderId': folder, 'categoryPriceRevision': folder_revision,
            'members': [{'mediaId': m, 'role': r, 'kind': k} for m, r, k in members], 'counts': counts}


def insert_revision(s, runtime, item, *, standard_cents, discount_cents, actor, command_key, request_hash,
                    price_source=None, category_folder_id=None, category_price_revision=None):
    """Write the next approved revision inside the caller's transaction (the only writer of the terms tables).

    An identical live revision is returned unchanged. Otherwise the live one is closed (`superseded`) and revision+1 is
    inserted with its exact members. Caller validates authority and where the price came from.
    """
    account = runtime.account_id
    if item.account_id != account or item.script_id is not None or item.is_free_teaser:
        raise Refused('approved catalogue terms unavailable')
    standard = _cents(standard_cents)
    discount = None if discount_cents is None else _cents(discount_cents, high=standard - 1)
    members, kind, counts = derive_members(s, account, item)
    single = kind.startswith('single_')
    source = 'agency_category' if single else 'agency_bundle_total'
    if price_source is not None and price_source != source:
        raise Refused('the price source does not fit the product kind')
    folder = category_folder_id if single else None
    folder_revision = category_price_revision if single else None
    if single and (type(folder) is not int or type(folder_revision) is not int or folder_revision < 1):
        raise Refused('a single product needs its agency category price revision')
    snapshot = _snapshot(item, kind, counts, members, standard, discount, source, folder, folder_revision)
    current = live_terms(s, account, item.id, lock=True)
    if current is not None:
        if (current.standard_price_cents, current.discount_price_cents, current.price_source, current.category_folder_id,
                current.category_price_revision, members_of(s, current)) == (
                standard, discount, source, folder, folder_revision, {(m, r) for m, r, _ in members}):
            return current
        s.execute(update(OfCatalogueTerms).where(OfCatalogueTerms.account_id == account, OfCatalogueTerms.item_id == item.id,
                                                  OfCatalogueTerms.revision == current.revision).values(status='superseded'))
        s.flush()
        s.expire(current)
    previous = s.scalar(select(OfCatalogueTerms.revision).where(OfCatalogueTerms.account_id == account,
        OfCatalogueTerms.item_id == item.id).order_by(OfCatalogueTerms.revision.desc()).limit(1)) or 0
    revision = previous + 1
    snapshot['revision'] = revision
    row = OfCatalogueTerms(account_id=account, item_id=item.id, revision=revision, status='approved', product_kind=kind,
        standard_price_cents=standard, discount_price_cents=discount, price_source=source, category_folder_id=folder,
        category_price_revision=folder_revision, approved_by=actor, approved_at=utcnow(), binding_id=runtime.binding_id,
        command_key=command_key, request_hash=request_hash, snapshot_json=canonical(snapshot),
        snapshot_sha256=_digest(snapshot), **counts)
    s.add(row)
    s.flush()
    s.add_all([OfCatalogueTermsMember(account_id=account, item_id=item.id, revision=revision, media_id=m, role=r, kind=k)
               for m, r, k in members])
    s.flush()
    return row


def _ack(row):
    return {'itemId': str(row.item_id), 'revision': row.revision, 'status': row.status, 'productKind': row.product_kind,
            'standardCents': row.standard_price_cents, 'discountCents': row.discount_price_cents,
            'priceSource': row.price_source, 'snapshotSha256': row.snapshot_sha256, 'commandKey': row.command_key}


def _prior_command(s, account, command_key, request_hash, actor):
    row = s.scalar(select(OfCatalogueTerms).where(OfCatalogueTerms.account_id == account,
                                                  OfCatalogueTerms.command_key == command_key))
    if row is None:
        return None
    if (row.request_hash, row.approved_by) != (request_hash, actor):
        raise Refused('terms idempotency conflict')
    return _ack(row)


def _category_audit(s, account, item_id):
    from db.onlyfans_vault_models import OfVaultProductCreate
    record = s.scalar(select(OfVaultProductCreate).where(OfVaultProductCreate.account_id == account,
                                                         OfVaultProductCreate.item_id == item_id))
    saved = json.loads(record.product_json) if record is not None else {}
    if 'agencyCategoryId' not in saved:
        raise Refused('this item was not created from an agency category; use the category draft command first')
    return saved


def approve_terms(store, tenant, account, item_id, *, standard_cents, discount_cents=None, command_key, actor, expected_revision=None, expected_fingerprint=None):
    """Agency approval of an existing category-created item at its manual price. Approval is not enabling."""
    _id(item_id)
    _identity(command_key, actor)
    _cents(standard_cents)
    if discount_cents is not None:
        _cents(discount_cents, high=standard_cents - 1)
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise Refused('invalid terms revision')
    request_hash = _digest(['approve_terms', item_id, standard_cents, discount_cents] +
                           ([] if expected_revision is None else [expected_revision,expected_fingerprint]))
    from sqlalchemy.orm import Session
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        replay = _prior_command(s, account, command_key, request_hash, actor)
        if replay is not None:
            return replay
        item = s.scalar(select(CatalogItem).where(CatalogItem.id == item_id, CatalogItem.account_id == account).with_for_update())
        if item is None:
            raise Refused('catalogue item unavailable')
        from .content_management import fingerprint
        if expected_fingerprint is not None and fingerprint(item) != expected_fingerprint:
            raise Refused('product changed since review')
        latest = latest_terms(s, account, item_id, lock=True)
        if expected_revision is not None and expected_revision != (latest.revision if latest else 0):
            raise Refused('terms revision conflict')
        saved = _category_audit(s, account, item_id)
        from .offer_import import provenance
        if provenance(s, account, item_id) is not None:
            raise Refused('a price taken from history cannot be approved as a commercial term')
        if (item.pricing_mode != 'fixed' or item.script_id is not None or item.is_free_teaser
                or type(item.description_for_ai) is not str or not item.description_for_ai.strip()):
            raise Refused('approved catalogue terms unavailable')
        members, kind, counts = derive_members(s, account, item)
        if kind.startswith('single_'):
            folder = s.scalar(select(VaultFolder).where(VaultFolder.id == int(saved['agencyCategoryId']),
                VaultFolder.account_id == account, VaultFolder.deleted_at.is_(None)).with_for_update(read=True))
            if (folder is None or folder.pricing_binding_id != runtime.binding_id or folder.single_price_cents is None
                    or folder.single_price_cents != standard_cents):
                raise Refused('a single is sold at its agency category price; the category price differs')
            folder_id, folder_revision = folder.id, folder.price_revision
        else:
            folder_id = folder_revision = None
            if saved.get('priceCents') != standard_cents:
                raise Refused('a bundle is sold at its own approved total; the approved total differs')
        if item.price_cents != standard_cents:
            raise Refused('catalogue price differs from the approved term; re-save the item at that price first')
        row = insert_revision(s, runtime, item, standard_cents=standard_cents, discount_cents=discount_cents, actor=actor,
            command_key=command_key, request_hash=request_hash, category_folder_id=folder_id,
            category_price_revision=folder_revision)
        return _ack(row)


def set_discount(store, tenant, account, item_id, *, discount_cents, command_key, actor):
    """New revision with an optional approved discount amount (never stacked on a previous one; None removes it)."""
    _id(item_id)
    _identity(command_key, actor)
    if discount_cents is not None:
        _cents(discount_cents)
    request_hash = _digest(['set_discount', item_id, discount_cents])
    from sqlalchemy.orm import Session
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        replay = _prior_command(s, account, command_key, request_hash, actor)
        if replay is not None:
            return replay
        item = s.scalar(select(CatalogItem).where(CatalogItem.id == item_id, CatalogItem.account_id == account).with_for_update())
        live = live_terms(s, account, item_id, lock=True) if item is not None else None
        if live is None:
            raise Refused('no approved terms to discount')
        if item.price_cents != live.standard_price_cents or members_of(s, live) != {
                (m, r) for m, r, _ in derive_members(s, account, item)[0]}:
            raise Refused('catalogue differs from approved terms; approve it again first')
        if discount_cents is not None and discount_cents >= live.standard_price_cents:
            raise Refused('a discount must be below the approved standard price')
        if discount_cents == live.discount_price_cents:
            return _ack(live)
        row = insert_revision(s, runtime, item, standard_cents=live.standard_price_cents, discount_cents=discount_cents,
            actor=actor, command_key=command_key, request_hash=request_hash, category_folder_id=live.category_folder_id,
            category_price_revision=live.category_price_revision)
        return _ack(row)


def withdraw_terms(store, tenant, account, item_id, *, command_key, actor, expected_revision=None):
    """Close the live revision (the only other legal change). Idempotent by state: nothing live is already withdrawn."""
    _id(item_id)
    _identity(command_key, actor)
    from sqlalchemy.orm import Session
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        from .launch_operations_models import OfLaunchCommand
        request_hash = _digest(['withdraw_terms', item_id, expected_revision])
        prior = s.get(OfLaunchCommand, (account, command_key))
        if prior is not None:
            if prior.kind != 'commercial_terms_withdraw' or prior.request_hash != request_hash or prior.actor != actor:
                raise Refused('terms idempotency conflict')
            return json.loads(prior.result_json)
        latest = latest_terms(s, account, item_id, lock=True)
        if expected_revision is not None and (type(expected_revision) is not int or
                expected_revision != (latest.revision if latest else 0)):
            raise Refused('terms revision conflict')
        live = live_terms(s, account, item_id, lock=True)
        if live is None:
            last = s.scalar(select(OfCatalogueTerms).where(OfCatalogueTerms.account_id == account,
                OfCatalogueTerms.item_id == item_id).order_by(OfCatalogueTerms.revision.desc()).limit(1))
            if last is None:
                raise Refused('no approved terms to withdraw')
            return _ack(last)
        live.status = 'withdrawn'
        s.flush()
        result = _ack(live)
        s.add(OfLaunchCommand(account_id=account, command_key=command_key, binding_id=runtime.binding_id,
            kind='commercial_terms_withdraw', request_hash=request_hash, actor=actor,
            reason='Agency withdrew approved commercial terms',
            before_json=json.dumps({'revision':live.revision,'status':'approved'}),
            result_json=json.dumps(result), created_at=utcnow()))
        return result


def bundle_below_singles(s, account, item_id):
    """Dashboard-warning flag: the approved bundle total is below the sum of its paid assets' single prices.

    Allowed (the agency's choice); it is information, never a block. False when a paid asset has no single product, so
    the sum is unknown.
    """
    live = live_terms(s, account, item_id)
    if live is None or not live.product_kind.startswith('bundle_'):
        return False
    paid = [m for m, r in members_of(s, live) if r == 'paid']
    total = 0
    for media in paid:
        price = s.scalar(select(OfCatalogueTerms.standard_price_cents).join(OfCatalogueTermsMember, (
            OfCatalogueTermsMember.account_id == OfCatalogueTerms.account_id) & (
            OfCatalogueTermsMember.item_id == OfCatalogueTerms.item_id) & (
            OfCatalogueTermsMember.revision == OfCatalogueTerms.revision)).where(
            OfCatalogueTerms.account_id == account, OfCatalogueTerms.status == 'approved',
            OfCatalogueTerms.product_kind.in_(('single_photo', 'single_video')), OfCatalogueTermsMember.media_id == media,
            OfCatalogueTermsMember.role == 'paid').order_by(OfCatalogueTerms.standard_price_cents).limit(1))
        if price is None:
            return False
        total += price
    return live.standard_price_cents < total

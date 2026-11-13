"""Deterministic recent-media policy for the existing vault job and workers.

No queue, catalogue approval, delivery, model call or destructive operation lives
here. Older metadata and approved products retain their existing owners.
"""
import copy
from datetime import datetime, timedelta
import json
import os

PHOTO_CAP = 1500
VIDEO_CAP = 700
CAPS = {'photo': PHOTO_CAP, 'video': VIDEO_CAP}
POLL_SECONDS = 7200
RECONCILE_SECONDS = 86400
POLICY = {'version': 1, 'photos': PHOTO_CAP, 'videos': VIDEO_CAP, 'order': 'recent'}


def enabled():
    return os.environ.get('ONLYFANS_VAULT_RECENT_WINDOW_ENABLED') == '1'


def scope():
    return {'selection': 'newest_per_type', **POLICY}


def stamp(now):
    return now.isoformat()


def nodes():
    return {kind: {'offset': 0, 'done': False, 'known': 0, 'hash': None} for kind in CAPS}


def start(now, mode='window'):
    if mode not in ('window', 'head'):
        raise ValueError('invalid vault window mode')
    return {'policy': dict(POLICY), 'mode': mode, 'started': stamp(now),
            'next': 'photo', 'last_head': stamp(now), 'head': None, **nodes()}


def load(raw):
    if raw is None:
        return None
    if type(raw) is not str or len(raw) > 65536:
        raise ValueError('invalid vault window progress')
    value = json.loads(raw)
    if type(value) is dict and value.get("policy")=={"version":2,"combined":1000,"order":"created_at_desc_id_desc"}:
        ids=value.get("selected")
        if (value.get("mode")!="window" or type(ids) is not list or len(ids)>1000 or
                any(type(mid) is not str or not mid.isascii() or not mid.isdecimal() or
                    not 0<int(mid)<2**63 or str(int(mid))!=mid for mid in ids) or len(set(ids))!=len(ids)):
            raise ValueError("invalid combined vault selection")
        if value.get("lastTimestamp") is not None:datetime.fromisoformat(value["lastTimestamp"])
        return value
    if type(value) is not dict or value.get('policy') != POLICY or value.get('mode') not in ('window', 'head'):
        raise ValueError('invalid vault window policy')
    validate_lane(value)
    datetime.fromisoformat(value['started'])
    datetime.fromisoformat(value['last_head'])
    if value.get('head') is not None:
        validate_lane(value['head'])
        datetime.fromisoformat(value['head']['started'])
    return value


def validate_lane(value):
    if value.get('next') not in CAPS:
        raise ValueError('invalid vault window kind')
    for kind, cap in CAPS.items():
        node = value.get(kind)
        if (type(node) is not dict or type(node.get('offset')) is not int
                or not 0 <= node['offset'] <= cap or type(node.get('done')) is not bool
                or type(node.get('known')) is not int or not 0 <= node['known'] <= cap
                or (node.get('hash') is not None and
                    (type(node['hash']) is not str or len(node['hash']) != 64))):
            raise ValueError('invalid vault window cursor')


def dump(value):
    raw = json.dumps(value, separators=(',', ':'), sort_keys=True)
    load(raw)
    return raw


def complete(value):
    return value['head'] is None and all(value[k]['done'] for k in CAPS)


def next_read(value, now, page_size):
    """Alternate photo/video pages; check fresh heads during a long first scan."""
    value = copy.deepcopy(value)
    if (value['mode'] == 'window' and value['head'] is None
            and not all(value[k]['done'] for k in CAPS)
            and now - datetime.fromisoformat(value['last_head']) >= timedelta(seconds=POLL_SECONDS)):
        value['head'] = {'started': stamp(now), 'next': 'photo', **nodes()}
    lane = value['head'] or value
    kind = lane['next']
    if lane[kind]['done']:
        kind = 'video' if kind == 'photo' else 'photo'
    node = lane[kind]
    if node['done']:
        raise ValueError('completed vault window has no page')
    limit = min(page_size, CAPS[kind] - node['offset'])
    if limit <= 0:
        raise ValueError('invalid vault window bound')
    return value, {'kind': kind, 'offset': node['offset'], 'limit': limit,
                   'head': value['head'] is not None or value['mode'] == 'head',
                   'started': lane['started']}


def advance(value, read, rows, more, known, page_hash, now, page_size):
    if len(rows) > read['limit'] or len(rows) != len(known) or any(r['type'] != read['kind'] for r in rows):
        raise ValueError('vault provider did not honour type/window bound')
    lane = value['head'] or value
    node = lane[read['kind']]
    if more and page_hash == node['hash']:
        raise ValueError('repeated vault window page')
    node['hash'] = page_hash
    node['offset'] += len(rows)
    for seen in known:
        node['known'] = node['known'] + 1 if seen else 0
    node['done'] = (not more or node['offset'] >= CAPS[read['kind']]
                    or read['head'] and node['known'] >= min(page_size, read['limit']))
    lane['next'] = 'video' if read['kind'] == 'photo' else 'photo'
    if value['head'] is not None and all(lane[k]['done'] for k in CAPS):
        value['head'] = None
        value['last_head'] = stamp(now)
    return value


def _latest_selection(account):
    """Window of the latest completed FansAPI job at the account's current binding and active epoch."""
    from sqlalchemy import select
    from db.onlyfans_vault_models import OfVaultSyncJob
    from db.onlyfans_models import OfRuntime
    from db.onlyfans_engine_models import OfFansConnection
    return (select(OfVaultSyncJob.window_json).join(OfRuntime,
        OfRuntime.account_id==OfVaultSyncJob.account_id).join(OfFansConnection,
        (OfFansConnection.account_id==OfVaultSyncJob.account_id)&
        (OfFansConnection.epoch==OfVaultSyncJob.connection_revision))
        .where(OfVaultSyncJob.account_id==account,OfVaultSyncJob.provider_kind=="fansapi",
            OfVaultSyncJob.status=="complete",OfVaultSyncJob.binding_id==OfRuntime.binding_id,
            OfFansConnection.binding_id==OfRuntime.binding_id,OfFansConnection.status=="active",
            OfVaultSyncJob.provider_account==OfFansConnection.provider_account_id)
        .order_by(OfVaultSyncJob.completed_at.desc(),OfVaultSyncJob.job_id.desc()).limit(1))


def commercial_window():
    """Latest completed account/binding/epoch selection; no history rows are removed."""
    from sqlalchemy import select,cast,func,BigInteger
    from sqlalchemy.dialects.postgresql import JSONB
    from db.models import VaultItem
    selected=_latest_selection(VaultItem.account_id).correlate(VaultItem).scalar_subquery()
    ids=select(cast(func.jsonb_array_elements_text(cast(selected,JSONB)["selected"]),BigInteger)).correlate(VaultItem)
    return VaultItem.media_id.in_(ids)


def outside_selection(session,account,media_ids):
    """Media verified outside the commercial selection: a completed selection exists at the current
    binding/epoch and lacks them (vanished from the provider listing, or past the newest window).
    No such selection (session gone, reconnect mid-import) is unknown, not verified: returns none."""
    from sqlalchemy import select
    from db.models import VaultItem
    if not media_ids or session.scalar(_latest_selection(account)) is None:
        return set()
    return set(media_ids)-set(session.scalars(select(VaultItem.media_id).where(VaultItem.account_id==account,
        VaultItem.media_id.in_(media_ids),commercial_window())))


def automatic_window():
    """Only bound automatic work. Approved product eligibility is unchanged."""
    from sqlalchemy import and_, or_, select, true
    from sqlalchemy.orm import aliased
    from db.models import VaultItem
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    fansapi=select(OfDeliveryPolicy.account_id).where(OfDeliveryPolicy.account_id==VaultItem.account_id,
        OfDeliveryPolicy.transport_kind=="fansapi").correlate(VaultItem).exists()
    if not enabled():
        return or_(~fansapi,commercial_window())
    item = aliased(VaultItem)
    return or_(and_(fansapi,commercial_window()),and_(~fansapi,or_(*[and_(VaultItem.kind == kind, VaultItem.media_id.in_(
        select(item.media_id).where(item.account_id == VaultItem.account_id,
            item.kind == kind, item.removed_at.is_(None))
        .order_by(item.created_at.desc(), item.media_id.desc()).limit(cap).correlate(VaultItem)))
        for kind, cap in CAPS.items()])))

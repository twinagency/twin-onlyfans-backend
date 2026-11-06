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
POLL_SECONDS = 300
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
    if type(raw) is not str or len(raw) > 16384:
        raise ValueError('invalid vault window progress')
    value = json.loads(raw)
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


def automatic_window():
    """Only bound automatic work. Approved product eligibility is unchanged."""
    from sqlalchemy import and_, or_, select, true
    from sqlalchemy.orm import aliased
    from db.models import VaultItem
    if not enabled():
        return true()
    item = aliased(VaultItem)
    return or_(*[and_(VaultItem.kind == kind, VaultItem.media_id.in_(
        select(item.media_id).where(item.account_id == VaultItem.account_id,
            item.kind == kind, item.removed_at.is_(None))
        .order_by(item.created_at.desc(), item.media_id.desc()).limit(cap).correlate(VaultItem)))
        for kind, cap in CAPS.items()])

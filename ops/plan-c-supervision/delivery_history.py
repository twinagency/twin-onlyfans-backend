"""Complete, bounded provider delivery-history reads with a durable verified checkpoint.

Pure standard library; no guard_core dependency, no provider credentials.  Every
failure here is fail-closed: the caller must not publish permission from a run that
raised.  TransientHistoryError marks a lookup that merely could not complete (the
caller may tolerate a bounded number in a row, still without publishing state).
"""
import datetime
import hashlib
import json
import time
from pathlib import Path

import incident_state as store

PAGE_SIZE = 100
MAX_PAGES = 20            # hard bound: 2000 deliveries in one window, then fail closed
OVERLAP = datetime.timedelta(minutes=15)
MAX_SCAN_ATTEMPTS = 3
MAX_RECENT = 2000
CHECKPOINT_VERSION = 'plan-c-delivery-checkpoint.v1'
BOUNDARY_VERSION = 'plan-c-delivery-boundary.v1'
TS_FIELDS = ('created_at', 'delivered_at', 'attempted_at', 'timestamp')


class HistoryError(RuntimeError):
    """Evidence is incomplete, conflicting, unresolved or changed: stay held."""


class TransientHistoryError(HistoryError):
    """A lookup failed to complete; nothing was established either way."""


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canon(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def parse_ts(value):
    try:
        if type(value) in (int, float) and type(value) is not bool:
            return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
        text = value[:-1] + '+00:00' if value.endswith('Z') else value
        parsed = datetime.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(datetime.timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        raise HistoryError('malformed_delivery_timestamp') from None


def fmt(moment):
    return moment.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def row_ts(row):
    for field in TS_FIELDS:
        if field in row:
            return parse_ts(row[field])
    return None


def identity(row):
    """Stable tie-break identity: (timestamp, idempotency key, delivery id or content hash)."""
    if type(row) is not dict:
        raise HistoryError('malformed_delivery_row')
    ts = row_ts(row)
    key = row.get('idempotency_key')
    if key is not None and (type(key) is not str or not key):
        raise HistoryError('malformed_delivery_key')
    content = sha(canon(row))
    rid = row.get('id')
    rid = str(rid) if type(rid) in (str, int) and type(rid) is not bool and rid != '' else content
    return (fmt(ts) if ts else '', key or ('sha:' + content), rid)


def check_page(obj):
    try:
        data, page = obj['data'], obj['_pagination']
        used = obj['_meta']['_credits']['used']
    except (KeyError, TypeError):
        raise HistoryError('delivery_page_malformed') from None
    if type(obj) is not dict or type(data) is not list or type(page) is not dict or 'next_page' not in page:
        raise HistoryError('delivery_page_malformed')
    if type(used) is not int or used != 0:
        raise HistoryError('unexpected_monitored_request_cost')
    total = page.get('total')
    if total is not None and (type(total) is not int or type(total) is bool or total < 0):
        raise HistoryError('delivery_page_malformed')
    return data, page


def fetch_all(get_page, *, start, deadline=None, clock=time.monotonic, max_pages=MAX_PAGES,
              page_size=PAGE_SIZE, attempts=MAX_SCAN_ATTEMPTS):
    """Read every page of one window.  get_page(start_iso, limit, offset) -> parsed object.

    Duplicates across shifting pages are folded by identity; the same identity with
    different content is conflicting evidence.  The scan is accepted only if it is
    internally stable (provider total unchanged and fully covered, or page 0 unchanged
    by a final re-read).  Exceeding max_pages fails closed.
    """
    for _ in range(attempts):
        rows, pages, raw_pages, dupes = {}, 0, [], 0
        offset, totals, unstable = 0, set(), False
        first_ids = None
        while True:
            if pages >= max_pages:
                raise HistoryError('delivery_page_bound_exceeded')
            if deadline is not None and clock() > deadline:
                raise TransientHistoryError('delivery_lookup_deadline')
            obj = get_page(start, page_size, offset)
            data, page = check_page(obj)
            raw_pages.append(obj)
            ids = []
            for row in data:
                ident = identity(row)
                ids.append(ident)
                if ident in rows:
                    if rows[ident] != row:
                        raise HistoryError('conflicting_delivery_evidence')
                    dupes += 1
                else:
                    rows[ident] = row
            if first_ids is None:
                first_ids = set(ids)
            if page.get('total') is not None:
                totals.add(page['total'])
            pages += 1
            if page['next_page'] is None:
                break
            if not data:
                raise HistoryError('empty_page_with_next_page')
            offset += len(data)
        if len(totals) > 1:
            unstable = True
        elif totals and len(rows) != next(iter(totals)):
            unstable = True
        elif not totals and pages > 1:
            again = get_page(start, page_size, 0)
            data, _ = check_page(again)
            if {identity(r) for r in data} != first_ids:
                unstable = True
        if unstable:
            continue
        ordered = sorted(rows)
        return {'ids': ordered, 'rows': [rows[i] for i in ordered], 'pages': pages,
                'raw_pages': raw_pages, 'duplicates_folded': dupes,
                'total': next(iter(totals)) if totals else None}
    raise HistoryError('delivery_pages_unstable')


def config_version(subscription):
    """Digest of the exact verified subscription configuration (url, events, scope, state)."""
    fields = {k: subscription.get(k) for k in ('id', 'url', 'account_ids', 'account_scope', 'enabled',
                                              'has_signing_secret', 'paused')}
    fields['events'] = sorted(subscription.get('events', []))
    return sha(canon(fields))


# --- durable files -------------------------------------------------------------------

def _write(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {**body, 'sha256': sha(canon(body))}
    store.atomic(path, canon(body))
    return body


def _read(path):
    """None if absent; HistoryError if present but not exactly the bytes we committed."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        body = json.loads(path.read_bytes())
        claimed = body.pop('sha256')
        if type(body) is not dict or claimed != sha(canon(body)):
            raise ValueError()
        body['sha256'] = claimed
        return body
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise HistoryError('durable_delivery_state_corrupt') from None


def load_boundary(path, intent_path, *, webhook_id, url, now):
    """The recorded enable boundary.  The intent file mtime is only the first-recording input."""
    existing = _read(path)
    intent = Path(intent_path)
    try:
        intent_sha = sha(intent.read_bytes())
    except OSError:
        raise HistoryError('enable_intent_unavailable') from None
    if existing is not None:
        if (existing.get('version') != BOUNDARY_VERSION or existing.get('webhook_id') != webhook_id
                or existing.get('url') != url):
            raise HistoryError('enable_boundary_identity_changed')
        if existing.get('put_intent_sha256') != intent_sha:
            raise HistoryError('enable_intent_changed')
        return existing
    mtime = datetime.datetime.fromtimestamp(intent.stat().st_mtime, datetime.timezone.utc)
    return _write(path, {'version': BOUNDARY_VERSION, 'webhook_id': webhook_id, 'url': url,
                         'boundary_utc': fmt(mtime), 'put_intent_sha256': intent_sha,
                         'recorded_at': fmt(now)})


def load_checkpoint(path, *, webhook_id, url, config, boundary):
    cp = _read(path)
    if cp is None:
        return None
    expected = {'version': CHECKPOINT_VERSION, 'webhook_id': webhook_id, 'url': url,
                'config_version': config, 'boundary_sha256': boundary['sha256']}
    for field, value in expected.items():
        if cp.get(field) != value:
            raise HistoryError('checkpoint_identity_changed:' + field)
    parse_ts(cp['cursor_utc'])
    return cp


# --- accounting ----------------------------------------------------------------------

def success(row, *, url, events, account_id):
    return (row.get('succeeded') is True and type(row.get('status_code')) is int
            and 200 <= row['status_code'] < 300 and row.get('url') == url
            and row.get('event') in events and type(row.get('payload')) is dict
            and row['payload'].get('account_id') == account_id)


def in_interval(ts, intervals):
    return ts is not None and any(parse_ts(a) <= ts <= parse_ts(b) for a, b in intervals)


def attempt_identity(row):
    """What a delivery attempt is an attempt OF: (event, destination, authenticated payload).

    The delivery-attempt id and status differ legitimately between a failed attempt and its retry, so
    they are not part of it.  The idempotency key is only the lookup handle: it is never proof of
    sameness by itself.  Any missing/malformed part means no identity (None = unresolved evidence).
    """
    key, event, url, payload = (row.get('idempotency_key'), row.get('event'), row.get('url'),
                                row.get('payload'))
    if (type(key) is not str or not key or type(event) is not str or not event
            or type(url) is not str or not url or type(payload) is not dict):
        return None
    return (key, event, url, sha(canon(payload)))


def retry_index(rows, is_success):
    """key -> identities of the verified successful deliveries carrying that key."""
    index = {}
    for row in rows:
        if is_success(row):
            ident = attempt_identity(row)
            if ident is not None:
                index.setdefault(ident[0], set()).add(ident)
    return index


def retry_state(row, index):
    """'resolved' only if a verified success is the same event, destination and payload as this
    failed attempt; 'conflict' if the key was delivered but for something else; else 'none'."""
    key = row.get('idempotency_key')
    if type(key) is not str or key not in index:
        return 'none'
    ident = attempt_identity(row)
    return 'resolved' if ident is not None and ident in index[key] else 'conflict'


def covered_by_resolution(row, ts, intervals, *, url, events, account_id):
    """A recorded incident resolution waives only failures inside its interval that belong to the
    monitored destination, event set and account; it never waives anything else."""
    payload = row.get('payload')
    return (in_interval(ts, intervals) and row.get('url') == url and row.get('event') in events
            and type(payload) is dict and payload.get('account_id') == account_id)


def account(window, *, url, events, account_id, resolved_intervals=(), prior_recent=(), has_prior=False):
    """Every row in the window must be a verified success or an explicitly resolved failure."""
    rows = window['rows']
    if not rows and not has_prior:
        raise HistoryError('no_delivery_evidence_since_enable')
    present = set(window['ids'])
    missing = [list(i) for i in prior_recent if tuple(i) not in present]
    if missing:
        raise HistoryError('accounted_delivery_missing')
    index = retry_index(rows, lambda r: success(r, url=url, events=events, account_id=account_id))
    unresolved, conflicting = 0, 0
    for row in rows:
        if success(row, url=url, events=events, account_id=account_id):
            continue
        state = retry_state(row, index)
        if state == 'resolved' or covered_by_resolution(row, row_ts(row), resolved_intervals, url=url,
                                                        events=events, account_id=account_id):
            continue
        unresolved += 1
        conflicting += state == 'conflict'
    if unresolved:
        raise HistoryError('unresolved_failed_delivery' + (':retry_identity_mismatch' if conflicting else ''))


def advance(cp, window, *, webhook_id, url, config, boundary, now):
    """Build the next checkpoint.  The cursor never moves past a row that was not accounted."""
    stamps = [i[0] for i in window['ids']]
    untimed = any(s == '' for s in stamps)
    old = parse_ts(cp['cursor_utc']) if cp else parse_ts(boundary['boundary_utc'])
    cursor = old
    if stamps and not untimed:
        cursor = max(old, parse_ts(max(stamps)))
    floor = fmt(cursor - OVERLAP)
    recent = [list(i) for i in window['ids'] if untimed or i[0] >= floor]
    previous = {tuple(i) for i in (cp or {}).get('recent', [])}
    new = sum(1 for i in window['ids'] if i not in previous)
    return {'version': CHECKPOINT_VERSION, 'webhook_id': webhook_id, 'url': url,
            'config_version': config, 'boundary_sha256': boundary['sha256'],
            'cursor_utc': fmt(cursor), 'recent': recent[-MAX_RECENT:],
            'accounted_count': (cp or {}).get('accounted_count', 0) + new,
            'seq': (cp or {}).get('seq', 0) + 1, 'committed_at': fmt(now)}


def verify_history(get_page, *, boundary_path, checkpoint_path, intent_path, webhook_id, url,
                   subscription, events, account_id, resolved_intervals=(), deadline=None,
                   clock=time.monotonic, now=None, max_pages=MAX_PAGES):
    """Read the whole unaccounted window, account for it, then durably advance the checkpoint."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    config = config_version(subscription)
    boundary = load_boundary(boundary_path, intent_path, webhook_id=webhook_id, url=url, now=now)
    cp = load_checkpoint(checkpoint_path, webhook_id=webhook_id, url=url, config=config, boundary=boundary)
    cursor = parse_ts(cp['cursor_utc']) if cp else parse_ts(boundary['boundary_utc'])
    start = max(cursor - OVERLAP, parse_ts(boundary['boundary_utc']))
    window = fetch_all(get_page, start=fmt(start), deadline=deadline, clock=clock, max_pages=max_pages)
    prior = [i for i in (cp or {}).get('recent', []) if i[0] == '' or parse_ts(i[0]) >= start]
    account(window, url=url, events=events, account_id=account_id, resolved_intervals=resolved_intervals,
            prior_recent=prior, has_prior=cp is not None)
    nxt = _write(checkpoint_path, advance(cp, window, webhook_id=webhook_id, url=url, config=config,
                                          boundary=boundary, now=now))
    return {'pages': window['pages'], 'rows': len(window['rows']), 'cursor_utc': nxt['cursor_utc'],
            'seq': nxt['seq'], 'config_version': config, 'url': url,
            'boundary_utc': boundary['boundary_utc'], 'duplicates_folded': window['duplicates_folded']}


def collect_pages(get_json, *, start, prefix, max_pages=MAX_PAGES):
    """Collector side: complete bounded read, retain every raw page, return a merged listing.

    get_json(start_iso, limit, offset, retained_name) is supplied by the caller and must
    retain the raw page under retained_name (unique per request, including re-scans).
    """
    counter = []

    def page(start_iso, limit, offset):
        counter.append(1)
        return get_json(start_iso, limit, offset, '%s-page-%d.json' % (prefix, len(counter)))
    window = fetch_all(page, start=start, max_pages=max_pages)
    merged = {'data': window['rows'],
              '_pagination': {'next_page': None, 'pages': window['pages'], 'complete': True,
                              'duplicates_folded': window['duplicates_folded']},
              '_meta': {'_credits': {'used': 0}}}
    return merged, window

"""Private message-attachment cache. API queues identity-only reads; worker owns CDN access."""
import hashlib
import json
import os
from datetime import timezone
from pathlib import Path
from sqlalchemy.orm import Session
from db.models import Message
from .store import Refused
from .vault_management import _connection
from .vault_media import SPOOL
import vault_still_store as images

QUEUE = SPOOL / 'message-previews'
MAX_VIDEO = 32 * 1024 * 1024


def reference(account, fan, message, media, variant):
    if variant not in {'preview', 'file'}:
        raise Refused('message media unavailable')
    if any(type(v) is not str or not v.isascii() or not v.isdecimal() or v.startswith('0') or not 0 < int(v) < 2**63 for v in (fan, message, media)):
        raise Refused('message media unavailable')
    return dict(account=account, fan=fan, message=message, media=media, variant=variant)


def scope(session, store, tenant, ref):
    runtime = store._runtime(session, tenant, ref['account'])
    row = session.get(Message, (ref['account'], int(ref['fan']), int(ref['message'])))
    if row is None or row.is_unsent:
        raise Refused('message media unavailable')
    try:
        raw = json.loads(row.raw_json or '{}')
        media = next(m for m in raw.get('media', []) if type(m) is dict and m.get('id') == int(ref['media']))
        if media.get('canView') is not True or media.get('isDeleted') or media.get('isHidden') or media.get('hasError') or media.get('isReady') is False:
            raise ValueError()
        if media.get('type') not in {'photo', 'video', 'gif'} or ref['variant'] == 'file' and media.get('type') != 'video':
            raise ValueError()
    except (ValueError, TypeError, StopIteration, AttributeError):
        raise Refused('message media unavailable') from None
    proof = {**ref, 'binding': runtime.binding_id, 'rawHash': hashlib.sha256(json.dumps(media, sort_keys=True).encode()).hexdigest()}
    return runtime, media, proof


def paths(ref):
    key = hashlib.sha256(json.dumps(ref, sort_keys=True).encode()).hexdigest()
    return QUEUE / (key + '.json'), images.IMAGE_DIR / 'message-previews' / (key + '.bin')


def cached(ref, proof):
    _, path = paths(ref)
    receipt = path.with_suffix('.json')
    try:
        if path.is_symlink() or receipt.is_symlink() or receipt.stat().st_size > 4096:
            return None
        saved = json.loads(receipt.read_bytes())
        cap = images.MAX_STILL_BYTES if ref['variant'] == 'preview' else MAX_VIDEO
        if path.stat().st_size > cap or any(saved.get(k) != v for k, v in proof.items()):
            return None
        data = path.read_bytes()
        if not data or saved.get('sha256') != hashlib.sha256(data).hexdigest():
            return None
        from .vault_video import video_type
        mime = images.image_type(data) if ref['variant'] == 'preview' else video_type(data)
        return (data, mime) if mime else None
    except (OSError, ValueError, TypeError):
        return None


def read_or_queue(store, tenant, ref):
    with Session(store.engine) as s, s.begin():
        _, _, proof = scope(s, store, tenant, ref)
        value = cached(ref, proof)
        if value:
            return value
    job, _ = paths(ref)
    import time
    QUEUE.mkdir(parents=True, exist_ok=True)
    if not job.exists() and len(list(QUEUE.glob('*.json'))) >= 512:
        raise Refused('message media unavailable')
    if not job.exists() or time.time() - job.stat().st_mtime > 60:
        images.write(job, json.dumps({'tenant': tenant, 'ref': ref}).encode())
    return None


def process_one(store, tenant, *, stop=None):
    if os.environ.get('ONLYFANS_PROCESS_ROLE') != 'worker':
        raise Refused('message media warming requires worker')
    if stop is not None and stop.is_set():
        return 0
    import time
    import shutil
    cache_dir = images.IMAGE_DIR / 'message-previews'
    # This namespace owns only message-cache files, never vault files.
    for old in list(cache_dir.glob('*'))[:1024]:
        if not old.is_symlink() and old.is_file() and time.time() - old.stat().st_mtime > 86400:
            old.unlink(missing_ok=True)
    if cache_dir.exists() and shutil.disk_usage(cache_dir).free < 2 * 1024**3:
        return 0
    for job in sorted(QUEUE.glob('*.json'), key=lambda p: p.stat().st_mtime)[:512]:
        if job.is_symlink() or job.stat().st_size > 2048:
            continue
        try:
            if time.time() - job.stat().st_mtime > 3600:
                job.unlink(missing_ok=True)
                continue
            task = json.loads(job.read_bytes())
            if task['tenant'] != tenant or task.get('attempted') and time.time() - job.stat().st_mtime < 60:
                continue
            ref = reference(**task['ref'])
            if paths(ref)[0] != job:
                continue
            with Session(store.engine) as s, s.begin():
                runtime, media, proof = scope(s, store, tenant, ref)
                if cached(ref, proof):
                    job.unlink(missing_ok=True)
                    return 1
                connection = _connection(s, runtime)
                from .connections import _open
                clear = _open(connection, tenant)
                from .provider_connector import NativeProviderConnector, session_from_capture
                transport = NativeProviderConnector(session_from_capture(account_id=ref['account'], owner_id=connection.owner_id,
                    policy_revision=1, capture=clear['capture'], x_hash=clear['xHash'], proxy_url=clear['proxyUrl'],
                    expires_at=connection.expires_at.replace(tzinfo=timezone.utc)))
            if ref['variant'] == 'preview':
                from of_shapes import still_url
                url = still_url(media)
                if not url:
                    raise Refused('message preview unavailable')
                data = transport.still_bytes(url)
            else:
                from of_shapes import media_urls
                from .provider_connector import validate_cdn_url
                from .vault_video import protected_source, video_type
                if protected_source(media):
                    raise Refused('protected message video unavailable')
                _, url = media_urls(media)
                validate_cdn_url(url)
                from curl_cffi import requests
                chunks, size = [], 0
                def receive(chunk):
                    nonlocal size
                    size += len(chunk)
                    if size > MAX_VIDEO or stop is not None and stop.is_set():
                        raise Refused('message video exceeds preview budget')
                    chunks.append(chunk)
                    return len(chunk)
                with requests.Session(impersonate='chrome', trust_env=False, retry=0) as client:
                    r = client.get(url, proxy=transport._handle.proxy_url, headers={'Accept-Encoding': 'identity', 'User-Agent': transport._handle.user_agent,
                        'Referer': 'https://onlyfans.com/'}, timeout=15, allow_redirects=False, verify=True, accept_encoding='identity',
                        content_callback=receive, discard_cookies=True)
                data = b''.join(chunks)
                if r.status_code != 200 or not video_type(data):
                    raise Refused('message video unavailable')
            with Session(store.engine) as s, s.begin():
                _, _, current = scope(s, store, tenant, ref)
                if current != proof:
                    raise Refused('message source changed')
                _, path = paths(ref)
                saved = {**proof, 'sha256': hashlib.sha256(data).hexdigest()}
                if not images.write(path, data) or not images.write(path.with_suffix('.json'), json.dumps(saved).encode()):
                    raise Refused('message cache unavailable')
            job.unlink(missing_ok=True)
        except Exception:
            # Signed URLs and provider errors never reach logs or the browser.
            try:
                images.write(job, json.dumps({**task, 'attempted': True}).encode())
            except Exception:
                pass
        return 1
    return 0

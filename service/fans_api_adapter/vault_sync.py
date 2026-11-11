"""FansAPI metadata import through the existing vault job/observation owners.

Latest selection does not erase older ownership, corrections or delivery history.
Missing URLs and DRM are retained honestly; provider readiness, classification,
operator approval, exact prices and ownership remain separate eligibility checks.
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


def request_sync(store,tenant,account,*,expected_connection_revision,command_key,actor,session):
    """Agency/BFF queues the existing provider-explicit job; no network or activation."""
    import uuid
    from db.onlyfans_engine_models import OfFansConnection
    from db.onlyfans_vault_models import OfVaultSyncJob
    from onlyfans_runtime.vault_management import digest,job_view
    from onlyfans_runtime.native_context import canonical
    runtime=store._runtime(session,tenant,account)
    prior=session.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id==account,
        OfVaultSyncJob.command_key==command_key))
    fingerprint=digest(["fansapi-vault.v2",runtime.binding_id,expected_connection_revision])
    if prior is not None:
        if (prior.request_hash,prior.actor,prior.binding_id)!=(fingerprint,actor,runtime.binding_id):
            raise Refused("vault idempotency conflict")
        return job_view(prior)
    connection=session.scalar(select(OfFansConnection).where(OfFansConnection.account_id==account,
        OfFansConnection.binding_id==runtime.binding_id,OfFansConnection.status=="active"))
    if connection is None or connection.epoch!=expected_connection_revision:
        raise Refused("vault connection revision conflict")
    active=session.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id==account,
        OfVaultSyncJob.status.in_(("pending","running"))).with_for_update())
    now=utcnow()
    if active is not None:
        if (active.provider_kind=="fansapi" and active.binding_id==runtime.binding_id
                and active.connection_revision==connection.epoch and active.provider_account==connection.provider_account_id):
            raise Refused("vault sync already active")
        active.status="superseded";active.last_error="connection_changed";active.completed_at=now
        active.lease_token=active.lease_until=None;active.updated_at=now;session.flush()
    job=OfVaultSyncJob(account_id=account,job_id=str(uuid.uuid4()),binding_id=runtime.binding_id,
        provider_kind="fansapi",provider_account=connection.provider_account_id,connection_revision=connection.epoch,
        owner_id=int(connection.provider_creator_id),command_key=command_key,request_hash=fingerprint,
        actor=actor,max_pages=200,status="pending",page_offset=0,pages_read=0,items_seen=0,
        window_json=canonical({"policy":{"version":2,"combined":1000,"order":"created_at_desc_id_desc"},
            "mode":"window","selected":[],"lastTimestamp":None}),requested_at=now,updated_at=now)
    session.add(job);session.flush()
    return job_view(job)


def _bound_connection(session,runtime,client):
    from db.onlyfans_engine_models import OfFansConnection
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    connection=session.get(OfFansConnection,(runtime.account_id,client.epoch))
    policy=session.get(OfDeliveryPolicy,runtime.account_id)
    if (connection is None or connection.status!="active" or connection.binding_id!=runtime.binding_id
            or (connection.provider_account_id,connection.provider_creator_id)!=(client.account,client.creator_id)
            or policy is None or policy.transport_kind!="fansapi"):
        raise Refused("FansAPI vault identity or provider family changed")
    return connection


def sync_vault(store,tenant,account,client,*,page_size=24,max_pages=200,command_key=None):
    """Resume the existing vault job, select 1,000 combined unique photos/videos.

    Ordered metadata only. Stop after the boundary timestamp is exhausted so
    IDs break ties deterministically. Missing/older media never erase ownership,
    descriptions, corrections, old cards or financial records. Every committed
    page uses the shared observation owner; crashes resume after its lease.
    """
    import uuid
    from datetime import timedelta
    from decimal import Decimal
    from db.onlyfans_vault_models import OfVaultSyncJob,OfVaultObservation
    from .protocol import FansApiClient
    from .https_transport import is_mock_wire
    from onlyfans_runtime.vault_management import parse_vault_page,digest,_stamp
    from onlyfans_runtime.native_context import canonical
    from vault_mirror import row_values,MIRROR_REFRESH_FIELDS
    if (type(client) is not FansApiClient or not client._identity_verified or
            type(page_size) is not int or not 10<=page_size<=24 or
            type(max_pages) is not int or not 1<=max_pages<=200 or
            command_key is not None and (type(command_key) is not str or len(command_key)!=64
                                        or any(c not in "0123456789abcdef" for c in command_key))):
        raise Refused("verified bounded FansAPI vault client required")
    if not is_mock_wire(client._http):
        from conversation_engine.pilot_scope import ProviderRequestGuard
        from conversation_engine.preparation import PreparationRequestGuard
        guard=client._request_guard
        if (type(guard) not in (ProviderRequestGuard,PreparationRequestGuard) or guard.store is not store or
                (guard.tenant,guard.account)!=(tenant,account)):
            raise Refused("budgeted account-scoped provider reads required")
    pages,upserted,skipped=0,0,{}
    job_id=None
    for _ in range(max_pages):
        with Session(store.engine) as s,s.begin():
            runtime=store._runtime(s,tenant,account)
            from conversation_engine.preparation import current as preparation_current,PreparationRequestGuard
            prep,_=preparation_current(s,runtime)
            if prep is not None:
                guard=client._request_guard
                if (type(guard) is not PreparationRequestGuard or guard.store is not store
                        or (guard.tenant,guard.account)!=(tenant,account)):
                    raise Refused("budgeted preparation guard required")
                guard.ensure_current(s)
            connection=_bound_connection(s,runtime,client)
            now=utcnow()
            job=s.get(OfVaultSyncJob,(account,job_id)) if job_id else None
            if job is None and command_key is not None:
                job=s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id==account,
                    OfVaultSyncJob.command_key==command_key))
                if job is not None and (job.binding_id!=runtime.binding_id or job.provider_kind!="fansapi"
                        or job.connection_revision!=client.epoch or job.provider_account!=client.account):
                    raise Refused("vault command identity conflict")
            if job is None:
                job=s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id==account,
                    OfVaultSyncJob.status.in_(("pending","running"))))
                if job is not None:
                    if (job.provider_kind!="fansapi" or job.binding_id!=runtime.binding_id or job.connection_revision!=client.epoch
                            or job.provider_account!=client.account):
                        job.status="superseded";job.last_error="connection_changed";job.completed_at=now
                        job.lease_token=job.lease_until=None;job.updated_at=now
                        s.flush();job=None
            if job is None:
                identity=str(uuid.uuid4())
                window={"policy":{"version":2,"combined":1000,"order":"created_at_desc_id_desc"},
                    "mode":"window","selected":[],"lastTimestamp":None}
                job=OfVaultSyncJob(account_id=account,job_id=identity,binding_id=runtime.binding_id,
                    provider_kind="fansapi",provider_account=client.account,connection_revision=client.epoch,
                    owner_id=int(client.creator_id),command_key=command_key or digest(["fansapi-vault",identity]),
                    request_hash=digest(["fansapi-vault.v2",runtime.binding_id,client.account,client.epoch]),
                    actor="worker-import:"+digest(["fansapi-vault",account]),max_pages=200,
                    status="pending",page_offset=0,pages_read=0,items_seen=0,
                    window_json=canonical(window),requested_at=now,updated_at=now)
                s.add(job)
            job_id=job.job_id
            if job.status=="complete":
                return {"complete":True,"pages":pages,"upserted":upserted,"skipped":skipped,
                        "error":None,"jobId":job_id,"selected":len(json.loads(job.window_json)["selected"])}
            if job.status in ("failed","superseded"):
                raise Refused("vault job needs a new reviewed import command")
            if job.status=="running" and job.lease_until>now:
                return {"complete":False,"pages":pages,"upserted":upserted,"skipped":skipped,
                        "error":"vault_job_busy","jobId":job_id}
            if job.pages_read>=job.max_pages:
                job.status="failed";job.last_error="vault_total_page_limit";job.completed_at=now
                job.lease_token=job.lease_until=None;job.updated_at=now
                return {"complete":False,"pages":pages,"upserted":upserted,"skipped":skipped,
                        "error":"vault_total_page_limit","jobId":job_id}
            token=str(uuid.uuid4());offset=job.page_offset
            job.status="running";job.lease_token=token;job.lease_until=now+timedelta(seconds=60)
        # No transaction spans provider I/O. The existing client applies read budgets.
        failure=None
        try:
            result=client.vault(limit=page_size,offset=offset)
            if result.state!="observed":raise ValueError("vault_read_failed")
            raw=json.dumps({"list":result.data,"hasMore":result.next_page is not None},
                default=lambda value:float(value) if isinstance(value,Decimal) else str(value)).encode()
            rows,more=parse_vault_page(raw,owner_id=int(client.creator_id),page_size=page_size,allow_identical_duplicates=True)
        except Exception:
            failure="vault_read_failed"
        with Session(store.engine) as s,s.begin():
            runtime=store._runtime(s,tenant,account)
            _bound_connection(s,runtime,client)
            job=s.get(OfVaultSyncJob,(account,job_id))
            if job.status!="running" or job.lease_token!=token:
                raise Refused("vault job lease changed")
            window=json.loads(job.window_json);now=utcnow()
            if type(client._request_guard) is PreparationRequestGuard:
                try:client._request_guard.ensure_current(s)
                except Refused:
                    job.status="pending";job.lease_token=job.lease_until=None
                    job.last_error="preparation_authorization_changed";job.updated_at=now
                    return {"complete":False,"pages":pages,"upserted":upserted,"skipped":skipped,
                            "error":job.last_error,"jobId":job_id}
            if failure:
                # Keep the same durable cursor for a bounded retry or restart.
                job.status="pending";job.lease_token=job.lease_until=None
                job.last_error=failure;job.updated_at=now
                return {"complete":False,"pages":pages,"upserted":upserted,"skipped":skipped,
                        "error":failure,"jobId":job_id}
            page_hash=digest(rows)
            dates=[_stamp(r["createdAt"]).replace(tzinfo=None) for r in rows]
            previous=datetime.fromisoformat(window["lastTimestamp"]) if window["lastTimestamp"] else None
            if ((more and job.last_page_hash==page_hash) or any(a<b for a,b in zip(dates,dates[1:]))
                    or dates and previous is not None and dates[0]>previous):
                job.status="failed";job.last_error="vault_order_or_replay";job.completed_at=now
                job.lease_token=job.lease_until=None;job.updated_at=now
                return {"complete":False,"pages":pages,"upserted":upserted,"skipped":skipped,
                        "error":"vault_order_or_replay","jobId":job_id}
            for raw_item,date in zip(rows,dates):
                if raw_item["type"] not in SELLABLE_KINDS:
                    skipped["unsupported_kind"]=skipped.get("unsupported_kind",0)+1
                    continue
                values=row_values(account,raw_item);values["created_at"]=date
                item=s.get(VaultItem,(account,values["media_id"]))
                if item is None:
                    item=VaultItem(**values);s.add(item)
                else:
                    for field in MIRROR_REFRESH_FIELDS:setattr(item,field,values[field])
                    item.created_at=date
                # Metadata identity is honest about its limited evidence scope.
                if not item.content_hash or item.content_hash_kind=="vault-metadata-v1":
                    identity={key:raw_item.get(key) for key in ("id","type","createdAt","duration")}
                    item.content_hash=digest(identity);item.content_hash_kind="vault-metadata-v1"
                if raw_item.get("isDeleted") is True or raw_item.get("isHidden") is True:item.removed_at=now
                s.flush()
                from vault_auto_folders import assign_sync
                assign_sync(s,account,item.media_id,commercial=True)
                observation=s.get(OfVaultObservation,(account,item.media_id))
                if observation is None:
                    observation=OfVaultObservation(account_id=account,media_id=item.media_id,
                        job_id=job_id,raw_hash=digest(item.raw_json),observed_at=now);s.add(observation)
                else:
                    observation.job_id=job_id;observation.raw_hash=digest(item.raw_json);observation.observed_at=now
                _,reason=classify(raw_item)
                if reason:skipped[reason]=skipped.get(reason,0)+1
                upserted+=1
            s.flush()
            candidates=s.execute(select(VaultItem.media_id,VaultItem.created_at).join(OfVaultObservation,
                (OfVaultObservation.account_id==VaultItem.account_id)&(OfVaultObservation.media_id==VaultItem.media_id))
                .where(VaultItem.account_id==account,OfVaultObservation.job_id==job_id,
                    VaultItem.kind.in_(("photo","video")))
                .order_by(VaultItem.created_at.desc(),VaultItem.media_id.desc()).limit(1000)).all()
            finished=not more or bool(len(candidates)==1000 and dates and dates[-1]<candidates[-1].created_at)
            if dates:window["lastTimestamp"]=dates[-1].isoformat()
            if finished:window["selected"]=[str(r.media_id) for r in candidates]
            job.window_json=canonical(window);job.last_page_hash=page_hash
            job.pages_read+=1;job.items_seen+=len(rows);job.page_offset=job.items_seen
            job.status="complete" if finished else "pending"
            job.lease_token=job.lease_until=None;job.updated_at=now;job.last_error=None
            if finished:job.completed_at=now
            pages+=1
            if finished:
                return {"complete":True,"pages":pages,"upserted":upserted,"skipped":skipped,
                        "error":None,"jobId":job_id,"selected":len(candidates)}
    return {"complete":False,"pages":pages,"upserted":upserted,"skipped":skipped,
            "error":"page_budget_exhausted","jobId":job_id}


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

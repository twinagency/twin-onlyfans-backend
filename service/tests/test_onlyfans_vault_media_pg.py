"""Native media acceptance with disposable PostgreSQL and synthetic transport.

No real credentials, provider requests, outgoing messages or production DB.
"""
import hashlib
import io
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified, ACTOR, TENANT
from test_onlyfans_vault_pg import sync, media
from test_onlyfans_provider_adapters import OWNER
from db.models import OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_media_models import OfVaultMediaJob
from db.onlyfans_connection_models import OfProviderConnection
from onlyfans_runtime import vault_media as upload
from onlyfans_runtime import vault_video as video
from onlyfans_runtime.vault_media_worker import recover, clean_one
from onlyfans_runtime.provider_connector import NativeProviderConnector, session_from_capture
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.vault_management import InvalidVault
from onlyfans_commands.vault_media_api import router, HEADER, SCHEMA
import vault_still_store as images


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv('ONLYFANS_VAULT_MEDIA_ENABLED', '1')
    monkeypatch.setattr(upload, 'SPOOL', tmp_path / 'spool')
    monkeypatch.setattr(upload, 'MIN_FREE_BYTES', 0)
    monkeypatch.setattr(video, 'MIN_FREE_BYTES', 0)
    monkeypatch.setattr(images, 'IMAGE_DIR', tmp_path / 'images')
    import vault_upload
    monkeypatch.setattr(vault_upload, 'SPOOL_ROOT', upload.SPOOL)
    monkeypatch.setattr(vault_upload, 'STAGE_ROOT', upload.SPOOL / 'incoming')
    monkeypatch.setattr(vault_upload, 'LOCK_DIR', upload.SPOOL / 'locks')
    monkeypatch.setattr(vault_upload, '_MEMO_PATH', upload.SPOOL / 'memo.json')


def photo():
    data = io.BytesIO()
    Image.new('RGB', (24, 24), '#247ba0').save(data, format='PNG')
    return data.getvalue()


def declared(name, data):
    return dict(fileId=str(uuid.uuid4()), name=name, size=len(data))


def admit(store, account, files, key='d' * 64):
    return upload.request_upload(store, TENANT, account, files=files,
        expected_connection_revision=1, command_key=key, actor=ACTOR)


def chunk(store, account, job, file, data, offset=0, actor=ACTOR):
    return upload.write_chunk(store, TENANT, account, job['jobId'], file['fileId'],
        offset=offset, digest=hashlib.sha256(data).hexdigest(), data=data, actor=actor)


def test_manifest_chunk_resume_conflicts_actor_and_finish(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    data = b'a' * upload.CHUNK_BYTES + b'final chunk'
    file = declared('synthetic.mp4', data)
    job = admit(store, account, [file])
    assert admit(store, account, [file]) == job
    with pytest.raises(Refused): admit(store, account, [file], key='e' * 64)
    with pytest.raises(Refused): chunk(store, account, job, file, data[:upload.CHUNK_BYTES], actor='another-session')
    first = data[:upload.CHUNK_BYTES]
    assert chunk(store, account, job, file, first)['receivedBytes'] == upload.CHUNK_BYTES
    assert chunk(store, account, job, file, first)['receivedBytes'] == upload.CHUNK_BYTES, 'lost ACK is an exact retry'
    with pytest.raises(Refused): chunk(store, account, job, file, b'b' * upload.CHUNK_BYTES)
    with pytest.raises(InvalidVault): upload.finish_upload(store, TENANT, account, job['jobId'], actor=ACTOR)
    with Session(store.engine) as session:
        row = session.get(OfVaultMediaJob, (account, job['jobId']))
        _, path = upload._file(row, file['fileId'])
    with path.open('ab') as handle: handle.write(b'fin')  # crash before fsync/receipt
    assert chunk(store, account, job, file, data[upload.CHUNK_BYTES:], upload.CHUNK_BYTES)['receivedBytes'] == len(data)
    assert path.read_bytes() == data
    view = upload.list_uploads(store, TENANT, account)['jobs'][0]['files'][0]
    expected = hashlib.sha256((''.join(hashlib.sha256(p).hexdigest() for p in [first, data[upload.CHUNK_BYTES:]])).encode()).hexdigest()
    assert view['prefixDigest'] == expected and view['receivedBytes'] == len(data)
    assert upload.finish_upload(store, TENANT, account, job['jobId'], actor=ACTOR)['status'] == 'pending'
    assert upload.finish_upload(store, TENANT, account, job['jobId'], actor=ACTOR)['status'] == 'pending'
    with pytest.raises(Refused): upload.list_uploads(store, 'foreign-tenant', account)
    assert upload.cancel_upload(store, TENANT, account, job['jobId'], actor=ACTOR)['status'] == 'expired'
    assert not path.exists()
    store.close()


@pytest.mark.parametrize('change', [{'name': '../photo.png'}, {'name': 'folder\\photo.png'}, {'size': True}, {'size': 0}, {'name': 'file.exe'}, {'sourceUrl': 'https://outside.invalid'}])
def test_manifest_rejects_paths_sources_and_invalid_files(change):
    with pytest.raises(InvalidVault): upload.manifest([{**declared('photo.png', b'abc'), **change}])


def test_real_shared_protocol_bulk_has_individual_outcomes_and_no_legacy_client(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    from onlyfans_runtime.vault_upload_transport import NativeVaultUpload, _Reply
    from of_client import OFClient
    import vault_upload
    # One valid photo, one malformed photo, then a transport failure. The batch
    # keeps the first result and does not turn either failure into success.
    payloads = [photo(), b'not an image', photo()]
    files = [declared(name, data) for name, data in zip(['one.png', 'bad.png', 'three.png'], payloads)]
    job = admit(store, account, files)
    for file, data in zip(files, payloads): chunk(store, account, job, file, data)
    upload.finish_upload(store, TENANT, account, job['jobId'], actor=ACTOR)
    events, creates = [], []
    def wire(self, method, path, body=b'', **options):
        assert store.engine.pool.checkedout() == 0, 'no database transaction spans provider I/O'
        events.append((method, path))
        value = json.loads(body) if body else {}
        if '/vault/media/hash?' in path: return 404, b'{}'
        if path.endswith('/upload/signed/create'):
            creates.append(value['key'])
            if len(creates) == 2: raise Refused('synthetic transport refusal')
            target = 'https://of2transcoder.s3-accelerate.amazonaws.com/' + value['key']
            return 200, json.dumps(dict(putUrl=target+'?signed=fixture', getUrl=target)).encode()
        if path.endswith('/users/me'): return 200, b'{}'
        if method == 'POST' and path.endswith('/posts'):
            state = upload._run(type('Job', (), {'account_id': account, 'run_id': account+'-native-'+job['jobId']})())
            assert state['items'][0]['carrier_intent'], 'intent committed before carrier POST'
            assert '[native:'+job['jobId']+']' in value['text'] and value['price'] == 0
            return 200, b'{"id":701}'
        if method == 'GET' and path.endswith('/posts/701'):
            return 200, b'{"media":[{"id":901,"isReady":true}]}'
        if method == 'DELETE' and path.endswith('/posts/701'): return 204, b'{}'
        pytest.fail('unexpected provider operation: '+method+' '+path)
    monkeypatch.setattr(NativeProviderConnector, '_signed_request', wire)
    monkeypatch.setattr(NativeVaultUpload, '_external', lambda self, method, url, **kw: _Reply(200,
        b'{"processId":"synthetic-process","host":"synthetic-host"}' if method == 'POST' else b'{}',
        {'ETag': '"'+hashlib.md5(kw.get('data', b'')).hexdigest()+'"'}))
    monkeypatch.setattr(vault_upload.time, 'sleep', lambda seconds: None)
    assert NativeVaultUpload.upload_media is OFClient.upload_media
    assert upload.process_upload(store, TENANT) == 1
    result = upload.list_uploads(store, TENANT, account)['jobs'][0]
    assert result['status'] == 'failed' and not result['cleanupRequired']
    assert [f['status'] for f in result['files']] == ['done', 'failed', 'failed'], {'events':events,'run':upload._run(type('Job',(),{'account_id':account,'run_id':account+'-native-'+job['jobId']})())}
    assert result['files'][0]['mediaId'] == '901'
    assert sum(method == 'POST' and path.endswith('/posts') for method, path in events) == 1
    assert ('DELETE', '/api2/v2/posts/701') in events
    assert upload.process_upload(store, TENANT) == 0, 'terminal jobs never replay provider writes'
    with Session(store.engine) as session: assert session.get(OfRuntime, account).enabled is False
    store.close()


def test_expired_lease_is_held_and_never_replayed(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    file = declared('one.png', photo()); job = admit(store, account, [file])
    with Session(store.engine) as session, session.begin():
        row = session.get(OfVaultMediaJob, (account, job['jobId']))
        row.status, row.lease_token, row.lease_until = 'running', str(uuid.uuid4()), utcnow()-timedelta(seconds=1)
        row.run_id = account+'-native-'+job['jobId']
        state = dict(account_id=account, run_id=row.run_id, items=[dict(carrier_intent='fixture', carrier_post_id=None)])
        path = upload.SPOOL / row.run_id / 'run.json'; path.parent.mkdir(parents=True); path.write_text(json.dumps(state))
    recover(store, TENANT)
    result = upload.list_uploads(store, TENANT, account)['jobs'][0]
    assert result['status'] == 'interrupted' and result['cleanupRequired']
    with pytest.raises(Refused): admit(store, account, [file], key='e' * 64)
    monkeypatch.setattr(NativeProviderConnector, 'verify_identity', lambda self: pytest.fail('must not replay'))
    assert upload.process_upload(store, TENANT) == 0
    store.close()


def cache(store, account, mid='13', data=None):
    data = data or b'\x00\x00\x00\x18ftypmp42'+bytes(range(256))*4
    with Session(store.engine) as session:
        binding = session.get(OfRuntime, account).binding_id
    path, receipt = video._files(account, mid); path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
    proof = dict(schemaVersion=video.SCHEMA, accountId=account, mediaId=int(mid), bindingId=binding, ownerId=OWNER,
        bytes=len(data), mtimeNs=path.stat().st_mtime_ns, mime='video/mp4', sha256=hashlib.sha256(data).hexdigest(),
        expiresAt=(utcnow()+timedelta(hours=48)).isoformat()+'Z')
    receipt.write_text(json.dumps(proof))
    return path, receipt, data


def api(store, monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'api')
    monkeypatch.setenv('ONLYFANS_CONTENT_TOKEN', 'synthetic-media-token')
    monkeypatch.setenv('ONLYFANS_CONTENT_TENANT', TENANT)
    app = FastAPI(); app.state.control_store = store; app.include_router(router)
    return TestClient(app), {'authorization':'Bearer synthetic-media-token', HEADER:SCHEMA}


def test_video_api_real_range_head_suffix_scope_and_expiry(database, monkeypatch):
    store, account, _, _ = sync(database, monkeypatch)
    path, receipt, data = cache(store, account)
    client, headers = api(store, monkeypatch)
    url = f'/onlyfans/content/v1/accounts/{account}/vault/videos/13/stream'
    monkeypatch.setattr(NativeProviderConnector, '_signed_request', lambda *a, **kw: pytest.fail('API must never contact provider'))
    assert client.get(url).status_code == 401
    result = client.get(url, headers=headers)
    assert result.status_code == 200 and result.content == data
    assert result.headers['cache-control'] == 'private, no-store'
    head = client.head(url, headers=headers)
    assert head.status_code == 200 and not head.content and int(head.headers['content-length']) == len(data)
    for spec, expected in [('bytes=12-31', data[12:32]), ('bytes=-20', data[-20:]), ('bytes=100-', data[100:])]:
        result = client.get(url, headers={**headers, 'Range':spec})
        assert result.status_code == 206 and result.content == expected
        assert int(result.headers['content-length']) == len(expected)
    for spec in ['bytes=999999-', 'bytes=0-1,4-6', 'nonsense']:
        assert client.get(url, headers={**headers,'Range':spec}).status_code == 416
    assert client.get(url+'?url=https://outside.invalid', headers=headers).status_code == 400
    with Session(store.engine) as session, session.begin():
        session.get(OfProviderConnection, account).expires_at = utcnow()-timedelta(seconds=1)
    assert client.get(url, headers=headers).status_code == 200, 'same-owner cache follows permanent-still permission policy'
    with Session(store.engine) as session, session.begin():
        session.get(OfCreatorBinding, session.get(OfRuntime, account).binding_id).revoked_at = utcnow()
    assert client.get(url, headers=headers).status_code == 404
    proof=json.loads(receipt.read_text()); proof['expiresAt']=(utcnow()-timedelta(seconds=1)).isoformat()+'Z'; receipt.write_text(json.dumps(proof))
    video.expire_videos(store, TENANT)
    assert not path.exists() and not receipt.exists()
    store.close()


def test_video_worker_uses_full_authoritative_source_and_cache_receipt(database, monkeypatch):
    row = media(13, 'video', files={'full':{'url':'https://cdn.onlyfans.com/synthetic/full.mp4'}, 'thumb':{'url':'https://cdn.onlyfans.com/synthetic/poster.jpg'}})
    store, account, _, _ = sync(database, monkeypatch, [row])
    assert video.request_video(store, TENANT, account, '13', command_key='d'*64, actor=ACTOR)['status'] == 'pending'
    sources=[]
    def download(transport, url, path, authorize):
        assert store.engine.pool.checkedout() == 0
        authorize(); sources.append(url); path.parent.mkdir(parents=True, exist_ok=True)
        data=b'\x00\x00\x00\x18ftypmp42'+b'synthetic-video'*100; path.write_bytes(data)
        return dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), mime='video/mp4')
    monkeypatch.setattr(video, 'download_video', download)
    assert video.process_video(store, TENANT) == 1
    assert sources == ['https://cdn.onlyfans.com/synthetic/full.mp4']
    assert video.video_status(store, TENANT, account, '13')['ready'] is True
    assert video.read_video(store, TENANT, account, '13')[0].suffix == '.video'
    assert video.process_video(store, TENANT) == 0
    store.close()


def test_upload_http_purpose_flag_chunk_and_no_provider(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    client, headers=api(store, monkeypatch)
    base=f'/onlyfans/content/v1/accounts/{account}/vault/uploads'
    monkeypatch.setattr(NativeProviderConnector, 'verify_identity', lambda self:pytest.fail('API must not contact provider'))
    data=photo(); file=declared('fixture.png', data)
    body=dict(files=[file],expectedConnectionRevision=1,commandKey='d'*64,actor=ACTOR)
    assert client.post(base,json=body).status_code == 401
    monkeypatch.delenv('ONLYFANS_VAULT_MEDIA_ENABLED')
    assert client.post(base,headers=headers,json=body).status_code == 503
    monkeypatch.setenv('ONLYFANS_VAULT_MEDIA_ENABLED','1')
    result=client.post(base,headers=headers,json=body)
    assert result.status_code==200, result.text
    job=result.json()['upload']; url=base+'/'+job['jobId']+'/files/'+file['fileId']+'?offset=0'
    binary={**headers,'content-type':'application/octet-stream','x-onlyfans-actor':ACTOR,'x-upload-sha256':hashlib.sha256(data).hexdigest()}
    assert client.post(url,headers={**binary,'x-upload-sha256':'f'*64},content=data).status_code==400
    assert client.post(url,headers=binary,content=data).json()['receivedBytes']==len(data)
    result=client.post(base+'/'+job['jobId']+'/finish',headers=headers,json={'commandKey':'e'*64,'actor':ACTOR})
    assert result.status_code==200 and result.json()['upload']['status']=='pending'
    with pytest.raises(Refused): upload.process_upload(store,TENANT)
    store.close()


def test_uncertain_carrier_is_not_reposted_and_unique_marker_cleanup_releases_hold(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    from onlyfans_runtime.vault_upload_transport import NativeVaultUpload
    from onlyfans_runtime.native_protocol import NativeProtocolError
    import vault_upload
    data=photo(); file=declared('one.png',data); job=admit(store,account,[file])
    chunk(store,account,job,file,data);upload.finish_upload(store,TENANT,account,job['jobId'],actor=ACTOR)
    posts=[];deletes=[]
    def lost(self, file_path, **options):
        # Exercise the actual carrier owner and its before-POST evidence hook.
        return self.materialize_to_vault([{'processId':'synthetic','host':'fixture','name':'one.png'}],
            on_carrier_intent=options['on_carrier_intent'],on_carrier=options['on_carrier'],on_carrier_deleted=options['on_carrier_deleted'])
    def wire(self,method,path,body=b'',**options):
        if method=='POST' and path.endswith('/posts'):
            posts.append(json.loads(body));raise NativeProtocolError('synthetic lost reply')
        if method=='GET' and '/schedules/later/post?' in path:
            return 200,json.dumps({'list':[{'id':702,'text':posts[0]['text']},{'id':703,'text':'unrelated scheduled post'}]}).encode()
        if method=='DELETE' and path.endswith('/posts/702'):
            deletes.append(702);return 204,b'{}'
        pytest.fail('unexpected cleanup operation: '+method+' '+path)
    monkeypatch.setattr(NativeVaultUpload,'upload_to_vault',lost)
    monkeypatch.setattr(NativeProviderConnector,'_signed_request',wire)
    assert upload.process_upload(store,TENANT)==1
    assert len(posts)==1, upload._run(type('Job',(),{'account_id':account,'run_id':account+'-native-'+job['jobId']})())
    assert upload.list_uploads(store,TENANT,account)['jobs'][0]['cleanupRequired']
    clean_one(store,TENANT)
    assert deletes==[702] and len(posts)==1
    assert not upload.list_uploads(store,TENANT,account)['jobs'][0]['cleanupRequired']
    assert admit(store,account,[file],key='f'*64)['status']=='receiving'
    store.close()


def test_native_upload_adapter_refuses_unrelated_writes_and_presigned_targets(database,monkeypatch):
    store,account,payload=verified(database,monkeypatch)
    from onlyfans_runtime.vault_upload_transport import NativeVaultUpload
    from test_onlyfans_provider_adapters import expires
    native=session_from_capture(account_id=account,owner_id=OWNER,policy_revision=1,capture=payload['capture'],
        x_hash=payload['xHash'],proxy_url=payload['proxyUrl'],expires_at=expires())
    adapter=NativeVaultUpload(native,authorize=lambda:None,job_id=str(uuid.uuid4()))
    calls=[]
    def wire(self,method,path,body=b'',**options):
        calls.append((method,path));return 200,b'{"putUrl":"https://outside.invalid/leak"}'
    monkeypatch.setattr(NativeProviderConnector,'_signed_request',wire)
    for method,path in [('POST','/chats/101/messages'),('DELETE','/posts/999'),('GET','/posts/999'),('POST','/posts')]:
        with pytest.raises(Refused):adapter._api(method,'https://onlyfans.com/api2/v2'+path,json_body={})
    assert calls==[]
    with pytest.raises(Refused):adapter._external('PUT','https://outside.invalid/upload',headers={'Content-Type':'video/mp4'},timeout=10,data=b'abc')
    with pytest.raises(Refused):adapter.request_signed_upload(key='upload/fixture/123/clip.mp4',content_type='video/mp4')
    assert len(calls)==1
    store.close()


def test_populated_0087_upgrade_preserves_connection_and_disabled_runtime(database,monkeypatch):
    from db.schema import ENROLLMENT,check_schema,upgrade
    from sqlalchemy import text
    store,account,_=verified(database,monkeypatch)
    tables=['accounts','of_runtime','of_provider_connection','of_creator_bindings']
    def snapshot(connection):
        return {table:[dict(row) for row in connection.execute(text('SELECT * FROM '+table)).mappings()] for table in tables}
    with database[1].begin() as connection:
        before=snapshot(connection)
        connection.exec_driver_sql('DROP TABLE of_vault_media_job')
        connection.execute(text('UPDATE alembic_version SET version_num=:revision'),{'revision':ENROLLMENT})
        check_schema(connection,expected=ENROLLMENT)
        upgrade(connection);upgrade(connection);check_schema(connection)
        assert snapshot(connection)==before
        assert connection.scalar(text('SELECT count(*) FROM of_vault_media_job'))==0
    store.close()


def test_single_video_and_multipart_photo_share_native_protocol(database,monkeypatch,tmp_path):
    import base64,shutil,subprocess
    from onlyfans_runtime.vault_upload_transport import NativeVaultUpload,_Reply
    import vault_upload,media_prep
    store,account,_=verified(database,monkeypatch)
    clip=Path(__file__).with_name('fixtures').joinpath('native-media-video.base64').read_text()
    clip=base64.b64decode(clip)
    # A PNG may carry trailing bytes. This crosses S3's real 5 MiB part size.
    large=photo()+b'\0'*(6*1024**2)
    files=[declared('clip.mp4',clip),declared('large.png',large)]
    job=admit(store,account,files)
    for file,data in zip(files,[clip,large]):chunk(store,account,job,file,data)
    upload.finish_upload(store,TENANT,account,job['jobId'],actor=ACTOR)
    if not shutil.which('ffprobe'):
        monkeypatch.setattr(upload.subprocess,'run',lambda args,**kw:subprocess.CompletedProcess(args,0,b'{"streams":[{"codec_type":"video"}]}'))
    # Container normalization has its own owner/suite. Pin its output here
    # to check the actual multipart boundaries against these original bytes.
    monkeypatch.setattr(media_prep,'prepare',lambda path,*args,**kw:(Path(path),False))
    requests,puts,carrier=[],[],0
    def wire(self,method,path,body=b'',**options):
        nonlocal carrier
        assert store.engine.pool.checkedout()==0
        value=json.loads(body) if body else {};requests.append((method,path))
        if '/vault/media/hash?' in path:return 404,b'{}'
        if path.endswith('/users/me'):return 200,b'{}'
        if path.endswith('/upload/signed/create'):
            target='https://of2transcoder.s3-accelerate.amazonaws.com/'+value['key']
            value={'getUrl':target,**({'keys':[{'part':n,'putUrl':target+'?partNumber='+str(n)} for n in range(1,value['parts']+1)],'uploadId':'fixture-upload'} if value['parts']>1 else {'putUrl':target+'?signed=fixture'})}
            return 200,json.dumps(value).encode()
        if path.endswith('/upload/signed/finish'):
            assert len(value['parts'])==2;return 200,b'{"ETag":"fixture-etag-2"}'
        if method=='POST' and path.endswith('/posts'):
            carrier+=1;return 200,json.dumps({'id':800+carrier}).encode()
        if method=='GET' and '/posts/' in path:return 200,json.dumps({'media':[{'id':900+carrier,'isReady':True}]}).encode()
        if method=='DELETE' and '/posts/' in path:return 204,b'{}'
        pytest.fail('unexpected provider operation '+method+' '+path)
    def external(self,method,url,**options):
        if method=='PUT':puts.append(len(options['data']))
        return _Reply(200,b'{"processId":"fixture","host":"fixture"}' if method=='POST' else b'{}',{'ETag':'fixture-etag'})
    monkeypatch.setattr(NativeProviderConnector,'_signed_request',wire)
    monkeypatch.setattr(NativeVaultUpload,'_external',external)
    monkeypatch.setattr(vault_upload.time,'sleep',lambda _:None)
    assert upload.process_upload(store,TENANT)==1
    result=upload.list_uploads(store,TENANT,account)['jobs'][0]
    assert result['status']=='complete',result
    assert [row['status'] for row in result['files']]==['done','done']
    assert sorted(puts)==sorted([len(clip),5*1024**2,len(large)-5*1024**2])
    assert any(path.endswith('/upload/signed/finish') for _,path in requests)
    with Session(store.engine) as session:
        from db.onlyfans_vault_models import OfVaultSyncJob
        assert session.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id==account)).status=='pending'
        assert session.get(OfRuntime,account).enabled is False
    store.close()


def test_admission_and_recorded_run_cannot_be_rewritten_or_deleted(database,monkeypatch):
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy import text
    store,account,_=verified(database,monkeypatch)
    admit(store,account,[declared('one.png',photo())])
    for statement in ["UPDATE of_vault_media_job SET actor='forged'", "UPDATE of_vault_media_job SET request_json='{}'", "DELETE FROM of_vault_media_job"]:
        with pytest.raises(DBAPIError),database[1].begin() as connection:
            connection.exec_driver_sql(statement)
    store.close()

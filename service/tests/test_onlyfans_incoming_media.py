"""Private cache tests with an owned SQLite store and synthetic CDN bytes."""
import json
from datetime import datetime
import tempfile
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from db.models import Message
from onlyfans_runtime import message_media as m
from onlyfans_runtime.store import Refused
from onlyfans_read.repo import _message_media_kinds


def test_message_attachment_cache(monkeypatch):
    with tempfile.TemporaryDirectory() as folder:
        engine = create_engine('sqlite:///'+folder+'/test.db')
        Message.__table__.create(engine)
        media={'id':11,'type':'photo','canView':True,'isReady':True,'files':{'preview':{'url':'https://cdn2.onlyfans.com/test.jpg'}}}
        with Session(engine) as s,s.begin():
            s.add(Message(account_id='of-1',fan_id=2,message_id=3,direction='in',body='',created_at=datetime.utcnow(),media_ids='[11]',media_count=1,raw_json=json.dumps({'media':[media]})))
        class Store:
            def _runtime(self,s,tenant,account):
                if tenant!='tenant' or account!='of-1':raise Refused('scope unavailable')
                return SimpleNamespace(binding_id='binding')
        store=Store();store.engine=engine
        monkeypatch.setattr(m,'QUEUE',Path(folder)/'queue');monkeypatch.setattr(m.images,'IMAGE_DIR',Path(folder)/'images')
        ref=m.reference('of-1','2','3','11','preview')
        assert m.read_or_queue(store,'tenant',ref) is None
        assert len(list(m.QUEUE.glob('*.json')))==1
        assert m.read_or_queue(store,'tenant',ref) is None
        for other in [dict(ref,fan='9'),dict(ref,message='9'),dict(ref,media='9')]:
            try:m.read_or_queue(store,'tenant',other);assert False
            except Refused:pass
        try:m.read_or_queue(store,'other',ref);assert False
        except Refused:pass
        with Session(engine) as s,s.begin():
            _,_,proof=m.scope(s,store,'tenant',ref)
        png=b'\x89PNG\r\n\x1a\nsynthetic-image'
        _,path=m.paths(ref)
        m.images.write(path,png);m.images.write(path.with_suffix('.json'),json.dumps({**proof,'sha256':m.hashlib.sha256(png).hexdigest()}).encode())
        assert m.read_or_queue(store,'tenant',ref)==(png,'image/png')
        m.images.write(path,b'corrupt');assert m.cached(ref,proof) is None
        with Session(engine) as s,s.begin():
            row=s.get(Message,('of-1',2,3));row.raw_json=json.dumps({'media':[dict(media,canView=False)]})
        try:m.read_or_queue(store,'tenant',ref);assert False
        except Refused:pass
        assert _message_media_kinds(json.dumps({'media':[media]}),[11])=={11:'photo'}
        assert _message_media_kinds(json.dumps({'replyToMessage':{'media':[media]}}),[11])=={}
        assert _message_media_kinds(json.dumps({'media':[media]}),[22])=={}
        for value in ['../11','0','-1','9223372036854775808']:
            try:m.reference('of-1','2','3',value,'preview');assert False
            except Refused:pass
        engine.dispose()

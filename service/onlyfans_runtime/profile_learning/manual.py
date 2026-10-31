"""Versioned operator corrections/protection. Existing managing-grant authority applies."""
from datetime import datetime
import json
import re
from sqlalchemy import text
from sqlalchemy.orm import Session
from onlyfans_read.agency_scope import actor_allowed,current_scope
from onlyfans_runtime.store import Refused
from .evidence import digest,project
from .storage import SCOPE,identity


def correct(store,tenant,account,fan,key,*,expected_revision,command_key,actor,reason,operation,value=None):
    if (type(expected_revision) is not int or expected_revision<0 or type(key) is not str
            or not re.fullmatch('[a-f0-9]{64}',key) or type(command_key) is not str
            or not 1<=len(command_key)<=64 or not actor_allowed(actor)
            or type(reason) is not str or not 1<=len(reason)<=512
            or operation not in {'set','clear','protect','unprotect'}):
        raise Refused('invalid memory correction')
    scope=current_scope.get()
    if scope is not None and not scope.can_manage: raise Refused('management permission required')
    if operation=='set' and (type(value) is not dict or not set(value)<=
            {'value','event_start','event_end','status'} or type(value.get('value')) is not str
            or not 1<=len(value['value'])<=240): raise Refused('invalid correction value')
    if operation!='set' and value is not None: raise Refused('invalid correction value')
    request_hash=digest([key,expected_revision,operation,value,actor,reason])
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        p=identity(tenant,account,runtime.binding_id,fan)
        s.execute(text('SELECT pg_advisory_xact_lock(hashtext(:k))'),{'k':'profile-correction:'+digest(p)})
        replay=s.execute(text(f'SELECT request_hash,revision FROM of_profile_manual_changes WHERE {SCOPE} AND command_key=:command'),
                         {**p,'command':command_key}).first()
        if replay:
            if replay[0]!=request_hash: raise Refused('correction idempotency conflict')
            return {'revision':replay[1],'replayed':True}
        existing=s.execute(text(f'SELECT * FROM of_profile_manual WHERE {SCOPE} AND memory_key=:key'),{**p,'key':key}).mappings().first()
        revision=existing['revision'] if existing else 0
        if revision!=expected_revision: raise Refused('memory correction version conflict')
        raw=s.execute(text(f'SELECT document_json FROM of_profile_memories WHERE {SCOPE} AND memory_key=:key'),{**p,'key':key}).scalar()
        if existing and existing['protected']: raw=existing['document_json']
        if not raw: raise Refused('scope unavailable')
        document=json.loads(raw)
        if operation=='set':
            document.update(value)
            for name in ('event_start','event_end'):
                if document.get(name):
                    from datetime import date
                    try: date.fromisoformat(document[name])
                    except (ValueError,TypeError): raise Refused('invalid correction date') from None
            if document.get('event_start') and document.get('event_end') and document['event_end']<document['event_start']:
                raise Refused('invalid correction date range')
            if document.get('status') not in {'current','upcoming','past','expired','superseded','cancelled','uncertain'}:
                raise Refused('invalid correction status')
        protected=operation!='unprotect'
        document.update(manual=protected,protected=protected,manual_revision=revision+1,
                        last_confirmed=datetime.utcnow().isoformat(),attribution='operator correction')
        if operation=='clear': document['status']='invalidated'
        if document['field']=='age' and (not document['value'].isascii() or not document['value'].isdigit() or not 18<=int(document['value'])<=120):
            raise Refused('invalid correction age')
        if document['field']=='date_of_birth':
            from .temporal import age_on
            try: age_on(document['value'],datetime.utcnow())
            except (ValueError,TypeError): raise Refused('invalid correction birth date') from None
        if document.get('event_end'):
            from datetime import date,timedelta
            document['active_until']=(date.fromisoformat(document['event_end'])+timedelta(days=14)).isoformat()
        if protected:
            document['evidence']=[{'source':'operator:'+actor,'quote':document['value'],
                'at':document['last_confirmed'],'reason':reason}]
        doc=json.dumps(document,ensure_ascii=False)
        params={**p,'key':key,'revision':revision+1,'doc':doc,'protected':protected,'command':command_key,
                'hash':request_hash,'actor':actor,'reason':reason}
        s.execute(text('''INSERT INTO of_profile_manual(tenant_id,account_id,binding_id,fan_id,memory_key,revision,document_json,protected)
            VALUES(:tenant_id,:account_id,:binding_id,:fan_id,:key,:revision,:doc,:protected)
            ON CONFLICT(tenant_id,account_id,binding_id,fan_id,memory_key) DO UPDATE SET
            revision=excluded.revision,document_json=excluded.document_json,protected=excluded.protected'''),params)
        s.execute(text('''INSERT INTO of_profile_manual_changes(tenant_id,account_id,binding_id,fan_id,command_key,
            memory_key,revision,request_hash,actor,reason,document_json)
            VALUES(:tenant_id,:account_id,:binding_id,:fan_id,:command,:key,:revision,:hash,:actor,:reason,:doc)'''),params)
        return {'revision':revision+1,'replayed':False}

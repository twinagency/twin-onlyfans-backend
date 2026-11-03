"""Adapters into the existing one-use TextAuthority, not another sender."""
import json
from .runtime import MoxyAccount, MoxyEvent, snapshot, validate_decision, digest
from onlyfans_runtime.store import Refused


def prepare_decision(s,account,fan,event_id,body):
    owner=s.get(MoxyAccount,account);event=s.get(MoxyEvent,(account,event_id))
    if not owner or owner.mode!='on' or not event or event.status!='ready' or event.fan_id!=fan:
        raise Refused('moxy_decision_not_current')
    if event.charged_millicents is None or digest(snapshot(s,account,fan,event))!=event.snapshot_hash:
        raise Refused('moxy_snapshot_changed')
    from .parts import active
    decision=active(event)
    from .campaigns import validate
    validate(s,account,fan,event)
    if decision['text']!=body:raise Refused('moxy_writer_text_changed')
    terms=validate_decision(json.loads(owner.settings_json),json.loads(event.snapshot_json),decision)
    if terms:
        terms={**terms,'moxy_action':decision['action']}
        if terms['price_cents']==0:terms['preview_ids']=[]
    action='commercial' if decision['action'] in {'invite','offer','resend','alternative','continue'} else 'chat'
    return event,terms,action


def action_class(raw):
    value=json.loads(raw) if raw else {}
    return 'chat' if value.get('moxy_action') in {'correct','fulfil'} else 'commercial' if value else 'chat'


def media_current(s,row):
    from .runtime import current
    if not current(s,row):return False
    event=s.get(MoxyEvent,(row.account_id,row.moxy_event_id))
    try:
        from .parts import active
        decision=active(event)
        terms=validate_decision(json.loads(s.get(MoxyAccount,row.account_id).settings_json),
            json.loads(event.snapshot_json),decision)
        if terms:
            terms={**terms,'moxy_action':decision['action']}
            if terms['price_cents']==0:terms['preview_ids']=[]
            return (json.loads(row.catalog_snapshot_json)==terms and json.loads(row.media_json)==terms['media_ids']
                and row.price_cents==terms['price_cents'] and json.loads(row.preview_json)==terms['preview_ids'])
        return row.catalog_item_id is None and row.price_cents==0 and row.media_json=='[]'
    except (ValueError,TypeError,KeyError):return False


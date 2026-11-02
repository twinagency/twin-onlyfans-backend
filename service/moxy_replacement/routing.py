"""Ownership fencing for existing entry points, including process restarts."""
from sqlalchemy.orm import Session
from .runtime import selected, MoxyService
from onlyfans_runtime.store import Refused


def selected_sync(account):
    from twin_bridge.coordination import store_target
    from twin_bridge import store
    target=store_target()
    if not target:return False
    with store.readonly(target) as con:
        if not store.has_table(con,'of_moxy_account'):return False
        # Canonical IDs are preserved, never inferred as a different tenant.
        row=con.execute('SELECT account_id FROM of_moxy_account WHERE account_id=?',(str(account),)).fetchone()
        if row is None and str(account).isdigit():
            row=con.execute('SELECT account_id FROM of_moxy_account WHERE account_id=?',('of-'+str(account),)).fetchone()
        return row is not None


def refuse_legacy(account):
    if selected_sync(account):raise Refused('moxy_canonical_owner_required')


def route_legacy(authority,tenant,account,fan,origin,key,payload):
    if origin=='manual':return
    with Session(authority.engine) as s,s.begin():
        authority.store._runtime(s,tenant,account)
        if not selected(s,account):return
    if origin in {'campaign','engagement'}:
        # CampaignService owns audience/revision admission. Old engagement
        # timers have no authority to create fresh Moxy work after cutover.
        raise Refused('moxy_canonical_scheduler_required')
    MoxyService(authority.store).add_event(tenant,account,fan,
        'campaign' if origin=='campaign' else 'scheduled' if origin=='engagement' else 'turn',
        'routed:'+key,{'legacy_origin':origin,**payload})
    raise Refused('moxy_event_queued_for_authoritative_writer')

"""Ownership fencing for existing entry points, including process restarts."""
from sqlalchemy.orm import Session
from .runtime import selected, MoxyService
from onlyfans_runtime.store import Refused


SELECTED='selected'
NOT_SELECTED='not_selected'
UNKNOWN='unknown'

# Fan-messaging automation kinds. A Moxy-selected account must never run these
# (no model call, no state write, no send, no caption rewrite): the Moxy owner
# is the only automated writer. Non-messaging kinds (scrape_chats, describe_media,
# audience_sync, profile/feed/story kinds, unsend cleanup) are deliberately absent.
# `scheduled_send` is fenced only for non-manual payloads (see job_fence_reason).
MOXY_FENCED_KINDS=frozenset({
    'ai_chatter','send_welcome','welcome_chatter_for_info','make_right','reengage_buyers',
    'tip_reward','image_reply','tip_request','ppv_send','mass_nudge','online_blast',
    'send_mass_message','send_followup','reply_mass_funnel','nudge_online','nudge_online_fire',
    'autoreply','arc_tease','mass_premade','scheduled_send'})


def _candidates(account):
    """Every spelling a canonical Moxy id may take: 'of-<digits>' <-> '<digits>'."""
    a=str(account).strip()
    out=[a] if a else []
    if a.isdecimal():out.append('of-'+a)
    elif a.startswith('of-') and a[3:].isdecimal():out.append(a[3:])
    return out


def owner_state(account):
    """SELECTED / NOT_SELECTED / UNKNOWN for an account.

    NOT_SELECTED is returned only on a DEFINITIVE negative: the store was read and
    either the of_moxy_account table does not exist (schema not migrated, so no
    account can be Moxy-owned) or it has no row for any spelling of the id.
    Anything that stops us reading the store (no store target, connection or query
    error) is UNKNOWN, never NOT_SELECTED: callers that send automatically refuse.
    An empty account identity cannot be a Moxy tenant and is NOT_SELECTED."""
    cands=_candidates(account)
    if not cands:return NOT_SELECTED
    try:
        from twin_bridge.coordination import store_target
        from twin_bridge import store
        target=store_target()
        if not target:return UNKNOWN
        with store.readonly(target) as con:
            if not store.has_table(con,'of_moxy_account'):return NOT_SELECTED
            for c in cands:
                if con.execute('SELECT account_id FROM of_moxy_account WHERE account_id=?',(c,)).fetchone() is not None:
                    return SELECTED
        return NOT_SELECTED
    except Exception:
        return UNKNOWN


def selected_sync(account,*,unknown=False):
    """True when the account is definitively Moxy-selected. `unknown=True` also
    counts a failed lookup as selected (fail closed)."""
    st=owner_state(account)
    return st==SELECTED or (unknown and st==UNKNOWN)


def refuse_legacy(account,purpose='other'):
    """Refuse an automated legacy write. A manual operator send always passes (the
    human takeover path is not a competing automated writer and needs no lookup)."""
    if purpose=='manual':return
    st=owner_state(account)
    if st==SELECTED:raise Refused('moxy_canonical_owner_required')
    if st==UNKNOWN:raise Refused('moxy_owner_lookup_failed')


def job_fence_reason(account,kind,payload=None,*,state=None):
    """Why an automation job must not run for this account, or None to run it.

    Used by the executor before any model call or state write. Fail closed: a
    failed owner lookup refuses a fenced kind. Manual-stamped scheduled sends (a
    human deferral) are never fenced."""
    k=str(kind)
    if k not in MOXY_FENCED_KINDS:return None
    if k=='scheduled_send' and isinstance(payload,dict) and str(payload.get('send_purpose') or '')=='manual':return None
    st=state if state is not None else owner_state(account)
    if st==SELECTED:return 'moxy_canonical_owner_required'
    if st==UNKNOWN:return 'moxy_owner_lookup_failed'
    return None


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

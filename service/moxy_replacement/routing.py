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


def engine_owner_modes(account,fan_id=None):
    """Conversation-engine ownership for an account (or one fan of it).

    Returns (state, modes): state is SELECTED when at least one `of_engine_owner`
    row matches (modes = the set of their `mode` values), NOT_SELECTED on a
    DEFINITIVE negative (store read fine and no matching row, or the table does
    not exist), UNKNOWN when the store could not be read. `fan_id` None/0/invalid
    means account-level: any engine-owned fan of the account counts, because a
    broadcast or an account-wide write cannot be shown to exclude them."""
    cands=_candidates(account)
    if not cands:return NOT_SELECTED,frozenset()
    try:
        fan=int(fan_id) if fan_id not in (None,'') else 0
    except (TypeError,ValueError):
        fan=0
    try:
        from twin_bridge.coordination import store_target
        from twin_bridge import store
        target=store_target()
        if not target:return UNKNOWN,frozenset()
        sql='SELECT mode FROM of_engine_owner WHERE account_id IN (%s)'%','.join('?'*len(cands))
        args=list(cands)
        if fan>0:
            sql+=' AND fan_id=?'
            args.append(fan)
        with store.readonly(target) as con:
            if not store.has_table(con,'of_engine_owner'):return NOT_SELECTED,frozenset()
            modes=frozenset(str(r[0]) for r in con.execute(sql,tuple(args)).fetchall())
        return (SELECTED,modes) if modes else (NOT_SELECTED,frozenset())
    except Exception:
        return UNKNOWN,frozenset()


PILOT_ENV='TWIN_ENGINE_PILOT_ACCOUNTS'


def pilot_accounts():
    """Accounts declared as engine pilot scope in this process's own configuration (comma-separated ids,
    either spelling). Known WITHOUT the ownership store, so it still applies when the store cannot be read."""
    import os
    out=set()
    for item in str(os.environ.get(PILOT_ENV,'')).split(','):
        out.update(_candidates(item))
    return frozenset(out)


def in_pilot_scope(account):
    return bool(set(_candidates(account))&pilot_accounts())


def durable_pilot_state(account):
    """A configured/stopped pilot still owns its account before any lazy fan owner exists."""
    candidates = _candidates(account)
    if not candidates:
        return NOT_SELECTED
    try:
        from twin_bridge.coordination import store_target
        from twin_bridge import store
        target = store_target()
        if not target:
            return UNKNOWN
        with store.readonly(target) as con:
            if not store.has_table(con, 'of_engine_pilot_scope'):
                return NOT_SELECTED
            query = 'SELECT account_id FROM of_engine_pilot_scope WHERE scope_id=1 AND account_id IN (%s)' % ','.join('?' * len(candidates))
            return SELECTED if con.execute(query, tuple(candidates)).fetchone() is not None else NOT_SELECTED
    except Exception:
        return UNKNOWN


def refuse_engine_owned(account,purpose='other',fan_id=None):
    """Refuse a legacy write for an account/fan owned by the conversation engine.
    Fails closed on a failed lookup. The only pass for an owned fan is a manual
    operator send where every matching owner row is in mode 'human' (a verified
    human takeover read from the store, never assumed)."""
    # A process manifest is itself an explicit claim of exclusive pilot ownership.
    # It must not become a legacy-send allowance when any store lookup is stale,
    # unavailable, or pointed at an older database. Human takeover uses the
    # canonical authority, never this legacy transport.
    if in_pilot_scope(account) or durable_pilot_state(account) == SELECTED:
        raise Refused('pilot_canonical_authority_required')
    est,modes=engine_owner_modes(account,fan_id)
    if est==SELECTED and not (purpose=='manual' and modes=={'human'}):
        raise Refused('engine_canonical_owner_required')
    if est==UNKNOWN:
        # An unrelated account's operator is never blocked by a store outage (the documented human-takeover
        # policy). A DECLARED pilot account is: with the ownership store unreadable a manual send cannot be told
        # apart from a send into an engine-owned conversation, so it is held until takeover can be verified.
        if purpose!='manual' or in_pilot_scope(account):raise Refused('engine_owner_lookup_failed')


def refuse_legacy(account,purpose='other',fan_id=None):
    """Refuse a legacy write for a Moxy- or engine-owned account. Moxy: a manual
    operator send passes with no lookup; every other purpose is refused on
    SELECTED/UNKNOWN. Engine ownership is then checked for EVERY purpose."""
    if purpose!='manual':
        st=owner_state(account)
        if st==SELECTED:raise Refused('moxy_canonical_owner_required')
        if st==UNKNOWN:raise Refused('moxy_owner_lookup_failed')
    refuse_engine_owned(account,purpose,fan_id)


def job_fence_reason(account,kind,payload=None,*,state=None,engine_state=None):
    """Why an automation job must not run for this account, or None to run it.

    Used by the executor before any model call or state write. Fail closed: a
    failed owner lookup refuses a fenced kind. Manual-stamped scheduled sends (a
    human deferral) still obey exclusive pilot ownership."""
    k=str(kind)
    if k not in MOXY_FENCED_KINDS:return None
    if in_pilot_scope(account) or durable_pilot_state(account) == SELECTED:return 'pilot_canonical_authority_required'
    if k=='scheduled_send' and isinstance(payload,dict) and str(payload.get('send_purpose') or '')=='manual':return None
    st=state if state is not None else owner_state(account)
    if st==SELECTED:return 'moxy_canonical_owner_required'
    if st==UNKNOWN:return 'moxy_owner_lookup_failed'
    # The conversation engine is the only automated writer for an engine-owned account: refuse before any model
    # call or state write, not at the final send. Unreadable ownership holds the job (fail closed).
    est=engine_state if engine_state is not None else engine_owner_modes(account)[0]
    if est==SELECTED:return 'engine_canonical_owner_required'
    if est==UNKNOWN:return 'engine_owner_lookup_failed'
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

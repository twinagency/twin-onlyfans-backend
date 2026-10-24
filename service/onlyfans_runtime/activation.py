"""Account-scoped activation facts. Reads and validation never enable a fan or widen deployment access."""
import os


def deployment_scope(account):
    from .fastt_scope import parse_allowlist
    from .runtime_stop import to_fastt_account_id
    account = to_fastt_account_id(account)
    pairs, whole, database = parse_allowlist(os.environ.get("ONLYFANS_FASTT_SCOPE", ""))
    fans = sorted(fan for owner, fan in pairs if owner == account)
    all_allowed = account in whole or account in database
    return {"deploymentAllowed": bool(all_allowed or fans), "allFansAllowed": all_allowed, "allowedFans": fans}


def validate_selection(account, value):
    from .fastt_scope import normalize
    selected = normalize(value)
    allowed = deployment_scope(account)
    if not allowed["deploymentAllowed"]:
        raise ValueError("This creator needs deployment authorization before automation can be selected.")
    if selected["mode"] == "all" and not allowed["allFansAllowed"]:
        raise ValueError("All-fan automation is not authorized for this creator.")
    if any(fan >= 2**63 for fan in selected["fans"]):
        raise ValueError("A fan ID is outside the supported range.")
    if not allowed["allFansAllowed"] and not set(selected["fans"]) <= set(allowed["allowedFans"]):
        raise ValueError("One or more selected fans are outside this creator's deployment authorization.")
    return selected


def activation_view(session, account, config, *, binding_id, model_id):
    from sqlalchemy import select, func
    from db.models import OfConversationState
    from db.onlyfans_models import OfDeliveryPolicy
    from db.onlyfans_connection_models import OfProviderConnection
    from .connections import personality_credential_status, _open
    from .fastt_scope import normalize
    counts = dict(session.execute(select(OfConversationState.mode, func.count()).where(
        OfConversationState.account_id == account).group_by(OfConversationState.mode)).all())
    raw = config.get("fastt_scope")
    try:
        selection = normalize(raw) if raw is not None else None
        valid = True
    except ValueError:
        selection, valid = None, False
    policy = session.get(OfDeliveryPolicy, account)
    credential = personality_credential_status(session, account, binding_id)
    model_ready = bool(credential and credential.get("configured") and credential.get("modelId") == model_id)
    proxy_saved = False
    connection = session.get(OfProviderConnection, account)
    if connection is not None:
        from db.models import OfCreatorBinding
        binding = session.get(OfCreatorBinding, binding_id)
        try: proxy_saved = bool(_open(connection, binding.tenant_id).get("proxyUrl"))
        except Exception: pass  # Only a boolean leaves the credential authority; no error/secret text.
    takeover = session.scalar(select(func.count()).select_from(OfConversationState).where(
        OfConversationState.account_id == account, OfConversationState.takeover_state != "none"))
    return {**deployment_scope(account), "selection": selection, "selectionValid": valid,
            "modelCredentialReady": model_ready, "proxySaved": proxy_saved,
            "automatedConversations": counts.get("automated", 0), "pausedConversations": counts.get("paused", 0),
            "noContactConversations": counts.get("no_contact", 0), "takeoverConversations": takeover or 0,
            "nextSendAt": policy.next_dispatch_at.isoformat() + "Z" if policy and policy.next_dispatch_at else None,
            "throttleSeconds": policy.throttle_seconds if policy else None}

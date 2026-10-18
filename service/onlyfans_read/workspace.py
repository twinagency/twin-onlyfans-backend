"""Bounded native Content, Sales and Settings projections, never new owners.

Every page uses one repeatable-read snapshot and locks its active binding.
Read time is separate from the last completed history synchronization.
"""
import base64
import json
import re

from sqlalchemy import func, select

from db.models import AccountAiConfig, CatalogItem, CATALOG_IS_SINGLE, ContentOffer, OfCreatorBinding, Transaction
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_connection_models import OfProviderConnection
from .contract import iso
from .errors import BadRequest, Unavailable

SECTIONS = {"content", "sales", "settings"}
SCHEMA = "of-workspace.v1"
MAX_INTEGER = 2**53-1


def integer(value, *, signed=False):
    if type(value) is not int or not (-MAX_INTEGER if signed else 0) <= value <= MAX_INTEGER:
        raise ValueError("invalid recorded number")
    return value


def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError("duplicate key")
        result[key]=value
    return result


def object_json(raw, maximum=65536):
    if raw is None or raw == "":return {}
    if type(raw) is not str or len(raw.encode()) > maximum:raise ValueError("configuration exceeds bound")
    value=json.loads(raw,object_pairs_hook=unique)
    if type(value) is not dict:raise ValueError("configuration must be an object")
    return value


def cursor_for(account,section,identity):
    raw=json.dumps([1,account,section,identity],separators=(",",":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def cursor_id(raw,account,section):
    if not raw:return None
    try:
        if type(raw) is not str or len(raw)>256 or not re.fullmatch(r"[A-Za-z0-9_-]+",raw):raise ValueError()
        value=json.loads(base64.b64decode(raw+"="*(-len(raw)%4),altchars=b"-_",validate=True))
        if (type(value) is not list or len(value)!=4 or value[:3]!=[1,account,section]
                or type(value[3]) is not int or not 0<value[3]<2**63):raise ValueError()
        return value[3]
    except (ValueError,TypeError,UnicodeError) as exc:
        raise BadRequest("invalid_cursor","That page cursor is not usable.") from exc


def catalog_row(s,item):
    from onlyfans_runtime.media_delivery import catalog_terms, media_ids
    from onlyfans_runtime.store import Refused
    description=item.description_for_ai or ""
    readiness="disabled" if not item.enabled else "free_teaser" if item.is_free_teaser else "needs_review"
    media_count=preview_count=None
    try:media_count=len(media_ids(item.media_ids))
    except Refused:pass
    try:
        previews=json.loads(item.preview_media_ids)
        if type(previews) is list and len(previews)<=35 and all(type(x) is int and 0<x<2**63 for x in previews):preview_count=len(previews)
    except (ValueError,TypeError):pass
    if readiness=="needs_review":
        try:
            terms=catalog_terms(s,item.account_id,item.id)
            media_count,preview_count=len(terms["media_ids"]),len(terms["preview_ids"])
            readiness="terms_checked"
        except Refused:pass
    price=item.price_cents if type(item.price_cents) is int and 0<=item.price_cents<=MAX_INTEGER else None
    return {"itemId":str(item.id),"label":(item.label or "Unnamed item")[:160],
        "kind":str(item.kind)[:32],"description":description[:2000],"descriptionTruncated":len(description)>2000,
        "mediaCount":media_count,"previewCount":preview_count,"priceCents":price,"currency":"USD",
        "readiness":readiness,"updatedAt":iso(item.updated_at)}


def sale_row(row):
    currency=row.currency
    if type(currency) is not str or not re.fullmatch(r"[A-Z]{3}",currency):raise ValueError("invalid recorded currency")
    return {"transactionId":str(row.id),"fanId":None if row.fan_id is None else str(row.fan_id),
        "messageId":None if row.message_id is None else str(row.message_id),"kind":row.kind[:64],
        "status":row.status[:24],"grossCents":integer(row.amount_cents,signed=True),"currency":currency,
        "netCents":None if row.net_cents is None else integer(row.net_cents,signed=True),
        "occurredAt":iso(row.occurred_at)}


def config_view(cfg):
    if cfg is None:return None
    from automations._persona_contract import PERSONA_FACT_FIELDS, _parse_persona_facts
    issues=[]
    try:
        saved=object_json(cfg.persona_facts_json)
        facts=_parse_persona_facts(json.dumps(saved))
    except (ValueError,TypeError,RecursionError):facts={};issues.append("creator_facts_invalid")
    labels=dict(PERSONA_FACT_FIELDS)
    shown=[]
    for key,value in facts.items():
        if type(value) is not str or len(value)>1000:
            issues.append("creator_facts_invalid");continue
        if value.strip():shown.append({"label":labels[key],"value":value})
    try:
        purpose=object_json(cfg.model_by_purpose,16384)
        override=purpose.get("ai_chatter")
        if "ai_chatter" in purpose and (type(override) is not str or not override.strip() or len(override)>160):raise ValueError()
    except (ValueError,TypeError,RecursionError):override=None;issues.append("model_configuration_invalid")
    try:sales=object_json(cfg.ai_chatter_config_json)
    except (ValueError,TypeError,RecursionError):sales={};issues.append("sales_configuration_invalid")
    permissions={key:sales.get(key) if type(sales.get(key)) is bool else None for key in
        ("pack_send_enabled","pack_on_ask_enabled","qualification_gate_enabled")}
    persona=cfg.persona or ""
    if not persona.strip():issues.append("persona_missing")
    return {"personaExcerpt":persona[:4096],"personaTruncated":len(persona)>4096,
        "creatorFacts":shown,"language":cfg.language[:32] if cfg.language else None,
        "savedDefaultModel":cfg.model[:160] if cfg.model else None,"savedReplyModelOverride":override,
        "dailyModelCapCents":integer(cfg.daily_cost_cap_cents),"salesPermissions":permissions,
        "issues":sorted(set(issues))}


async def read_workspace(s,*,tenant_id,account_id,section,cursor=None,limit=25):
    if section not in SECTIONS:raise BadRequest("invalid_section","That OnlyFans section is not available.")
    if type(limit) is not int or not 1<=limit<=100:raise BadRequest("invalid_limit","The page size must be between 1 and 100.")
    if section=="settings" and cursor:raise BadRequest("invalid_cursor","Settings do not use a page cursor.")
    after=cursor_id(cursor,account_id,section)
    await s.connection(execution_options={"isolation_level":"REPEATABLE READ"})
    binding=select(OfCreatorBinding.id).where(OfCreatorBinding.account_id==account_id,
        OfCreatorBinding.channel=="onlyfans",OfCreatorBinding.revoked_at.is_(None))
    if tenant_id is not None:binding=binding.where(OfCreatorBinding.tenant_id==tenant_id)
    if await s.scalar(binding.with_for_update(read=True)) is None:return None
    runtime=await s.get(OfRuntime,account_id)
    connection=await s.get(OfProviderConnection,account_id)
    now=(await s.scalar(select(func.clock_timestamp()))).replace(tzinfo=None)
    snapshot={"schemaVersion":SCHEMA,"section":section,"accountId":account_id,
        "asOf":iso(await s.scalar(select(func.transaction_timestamp()))),
        "lastCompleteSyncAt":iso(runtime.last_sync_at) if runtime else None,
        "historyRecoveryRequired":bool(runtime is None or runtime.recovery_needed or runtime.last_sync_at is None),
        "providerConnectionVerified":bool(connection and runtime and connection.binding_id==runtime.binding_id and connection.status=='verified' and connection.verified_at is not None and connection.expires_at>now)}
    if section=="content":
        query=select(CatalogItem).where(CatalogItem.account_id==account_id,CATALOG_IS_SINGLE)
        if after is not None:query=query.where(CatalogItem.id>after)
        rows=(await s.scalars(query.order_by(CatalogItem.id).limit(limit+1))).all()
        items=[]
        for row in rows[:limit]:items.append(await s.run_sync(lambda sync,item=row:catalog_row(sync,item)))
        return {**snapshot,"items":items,"nextCursor":cursor_for(account_id,section,rows[limit-1].id) if len(rows)>limit else None}
    if section=="sales":
        query=select(Transaction).where(Transaction.account_id==account_id)
        if after is not None:query=query.where(Transaction.id<after)
        rows=(await s.scalars(query.order_by(Transaction.id.desc()).limit(limit+1))).all()
        groups=(await s.execute(select(Transaction.currency,Transaction.status,func.count(),func.sum(Transaction.amount_cents))
            .where(Transaction.account_id==account_id).group_by(Transaction.currency,Transaction.status)
            .order_by(Transaction.currency,Transaction.status).limit(65))).all()
        if len(groups)>64:raise ValueError("too many financial groups")
        totals=[]
        for currency,status,count,gross in groups:
            if not re.fullmatch(r"[A-Z]{3}",currency) or len(status)>24:raise ValueError("invalid financial group")
            totals.append({"currency":currency,"status":status,"count":integer(count),"grossCents":integer(gross,signed=True)})
        offers=(await s.scalars(select(ContentOffer).where(ContentOffer.account_id==account_id,ContentOffer.status=="open")
            .order_by(ContentOffer.id.desc()).limit(25))).all()
        open_count=integer(await s.scalar(select(func.count()).select_from(ContentOffer).where(
            ContentOffer.account_id==account_id,ContentOffer.status=="open")))
        open_offers=[{"offerId":str(row.id),"fanId":str(row.fan_id),"messageId":None if row.offer_message_id is None else str(row.offer_message_id),
            "mode":row.mode[:24],"priceCents":integer(row.price_cents),"tipUnlockCents":integer(row.tip_unlock_cents),"currency":"USD","offeredAt":iso(row.offered_at)} for row in offers]
        return {**snapshot,"transactions":[sale_row(row) for row in rows[:limit]],"totals":totals,
            "openOffers":open_offers,"openOfferCount":open_count,"moreOpenOffers":open_count>len(open_offers),
            "nextCursor":cursor_for(account_id,section,rows[limit-1].id) if len(rows)>limit else None}
    policy=await s.get(OfDeliveryPolicy,account_id)
    return {**snapshot,"configuration":config_view(await s.get(AccountAiConfig,account_id)),
        "runtimeEnabled":bool(runtime and runtime.enabled),
        "deliveryPolicy":None if policy is None else {"revision":integer(policy.revision),"enabled":policy.enabled,
            "mediaEnabled":policy.media_enabled,"usedUnits":integer(policy.used_units),"limitUnits":integer(policy.limit_units)}}

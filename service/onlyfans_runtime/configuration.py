"""Bounded operator edits to AccountAiConfig, with immutable change evidence."""
import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AccountAiConfig
from db.onlyfans_models import OfConfigurationChange
from llm_models import MODELS
from .native_context import canonical,approved_persona
from .store import Refused,utcnow

def fact_fields():
    from automations._persona_contract import PERSONA_FACT_FIELDS
    return PERSONA_FACT_FIELDS


class InvalidConfiguration(ValueError):
    pass


SALES_FLAGS = ("pack_send_enabled", "pack_on_ask_enabled", "qualification_gate_enabled")


def sales_settings(row):
    """Project only the existing native on-request controls, never private JSON."""
    try:
        raw = object_value(row.ai_chatter_config_json)
        if any(key in raw and type(raw[key]) is not bool for key in SALES_FLAGS):
            raise ValueError("invalid sales switch")
        return raw, True
    except (ValueError, TypeError, RecursionError):
        return {}, False


def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


def fingerprint(row):
    value={column.name:getattr(row,column.name) for column in row.__table__.columns}
    encoded=canonical(value).encode()
    if len(encoded)>262144:raise InvalidConfiguration("Saved configuration is too large for this editor.")
    return hashlib.sha256(encoded).hexdigest()


def object_value(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError("duplicate key")
            result[key]=value
        return result
    if not raw:return {}
    if type(raw) is not str or len(raw.encode())>65536:raise ValueError("configuration too large")
    value=json.loads(raw,object_pairs_hook=unique)
    if type(value) is not dict:raise ValueError("configuration object required")
    return value


def editable(row,runtime,session):
    """Only approved fields and catalogue IDs; no arbitrary config or credentials."""
    from automations._voice import fact_labels
    from automations._model_selection import explicit_model
    labels=fact_labels(row.voice)
    facts_valid=True;models_valid=True
    from .creator_facts import source_view
    source={"state":"unavailable","checkedAt":None,"sourceUpdatedAt":None,"overrideKeys":[]}
    try:facts,source=source_view(session,row)
    except Refused:raise
    except (ValueError,TypeError,RecursionError):facts={};facts_valid=False
    try:
        purpose=object_value(row.model_by_purpose)
        override=purpose.get("ai_chatter")
        if override is not None and (type(override) is not str or len(override)>160):raise ValueError()
    except (ValueError,TypeError,RecursionError):override=None;models_valid=False
    values={};readonly=[]
    for key,_ in fact_fields():
        value=facts.get(key)
        if value is not None and (type(value) is not str or len(value)>16000):value=None;readonly.append(key)
        if type(value) is str and len(value)>240 and key not in readonly:readonly.append(key)
        values[key]=value
    persona=row.persona or ""
    try:resolved=explicit_model(row,"ai_chatter",MODELS)
    except (ValueError,RecursionError):resolved=None
    sales, sales_valid = sales_settings(row)
    return {"configurationRevision":runtime.configuration_revision,"fingerprint":fingerprint(row),
        "persona":persona if len(persona)<=4096 else None,"personaTruncated":len(persona)>4096,
        "creatorFacts":values,"factFields":[{"key":key,"label":labels.get(key,label)} for key,label in fact_fields()],
        "factsValid":facts_valid,"readOnlyFacts":readonly,"modelsValid":models_valid,"sharedFacts":source,
        "defaultModel":row.model,"replyModelOverride":override,"replyOverridePresent":models_valid and "ai_chatter" in purpose,"resolvedReplyModel":resolved,
        "modelOptions":sorted(MODELS), "salesConfigurationValid":sales_valid,
        "onRequestSalesEnabled":sales_valid and all(sales.get(key) is True for key in SALES_FLAGS)}


def get_configuration(store,tenant_id,account_id):
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant_id,account_id)
        row=s.scalar(select(AccountAiConfig).where(AccountAiConfig.account_id==account_id).with_for_update(read=True))
        if row is None:raise Refused("configuration unavailable")
        return editable(row,runtime,s)


def validate_patch(patch):
    if type(patch) is not dict or not patch or not set(patch)<= {"persona","creatorFacts","inheritCreatorFacts","defaultModel","replyModelOverride","onRequestSalesEnabled"}:
        raise InvalidConfiguration("Only creator persona, facts, model choices and on-request sales may be edited here.")
    result={}
    if "onRequestSalesEnabled" in patch:
        if type(patch["onRequestSalesEnabled"]) is not bool:
            raise InvalidConfiguration("The on-request sales setting must be an explicit boolean.")
        result["onRequestSalesEnabled"]=patch["onRequestSalesEnabled"]
    if "inheritCreatorFacts" in patch:
        keys=patch["inheritCreatorFacts"]
        if (type(keys) is not list or not keys
                or any(type(key) is not str or key not in dict(fact_fields()) for key in keys)
                or len(set(keys))!=len(keys)
                or type(patch.get("creatorFacts")) is dict and set(keys)&set(patch["creatorFacts"])):
            raise InvalidConfiguration("Choose distinct supported facts to inherit from Twin.")
        result["inheritCreatorFacts"]=keys
    if "persona" in patch:
        persona=patch["persona"]
        if type(persona) is not str or len(persona)>4096 or not approved_persona(persona):
            raise InvalidConfiguration("Use a nonempty creator persona of at most 4096 characters that preserves honest AI disclosure.")
        result["persona"]=persona.strip()
    if "creatorFacts" in patch:
        facts=patch["creatorFacts"]
        if type(facts) is not dict or not facts or not set(facts)<=set(dict(fact_fields())):
            raise InvalidConfiguration("Choose a supported creator fact.")
        clean={}
        for key,value in facts.items():
            if value is not None:
                if type(value) is not str or not value.strip() or len(value.strip())>240:
                    raise InvalidConfiguration("Each creator fact must be nonempty text of at most 240 characters, or explicitly cleared.")
                value=value.strip()
                if key=="age" and (not re.fullmatch(r"[0-9]{2,3}",value) or not 18<=int(value)<=120):
                    raise InvalidConfiguration("Creator age must be an adult age between 18 and 120.")
            clean[key]=value
        result["creatorFacts"]=clean
    for field in ("defaultModel","replyModelOverride"):
        if field not in patch:continue
        value=patch[field]
        if value is None and field=="replyModelOverride":result[field]=None;continue
        if type(value) is not str or value not in MODELS:
            raise InvalidConfiguration("Choose a model from the saved native model catalogue.")
        result[field]=value
    return result


def update_configuration(store,tenant_id,account_id,*,patch,expected_revision,expected_fingerprint,command_key,actor):
    patch=validate_patch(patch)
    if (type(expected_revision) is not int or not 0<=expected_revision<2**31-1 or
        type(expected_fingerprint) is not str or not re.fullmatch(r"[a-f0-9]{64}",expected_fingerprint) or
        type(command_key) is not str or not re.fullmatch(r"[a-f0-9]{64}",command_key) or
        type(actor) is not str or not re.fullmatch(r"op-session:[a-f0-9]{64}",actor)):
        raise InvalidConfiguration("The configuration version or command identity is invalid.")
    request_hash=digest(patch)
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant_id,account_id)
        prior=s.get(OfConfigurationChange,(account_id,command_key))
        if prior:
            if (prior.previous_revision,prior.fingerprint_before,prior.request_hash,prior.actor)!=(expected_revision,expected_fingerprint,request_hash,actor):
                raise Refused("configuration idempotency conflict")
            return prior.revision,prior.fingerprint_after
        row=s.scalar(select(AccountAiConfig).where(AccountAiConfig.account_id==account_id).with_for_update())
        if row is None:raise Refused("configuration unavailable")
        if runtime.configuration_revision!=expected_revision or fingerprint(row)!=expected_fingerprint:
            raise Refused("configuration revision conflict")
        before={};after={};changed=False
        if "onRequestSalesEnabled" in patch:
            sales, valid = sales_settings(row)
            if not valid:
                raise InvalidConfiguration("Saved sales configuration needs review before it can be edited.")
            before["salesSettings"]={key:{"present":key in sales,"value":sales.get(key)} for key in SALES_FLAGS}
            previous_sales=canonical(sales)
            # Enable the existing request/qualification lane explicitly. Turning
            # it off stops offers without disabling qualification or other brakes.
            sales["pack_send_enabled"]=patch["onRequestSalesEnabled"]
            if patch["onRequestSalesEnabled"]:
                sales.update({key:True for key in SALES_FLAGS})
            after["salesSettings"]={key:{"present":key in sales,"value":sales.get(key)} for key in SALES_FLAGS}
            changed=changed or previous_sales!=canonical(sales)
            if previous_sales!=canonical(sales):
                row.ai_chatter_config_json=canonical(sales)
        if "persona" in patch:
            changed=changed or row.persona!=patch["persona"]
            before["persona"]=row.persona;after["persona"]=patch["persona"];row.persona=patch["persona"]
        if "creatorFacts" in patch or "inheritCreatorFacts" in patch:
            try:facts=object_value(row.persona_facts_json)
            except (ValueError,TypeError,RecursionError) as exc:raise InvalidConfiguration("Saved creator facts need review before they can be merged.") from exc
            previous_facts=canonical(facts)
            from db.onlyfans_creator_facts_models import OfCreatorFactsSource
            from .creator_facts import effective
            source=s.get(OfCreatorFactsSource,account_id)
            edits=patch.get("creatorFacts",{})
            inherits=patch.get("inheritCreatorFacts",[])
            before["creatorFacts"]={key:{"present":key in facts,"value":facts.get(key)} for key in [*edits,*inherits]}
            if source is not None:
                overrides=object_value(source.overrides_json)
                old_overrides=canonical(overrides)
                for key,value in edits.items():overrides[key]=value
                for key in inherits:overrides.pop(key,None)
                source.overrides_json=canonical(overrides)
                facts,_=effective(s,row)
                changed=changed or old_overrides!=source.overrides_json
                before["overrides"]=object_value(old_overrides)
                after["overrides"]=overrides
            else:
                if inherits:raise InvalidConfiguration("Synchronize Twin facts before inheriting them.")
                for key,value in edits.items():
                    if value is None:facts.pop(key,None)
                    else:facts[key]=value
            after["creatorFacts"]={key:{"present":key in facts,"value":facts.get(key)} for key in [*edits,*inherits]}
            row.persona_facts_json=canonical(facts) if facts else None
            changed=changed or canonical(facts)!=previous_facts
        if "defaultModel" in patch:
            changed=changed or row.model!=patch["defaultModel"]
            before["defaultModel"]=row.model;after["defaultModel"]=patch["defaultModel"];row.model=patch["defaultModel"]
        if "replyModelOverride" in patch:
            try:purpose=object_value(row.model_by_purpose)
            except (ValueError,TypeError,RecursionError) as exc:raise InvalidConfiguration("Saved model-purpose configuration needs review before it can be merged.") from exc
            previous_purpose=canonical(purpose)
            before["replyModelOverride"]={"present":"ai_chatter" in purpose,"value":purpose.get("ai_chatter")}
            after["replyModelOverride"]={"present":patch["replyModelOverride"] is not None,"value":patch["replyModelOverride"]}
            if patch["replyModelOverride"] is None:purpose.pop("ai_chatter",None)
            else:purpose["ai_chatter"]=patch["replyModelOverride"]
            row.model_by_purpose=canonical(purpose) if purpose else None
            changed=changed or canonical(purpose)!=previous_purpose
        if "defaultModel" in patch or "replyModelOverride" in patch:
            from automations._model_selection import explicit_model
            try:explicit_model(row,"ai_chatter",MODELS)
            except (ValueError,RecursionError) as exc:raise InvalidConfiguration("The resulting saved reply model must resolve to a supported explicit choice.") from exc
        if not changed:raise InvalidConfiguration("No configuration change was needed.")
        row.updated_at=utcnow()
        s.flush()
        after_fingerprint=fingerprint(row)
        runtime.configuration_revision+=1
        s.add(OfConfigurationChange(account_id=account_id,command_key=command_key,
            previous_revision=expected_revision,revision=runtime.configuration_revision,
            fingerprint_before=expected_fingerprint,fingerprint_after=after_fingerprint,request_hash=request_hash,
            actor=actor,before_json=canonical(before),after_json=canonical(after),created_at=utcnow()))
        return runtime.configuration_revision,after_fingerprint
